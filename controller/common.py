"""Shared types for decision modules."""
from __future__ import annotations

from dataclasses import dataclass, field

from .model import Model
from .store import Store

MIN_PER_DAY = 1440
CUSTOMER_FACING = {"welcome_message", "voice", "prompt_version"}


@dataclass
class Ctx:
    now: int                 # sim minute
    ts: str
    store: Store
    model: Model
    state: dict              # module state root (persisted in learn_state)
    wfs: dict                # workflow_id -> public workflow description
    configs: dict            # workflow_id -> {"version", "config", "actor"}
    credits: float
    api: object = None
    settings: dict = field(default_factory=dict)

    @property
    def today(self) -> int:
        return self.now // MIN_PER_DAY

    @property
    def hour(self) -> int:
        return (self.now % MIN_PER_DAY) // 60

    def mstate(self, module: str, wf: str) -> dict:
        return self.state.setdefault("mod", {}).setdefault(module, {}).setdefault(wf, {})

    def decide(self, wf, module, kind, summary, detail=None, proposal_id=None) -> int:
        return self.store.decision(self.now, self.ts, wf, module, kind, summary, detail, proposal_id)

    def alert(self, wf, severity, code, message):
        return self.store.alert(self.now, self.ts, wf, severity, code, message)

    def engaged_per_day(self, wf: str, days: int = 3) -> float:
        a = self.model.agg(wf)
        lo = self.today - days
        tot = sum(v[2] for k, v in a.kpi.items() if lo <= int(k.split("|", 1)[0]) < self.today)
        span = min(days, max(1, self.today - min((int(k.split("|", 1)[0]) for k in a.kpi), default=self.today)))
        return tot / span if span else 0.0


@dataclass
class Part:
    """One module's requested change, as governance sees it."""
    module: str
    kind: str
    dims: set
    summary: str
    rationale: str
    urgent: bool = False
    material: bool = True
    auto_ok: bool = True           # module's own view; policy and our voluntary rules still apply
    harm_reduction: bool = False   # moves traffic *away* from something measured as harmful
    impact: float = 0.0            # expected extra concluded leads per day (signed)
    safety: bool = False           # fixes a customer-safety incident: ranks above any business impact
    evidence: dict = field(default_factory=dict)
    dry_run: dict = field(default_factory=dict)
    affected_preds: list | None = None   # cohort predicates whose leads are affected (None = all)
    decision_id: int | None = None

    def brief(self) -> dict:
        return {"module": self.module, "kind": self.kind, "dims": sorted(self.dims), "summary": self.summary,
                "urgent": self.urgent, "safety": self.safety, "harm_reduction": self.harm_reduction, "impact_per_day": round(self.impact, 2),
                "decision_id": self.decision_id}


def window_keys(d: dict, now: int, hours: int):
    """Yields (key, value) for day|hour|... keys within the last `hours` clock hours."""
    cur = now // 60
    for k, v in d.items():
        day, hour, _ = k.split("|", 2)
        h = int(day) * 24 + int(hour)
        if cur - hours <= h < cur:
            yield k, v


def tv(a: dict, b: dict) -> float:
    sa, sb = sum(a.values()) or 1, sum(b.values()) or 1
    return 0.5 * sum(abs(a.get(o, 0) / sa - b.get(o, 0) / sb) for o in sorted(set(a) | set(b)))


def norm(d: dict) -> dict:
    s = sum(d.values())
    return {k: round(v / s, 4) for k, v in d.items() if v > 0} if s > 0 else d
