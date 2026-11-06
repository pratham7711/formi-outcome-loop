"""Call-time optimisation per cohort (C3 + C4).

The business owns how many dials happen each hour; we only choose *who* is dialled in that hour
(schedule.hour_weights). The lever is matching people to the hours they answer in.

1. **Which feature?** From history (pre-D0, default config, unweighted schedule) we fit pickup as
   level(segment) x shape(hour) and measure how much a segment-specific hour *shape* adds (G-test of
   the segment x hour interaction). The feature with the largest G per degree of freedom becomes the
   schedule feature, if it clears G/df >= MIN_G_PER_DF. No feature is assumed (hidden scenarios can
   differ).
2. **Profiles.** For each value v of that feature and each dialling hour h, pickup p_vh is shrunk
   toward the pooled hour shape with K pseudo-dials, and turned into a relative profile
   rel_vh = p_vh / mean_h(p_vh). Only the *shape* matters: the dialler picks the leads with the
   largest weight in each hour, so what helps is giving the people who answer at 10:00 priority at 10:00.
3. **Weights** w_vh = clip(rel_vh ** GAMMA, W_MIN, W_MAX). Every dialling hour keeps a positive
   weight (an omitted hour is weight 0 in the simulator and would idle capacity).
4. **Dry run.** Re-weight the last 7 days of observed dials inside each hour by w_new/w_old and
   recompute expected pickup. This assumes capacity is the binding constraint (it is until early
   afternoon in every public scenario) and ignores the knock-on effect on later attempts. That's a
   stated approximation, so the impact score is discounted by half.
Per-person preferred hours are deliberately not used (see SUBMISSION.md section 5).
"""
from __future__ import annotations

import math

from .common import Ctx, Part

MODULE = "schedule"
K_SHRINK = 150.0
GAMMA = 2.0
W_MIN, W_MAX = 0.25, 4.0
MIN_G_PER_DF = 3.0
MIN_GAIN_FIRST = 0.01
MIN_GAIN_RETUNE = 0.005
HISTORY_WINDOW_DAYS = 14


def _xlogy(x, y):
    return x * math.log(x / y) if x > 0 and y > 0 else 0.0


def _cube(a, lo_day: int, hi_day: int):
    """feat -> val -> hour -> [dials, conn] over days [lo_day, hi_day)."""
    out: dict = {}
    for k, (d, c) in a.pk_seg.items():
        day, hour, seg = k.split("|", 2)
        if not lo_day <= int(day) < hi_day:
            continue
        f, v = seg.split("=", 1)
        cell = out.setdefault(f, {}).setdefault(v, {}).setdefault(int(hour), [0, 0])
        cell[0] += d
        cell[1] += c
    return out


def choose_feature(cube: dict, hours: list[int]):
    best, scores = None, {}
    for f, vals in cube.items():
        tot_h = {h: [0, 0] for h in hours}
        for v, hs in vals.items():
            for h in hours:
                d, c = hs.get(h, [0, 0])
                tot_h[h][0] += d
                tot_h[h][1] += c
        D = sum(x[0] for x in tot_h.values())
        C = sum(x[1] for x in tot_h.values())
        if not D or not C:
            continue
        p = C / D
        G, df = 0.0, 0
        for v, hs in vals.items():
            dv = sum(hs.get(h, [0, 0])[0] for h in hours)
            cv = sum(hs.get(h, [0, 0])[1] for h in hours)
            if dv < 200:
                continue
            pv = cv / dv
            df += len(hours) - 1
            for h in hours:
                d, c = hs.get(h, [0, 0])
                if not d or not tot_h[h][0]:
                    continue
                e = d * min(max(pv * (tot_h[h][1] / tot_h[h][0]) / p, 1e-4), 0.999)
                G += 2 * (_xlogy(c, e) + _xlogy(d - c, d - e))
        df = max(df - (len(hours) - 1), 1)
        scores[f] = round(G / df, 2)
        if G / df >= MIN_G_PER_DF and (best is None or G / df > scores[best]):
            best = f
    return best, scores


def profiles(vals: dict, hours: list[int]) -> dict:
    tot = {h: [sum(hs.get(h, [0, 0])[0] for hs in vals.values()),
               sum(hs.get(h, [0, 0])[1] for hs in vals.values())] for h in hours}
    pooled = {h: (tot[h][1] / tot[h][0]) if tot[h][0] else None for h in hours}
    known = [x for x in pooled.values() if x]
    pbar = sum(known) / len(known) if known else 0.0
    shape = {h: (pooled[h] / pbar if pooled[h] and pbar else 1.0) for h in hours}
    out = {}
    for v, hs in vals.items():
        dv = sum(hs.get(h, [0, 0])[0] for h in hours)
        cv = sum(hs.get(h, [0, 0])[1] for h in hours)
        if dv == 0:
            continue
        pv = cv / dv
        est = {}
        for h in hours:
            d, c = hs.get(h, [0, 0])
            est[h] = (c + K_SHRINK * pv * shape[h]) / (d + K_SHRINK)
        m = sum(est.values()) / len(est)
        out[v] = {"rel": {h: est[h] / m for h in hours}, "p": est, "dials": dv}
    return out


def weights_from(prof: dict) -> dict:
    return {v: {str(h): round(min(max(r ** GAMMA, W_MIN), W_MAX), 2) for h, r in p["rel"].items()}
            for v, p in prof.items()}


def dry_run(cube_f: dict, prof: dict, w_old: dict, w_new: dict, hours: list[int]) -> dict:
    tot_old = tot_new = tot_d = 0.0
    for h in hours:
        cells = []
        for v, hs in cube_f.items():
            d = hs.get(h, [0, 0])[0]
            if d and v in prof:
                cells.append((v, d, prof[v]["p"][h]))
        D = sum(d for _, d, _ in cells)
        if not D:
            continue
        old = sum(d * p for _, d, p in cells) / D
        wsum = sum(d * w_new.get(v, {}).get(str(h), 1.0) / w_old.get(v, {}).get(str(h), 1.0) for v, d, _ in cells)
        new = sum(d * w_new.get(v, {}).get(str(h), 1.0) / w_old.get(v, {}).get(str(h), 1.0) * p
                  for v, d, p in cells) / wsum
        tot_old += old * D
        tot_new += new * D
        tot_d += D
    if not tot_d:
        return {"expected_pickup_old": None, "expected_pickup_new": None, "relative_change": 0.0}
    po, pn = tot_old / tot_d, tot_new / tot_d
    return {"expected_pickup_old": round(po, 4), "expected_pickup_new": round(pn, 4),
            "relative_change": round(pn / po - 1, 4), "dials_replayed": int(tot_d)}


def evaluate(ctx: Ctx, wf: str, current_tables: dict) -> tuple[dict | None, list[Part]]:
    """Returns (target {"feature", "tables": {value: {hour: w}}} | None, parts).

    current_tables: {value: table} the controller has live now for the schedule feature (or {}).
    Re-evaluated once per sim day (before the dialling window) and at the first opportunity.
    """
    st = ctx.mstate(MODULE, wf)
    info = ctx.wfs[wf]
    lo, hi = info["dialling_window"]
    hours = list(range(lo, hi))
    if st.get("evaluated_day") == ctx.today and st.get("target"):
        return st["target"], []
    a = ctx.model.agg(wf)
    cube = _cube(a, ctx.today - HISTORY_WINDOW_DAYS, ctx.today + 1)
    feat, scores = choose_feature(cube, hours) if not st.get("feature") else (st["feature"], st.get("scores", {}))
    st["evaluated_day"] = ctx.today
    if feat is None:
        if not st.get("logged_none"):
            ctx.decide(wf, MODULE, "no_schedule", "No segment has a distinct enough answering-hour pattern; "
                       "leaving the schedule alone", {"g_per_df": scores})
            st["logged_none"] = True
        return None, []
    st["feature"], st["scores"] = feat, scores
    prof = profiles(cube[feat], hours)
    w_new = weights_from(prof)
    recent = _cube(a, ctx.today - 7, ctx.today + 1).get(feat, {})
    dr = dry_run(recent, prof, current_tables or {}, w_new, hours)
    gain = dr["relative_change"]
    first = not current_tables
    need = MIN_GAIN_FIRST if first else MIN_GAIN_RETUNE
    if not first:
        drift = max((abs(w_new[v][h] / current_tables.get(v, {}).get(h, 1.0) - 1)
                     for v in w_new for h in w_new[v]), default=0)
        if drift < 0.25:
            st["target"] = {"feature": feat, "tables": current_tables}
            return st["target"], []
    if gain < need:
        ctx.decide(wf, MODULE, "skip", f"Schedule re-tune by {feat} predicts {gain:+.1%} pickup; below the "
                   f"{need:.1%} bar, not proposing", {"dry_run": dr})
        st["target"] = {"feature": feat, "tables": current_tables} if current_tables else None
        return st["target"], []
    target = {"feature": feat, "tables": w_new}
    unchanged = st.get("target") == target
    st["target"] = target
    if unchanged:
        return target, []
    top = sorted(prof.items(), key=lambda kv: -kv[1]["dials"])
    peaks = "; ".join(f"{feat}={v}: best {max(p['rel'], key=p['rel'].get)}:00" for v, p in top[:4])
    did = ctx.decide(wf, MODULE, "propose", f"Call-time weights by {feat} ({peaks}); dry run {gain:+.1%} pickup",
                     {"feature": feat, "g_per_df": scores, "dry_run": dr, "weights": w_new})
    return target, [Part(
        module=MODULE, kind="schedule", dims={"schedule"},
        summary=f"Call each {feat.replace('_', ' ')} group in the hours it answers ({peaks})",
        rationale=(f"History + recent dials show answering-hour shapes differ most by {feat} "
                   f"(G/df {scores.get(feat)} vs {scores}). Weights = (relative pickup)^{GAMMA:g}, clipped "
                   f"[{W_MIN}, {W_MAX}]; every dialling hour keeps a positive weight."),
        urgent=False, material=True, auto_ok=True, impact=0.5 * gain * ctx.engaged_per_day(wf),
        evidence={"g_per_df": scores, "profile_dials": {v: p["dials"] for v, p in prof.items()}},
        dry_run={"method": "re-weight last 7 days of dials within each hour by w_new/w_old", **dr},
        decision_id=did)]
