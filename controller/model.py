"""Ingestion: NDJSON events -> compact counters every module reads.

Design rules
* Only counters, never per-call rows. Memory is O(workflows x days x hours x segments), not
  O(calls). The one per-entity map is lead_id -> features for leads still inside their cadence;
  it is evicted as campaigns age out and is rebuilt on restart by re-reading `lead` events from
  the chunks that cover the active campaigns (chunks are immutable and re-fetchable).
* Every counter key carries the stratum it was observed in (sim day, hour, config version,
  cohort) so modules can compare like with like.
* A **segment** is one `feature=value` predicate (e.g. `age_band=60+`). Segments are the shared
  vocabulary: detection, experiments, cohorts and the supervisor view all speak in segments, and
  cohorts are built from them (see cohorts.py).
"""
from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict

AGENT_FACING = ("welcome_message", "voice")


def _bump(d: dict, key: str, vals: tuple):
    cur = d.get(key)
    if cur is None:
        d[key] = list(vals)
    else:
        for i, v in enumerate(vals):
            cur[i] += v


class WorkflowAgg:
    """Counters for one workflow. Serialised as plain dicts of lists."""

    FIELDS = ("pk_seg", "pk_did", "arm", "hal", "kpi", "camp", "lf")

    def __init__(self, d: dict | None = None):
        d = d or {}
        self.pk_seg: dict[str, list] = d.get("pk_seg", {})   # day|hour|seg -> [dials, conn]
        self.pk_did: dict[str, list] = d.get("pk_did", {})   # day|hour|did -> [dials, conn]
        self.arm: dict[str, list] = d.get("arm", {})         # day|ver|cohort|dim|opt|seg -> [dials, conn, eng]
        self.hal: dict[str, list] = d.get("hal", {})         # day|hour|prompt -> [conn, flags, eng]
        self.kpi: dict[str, list] = d.get("kpi", {})         # day|hour -> [dials, conn, eng]
        self.camp: dict[str, list] = d.get("camp", {})       # campaign -> [day, leads, concluded]
        self.lf: dict[str, int] = d.get("lf", {})            # day|seg -> leads uploaded

    def to_dict(self) -> dict:
        return {f: getattr(self, f) for f in self.FIELDS}


class Model:
    """All learned counters, plus the transient lead->features map."""

    def __init__(self, d0_date: str, cadence_days: dict[str, int], d: dict | None = None):
        d = d or {}
        self.d0 = dt.date.fromisoformat(d0_date)
        self.cadence_days = cadence_days
        self.wf: dict[str, WorkflowAgg] = {k: WorkflowAgg(v) for k, v in d.get("wf", {}).items()}
        self.last_event_id: int = d.get("last_event_id", 0)
        self.last_ts: str | None = d.get("last_ts")
        self.events_total: int = d.get("events_total", 0)
        self.segments_seen: dict[str, dict[str, list]] = d.get("segments_seen", {})  # wf -> feat -> values
        # transient
        self.leads: dict[str, tuple] = {}
        self.camp_leads: dict[str, list] = defaultdict(list)
        self._day_cache: dict[str, int] = {}
        self._feat_cache: dict[tuple, tuple] = {}

    def to_dict(self) -> dict:
        return {"wf": {k: v.to_dict() for k, v in self.wf.items()}, "last_event_id": self.last_event_id,
                "last_ts": self.last_ts, "events_total": self.events_total, "segments_seen": self.segments_seen}

    def agg(self, wf: str) -> WorkflowAgg:
        a = self.wf.get(wf)
        if a is None:
            a = self.wf[wf] = WorkflowAgg()
        return a

    def day_of(self, ts: str) -> int:
        k = ts[:10]
        d = self._day_cache.get(k)
        if d is None:
            d = self._day_cache[k] = (dt.date.fromisoformat(k) - self.d0).days
        return d

    def _features(self, wf: str, f: dict) -> tuple:
        """Interned per-combination info: (segment strings, schedule key fragments)."""
        key = (wf,) + tuple(sorted(f.items()))
        info = self._feat_cache.get(key)
        if info is None:
            segs = tuple(f"{k}={v}" for k, v in sorted(f.items()))
            seen = self.segments_seen.setdefault(wf, {})
            for k, v in f.items():
                vals = seen.setdefault(k, [])
                if v not in vals:
                    vals.append(v)
                    vals.sort()
            info = self._feat_cache[key] = (segs, ("*",) + segs)
        return info

    # ------------------------------------------------------------------ ingestion
    def ingest_lines(self, lines, counters_on: bool = True) -> list[dict]:
        """Consumes raw NDJSON lines. Returns the non-call events modules need to react to.

        counters_on=False is the restart path: only the lead map is rebuilt, nothing is counted
        twice (counters for those chunks are already inside the persisted state).
        """
        notable: list[dict] = []
        loads = json.loads
        leads = self.leads
        n = 0
        for raw in lines:
            if not raw:
                continue
            ev = loads(raw)
            t = ev["type"]
            eid = ev["event_id"]
            if counters_on and eid <= self.last_event_id:
                continue  # idempotent: already counted
            n += 1
            if t == "call":
                if counters_on:
                    self._call(ev)
            elif t == "lead":
                info = self._features(ev["workflow_id"], ev["features"])
                leads[ev["lead_id"]] = info
                self.camp_leads[ev["campaign_id"]].append(ev["lead_id"])
                if counters_on:
                    a = self.agg(ev["workflow_id"])
                    day = self.day_of(ev["ts"])
                    for s in info[0]:
                        k = f"{day}|{s}"
                        a.lf[k] = a.lf.get(k, 0) + 1
                    c = a.camp.get(ev["campaign_id"])
                    if c is not None:
                        c[1] += 1
            elif t == "campaign_created":
                if counters_on:
                    self.agg(ev["workflow_id"]).camp.setdefault(ev["campaign_id"], [self.day_of(ev["ts"]), 0, 0])
                    notable.append(ev)
            else:
                if counters_on:
                    notable.append(ev)
            if counters_on:
                self.last_event_id = eid
                self.last_ts = ev["ts"]
        if counters_on:
            self.events_total += n
        return notable

    def _call(self, ev: dict):
        wf = ev["workflow_id"]
        a = self.agg(wf)
        ts = ev["ts"]
        day = self.day_of(ts)
        hour = ts[11:13].lstrip("0") or "0"
        conn = 1 if ev["connected"] else 0
        eng = 1 if ev["engaged"] else 0
        var = ev["variant"]
        info = self.leads.get(ev["lead_id"])
        segs = info[0] if info else ()
        dh = f"{day}|{hour}"
        _bump(a.kpi, dh, (1, conn, eng))
        _bump(a.pk_did, f"{dh}|{var['caller_id']}", (1, conn))
        for s in segs:
            _bump(a.pk_seg, f"{dh}|{s}", (1, conn))
        if conn:
            flag = 1 if (ev.get("qa") or {}).get("hallucination_flag") else 0
            _bump(a.hal, f"{dh}|{var['prompt_version']}", (1, flag, eng))
        stratum = f"{day}|{ev['config_version']}|{ev['cohort_id']}"
        all_segs = info[1] if info else ("*",)
        for dim in AGENT_FACING:
            pre = f"{stratum}|{dim}|{var[dim]}|"
            for s in all_segs:
                _bump(a.arm, pre + s, (1, conn, eng))
        if eng:
            c = a.camp.get(ev["campaign_id"])
            if c is not None:
                c[2] += 1

    # ------------------------------------------------------------------ housekeeping
    def evict(self, today: int):
        """Drops lead features for campaigns past their cadence (they can no longer be dialled)."""
        for wf, a in self.wf.items():
            keep_from = today - self.cadence_days.get(wf, 3) - 1
            for cid, (cday, _, _) in a.camp.items():
                if cday < keep_from and cid in self.camp_leads:
                    for lid in self.camp_leads.pop(cid):
                        self.leads.pop(lid, None)

    def active_leads(self, wf: str, today: int) -> int:
        a = self.agg(wf)
        cd = self.cadence_days.get(wf, 3)
        return sum(max(n - c, 0) for (d, n, c) in a.camp.values() if today - cd < d <= today)

    def lead_share(self, wf: str, today: int, preds: list[dict] | None, days: int = 7) -> float:
        """Share of recently uploaded leads matching ANY of the cohort predicates (None = all)."""
        if not preds:
            return 1.0
        a = self.agg(wf)
        # approximate: assume features independent across predicates of one cohort (documented)
        tot = sum(v for k, v in a.lf.items() if k.split("|")[1].startswith("lang=") and
                  today - days <= int(k.split("|")[0]) <= today) or 1
        share = 0.0
        for where in preds:
            s = 1.0
            for feat, vals in where.items():
                num = sum(v for k, v in a.lf.items() if today - days <= int(k.split("|")[0]) <= today and
                          k.split("|", 1)[1] in {f"{feat}={x}" for x in vals})
                s *= num / tot
            share += s
        return min(share, 1.0)
