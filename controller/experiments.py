"""Variant experimentation on welcome message and voice (C6) + promotion, decay and rollback (C7).

Scope rule: experiments run only where the workflow's *primary* KPI is engagement. A welcome line
or a voice is heard after the person has picked up, so it cannot move pickup. On a pickup-primary
workflow every arm is pure guardrail risk with no primary upside, so we don't run them there.

Design (per workflow, per dimension; the two dimensions run as one factorial, because the simulator
samples each dimension independently per call):

  explore   control (current majority option) keeps 1-eps; each challenger gets EPS_PER_ARM
            (eps capped at EPS_MAX). Asymmetric stopping:
              drop      z <= Z_DROP (-2.0) on >= MIN_DROP connected calls. Cheap to be wrong (we lose
                        a maybe-fine arm), expensive to be slow (customers hear a worse variant).
              promote   z >= Z_PROMOTE on >= MIN_PROMOTE connected calls, AND the last 24h is not
                        pointing the other way (z_24h > -1), AND the pickup guardrail is not tripped,
                        AND no segment with >= 10% of traffic is significantly hurt.
            Z_PROMOTE is calibrated by simulation (tests/test_false_winner.py) so the chance of
            promoting *any* arm in an A/A world with hourly looks over 7 days is <= 5%.
  segment   if no arm wins overall, a single-feature segment (>= 15% of traffic) can get its own
            winner at a stricter Z_SEGMENT (Bonferroni over segments x arms). It becomes a cohort.
  promoted  winner gets 1-HOLDOUT, the old control keeps HOLDOUT. The holdout is what makes decay
            detectable: it is the only concurrent comparison we have. Rollback when, over the last
            24h, the winner is behind the holdout (z <= -1.64), or its lift has fallen significantly
            below the lift it was promoted on (z <= -2.5) and is now <= 0.
  timeout   no winner after MAX_EXPLORE_DAYS -> stop exploring (exploration has a cost).

Every comparison is stratified by (day, config version, cohort) (stats.stratified_diff), so arms
are only compared inside the same stratum, and arms that never ran contribute nothing.
"""
from __future__ import annotations

from .common import Ctx, Part, norm
from .model import AGENT_FACING
from .stats import bonferroni_z, stratified_diff

MODULE = "experiments"
EPS_PER_ARM = 0.15
EPS_MAX = 0.45
HOLDOUT = 0.20
MIN_DROP = 150
MIN_PROMOTE = 400
Z_DROP = -2.0
Z_PROMOTE = 3.0
Z_GUARD = -3.0
MIN_SEG_SHARE = 0.15
HARM_SEG_SHARE = 0.10
Z_SEG_HARM = -2.5
MAX_EXPLORE_DAYS = 5
MIN_SEG_DAYS = 2
FREEZE_MINUTES = 48 * 60
DECAY_WINDOW_H = 24


class Cube:
    """(dim, seg) -> stratum -> option -> [dials, conn, eng], restricted to days >= from_day."""

    def __init__(self, a, from_day: int, min_minute_day: int | None = None):
        self.c: dict = {}
        for k, v in a.arm.items():
            day, ver, coh, dim, opt, seg = k.split("|", 5)
            if int(day) < from_day:
                continue
            s = self.c.setdefault((dim, seg), {}).setdefault(f"{day}|{ver}|{coh}", {})
            cur = s.get(opt)
            if cur is None:
                s[opt] = list(v)
            else:
                for i in range(3):
                    cur[i] += v[i]

    def compare(self, dim, seg, a_opt, b_opt, metric="eng", day_from=None):
        pairs = []
        for st, opts in self.c.get((dim, seg), {}).items():
            if day_from is not None and int(st.split("|", 1)[0]) < day_from:
                continue
            if a_opt in opts and b_opt in opts:
                A, B = opts[a_opt], opts[b_opt]
                if metric == "eng":
                    pairs.append(((A[2], A[1]), (B[2], B[1])))
                else:
                    pairs.append(((A[1], A[0]), (B[1], B[0])))
        return stratified_diff(pairs)

    def conn(self, dim, seg, opt=None) -> float:
        return sum(v[1] for s in self.c.get((dim, seg), {}).values() for o, v in s.items() if opt in (None, o))

    def segments(self, dim):
        return [seg for (d, seg) in self.c if d == dim and seg != "*"]


def _st(ctx: Ctx, wf: str, dim: str) -> dict:
    return ctx.mstate(MODULE, wf).setdefault(dim, {"phase": "off", "dropped": {}, "decayed": {}, "overrides": {}})


def on_supervisor_edit(ctx: Ctx, wf: str, dims: list[str], new_cfg: dict):
    """A supervisor changed a dimension we were testing: stop, adopt their choice, back off."""
    for dim in AGENT_FACING:
        if dim not in dims:
            continue
        st = _st(ctx, wf, dim)
        dist = new_cfg["allocations"]["default"][dim]
        ctrl = max(dist, key=dist.get)
        prev = st.get("phase")
        st.update({"phase": "frozen", "frozen_until": ctx.now + FREEZE_MINUTES, "control": ctrl,
                   "active": [], "winner": None, "overrides": {}})
        ctx.decide(wf, MODULE, "supervisor_freeze",
                   f"Supervisor set {dim} to {norm(dist)} while our {dim} work was '{prev}'. Adopting it as the new "
                   f"control; no {dim} proposals for {FREEZE_MINUTES // 60}h, and any later one goes through approval.",
                   {"dim": dim, "previous_phase": prev, "supervisor_dist": dist})


def evaluate(ctx: Ctx, wf: str) -> tuple[dict, list[Part]]:
    """Returns ({dim: {"default": dist, "overrides": {seg: dist}}} for dims we control, parts)."""
    info = ctx.wfs[wf]
    if info["objective"]["primary"] != "engagement_rate":
        st = ctx.mstate(MODULE, wf)
        if not st.get("logged_scope"):
            ctx.decide(wf, MODULE, "out_of_scope", "Primary KPI is pickup; welcome/voice are heard after pickup and "
                       "cannot move it, so no variant experiments run here (guardrail risk without primary upside)")
            st["logged_scope"] = True
        return {}, []
    guard = info["objective"].get("guardrail", {})
    max_drop = float(guard.get("pickup_rate_max_drop", 0.02))
    a = ctx.model.agg(wf)
    cfg = ctx.configs[wf]["config"]
    targets, parts = {}, []
    for dim in AGENT_FACING:
        opts = info["options"][dim]
        if len(opts) < 2:
            continue
        st = _st(ctx, wf, dim)
        cur = cfg["allocations"]["default"][dim]
        if st["phase"] == "frozen":
            if ctx.now < st["frozen_until"]:
                continue
            st["phase"] = "off"
        if st["phase"] in ("off",):
            t, p = _start(ctx, wf, dim, st, opts, cur)
            if t:
                targets[dim] = t
                parts += p
            continue
        cube = Cube(a, st["start_day"])
        if st["phase"] == "exploring":
            t, p = _explore_step(ctx, wf, dim, st, cube, max_drop, opts)
        elif st["phase"] == "promoted":
            t, p = _promoted_step(ctx, wf, dim, st, cube)
        else:  # done
            t, p = _target(st), []
        targets[dim] = t
        parts += p
    return targets, parts


def _target(st: dict) -> dict:
    ph = st["phase"]
    c = st["control"]
    if ph == "exploring":
        act = st["active"]
        eps = min(EPS_PER_ARM * len(act), EPS_MAX)
        d = {c: 1 - eps, **{x: eps / len(act) for x in act}} if act else {c: 1.0}
    elif ph == "promoted":
        d = {st["winner"]: 1 - HOLDOUT, c: HOLDOUT}
    else:
        d = {c: 1.0}
    ov = {seg: {o["winner"]: 1 - HOLDOUT, o["control"]: HOLDOUT} if o.get("winner") else {o["control"]: 1.0}
          for seg, o in st.get("overrides", {}).items() if o.get("state") in ("won", "protect")}
    return {"default": norm(d), "overrides": ov}


def _start(ctx, wf, dim, st, opts, cur):
    ctrl = max(cur, key=cur.get)
    active = [o for o in opts if o != ctrl and o not in st["dropped"] and o not in st["decayed"]]
    if not active:
        if st["phase"] != "done":
            st.update({"phase": "done", "control": ctrl})
        return None, []
    st.update({"phase": "exploring", "control": ctrl, "active": active, "start_day": ctx.today,
               "started_at": ctx.now, "live_at": None, "winner": None, "overrides": {}})
    t = _target(st)
    eps = 1 - t["default"].get(ctrl, 0)
    did = ctx.decide(wf, MODULE, "explore", f"Start testing {dim} {', '.join(active)} against {ctrl}",
                     {"control": ctrl, "arms": active, "eps": eps})
    return t, [Part(
        module=MODULE, kind="explore", dims={dim},
        summary=f"Test {dim.replace('_', ' ')} {', '.join(active)} on {eps:.0%} of calls; {ctrl} stays on {1 - eps:.0%}",
        rationale=(f"No production evidence exists for {', '.join(active)} (history ran only {ctrl}). "
                   f"Each challenger gets {EPS_PER_ARM:.0%}; an arm is withdrawn as soon as it is measurably worse "
                   f"(z<={Z_DROP} on >={MIN_DROP} connected calls); a winner needs z>={Z_PROMOTE} on >={MIN_PROMOTE}."),
        urgent=False, material=True, auto_ok=False, impact=0.0,
        evidence={"control": ctrl, "arms": active},
        dry_run={"method": "exploration has no measured effect yet",
                 "expected_engagement_change": 0.0,
                 "worst_case_if_every_challenger_is_10pct_worse": round(-0.10 * eps, 4)},
        decision_id=did)]


def _explore_step(ctx, wf, dim, st, cube: Cube, max_drop: float, opts):
    parts: list[Part] = []
    ctrl = st["control"]
    live = ctx.configs[wf]["config"]["allocations"]["default"][dim]
    if not st.get("live_at") and any(live.get(a, 0) > 0 for a in st["active"]):
        st["live_at"] = ctx.now
        ctx.decide(wf, MODULE, "explore_live", f"{dim} test is live: {live}", {"alloc": live})
    results = {}
    for arm in list(st["active"]):
        e = cube.compare(dim, "*", arm, ctrl, "eng")
        pk = cube.compare(dim, "*", arm, ctrl, "pick")
        results[arm] = (e, pk)
        if e.n_a >= MIN_DROP and e.z <= Z_DROP:
            st["active"].remove(arm)
            st["dropped"][arm] = {"at": ctx.now, "lift": round(e.lift, 4), "z": round(e.z, 2), "n": e.n_a}
            did = ctx.decide(wf, MODULE, "drop", f"{dim} {arm} withdrawn: engagement {e.lift:+.1%} vs {ctrl} "
                             f"(z={e.z:.1f}, {int(e.n_a)} vs {int(e.n_b)} connected calls)",
                             {"arm": arm, "lift": e.lift, "z": e.z, "n_arm": e.n_a, "n_ctrl": e.n_b})
            parts.append(Part(module=MODULE, kind="drop", dims={dim},
                              summary=f"Stop {dim.replace('_', ' ')} {arm}: {e.lift:+.1%} engagement vs {ctrl}",
                              rationale=f"Stratified comparison z={e.z:.2f} on {int(e.n_a)} connected calls.",
                              urgent=True, harm_reduction=True, auto_ok=True,
                              impact=abs(e.lift) * EPS_PER_ARM * ctx.engaged_per_day(wf),
                              evidence={"lift": e.lift, "z": e.z, "ci": e.ci()},
                              dry_run={"method": "measured arm vs control, same strata",
                                       "engagement_arm": round(e.rate_b + e.diff, 4), "engagement_control": round(e.rate_b, 4)},
                              decision_id=did))
        elif pk.n_a >= MIN_DROP and pk.z <= Z_GUARD and pk.diff < -max_drop / 2:
            st["active"].remove(arm)
            st["dropped"][arm] = {"at": ctx.now, "guardrail": True, "pickup_diff": round(pk.diff, 4), "z": round(pk.z, 2)}
            did = ctx.decide(wf, MODULE, "drop_guardrail", f"{dim} {arm} withdrawn on the pickup guardrail "
                             f"({pk.diff:+.3f}, z={pk.z:.1f})", {"arm": arm, "pickup_diff": pk.diff, "z": pk.z})
            parts.append(Part(module=MODULE, kind="drop", dims={dim}, summary=f"Stop {dim} {arm}: pickup guardrail",
                              rationale=f"pickup {pk.diff:+.3f} vs control (z={pk.z:.2f})", urgent=True,
                              harm_reduction=True, auto_ok=True, decision_id=did))
    cands = []
    for arm in st["active"]:
        e, pk = results[arm]
        if e.n_a < MIN_PROMOTE or e.z < Z_PROMOTE:
            continue
        r24 = cube.compare(dim, "*", arm, ctrl, "eng", day_from=ctx.today - 1)
        if r24.strata and r24.z <= -1.0:
            ctx.decide(wf, MODULE, "hold", f"{dim} {arm} leads overall (z={e.z:.1f}) but the last 24h points the other "
                       f"way (z={r24.z:.1f}); not promoting", {"arm": arm, "z": e.z, "z_24h": r24.z})
            continue
        if pk.z <= -2.0 and pk.diff < -max_drop / 2:
            ctx.decide(wf, MODULE, "hold_guardrail", f"{dim} {arm} wins on engagement but pickup is {pk.diff:+.3f} "
                       f"(z={pk.z:.1f}); not promoting", {"arm": arm, "pickup_diff": pk.diff, "z": pk.z})
            continue
        cands.append((e.z, arm, e))
    if cands:
        _, arm, e = max(cands)
        harm = []
        tot = cube.conn(dim, "*")
        for seg in cube.segments(dim):
            if cube.conn(dim, seg) < HARM_SEG_SHARE * tot:
                continue
            s = cube.compare(dim, seg, arm, ctrl, "eng")
            if s.z <= Z_SEG_HARM:
                harm.append((seg, s))
        st.update({"phase": "promoted", "winner": arm, "promoted_at": ctx.now, "promo_diff": e.diff,
                   "promo_se": e.se, "promo_lift": e.lift, "not_chosen": [x for x in st["active"] if x != arm],
                   "active": []})
        for seg, s in harm[:2]:
            st["overrides"][seg] = {"state": "protect", "winner": None, "control": ctrl, "at": ctx.now,
                                    "lift": round(s.lift, 4), "z": round(s.z, 2)}
        did = ctx.decide(wf, MODULE, "promote",
                         f"{dim} {arm} promoted: engagement {e.lift:+.1%} vs {ctrl} (z={e.z:.1f}, "
                         f"{int(e.n_a)}/{int(e.n_b)} connected); {ctrl} kept on {HOLDOUT:.0%} as holdout"
                         + (f"; protected segments {[h[0] for h in harm[:2]]}" if harm else ""),
                         {"arm": arm, "lift": e.lift, "z": e.z, "ci": e.ci(), "n": [e.n_a, e.n_b],
                          "protected": [(h[0], h[1].lift, h[1].z) for h in harm[:2]]})
        parts.append(Part(
            module=MODULE, kind="promote", dims={dim} | ({"cohorts"} if harm else set()),
            summary=f"Make {dim.replace('_', ' ')} {arm} the default ({e.lift:+.1%} engagement vs {ctrl}); "
                    f"keep {ctrl} on {HOLDOUT:.0%} to watch for decay",
            rationale=(f"Stratified by day x config version x cohort: {arm} {e.rate_b + e.diff:.3f} vs {ctrl} "
                       f"{e.rate_b:.3f}, diff {e.diff:+.3f} (95% CI {e.ci()[0]:+.3f}..{e.ci()[1]:+.3f}), z={e.z:.2f}, "
                       f"{int(e.n_a)} vs {int(e.n_b)} connected calls. Threshold z>={Z_PROMOTE} (calibrated for <=5% "
                       f"false winners under hourly looks)."
                       + (f" Segments where {arm} measurably hurts keep {ctrl}: "
                          + ", ".join(f"{h[0]} ({h[1].lift:+.1%}, z={h[1].z:.1f})" for h in harm[:2]) if harm else "")),
            urgent=False, material=True, auto_ok=False, impact=e.lift * (1 - HOLDOUT) * ctx.engaged_per_day(wf),
            evidence={"lift": e.lift, "z": e.z, "ci": e.ci(), "n_arm": e.n_a, "n_ctrl": e.n_b},
            dry_run={"method": "measured arm vs control, same strata, applied to new traffic split",
                     "engagement_control": round(e.rate_b, 4), "engagement_winner": round(e.rate_b + e.diff, 4),
                     "expected_relative_change": round(e.lift * (1 - HOLDOUT), 4)},
            affected_preds=None, decision_id=did))
        return _target(st), parts
    # segment-level winners, after a minimum run and only if nothing won overall
    if ctx.today - st["start_day"] >= MIN_SEG_DAYS and len(st.get("overrides", {})) < 2:
        segs = [s for s in cube.segments(dim) if cube.conn(dim, s) >= MIN_SEG_SHARE * cube.conn(dim, "*")]
        zc = bonferroni_z(0.05, max(1, len(segs) * len(st["active"])))
        zc = max(zc, Z_PROMOTE + 0.5)
        best = None
        for seg in segs:
            if seg in st["overrides"]:
                continue
            for arm in st["active"]:
                s = cube.compare(dim, seg, arm, st["control"], "eng")
                if s.n_a >= MIN_PROMOTE // 2 and s.z >= zc and (best is None or s.z > best[2].z):
                    best = (seg, arm, s)
        if best:
            seg, arm, s = best
            st["overrides"][seg] = {"state": "won", "winner": arm, "control": st["control"], "at": ctx.now,
                                    "promo_diff": s.diff, "promo_se": s.se, "lift": round(s.lift, 4)}
            did = ctx.decide(wf, MODULE, "promote_segment", f"{dim} {arm} wins for {seg}: {s.lift:+.1%} "
                             f"(z={s.z:.1f} >= {zc:.2f}, Bonferroni over {len(segs)} segments)",
                             {"seg": seg, "arm": arm, "lift": s.lift, "z": s.z, "z_crit": zc})
            f, v = seg.split("=", 1)
            parts.append(Part(
                module=MODULE, kind="promote_segment", dims={dim, "cohorts"},
                summary=f"Use {dim.replace('_', ' ')} {arm} for {f.replace('_', ' ')} {v} ({s.lift:+.1%} engagement)",
                rationale=(f"Only this segment shows a reliable effect: {s.rate_b + s.diff:.3f} vs {s.rate_b:.3f}, "
                           f"z={s.z:.2f} against a Bonferroni-corrected bar of {zc:.2f}. Creates cohort for {seg}."),
                material=True, auto_ok=False, impact=s.lift * (1 - HOLDOUT) * ctx.engaged_per_day(wf) *
                (cube.conn(dim, seg) / max(cube.conn(dim, '*'), 1)),
                evidence={"seg": seg, "lift": s.lift, "z": s.z, "z_crit": zc},
                dry_run={"method": "measured segment arm vs control", "engagement_control": round(s.rate_b, 4),
                         "engagement_winner": round(s.rate_b + s.diff, 4)},
                affected_preds=[{f: [v]}], decision_id=did))
    if not st["active"] and st["phase"] == "exploring":
        st["phase"] = "done"
        ctx.decide(wf, MODULE, "explore_end", f"{dim}: every challenger withdrawn; {ctrl} stays",
                   {"dropped": st["dropped"]})
    elif st["phase"] == "exploring" and st.get("live_at") is not None and \
            ctx.now - st["live_at"] >= MAX_EXPLORE_DAYS * 1440:
        st["phase"] = "done"
        st["active"] = []
        did = ctx.decide(wf, MODULE, "explore_timeout", f"{dim}: no winner after {MAX_EXPLORE_DAYS} days; stop "
                         f"exploring, {ctrl} back to 100%", {k: (v[0].lift, v[0].z, v[0].n_a) for k, v in results.items()})
        parts.append(Part(module=MODULE, kind="explore_end", dims={dim}, summary=f"End {dim} test; no reliable winner",
                          rationale="Exploration costs traffic; no arm cleared the bar.", material=True,
                          auto_ok=True, harm_reduction=True, decision_id=did))
    return _target(st), parts


def _promoted_step(ctx, wf, dim, st, cube: Cube):
    parts: list[Part] = []
    ctrl, win = st["control"], st["winner"]
    since = (ctx.now - DECAY_WINDOW_H * 60) // 1440
    r = cube.compare(dim, "*", win, ctrl, "eng", day_from=since)
    decayed = False
    why = ""
    if r.strata and r.n_b >= 60:
        if r.z <= -1.64 and r.diff < 0:
            decayed, why = True, f"behind the holdout over {DECAY_WINDOW_H}h ({r.lift:+.1%}, z={r.z:.1f})"
        else:
            se = (r.se ** 2 + st["promo_se"] ** 2) ** 0.5
            zd = (r.diff - st["promo_diff"]) / se if se else 0
            if zd <= -2.5 and r.diff <= 0:
                decayed, why = True, (f"lift fell from {st['promo_lift']:+.1%} at promotion to {r.lift:+.1%} "
                                      f"(change z={zd:.1f})")
    for seg, o in list(st.get("overrides", {}).items()):
        if o.get("state") != "won":
            continue
        s = cube.compare(dim, seg, o["winner"], o["control"], "eng", day_from=since)
        if s.strata and s.n_b >= 40 and s.z <= -1.64 and s.diff < 0:
            o["state"] = "decayed"
            did = ctx.decide(wf, MODULE, "rollback_segment", f"{dim} {o['winner']} for {seg} rolled back: "
                             f"{s.lift:+.1%} vs holdout (z={s.z:.1f})", {"seg": seg, "lift": s.lift, "z": s.z})
            parts.append(Part(module=MODULE, kind="rollback", dims={dim}, summary=f"Roll back {dim} {o['winner']} for {seg}",
                              rationale=f"Recent {DECAY_WINDOW_H}h: {s.lift:+.1%} vs holdout, z={s.z:.2f}",
                              urgent=True, harm_reduction=True, auto_ok=True, decision_id=did))
    if decayed:
        st["decayed"][win] = {"at": ctx.now, "why": why}
        st["phase"] = "done"
        st["winner"] = None
        did = ctx.decide(wf, MODULE, "rollback", f"{dim} {win} rolled back to {ctrl}: {why}",
                         {"arm": win, "recent_lift": r.lift, "z": r.z, "n": [r.n_a, r.n_b],
                          "promo_lift": st["promo_lift"]})
        ctx.alert(wf, "info", f"decay:{dim}:{win}", f"{wf}: promoted {dim} {win} stopped winning ({why}); "
                  f"controller rolled back to {ctrl}. No action needed unless rollback is stuck in approval.")
        parts.append(Part(module=MODULE, kind="rollback", dims={dim},
                          summary=f"Roll back {dim.replace('_', ' ')} {win} -> {ctrl}: it stopped winning",
                          rationale=f"Concurrent holdout comparison, last {DECAY_WINDOW_H}h: {why}.",
                          urgent=True, harm_reduction=True, auto_ok=True,
                          impact=max(-r.lift, 0) * (1 - HOLDOUT) * ctx.engaged_per_day(wf),
                          evidence={"recent_lift": r.lift, "z": r.z, "promo_lift": st["promo_lift"]},
                          dry_run={"method": "recent winner vs holdout", "engagement_winner": round(r.rate_b + r.diff, 4),
                                   "engagement_holdout": round(r.rate_b, 4)},
                          decision_id=did))
    return _target(st), parts
