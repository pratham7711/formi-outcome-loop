"""Monte Carlo of the experiment stopping rules, using the controller's own statistics code.

Question 1 (false winners): in an A/A world (every arm identical), how often does the promote rule fire
for ANY arm, with hourly looks over 7 days? That is the family-wise false-winner rate.
Question 2 (power): with one arm truly +10% / +18%, how often and how fast is it promoted?
Question 3 (false drops): how often is a truly-equal arm withdrawn by the harm rule?

Traffic model: connected calls/hour per workflow (default 90, like nw_renewal), allocation 1-eps control
and EPS_PER_ARM per challenger, 12 dialling hours/day, one stratum per day (as the controller stratifies).

    python scripts/calibrate.py [--sims 2000] [--conn-per-hour 90] [--out reports/calibration.md]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from controller.experiments import EPS_PER_ARM, MIN_DROP, MIN_PROMOTE, Z_DROP, Z_PROMOTE  # noqa: E402
from controller.stats import stratified_diff  # noqa: E402


def run(sims: int, cph: float, lifts: list[float], days: int = 7, p0: float = 0.55, seed: int = 7,
        z_promote: float = Z_PROMOTE):
    rng = np.random.default_rng(seed)
    k = len(lifts)
    shares = np.array([1 - EPS_PER_ARM * k] + [EPS_PER_ARM] * k)
    ps = np.array([p0] + [min(p0 * (1 + l), 0.95) for l in lifts])
    promoted_any = 0
    promoted_true = 0
    hours_to_promote = []
    dropped_null = 0
    null_arms = sum(1 for l in lifts if l == 0) * sims
    for _ in range(sims):
        succ = np.zeros((days, k + 1))
        tri = np.zeros((days, k + 1))
        dropped = set()
        done = False
        for h in range(days * 12):
            d = h // 12
            n = rng.poisson(cph * shares)
            for j in dropped:
                n[j + 1] = 0
            tri[d] += n
            succ[d] += rng.binomial(n, ps)
            for j in range(k):
                if j in dropped:
                    continue
                pairs = [((succ[x, j + 1], tri[x, j + 1]), (succ[x, 0], tri[x, 0])) for x in range(d + 1)]
                r = stratified_diff(pairs)
                if r.n_a >= MIN_DROP and r.z <= Z_DROP:
                    dropped.add(j)
                    if lifts[j] == 0:
                        dropped_null += 1
                    continue
                if r.n_a >= MIN_PROMOTE and r.z >= z_promote:
                    promoted_any += 1
                    if lifts[j] > 0:
                        promoted_true += 1
                        hours_to_promote.append(h + 1)
                    done = True
                    break
            if done:
                break
    return {"fwer_or_power": promoted_any / sims, "true_winner_rate": promoted_true / sims,
            "median_hours": float(np.median(hours_to_promote)) if hours_to_promote else None,
            "false_drop_rate": dropped_null / null_arms if null_arms else None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sims", type=int, default=2000)
    ap.add_argument("--conn-per-hour", type=float, default=90.0)
    ap.add_argument("--out", default="reports/calibration.md")
    a = ap.parse_args()
    rows = []
    for name, lifts in [("A/A, 3 challengers (false winners)", [0, 0, 0]),
                        ("A/A, 2 challengers", [0, 0]),
                        ("one arm +10%, two null", [0.10, 0, 0]),
                        ("one arm +18%, one null", [0.18, 0]),
                        ("one arm -10% (harm), two null", [-0.10, 0, 0])]:
        r = run(a.sims, a.conn_per_hour, lifts)
        rows.append((name, r))
        print(name, r, flush=True)
    zs = []
    for z in (2.5, 2.75, 3.0, 3.25):
        zs.append((z, run(max(a.sims // 2, 200), a.conn_per_hour, [0, 0, 0], z_promote=z)["fwer_or_power"]))
    L = [f"# Stopping-rule calibration ({a.sims} sims, {a.conn_per_hour:g} connected calls/hour, hourly looks, 7 days)",
         "", f"Rule: promote at z >= {Z_PROMOTE} with >= {MIN_PROMOTE} connected calls on the arm; drop at z <= {Z_DROP} "
         f"with >= {MIN_DROP}. Challenger share {EPS_PER_ARM:.0%} each.", "",
         "| world | P(any arm promoted) | P(true winner promoted) | median hours to promote | P(null arm wrongly dropped) |",
         "|---|---|---|---|---|"]
    for name, r in rows:
        fd = "—" if r["false_drop_rate"] is None else f"{r['false_drop_rate']:.3f}"
        L.append(f"| {name} | {r['fwer_or_power']:.3f} | {r['true_winner_rate']:.3f} | {r['median_hours'] or '—'} | {fd} |")
    L += ["", "False-winner rate (A/A, 3 challengers) by promote threshold:", "", "| z_promote | FWER |", "|---|---|"]
    L += [f"| {z} | {f:.3f} |" for z, f in zs]
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
