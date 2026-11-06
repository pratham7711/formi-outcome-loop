"""Config normalisation and diffing, mirroring the contract in docs/SIMULATOR.md section 3-4.

Kept independent of the simulator code on purpose: the controller only knows the documented
contract (weights are normalised, zero weights dropped, a cohort that omits a dimension falls back
to `default`, max_shift is total-variation distance per cohort and dimension).
"""
from __future__ import annotations

import copy

AGENT_DIMS = ("welcome_message", "voice", "caller_id")
CHANGE_DIMS = ("cohorts", "welcome_message", "voice", "caller_id", "prompt_version", "schedule")


def normalise(cfg: dict) -> dict:
    out = {"cohorts": [{"id": c["id"], "where": {k: list(v) for k, v in c["where"].items()}} for c in cfg.get("cohorts", [])],
           "allocations": {}, "prompt_version": cfg.get("prompt_version"),
           "schedule": {"hour_weights": {}, "lead_preferred_hour": dict(cfg.get("schedule", {}).get("lead_preferred_hour", {}))}}
    for key, dims in cfg.get("allocations", {}).items():
        out["allocations"][key] = {}
        for dim, w in dims.items():
            s = float(sum(w.values()))
            out["allocations"][key][dim] = {o: float(x) / s for o, x in w.items() if x > 0}
    for key, table in cfg.get("schedule", {}).get("hour_weights", {}).items():
        out["schedule"]["hour_weights"][key] = {str(int(h)): float(x) for h, x in table.items()}
    return out


def dist(cfg: dict, cohort: str, dim: str) -> dict:
    return (cfg["allocations"].get(cohort) or {}).get(dim) or cfg["allocations"]["default"][dim]


def _close(a: dict, b: dict) -> bool:
    return set(a) == set(b) and all(abs(a[k] - b[k]) < 1e-9 for k in a)


def changed_dims(old: dict, new: dict) -> list[str]:
    ch = set()
    if old["cohorts"] != new["cohorts"]:
        ch.add("cohorts")
    keys = set(old["allocations"]) | set(new["allocations"])
    for dim in AGENT_DIMS:
        if any(not _close(dist(old, k, dim), dist(new, k, dim)) for k in keys if k == "default" or
               k in {c["id"] for c in old["cohorts"]} | {c["id"] for c in new["cohorts"]}):
            ch.add(dim)
    if old["prompt_version"] != new["prompt_version"]:
        ch.add("prompt_version")
    if old["schedule"] != new["schedule"]:
        ch.add("schedule")
    return [d for d in CHANGE_DIMS if d in ch]


def max_shift(old: dict, new: dict) -> float:
    keys = sorted({"default"} | {c["id"] for c in old["cohorts"]} | {c["id"] for c in new["cohorts"]})
    worst = 0.0
    for k in keys:
        for dim in AGENT_DIMS:
            a, b = dist(old, k, dim), dist(new, k, dim)
            worst = max(worst, 0.5 * sum(abs(a.get(o, 0) - b.get(o, 0)) for o in sorted(set(a) | set(b))))
    return worst


def interpolate(old: dict, new: dict, alpha: float, dims) -> dict:
    """old + alpha*(new-old) on the agent dims listed (same cohort list assumed)."""
    out = copy.deepcopy(new)
    keys = ["default"] + [c["id"] for c in new["cohorts"]]
    for k in keys:
        for dim in dims:
            if dim not in AGENT_DIMS:
                continue
            a, b = dist(old, k, dim), dist(new, k, dim)
            mix = {o: (1 - alpha) * a.get(o, 0) + alpha * b.get(o, 0) for o in sorted(set(a) | set(b))}
            mix = {o: round(w, 6) for o, w in mix.items() if w > 1e-6}
            if k == "default" or dim in (new["allocations"].get(k) or {}) or dim in (old["allocations"].get(k) or {}):
                out["allocations"].setdefault(k, {})[dim] = mix
    return normalise(out)


def diff(old: dict, new: dict) -> dict:
    d = {}
    for dim in changed_dims(old, new):
        if dim == "cohorts":
            d["cohorts"] = {"before": [c["id"] for c in old["cohorts"]], "after": [c["id"] for c in new["cohorts"]]}
        elif dim in AGENT_DIMS:
            keys = ["default"] + [c["id"] for c in new["cohorts"] if dim in (new["allocations"].get(c["id"]) or {})
                                  or dim in (old["allocations"].get(c["id"]) or {})]
            d[dim] = {k: {"before": {o: round(w, 3) for o, w in dist(old, k, dim).items()},
                          "after": {o: round(w, 3) for o, w in dist(new, k, dim).items()}}
                      for k in keys if not _close(dist(old, k, dim), dist(new, k, dim))}
        elif dim == "prompt_version":
            d[dim] = {"before": old["prompt_version"], "after": new["prompt_version"]}
        else:
            hw_o, hw_n = old["schedule"]["hour_weights"], new["schedule"]["hour_weights"]
            d[dim] = {"hour_weights_changed_for": sorted(k for k in set(hw_o) | set(hw_n) if hw_o.get(k) != hw_n.get(k)),
                      "after": hw_n}
    return d
