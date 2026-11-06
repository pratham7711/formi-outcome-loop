"""The control loop.

    ingest new chunks -> refresh live configs -> per workflow: track approvals, run modules,
    compose target, plan (auto / approval) -> record loop metrics -> advance the clock

Transaction boundaries (what survives a kill -9 anywhere):
  * each chunk: counters + watermark commit together. A kill mid-chunk re-ingests that chunk;
    event_id dedup makes re-ingest a no-op for anything already counted.
  * decision phase: module state + decision rows + proposal intent rows commit *before* any HTTP
    write; the HTTP outcome is recorded and committed right after. On restart,
    governance.reconcile() resolves intents whose outcome was not recorded.
  * advancing the clock is the last step. If we die after the sim advanced, the next start simply
    finds new chunks after the watermark.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import resource
import sys
import time
from pathlib import Path

from . import callerid, experiments, governance, incidents, schedule
from .api import Api
from .cohorts import PREFIX
from .common import MIN_PER_DAY, Ctx
from .model import Model
from .store import Store, dumps

log = logging.getLogger("controller")


def fmt_minute(m: int) -> str:
    return f"D{m // MIN_PER_DAY} {(m % MIN_PER_DAY) // 60:02d}:{m % 60:02d}"


class Controller:
    def __init__(self, url: str, db: str, settings: dict, step: int = 60):
        self.api = Api(url)
        self.step = step
        self.settings = settings
        st = self.api.must(self.api.get("/sim/state"), "state")
        self.store = self._open_store(db, st)
        self.wfs = {w["workflow_id"]: w for w in self.api.must(self.api.get("/workflows"), "workflows")}
        d0 = (dt.date.fromisoformat(st["ts"][:10]) - dt.timedelta(days=st["sim_minute"] // MIN_PER_DAY)).isoformat()
        blob, self.watermark, _ = self.store.load_state()
        self.state = blob or {"model": {}, "mod": {}}
        self.model = Model(d0, {w: len(i["cadence"]) for w, i in self.wfs.items()}, self.state.get("model"))
        self.credits = st["credits_remaining"]
        self.now = st["sim_minute"]
        self.ts = st["ts"]
        self.configs: dict = {}
        self.restarted = blob is not None
        self.store.set_meta("scenario", st["scenario"])
        self.store.set_meta("d0", d0)

    def _open_store(self, db: str, st: dict) -> Store:
        s = Store(db)
        prev = s.meta("scenario")
        _, wm, _ = s.load_state()
        last = s.meta("last_event_id", 0) or 0
        if prev is not None and (prev != st["scenario"] or last > st["last_event_id"]):
            s.db.close()
            stamp = time.strftime("%Y%m%d-%H%M%S")
            for suf in ("", "-wal", "-shm"):
                if os.path.exists(db + suf):
                    os.replace(db + suf, f"{db}.{stamp}.old{suf}")
            log.warning("store belonged to another world (%s); archived it and starting fresh", prev)
            s = Store(db)
        return s

    # ------------------------------------------------------------------ persistence
    def persist(self):
        self.state["model"] = self.model.to_dict()
        size = self.store.save_state(self.state, self.watermark, self.now)
        self.store.set_meta("last_event_id", self.model.last_event_id)
        self.store.commit()
        self.store.begin()
        return size

    def ctx(self) -> Ctx:
        return Ctx(now=self.now, ts=self.ts, store=self.store, model=self.model, state=self.state, wfs=self.wfs,
                   configs=self.configs, credits=self.credits, api=self.api, settings=self.settings)

    # ------------------------------------------------------------------ ingest
    def list_chunks(self, after: int) -> list[dict]:
        out = []
        for kind in ("history", "events"):
            out += self.api.must(self.api.get("/events/chunks", kind=kind, after=after), "chunks")
        return sorted(out, key=lambda c: c["chunk"])

    def rebuild_leads(self):
        """Restart path: lead -> features for still-active campaigns, from immutable chunks."""
        horizon = self.now - (max(self.model.cadence_days.values(), default=3) + 2) * MIN_PER_DAY
        t0 = time.time()
        n = 0
        for c in self.list_chunks(-1):
            if c["chunk"] > self.watermark or c["until_minute"] < horizon:
                continue
            self.model.ingest_lines(self.api.chunk_lines(c["chunk"]), counters_on=False)
            n += 1
        log.info("rebuilt lead map from %d chunks (%d leads) in %.1fs", n, len(self.model.leads), time.time() - t0)

    def ingest(self) -> tuple[int, float]:
        t0 = time.time()
        n0 = self.model.events_total
        for c in self.list_chunks(self.watermark):
            notable = self.model.ingest_lines(self.api.chunk_lines(c["chunk"]))
            self.store.begin()
            self.store.db.execute("INSERT OR IGNORE INTO chunks_seen VALUES(?,?,?,?,?,?)",
                                  (c["chunk"], c["kind"], c["until_minute"], c["events"], c["first_event_id"],
                                   c["last_event_id"]))
            self._route(notable)
            self.watermark = c["chunk"]
            self.model.evict(c["until_minute"] // MIN_PER_DAY)
            self.state["model"] = self.model.to_dict()
            self.store.save_state(self.state, self.watermark, self.now)
            self.store.set_meta("last_event_id", self.model.last_event_id)
            self.store.commit()
        return self.model.events_total - n0, time.time() - t0

    def _minute(self, ts: str) -> int:
        return self.model.day_of(ts) * MIN_PER_DAY + int(ts[11:13]) * 60 + int(ts[14:16])

    def _route(self, notable: list[dict]):
        pend = self.state.setdefault("pending_events", [])
        for ev in notable:
            t = ev["type"]
            if t == "config_changed":
                m = self._minute(ev["ts"])
                self.store.db.execute("INSERT OR IGNORE INTO config_versions VALUES(?,?,?,?,?,?,?,?,?)",
                                      (ev["workflow_id"], ev["version"], ev["actor"], dumps(ev["changed_dims"]),
                                       ev.get("proposal_id"), ev.get("approval_id"), m, ev["ts"], ev["event_id"]))
                pend.append({"type": t, "wf": ev["workflow_id"], "actor": ev["actor"], "dims": ev["changed_dims"],
                             "version": ev["version"], "minute": m})
            elif t == "approval_decided":
                self.store.decision(self._minute(ev["ts"]), ev["ts"], ev["workflow_id"], "governance", "event",
                                    f"{ev['approval_id']} decided: {ev['status']}", {"reasons": ev.get("reasons")})

    # ------------------------------------------------------------------ decide
    def refresh_configs(self):
        for wf in self.wfs:
            c = self.api.must(self.api.get(f"/workflows/{wf}/config"), "config")
            self.configs[wf] = {"version": c["version"], "config": c["config"], "actor": c["actor"]}

    def handle_pending_events(self, ctx: Ctx):
        for ev in self.state.pop("pending_events", []):
            wf = ev["wf"]
            if "prompt_version" in ev["dims"]:
                prompt = self._prompt_at(wf, ev["version"])
                save = ctx.now
                ctx.now = ev["minute"]
                incidents.on_prompt_change(ctx, wf, ev["version"], prompt, ev["actor"])
                ctx.now = save
            if ev["actor"] == "supervisor":
                governance.on_supervisor_edit(ctx, wf, ev["dims"], ev["version"])
                experiments.on_supervisor_edit(ctx, wf, ev["dims"], self.configs[wf]["config"])
                ctx.alert(wf, "info", f"supervisor_edit:v{ev['version']}", f"{wf}: supervisor edited {ev['dims']} "
                          f"(v{ev['version']}); controller adapted. Shown in the supervisor view, no action.")

    def _prompt_at(self, wf: str, version: int) -> str:
        """Configs are refreshed before events are handled, so the live prompt is the newest one.
        Two prompt changes inside one loop step collapse to the later one (documented limitation)."""
        return self.configs[wf]["config"]["prompt_version"]

    def harmful_veto(self, wf: str):
        def check(cfg: dict) -> str | None:
            st = self.state.get("mod", {}).get("experiments", {}).get(wf, {})
            for dim, s in st.items():
                if not isinstance(s, dict):
                    continue
                bad = set(s.get("dropped", {})) | set(s.get("decayed", {}))
                for k, alloc in cfg["allocations"].items():
                    d = alloc.get(dim) or {}
                    hit = [o for o in bad if d.get(o, 0) > 0.0]
                    if hit:
                        return f"it still routes {k} traffic to {dim} {hit}, measured harmful since it was requested"
            return None
        return check

    def decide(self) -> float:
        t0 = time.time()
        self.store.begin()
        ctx = self.ctx()
        self.refresh_configs()
        self.handle_pending_events(ctx)
        decided = self.state.setdefault("decided", {})
        for wf in sorted(self.wfs):
            if decided.get(wf) == self.now:
                continue  # already decided this sim minute before a restart; never decide twice
            governance.track(ctx, wf, self.harmful_veto(wf), self.persist)
            cfg = self.configs[wf]["config"]
            sched_live = {}
            sst = ctx.mstate("schedule", wf)
            if sst.get("feature"):
                for c in cfg["cohorts"]:
                    if c["id"].startswith(PREFIX) and sst["feature"] in c["where"] and \
                            c["id"] in cfg["schedule"]["hour_weights"]:
                        sched_live.setdefault(c["where"][sst["feature"]][0], cfg["schedule"]["hour_weights"][c["id"]])
            sched_t, p1 = schedule.evaluate(ctx, wf, sched_live)
            did_t, p2 = callerid.evaluate(ctx, wf)
            exp_t, p3 = experiments.evaluate(ctx, wf)
            prompt_t, p4 = incidents.evaluate(ctx, wf)
            self.credits = ctx.credits
            T = governance.compose(ctx, wf, sched_t, did_t, exp_t, prompt_t)
            governance.plan(ctx, wf, T, p1 + p2 + p3 + p4, self.persist)
            decided[wf] = self.now
            self.persist()
        self.persist()
        self.store.commit()
        return time.time() - t0

    # ------------------------------------------------------------------ run
    def run(self):
        self.store.begin()
        if self.restarted:
            self.refresh_configs()
            self.rebuild_leads()
            governance.reconcile(self.ctx(), self.persist)
            self.store.decision(self.now, self.ts, None, "loop", "restart",
                                f"Controller restarted at {fmt_minute(self.now)}; resumed from chunk {self.watermark}")
        self.store.commit()
        last_day = None
        while True:
            w0 = time.time()
            n, ingest_s = self.ingest()
            decide_s = self.decide()
            st = self.api.must(self.api.get("/sim/state"), "state")
            self.credits = st["credits_remaining"]
            finished = st["finished"]
            adv_s = 0.0
            if not finished:
                a0 = time.time()
                r = self.api.post("/sim/advance", {"minutes": self.step})
                adv_s = time.time() - a0
                if r.status == 409:
                    finished = True
                else:
                    b = self.api.must(r, "advance")
                    self.now, self.ts = b["sim_minute"], b["ts"]
            pend = self.store.db.execute("SELECT COUNT(*) FROM proposals WHERE status IN ('pending','approved')").fetchone()[0]
            rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024 if sys.platform == "darwin" else 1024)
            with self.store.tx():
                self.store.db.execute(
                    "INSERT INTO metrics(wall_ts, sim_minute, events, ingest_s, decide_s, advance_s, events_per_s, credits,"
                    " pending_approvals, rss_mb, decision_lag_min, state_kb) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (time.time(), st["sim_minute"], n, ingest_s, decide_s, adv_s, n / ingest_s if ingest_s > 0 else 0,
                     self.credits, pend, rss, self.step, None))
            day = st["sim_minute"] // MIN_PER_DAY
            if day != last_day:
                log.info("%s  events=%d ingest=%.2fs decide=%.2fs credits=%.0f rss=%.0fMB loop=%.2fs",
                         fmt_minute(st["sim_minute"]), self.model.events_total, ingest_s, decide_s, self.credits, rss,
                         time.time() - w0)
                last_day = day
            if finished:
                break
        self.ingest()
        log.info("scenario finished at %s", fmt_minute(self.now))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="controller")
    ap.add_argument("--url", required=True)
    ap.add_argument("--db", default="state/controller.db")
    ap.add_argument("--settings", default=str(Path(__file__).resolve().parent.parent / "config" / "overrides.json"))
    ap.add_argument("--step", type=int, default=60, help="sim minutes per loop (multiple of 15)")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = {"overrides": json.loads(Path(a.settings).read_text())} if Path(a.settings).exists() else {}
    Controller(a.url, a.db, settings, a.step).run()


if __name__ == "__main__":
    main()
