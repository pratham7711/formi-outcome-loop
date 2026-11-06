"""Unit tests for the parts where a silent bug would cost a client: statistics, cohort semantics,
config diffing against the governance contract, approval timing, ingest idempotency, and the
false-winner rate of the stopping rule.  Run: python -m pytest -q tests
"""
import json
import math

import pytest

from controller import cohorts, configutil, governance, stats
from controller.model import Model
from scripts.calibrate import run as calibrate


# ---------------------------------------------------------------- stats
def test_z_for_p_matches_known_quantiles():
    assert abs(stats.z_for_p(0.975) - 1.959964) < 1e-5
    assert abs(stats.z_for_p(0.999) - 3.090232) < 1e-5
    assert abs(stats.z_for_p(0.025) + 1.959964) < 1e-5


def test_stratified_diff_ignores_between_stratum_confounding():
    # Day 1: both arms 50%. Day 2: both arms 20%. Arm A got most traffic on day 2, so a pooled
    # comparison would say A is worse; within strata they are identical.
    pairs = [((50, 100), (500, 1000)), ((200, 1000), (20, 100))]
    pooled = (250 / 1100) - (520 / 1100)
    assert pooled < -0.2
    d = stats.stratified_diff(pairs)
    assert abs(d.diff) < 1e-9 and abs(d.z) < 1e-6


def test_stratified_diff_detects_real_lift():
    d = stats.stratified_diff([((660, 1000), (550, 1000))])
    assert d.z > 4 and abs(d.lift - 0.2) < 1e-9


def test_stratified_diff_skips_thin_strata():
    d = stats.stratified_diff([((1, 2), (0, 2))], min_n=5)
    assert d.strata == 0 and d.z == 0


# ---------------------------------------------------------------- cohorts
def test_cells_most_specific_first_and_partition():
    cells = cohorts.build_cells("age_band", ["18-30", "60+"], ["lang=ta"])
    ids = [c.id for c in cells]
    assert ids.index("au_age_band_60p__lang_ta") < ids.index("au_age_band_60p")
    first = lambda f: next(c for c in cells if cohorts.matches(c.where, f))
    assert first({"age_band": "60+", "lang": "ta"}).segs == frozenset({"lang=ta"})
    assert first({"age_band": "60+", "lang": "hi"}).id == "au_age_band_60p"


def test_cells_drop_contradictions_and_cap():
    cells = cohorts.build_cells("lang", ["hi", "ta"], ["lang=ta"])
    assert all(not (c.where["lang"] == ["hi"] and "lang=ta" in c.segs) for c in cells)
    assert len(cohorts.build_cells("lang", list("abcdefg"), ["age_band=60+", "city_tier=1"])) <= 28


def test_cohort_ids_are_stable():
    assert cohorts.cohort_id({"lang": ["ta"], "age_band": ["60+"]}) == "au_age_band_60p__lang_ta"


# ---------------------------------------------------------------- config contract
BASE = {"cohorts": [], "allocations": {"default": {"welcome_message": {"W1": 1}, "voice": {"V1": 1},
                                                   "caller_id": {"D1": 0.5, "D2": 0.5}}},
        "prompt_version": "P1", "schedule": {"hour_weights": {}, "lead_preferred_hour": {}}}


def test_changed_dims_and_shift():
    old = configutil.normalise(BASE)
    new = json.loads(json.dumps(old))
    new["allocations"]["default"]["caller_id"] = {"D1": 0.3, "D2": 0.3, "D3": 0.4}
    new = configutil.normalise(new)
    assert configutil.changed_dims(old, new) == ["caller_id"]
    assert abs(configutil.max_shift(old, new) - 0.4) < 1e-9


def test_interpolate_respects_max_shift():
    old = configutil.normalise(BASE)
    new = json.loads(json.dumps(old))
    new["allocations"]["default"]["caller_id"] = {"D3": 1.0}
    new = configutil.normalise(new)
    step = configutil.interpolate(old, new, 0.2 / configutil.max_shift(old, new), ["caller_id"])
    assert configutil.max_shift(old, step) <= 0.2 + 1e-6
    assert list(step["allocations"]["default"]["caller_id"]) == sorted(step["allocations"]["default"]["caller_id"])


def test_cohort_without_dim_falls_back_to_default():
    cfg = configutil.normalise({**BASE, "cohorts": [{"id": "x", "where": {"lang": ["ta"]}}],
                                "allocations": {**BASE["allocations"], "x": {"voice": {"V2": 1}}}})
    assert configutil.dist(cfg, "x", "welcome_message") == {"W1": 1.0}
    assert configutil.dist(cfg, "x", "voice") == {"V2": 1.0}


# ---------------------------------------------------------------- approval timing
class _Ctx:
    def __init__(self, now):
        self.now = now

    @property
    def hour(self):
        return (self.now % 1440) // 60


@pytest.mark.parametrize("hhmm,ok", [("09:00", False), ("10:00", True), ("17:30", False), ("18:30", False)])
def test_window_requires_decision_before_ttl(hhmm, ok):
    h, m = map(int, hhmm.split(":"))
    pol = {"lo": 10, "hi": 19, "lat": [20, 120], "ttl_h": 12}
    assert governance.window_ok(_Ctx(2 * 1440 + h * 60 + m), pol)[0] is ok


@pytest.mark.parametrize("hhmm,urgent,ok", [("17:00", False, False), ("17:00", True, True),
                                             ("18:30", True, False), ("19:30", True, False)])
def test_urgent_fix_accepts_small_expiry_risk(hhmm, urgent, ok):
    # Kestrel-like policy: 11:00-19:00, latency 20-120 min, TTL 12h. At 17:00 only a 120-minute
    # latency misses 19:00 (P(in time)=0.99); routine still waits for certainty, an incident fix goes.
    h, m = map(int, hhmm.split(":"))
    pol = {"lo": 11, "hi": 19, "lat": [20, 120], "ttl_h": 12}
    assert governance.window_ok(_Ctx(2 * 1440 + h * 60 + m), pol, urgent)[0] is ok


# ---------------------------------------------------------------- ingest idempotency
def _ev(i, **kw):
    base = {"type": "call", "ts": "2027-01-05T10:05:00+05:30", "workflow_id": "w", "campaign_id": "w:c1",
            "lead_id": "w:l1", "person_id": "w:p1", "config_version": 1, "cohort_id": "default",
            "variant": {"welcome_message": "W1", "voice": "V1", "caller_id": "D1", "prompt_version": "P1"},
            "connected": True, "engaged": True, "qa": {"hallucination_flag": False}, "event_id": i}
    base.update(kw)
    return json.dumps(base).encode()


def test_reingesting_a_chunk_counts_nothing_twice():
    m = Model("2027-01-04", {"w": 3})
    lead = json.dumps({"type": "lead", "ts": "2027-01-05T08:00:00+05:30", "workflow_id": "w", "campaign_id": "w:c1",
                       "lead_id": "w:l1", "person_id": "w:p1", "event_id": 1,
                       "features": {"lang": "hi", "city_tier": "1", "age_band": "60+", "value_band": "mid"}}).encode()
    chunk = [lead, _ev(2), _ev(3, engaged=False)]
    m.ingest_lines(chunk)
    snap = json.dumps(m.to_dict(), sort_keys=True)
    m.ingest_lines(chunk)
    assert json.dumps(m.to_dict(), sort_keys=True) == snap
    assert m.agg("w").kpi["1|10"] == [2, 2, 1]
    assert m.agg("w").arm["1|1|default|voice|V1|age_band=60+"] == [2, 2, 1]


# ---------------------------------------------------------------- the stopping rule
def test_false_winner_rate_under_five_percent():
    r = calibrate(sims=300, cph=90, lifts=[0, 0, 0], days=7, seed=3)
    assert r["fwer_or_power"] <= 0.05


def test_large_effect_is_found_within_three_days():
    r = calibrate(sims=100, cph=90, lifts=[0.18, 0], days=3, seed=5)
    assert r["true_winner_rate"] >= 0.8  # measured 0.87 at seed 5; 0.998 over 7 days
