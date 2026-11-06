"""Read-only views over the controller's own store. Nothing here talks to the simulator.

  python -m controller.report --db state/public_02_incidents.db supervisor [--wf ks_cancel]
  python -m controller.report --db ... why --wf nw_renewal --at "D3 14:00"
  python -m controller.report --db ... heard --wf nw_kyc --at "D2 11:00" --features age_band=60+,lang=hi
  python -m controller.report --db ... health
  python -m controller.report --db ... weekly --wf nw_renewal

`why` answers "why did workflow X change at T, who approved it, and what happened next?".
`heard` answers the IRDAI question: what could a customer with these features have heard at T, and why
that was live (the exact per-call variant lives in the dialler's call record; we keep the reason).
"""
from __future__ import annotations

import argparse
import json
import sys

from .cohorts import matches
from .experiments import Cube
from .model import Model
from .store import Store

MIN_PER_DAY = 1440


def parse_when(s: str) -> int:
    day, hm = s.strip().split()
    h, m = hm.split(":")
    return int(day.lstrip("Dd")) * MIN_PER_DAY + int(h) * 60 + int(m)


def fm(m: int | None) -> str:
    if m is None:
        return "—"
    return f"D{m // MIN_PER_DAY} {(m % MIN_PER_DAY) // 60:02d}:{m % 60:02d}"


def _kpis(model: Model, wf: str, lo: int, hi: int) -> dict:
    a = model.agg(wf)
    d = c = e = 0
    for k, v in a.kpi.items():
        day, hour = k.split("|")
        h = int(day) * 24 + int(hour)
        if lo // 60 <= h < hi // 60:
            d += v[0]
            c += v[1]
            e += v[2]
    return {"dials": d, "pickup": round(c / d, 4) if d else None, "engagement": round(e / c, 4) if c else None}


def _load(st: Store):
    blob, wm, minute = st.load_state()
    model = Model(st.meta("d0"), {}, (blob or {}).get("model"))
    return blob or {}, model, minute or 0


def why(st: Store, wf: str, at: int, out):
    rows = st.rows("SELECT * FROM config_versions WHERE wf=? AND sim_minute<=? ORDER BY version DESC LIMIT 1", wf, at)
    if not rows:
        print(f"{wf}: no config change recorded at or before {fm(at)}; the business default was live.", file=out)
        return
    v = rows[0]
    print(f"## Why did {wf} look the way it did at {fm(at)}?\n", file=out)
    print(f"Config **v{v['version']}** went live at {fm(v['sim_minute'])} ({v['ts']}), actor **{v['actor']}**, "
          f"changed {json.loads(v['changed_dims'])}.", file=out)
    if v["actor"] == "supervisor":
        print("\nThis was a client supervisor's direct edit (outside governance). What we did about it:", file=out)
        for d in st.rows("SELECT * FROM decisions WHERE wf=? AND sim_minute>=? AND sim_minute<=? AND module IN "
                         "('governance','experiments','incidents') ORDER BY id LIMIT 8", wf, v["sim_minute"],
                         v["sim_minute"] + 6 * 60):
            print(f"- {fm(d['sim_minute'])} [{d['module']}/{d['kind']}] {d['summary']}", file=out)
    pid = v["proposal_id"]
    if pid:
        p = st.rows("SELECT * FROM proposals WHERE proposal_id=?", pid)
        if p:
            p = p[0]
            how = (f"approval **{p['approval_id']}**, filed {fm(p['created_minute'])}, decided by the {wf} approver at "
                   f"{fm(p['decided_minute'])}, applied {fm(p['applied_minute'])}" if p["path"] == "approval"
                   else "auto-apply within the workflow's governance policy (no human approval required)")
            print(f"\n**How it passed governance:** {how}.", file=out)
            print(f"\n**Summary shown to the approver:** {p['change_summary']}", file=out)
            print(f"\n**Impact score:** {p['impact_score']} · **affected leads (est.):** {p['affected_leads']}", file=out)
            print(f"\n**Rationale:**\n```\n{p['rationale']}\n```", file=out)
            print(f"**Diff:** `{p['diff'][:1500]}`\n\n**Dry run:** `{p['dry_run'][:1200]}`", file=out)
            print(f"\n**Evidence:** `{p['evidence'][:800]}`", file=out)
            ds = st.rows("SELECT * FROM decisions WHERE proposal_id=? ORDER BY id", pid)
            if ds:
                print("\n**Decision trail:**", file=out)
                for d in ds:
                    print(f"- {fm(d['sim_minute'])} [{d['module']}/{d['kind']}] {d['summary']}", file=out)
    _, model, _ = _load(st)
    t = v["sim_minute"]
    b, a = _kpis(model, wf, t - MIN_PER_DAY, t), _kpis(model, wf, t, t + MIN_PER_DAY)
    print(f"\n**What happened next:** 24h before: pickup {b['pickup']}, engagement {b['engagement']} ({b['dials']} dials) "
          f"→ 24h after: pickup {a['pickup']}, engagement {a['engagement']} ({a['dials']} dials). "
          f"(Raw before/after, not causal: the experiment holdout is the causal read.)", file=out)
    for d in st.rows("SELECT * FROM decisions WHERE wf=? AND sim_minute>? AND sim_minute<=? AND kind NOT IN "
                     "('wait','event','skip') ORDER BY id LIMIT 12", wf, t, t + 2 * MIN_PER_DAY):
        print(f"- {fm(d['sim_minute'])} [{d['module']}/{d['kind']}] {d['summary']}", file=out)


def heard(st: Store, wf: str, at: int, feats: dict, out):
    rows = st.rows("SELECT * FROM config_versions WHERE wf=? AND sim_minute<=? ORDER BY version DESC LIMIT 1", wf, at)
    pv = rows[0] if rows else None
    cfgrow = None
    if pv and pv["proposal_id"]:
        r = st.rows("SELECT config FROM proposals WHERE proposal_id=?", pv["proposal_id"])
        cfgrow = json.loads(r[0]["config"]) if r else None
    print(f"## What could a {feats} customer of {wf} hear at {fm(at)}?\n", file=out)
    if not pv:
        print("Business default config (no change by anyone yet).", file=out)
        return
    if cfgrow is None:
        print(f"Config v{pv['version']} was set by **{pv['actor']}** at {fm(pv['sim_minute'])}; its contents are in the "
              f"platform config history (we store our own proposals' configs, not supervisors').", file=out)
        return
    cohort = next((c["id"] for c in cfgrow["cohorts"] if matches(c["where"], feats)), "default")
    print(f"Config **v{pv['version']}** (actor {pv['actor']}, live since {fm(pv['sim_minute'])}); this customer falls in "
          f"cohort **{cohort}**.", file=out)
    for dim in ("welcome_message", "voice", "caller_id"):
        d = (cfgrow["allocations"].get(cohort) or {}).get(dim) or cfgrow["allocations"]["default"][dim]
        print(f"- {dim}: {', '.join(f'{o} {w:.0%}' for o, w in d.items())}", file=out)
    print(f"- prompt: {cfgrow['prompt_version']}", file=out)
    print(f"\nWhy: run `why --wf {wf} --at \"{fm(at)}\"`. Note: for `next_campaign_only` workflows the allocation is the "
          f"one live when the lead's campaign was created.", file=out)


def supervisor(st: Store, wf_filter: str | None, out):
    blob, model, now = _load(st)
    mods = blob.get("mod", {})
    wfs = sorted({r["wf"] for r in st.rows("SELECT DISTINCT wf FROM decisions WHERE wf IS NOT NULL")})
    print(f"# Supervisor view — as of {fm(now)}\n", file=out)
    for wf in wfs:
        if wf_filter and wf != wf_filter:
            continue
        print(f"## {wf}\n", file=out)
        alerts = st.rows("SELECT * FROM alerts WHERE wf=? AND resolved_minute IS NULL AND severity!='info'", wf)
        print("**Needs you:** " + ("; ".join(f"[{a['severity']}] {a['message']}" for a in alerts) or "nothing open"), file=out)
        pend = st.rows("SELECT * FROM proposals WHERE wf=? AND path='approval' AND status IN ('pending','approved') "
                       "ORDER BY impact_score DESC", wf)
        print("\n**Waiting for your approval** (ranked by impact score):", file=out)
        for p in pend or []:
            print(f"- {p['approval_id']} · impact {p['impact_score']} · ~{p['affected_leads']} leads · {p['change_summary']}",
                  file=out)
        if not pend:
            print("- none", file=out)
        print("\n**Being tested / winning:**", file=out)
        ex = mods.get("experiments", {}).get(wf, {})
        cube = None
        for dim in ("welcome_message", "voice"):
            s = ex.get(dim)
            if not isinstance(s, dict):
                continue
            line = f"- {dim}: {s.get('phase')} · control {s.get('control')}"
            if s.get("phase") == "exploring" and s.get("start_day") is not None:
                cube = cube or Cube(model.agg(wf), s["start_day"])
                for arm in s.get("active", []):
                    d = cube.compare(dim, "*", arm, s["control"], "eng")
                    line += f" · {arm} {d.lift:+.1%} (z={d.z:.1f}, n={int(d.n_a)})"
            if s.get("winner"):
                line += f" · **winner {s['winner']}** ({s.get('promo_lift', 0):+.1%} at promotion, holdout kept)"
            if s.get("dropped"):
                line += f" · withdrawn {sorted(s['dropped'])}"
            if s.get("decayed"):
                line += f" · rolled back after decay {sorted(s['decayed'])}"
            for seg, o in (s.get("overrides") or {}).items():
                line += f" · {seg}: {o.get('state')} {o.get('winner') or o.get('control')}"
            print(line, file=out)
        if ex.get("logged_scope"):
            print("- no variant tests: primary KPI is pickup (welcome/voice can't move it)", file=out)
        cid = mods.get("callerid", {}).get(wf, {})
        print(f"- caller IDs: quarantined {sorted(cid.get('quarantined', {})) or 'none'}; weak {cid.get('weak') or 'none'}",
              file=out)
        inc = mods.get("incidents", {}).get(wf, {}).get("incident")
        if inc:
            print(f"- hallucination incident {inc['id']}: {inc['state']} (bad prompt {inc['bad_prompt']}, fix "
                  f"{inc.get('candidate')})", file=out)
        day = now // MIN_PER_DAY
        print(f"\n**Changed today (D{day}):**", file=out)
        ch = st.rows("SELECT v.*, p.change_summary FROM config_versions v LEFT JOIN proposals p ON p.proposal_id=v.proposal_id "
                     "WHERE v.wf=? AND v.sim_minute>=? ORDER BY v.version", wf, day * MIN_PER_DAY)
        for c in ch or []:
            print(f"- {fm(c['sim_minute'])} v{c['version']} by {c['actor']}: {c['change_summary'] or json.loads(c['changed_dims'])}",
                  file=out)
        if not ch:
            print("- nothing", file=out)
        k = _kpis(model, wf, (day - 1) * MIN_PER_DAY, day * MIN_PER_DAY)
        print(f"\n**Yesterday:** pickup {k['pickup']} · engagement {k['engagement']} (kept separate, never blended)\n",
              file=out)


def weekly(st: Store, wf: str, out):
    print(f"## Operational changes made under policy (auto-apply) — {wf}\n", file=out)
    for p in st.rows("SELECT * FROM proposals WHERE wf=? AND path='auto' AND status='applied' ORDER BY created_minute", wf):
        print(f"- {fm(p['applied_minute'] or p['created_minute'])} v{p['applied_version']}: {p['change_summary']}", file=out)


def health(st: Store, out):
    m = st.rows("SELECT * FROM metrics ORDER BY id")
    if not m:
        print("no metrics yet", file=out)
        return
    ev = sum(r["events"] for r in m)
    ing = sum(r["ingest_s"] for r in m)
    dec = sum(r["decide_s"] for r in m)
    wall = m[-1]["wall_ts"] - m[0]["wall_ts"]
    days = max((m[-1]["sim_minute"] - m[0]["sim_minute"]) / MIN_PER_DAY, 1e-9)
    print("## Loop health\n", file=out)
    print(f"- loops: {len(m)} · sim days: {days:.2f} · wall: {wall:.0f}s → **{wall / days:.1f}s wall per sim day**", file=out)
    print(f"- events ingested: {ev} · ingest time {ing:.1f}s → **{ev / ing if ing else 0:,.0f} events/s**", file=out)
    print(f"- decision time: total {dec:.1f}s, worst loop {max(r['decide_s'] for r in m):.2f}s", file=out)
    print(f"- worst loop (ingest+decide): {max(r['ingest_s'] + r['decide_s'] for r in m):.2f}s wall; data freshness at "
          f"decision time ≤ {m[-1]['decision_lag_min']} sim-min (one loop step)", file=out)
    print(f"- peak RSS: {max(r['rss_mb'] for r in m):.0f} MB · credits left: {m[-1]['credits']}", file=out)
    hrs = {}
    for d in st.rows("SELECT sim_minute/60 AS h, COUNT(*) AS n FROM decisions WHERE kind NOT IN ('wait','event','skip') "
                     "GROUP BY h"):
        hrs[d["h"]] = d["n"]
    print(f"- decisions/hour: mean {sum(hrs.values()) / max(len(hrs), 1):.1f}, max {max(hrs.values(), default=0)}", file=out)
    spend = st.rows("SELECT kind, SUM(credits) AS c, COUNT(*) AS n FROM credit_spend GROUP BY kind")
    print(f"- credit burn: {spend or 'none'}", file=out)
    al = st.rows("SELECT severity, code, wf, message, ts FROM alerts WHERE resolved_minute IS NULL AND severity IN ('page','ticket')")
    print(f"- open alerts: {len(al)}", file=out)
    for a in al:
        print(f"  - [{a['severity']}] {a['ts']} {a['message']}", file=out)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="controller.report")
    ap.add_argument("--db", required=True)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("supervisor")
    s.add_argument("--wf")
    w = sub.add_parser("why")
    w.add_argument("--wf", required=True)
    w.add_argument("--at", required=True)
    h = sub.add_parser("heard")
    h.add_argument("--wf", required=True)
    h.add_argument("--at", required=True)
    h.add_argument("--features", required=True, help="lang=hi,age_band=60+,city_tier=1,value_band=mid")
    sub.add_parser("health")
    wk = sub.add_parser("weekly")
    wk.add_argument("--wf", required=True)
    a = ap.parse_args(argv)
    st = Store(a.db)
    out = sys.stdout
    if a.cmd == "supervisor":
        supervisor(st, a.wf, out)
    elif a.cmd == "why":
        why(st, a.wf, parse_when(a.at), out)
    elif a.cmd == "heard":
        heard(st, a.wf, parse_when(a.at), dict(x.split("=", 1) for x in a.features.split(",")), out)
    elif a.cmd == "health":
        health(st, out)
    else:
        weekly(st, a.wf, out)


if __name__ == "__main__":
    main()
