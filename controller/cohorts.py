"""The one shared cohort object.

Everything that talks about "a group of people" uses a **segment**: a single `feature=value`
predicate (`age_band=60+`). Detection reports by segment, experiments test arms per segment,
the supervisor view lists segments. The workflow config's `cohorts` list is *derived* from
segments here, deterministically, so a cohort id always means exactly the same predicate:

    au_age_band_60p                  -> {age_band: [60+]}
    au_age_band_60p__lang_ta         -> {age_band: [60+], lang: [ta]}

Two inputs decide the partition:
  * the **schedule feature** (one feature whose values get their own hour tables), and
  * up to MAX_OVERRIDES **override segments**, where an experiment promoted a different variant.
Cells are every (schedule value x subset of overrides), most specific first, because the
simulator assigns a lead to the *first* cohort it matches. Cohorts we did not create (a
supervisor's) are kept, untouched, in front of ours.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations

PREFIX = "au_"
MAX_COHORTS = 20
MAX_OVERRIDES = 2


def seg_parse(seg: str) -> tuple[str, str]:
    f, v = seg.split("=", 1)
    return f, v


def _slug(s: str) -> str:
    return s.replace("+", "p").replace("-", "_").replace(" ", "")


def cohort_id(where: dict) -> str:
    parts = [f"{k}_{_slug(v[0]) if len(v) == 1 else '_'.join(_slug(x) for x in v)}" for k, v in sorted(where.items())]
    return PREFIX + "__".join(parts)


@dataclass
class Cell:
    id: str
    where: dict
    sched_val: str | None
    segs: frozenset = field(default_factory=frozenset)


def build_cells(sched_feature: str | None, sched_values: list[str], overrides: list[str]) -> list[Cell]:
    overrides = list(dict.fromkeys(overrides))[:MAX_OVERRIDES]
    subsets = []
    for r in range(len(overrides), -1, -1):
        subsets += [frozenset(c) for c in combinations(overrides, r)]
    cells: list[Cell] = []
    seen = set()
    svals = sched_values if sched_feature else [None]
    for v in svals:
        for sub in subsets:
            where: dict[str, list] = {}
            if sched_feature:
                where[sched_feature] = [v]
            ok = True
            for seg in sorted(sub):
                f, val = seg_parse(seg)
                if f in where and where[f] != [val]:
                    ok = False
                    break
                where[f] = [val]
            if not ok or not where:
                continue
            key = tuple(sorted((k, tuple(x)) for k, x in where.items()))
            if key in seen:
                continue
            seen.add(key)
            # a cell "is in" an override segment iff its predicate implies it
            segs = frozenset(s for s in overrides if where.get(seg_parse(s)[0]) == [seg_parse(s)[1]])
            cells.append(Cell(cohort_id(where), where, v, segs))
    return cells


def foreign_cohorts(cfg: dict) -> list[dict]:
    return [c for c in cfg.get("cohorts", []) if not c["id"].startswith(PREFIX)]


def matches(where: dict, feats: dict) -> bool:
    return all(feats.get(k) in v for k, v in where.items())
