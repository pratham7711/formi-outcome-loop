"""Caller-ID health (C5): detect a degraded number, rotate off it, explore unused numbers safely.

Because the dialler samples a caller ID independently on every dial, two caller IDs dialled in the
same hour are a randomised comparison of the same population. So health is measured *relative*:
a number's pickup vs. the pooled pickup of the other numbers in the same clock hour, stratified by
hour. Absolute pickup moves with time of day and population mix; the relative number does not.

States per number: unexplored -> probation (small share) -> healthy | quarantined.
  * quarantine: relative pickup < 0.80 with z < -4 on >= 150 dials in the last 12 clock hours.
    (z < -4 one-sided ~ 3e-5 per check; ~100 checks per number per run => ~0.3% false-alarm rate.)
  * probation: an unused number gets at most PROBE_SHARE of traffic until it has 300 dials.
  * otherwise traffic is spread evenly over healthy numbers. Fewer dials per number keeps each
    one further from carrier spam thresholds, which is the operational point of rotation.
  * a quarantined number rests, then re-enters probation. How long depends on why it looked bad:
    if it carried >= 1.5x its fair share of dials in the window, the drop is plausibly load, so it
    rests REST_DAYS_OVERLOADED; at a normal share it is treated as flagged and rests
    REST_DAYS_FLAGGED (in practice: until a carrier ticket clears it). Spam reputation recovers when a
    number stops being hammered, and a number can also look bad simply because it carried far more
    traffic than its siblings. Probation re-tests it at a small share instead of burning it forever.
  * only ONE unexplored number is probed at a time, at PROBE_SHARE: a bad unknown number costs at
    most PROBE_SHARE x (1 - its health) of pickup until it is caught (~1 day at the lowest volumes).
"""
from __future__ import annotations

from .common import Ctx, Part, norm, tv, window_keys
from .stats import stratified_diff

MODULE = "callerid"
PROBE_SHARE = 0.10
REST_DAYS_OVERLOADED = 2
REST_DAYS_FLAGGED = 7
OVERLOAD_SHARE_X = 1.5
PROBATION_DIALS = 300
QUARANTINE_RATIO = 0.80
QUARANTINE_Z = -4.0
QUARANTINE_MIN_DIALS = 150
WEAK_RATIO = 0.90
WEAK_Z = -3.0
WEAK_EXIT_RATIO = 0.97
REBALANCE_TV = 0.05


def relative_health(pk_did: dict, did: str, now: int, hours: int):
    """Stratified comparison of `did` against all other numbers, by clock hour."""
    strata: dict[str, dict] = {}
    for k, (d, c) in window_keys(pk_did, now, hours):
        day, hour, x = k.split("|")
        s = strata.setdefault(f"{day}|{hour}", {})
        s[x] = (c, d)
    pairs = []
    for s in strata.values():
        if did not in s:
            continue
        oc = sum(v[0] for x, v in s.items() if x != did)
        od = sum(v[1] for x, v in s.items() if x != did)
        if od == 0:
            continue
        pairs.append((s[did], (oc, od)))
    return stratified_diff(pairs)


def evaluate(ctx: Ctx, wf: str) -> tuple[dict | None, list[Part]]:
    """Returns (target caller_id distribution for the default allocation, parts)."""
    info = ctx.wfs[wf]
    opts = info["options"]["caller_id"]
    st = ctx.mstate(MODULE, wf)
    quarantined: dict = st.setdefault("quarantined", {})
    cur = ctx.configs[wf]["config"]["allocations"]["default"]["caller_id"]
    a = ctx.model.agg(wf)
    parts: list[Part] = []
    if len(opts) < 2:
        return None, parts

    dials_3d = {d: 0 for d in opts}
    for k, (n, _) in window_keys(a.pk_did, ctx.now, 72):
        dials_3d[k.rsplit("|", 1)[1]] = dials_3d.get(k.rsplit("|", 1)[1], 0) + n
    dials_ever = {d: 0 for d in opts}
    for k, (n, _) in a.pk_did.items():
        x = k.rsplit("|", 1)[1]
        dials_ever[x] = dials_ever.get(x, 0) + n

    for did, q in list(quarantined.items()):
        if ctx.now - q["at"] >= q.get("rest_days", REST_DAYS_FLAGGED) * 1440:
            quarantined.pop(did)
            st.setdefault("rested", {})[did] = ctx.now
            ctx.decide(wf, MODULE, "release", f"caller ID {did} rested {q.get('rest_days')} days; back on probation at a small share",
                       {"did": did})
            ctx.store.resolve_alert(ctx.now, wf, f"did_quarantined:{did}")
    rested = st.get("rested", {})
    health = {}
    for did in opts:
        if did in quarantined:
            continue
        r12 = relative_health(a.pk_did, did, ctx.now, 12)
        health[did] = r12
        ratio = 1 + r12.lift
        if r12.n_a >= QUARANTINE_MIN_DIALS and ratio < QUARANTINE_RATIO and r12.z < QUARANTINE_Z:
            k_live = max(1, sum(1 for x in opts if x not in quarantined))
            share = r12.n_a / max(r12.n_a + r12.n_b, 1)
            overloaded = share >= OVERLOAD_SHARE_X / k_live
            rest = REST_DAYS_OVERLOADED if overloaded else REST_DAYS_FLAGGED
            quarantined[did] = {"at": ctx.now, "ratio": round(ratio, 3), "z": round(r12.z, 2), "dials": r12.n_a,
                                "share": round(share, 3), "overloaded": overloaded, "rest_days": rest}
            ctx.decide(wf, MODULE, "quarantine", f"caller ID {did} quarantined: pickup {ratio:.0%} of the other "
                       f"numbers over the last 12h (z={r12.z:.1f}, {int(r12.n_a)} dials, {share:.0%} of traffic"
                       f" -> {'overloaded' if overloaded else 'flagged'}, rest {rest}d)",
                       {"did": did, "ratio": ratio, "z": r12.z, "n": r12.n_a, "rate_others": r12.rate_b})
            ctx.alert(wf, "page" if cur.get(did, 0) >= 0.5 else "ticket", f"did_quarantined:{did}",
                      f"{wf}: caller ID {did} answers at {ratio:.0%} of its siblings (z={r12.z:.1f}). "
                      f"Controller is rotating it out; raise a carrier ticket for {did}.")

    # classify the rest
    healthy, probation, weak = [], [], []
    for did in opts:
        if did in quarantined:
            continue
        since = rested.get(did)
        n_probe = dials_ever.get(did, 0) if since is None else sum(
            v[0] for k, v in a.pk_did.items() if k.endswith(f"|{did}") and
            int(k.split("|")[0]) * 1440 + int(k.split("|")[1]) * 60 >= since)
        if n_probe < PROBATION_DIALS:
            probation.append(did)
            continue
        r = relative_health(a.pk_did, did, ctx.now, 72)
        ratio72 = 1 + r.lift
        was_weak = did in st.get("weak", [])
        if r.strata and ((not was_weak and ratio72 < WEAK_RATIO and r.z < WEAK_Z) or
                         (was_weak and ratio72 < WEAK_EXIT_RATIO)):
            weak.append(did)
        else:
            healthy.append(did)
    st["weak"] = weak
    if not healthy:
        healthy = [d for d in opts if d not in quarantined and d not in probation] or probation[:1]
        if not healthy:
            ctx.alert(wf, "page", "no_healthy_caller_id", f"{wf}: every caller ID is quarantined; dialling "
                      f"continues on the current numbers. Provision new numbers.")
            return None, parts
    live_probe = [d for d in probation if cur.get(d, 0) > 0]
    probation = (live_probe or probation)[:1]
    target: dict[str, float] = {}
    probe = min(PROBE_SHARE, 1.0 / (len(healthy) + len(probation)))
    for d in probation:
        target[d] = probe
    # weak numbers keep half a share rather than being dropped: concentrating every dial on fewer
    # numbers is exactly what wears a number out, so removal would oscillate.
    rest = 1.0 - probe * len(probation)
    units = {d: 1.0 for d in healthy}
    units.update({d: 0.5 for d in weak})
    tot = sum(units.values())
    for d, u in units.items():
        target[d] = rest * u / tot
    target = norm(target)

    bad_live = [d for d in cur if d in quarantined and cur[d] > 0]
    shift = tv(cur, target)
    if not bad_live and shift < REBALANCE_TV:
        return cur, parts  # close enough: don't churn config versions for a few points

    rates = {}
    for did in opts:
        dd = sum(v[0] for k, v in window_keys(a.pk_did, ctx.now, 72) if k.endswith(f"|{did}"))
        cc = sum(v[1] for k, v in window_keys(a.pk_did, ctx.now, 72) if k.endswith(f"|{did}"))
        rates[did] = (cc / dd) if dd else None
    known = [r for r in rates.values() if r]
    ref = sum(known) / len(known) if known else 0.0
    est = lambda d: rates.get(d) if rates.get(d) else ref
    exp_old = sum(w * est(d) for d, w in norm(cur).items())
    exp_new = sum(w * est(d) for d, w in target.items())
    rel = (exp_new / exp_old - 1) if exp_old else 0.0
    if bad_live:
        why = ", ".join(f"{d} at {quarantined[d]['ratio']:.0%} of sibling pickup" for d in bad_live)
        summary = f"Move calls off caller ID {', '.join(bad_live)} ({why})"
    else:
        summary = "Spread calls evenly over healthy caller IDs" + (
            f"; probe unused {', '.join(probation)} at {probe:.0%} each" if probation else "")
    parts.append(Part(
        module=MODULE, kind="did_rotate" if bad_live else "did_spread", dims={"caller_id"}, summary=summary,
        rationale=(f"Relative pickup by number over 12h/72h, stratified by hour. Quarantined: "
                   f"{sorted(quarantined) or 'none'}; healthy: {healthy}; on probation: {probation}; weak: {weak}."),
        urgent=bool(bad_live), material=True, auto_ok=True, harm_reduction=bool(bad_live),
        impact=rel * ctx.engaged_per_day(wf),
        evidence={"health": {d: {"ratio_12h": round(1 + h.lift, 3), "z": round(h.z, 2), "dials": int(h.n_a)}
                             for d, h in health.items()}, "dials_72h": dials_3d},
        dry_run={"method": "pickup by caller ID over the last 72h applied to old vs new split",
                 "expected_pickup_old": round(exp_old, 4), "expected_pickup_new": round(exp_new, 4),
                 "relative_change": round(rel, 4), "unexplored_assumed_at_mean": probation},
    ))
    return target, parts
