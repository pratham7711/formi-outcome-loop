"""Governance client (C9): compose -> split -> submit -> track -> apply, crash-safely.

Every loop, per workflow:
 1. **Compose** one target config T from every module's target (cohort scheme, schedule, caller IDs,
    variant allocations, prompt). Dims a supervisor touched in the last FREEZE hours are copied from
    the live config, so we never silently overwrite their edits.
 2. **Split** T - live into an auto part and an approval part. A dim goes auto only if the policy
    allows it AND it is operational (caller_id, schedule) or it moves traffic *away* from something
    measured as harmful. Customer-facing changes (welcome, voice, prompt) always go to a human,
    even where the policy would allow auto-apply. That is a voluntary rule; see SUBMISSION.md section 6.
    Auto changes larger than max_shift are walked in max_shift steps, one per loop.
 3. **Submit** the approval part as ONE batched request when: the approver is at their desk
    and will decide before the TTL; the rolling-24h budget (policy, tightened by client overrides)
    keeps one slot in reserve for incidents; MIN_GAP_H since our last routine request; and the same
    content isn't in a post-rejection cooldown.
 4. **Track**: pending -> approved -> applied | rejected | expired | lapsed | stale | withheld.
    An expired or rejected request is never resubmitted; the planner re-derives T from fresh
    data and, if a change is still wanted, files a *new* proposal (Product memo: expired = rejected).
    A stale base (supervisor edit) is detected *before* calling apply, so we don't trip conflicts.

Crash safety: every HTTP write is preceded by a committed intent row keyed by a deterministic
proposal_id, and reconciled on restart (see `reconcile`). The simulator rejects a reused
proposal_id, so a replayed write can never double-apply.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math

from .cohorts import PREFIX, build_cells, foreign_cohorts
from .common import CUSTOMER_FACING, MIN_PER_DAY, Ctx, Part
from .configutil import changed_dims, diff, interpolate, max_shift, normalise
from .store import dumps

MODULE = "governance"
FREEZE_H = 48
MIN_GAP_H = 3
REJECT_COOLDOWN_H = 4
OPEN = ("submitting", "pending", "approved", "applying")


# ------------------------------------------------------------------------------- compose
def compose(ctx: Ctx, wf: str, sched_t, did_t, exp_t: dict, prompt_t) -> dict:
    C = ctx.configs[wf]["config"]
    T = copy.deepcopy(C)
    gst = ctx.mstate(MODULE, wf)
    frozen = {d for d, until in gst.get("frozen", {}).items() if ctx.now < until}
    overrides = []
    for dim, t in exp_t.items():
        overrides += list(t.get("overrides", {}))
    ours_now = [c for c in C["cohorts"] if c["id"].startswith(PREFIX)]
    if sched_t or overrides:
        feat = sched_t["feature"] if sched_t else None
        vals = sorted(sched_t["tables"]) if sched_t else []
        cells = build_cells(feat, vals, overrides)
        T["cohorts"] = foreign_cohorts(C) + [{"id": c.id, "where": c.where} for c in cells]
    else:
        cells = []
        T["cohorts"] = C["cohorts"]
    our_ids = {c.id for c in cells} if cells else {c["id"] for c in ours_now}
    # allocations: foreign + default stay; our cohorts rebuilt
    allocs = {k: v for k, v in C["allocations"].items() if k == "default" or not k.startswith(PREFIX)}
    allocs = copy.deepcopy(allocs)
    for dim, t in exp_t.items():
        if dim in frozen:
            continue
        allocs["default"][dim] = dict(t["default"])
    if did_t and "caller_id" not in frozen:
        allocs["default"]["caller_id"] = dict(did_t)
    if cells:
        for c in cells:
            entry = {}
            for dim, t in exp_t.items():
                for seg in sorted(c.segs):
                    if seg in t.get("overrides", {}):
                        entry[dim] = dict(t["overrides"][seg])
                        break
            if entry:
                allocs[c.id] = entry
    else:
        for k, v in C["allocations"].items():
            if k.startswith(PREFIX) and k in our_ids:
                allocs[k] = copy.deepcopy(v)
    T["allocations"] = allocs
    # schedule
    hw = {k: v for k, v in C["schedule"]["hour_weights"].items() if not k.startswith(PREFIX)}
    if "schedule" in frozen:
        hw = copy.deepcopy(C["schedule"]["hour_weights"])
    elif sched_t:
        for c in cells:
            if c.sched_val is not None and c.sched_val in sched_t["tables"]:
                hw[c.id] = dict(sched_t["tables"][c.sched_val])
    else:
        hw.update({k: v for k, v in C["schedule"]["hour_weights"].items() if k in our_ids})
    T["schedule"] = {"hour_weights": hw, "lead_preferred_hour": C["schedule"].get("lead_preferred_hour", {})}
    if prompt_t:  # a validated safety fix is exempt from the supervisor freeze (it still needs approval)
        T["prompt_version"] = prompt_t
    return normalise(T)


# ------------------------------------------------------------------------------- helpers
def _policy(ctx: Ctx, wf: str) -> dict:
    gov = ctx.wfs[wf]["governance"]
    ov = ctx.settings.get("overrides", {})
    o = {**ov.get("tenants", {}).get(ctx.wfs[wf]["tenant_id"], {}), **ov.get("workflows", {}).get(wf, {})}
    lo, hi = gov["approver_hours"]
    return {"auto": gov.get("auto_apply", {}), "lo": max(lo, o.get("earliest_hour", lo)), "hi": hi,
            "lat": gov.get("approver_latency_minutes", [20, 240]), "ttl_h": gov.get("proposal_ttl_hours", 24),
            "apply_h": gov.get("apply_window_hours", 2),
            "max24": min(gov.get("max_requests_per_24h", 4), o.get("max_requests_per_24h", 99)),
            "override": o}


def _next_working(t: int, lo: int, hi: int) -> int:
    day, mod = divmod(t, MIN_PER_DAY)
    if mod < lo * 60:
        return day * MIN_PER_DAY + lo * 60
    if mod >= hi * 60:
        return (day + 1) * MIN_PER_DAY + lo * 60
    return t


def window_ok(ctx: Ctx, pol: dict) -> tuple[bool, str]:
    if not pol["lo"] <= ctx.hour < pol["hi"]:
        return False, f"approver hours {pol['lo']}:00-{pol['hi']}:00"
    worst = _next_working(ctx.now + pol["lat"][1], pol["lo"], pol["hi"])
    if worst > ctx.now + pol["ttl_h"] * 60 - 15:
        return False, "approver could not decide before the TTL"
    return True, ""


def _requests_24h(ctx: Ctx, wf: str) -> int:
    return ctx.store.db.execute("SELECT COUNT(*) FROM proposals WHERE wf=? AND path='approval' AND created_minute>? "
                                "AND status NOT IN ('not_sent')", (wf, ctx.now - MIN_PER_DAY)).fetchone()[0]


def _content_hash(cfg: dict, dims) -> str:
    return hashlib.sha1((dumps(cfg) + "|".join(sorted(dims))).encode()).hexdigest()[:12]


def _impact_score(ctx: Ctx, wf: str, parts: list[Part]) -> float:
    if any(p.safety for p in parts):
        return 1.0
    batch = ctx.wfs[wf]["daily_batch"]["size"]
    x = sum(max(p.impact, 0.0) for p in parts) / max(0.05 * batch, 1.0)
    return round(max(1 - math.exp(-x), 0.05 if parts else 0.0), 3)


def _affected(ctx: Ctx, wf: str, parts: list[Part], dims) -> int:
    today = ctx.today
    active = ctx.model.active_leads(wf, today)
    locked = ctx.wfs[wf]["mid_flight"] == "next_campaign_only"
    alloc_only = set(dims) <= {"cohorts", "welcome_message", "voice", "caller_id"}
    base = ctx.wfs[wf]["daily_batch"]["size"] if (locked and alloc_only) else active
    preds = []
    for p in parts:
        if p.affected_preds is None:
            return int(base)
        preds += p.affected_preds
    return int(round(base * ctx.model.lead_share(wf, today, preds)))


def _new_pid(ctx: Ctx, wf: str, kind: str) -> str:
    n = ctx.store.db.execute("SELECT COUNT(*) FROM proposals WHERE wf=?", (wf,)).fetchone()[0]
    return f"{wf}:{kind}:{ctx.now}:{n + 1}"


def _record(ctx: Ctx, pid: str, wf: str, path: str, status: str, urgent: bool, base: int, dims, body: dict,
            parts: list[Part]):
    ctx.store.db.execute(
        "INSERT INTO proposals(proposal_id, wf, path, status, urgent, base_version, dims, change_summary, rationale, "
        "impact_score, affected_leads, diff, dry_run, evidence, config, parts, created_minute) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, wf, path, status, int(urgent), base, dumps(sorted(dims)), body.get("change_summary"), body.get("rationale"),
         body.get("impact_score"), body.get("affected_leads"), dumps(body.get("diff", {})), dumps(body.get("dry_run", {})),
         dumps(body.get("evidence", {})), dumps(body["proposed_config"]), dumps([p.brief() for p in parts]), ctx.now))
    for p in parts:
        if p.decision_id:
            ctx.store.db.execute("UPDATE decisions SET proposal_id=? WHERE id=? AND proposal_id IS NULL",
                                 (pid, p.decision_id))


def _set(ctx: Ctx, pid: str, **kw):
    cols = ", ".join(f"{k}=?" for k in kw)
    ctx.store.db.execute(f"UPDATE proposals SET {cols} WHERE proposal_id=?", (*[
        dumps(v) if isinstance(v, (dict, list)) else v for v in kw.values()], pid))


# ------------------------------------------------------------------------------- planning
def _outstanding(ctx: Ctx, wf: str, new_parts: list[Part], dims: list[str]) -> list[Part]:
    """Modules speak once, when they decide; the request may only be fileable hours later. Keep each
    part until its dims have landed (or been abandoned). A newer part from the same module on an
    overlapping dim replaces the older one (e.g. promote supersedes explore)."""
    gst = ctx.mstate(MODULE, wf)
    keep = [Part(**{**d, "dims": set(d["dims"])}) for d in gst.get("outstanding", [])]
    for p in new_parts:
        keep = [k for k in keep if not (k.module == p.module and k.dims & p.dims)]
        keep.append(p)
    live = [k for k in keep if k.dims & set(dims)]
    gst["outstanding"] = [{**k.__dict__, "dims": sorted(k.dims)} for k in live]
    return live


def plan(ctx: Ctx, wf: str, T: dict, parts: list[Part], persist) -> None:
    """persist(): commits learned state + DB rows; called before every HTTP write (intent first)."""
    C = ctx.configs[wf]["config"]
    ver = ctx.configs[wf]["version"]
    dims = changed_dims(C, T)
    gst = ctx.mstate(MODULE, wf)
    parts = _outstanding(ctx, wf, parts, dims)
    if not dims:
        return
    pol = _policy(ctx, wf)
    auto = pol["auto"]
    by_dim: dict[str, list[Part]] = {}
    for p in parts:
        for d in p.dims:
            by_dim.setdefault(d, []).append(p)
    open_rows = ctx.store.rows("SELECT * FROM proposals WHERE wf=? AND path='approval' AND status IN "
                               "('submitting','pending','approved','applying')", wf)
    pending_urgent = any(r["urgent"] for r in open_rows)

    # ---- auto part
    auto_dims = []
    for d in dims:
        if not auto.get("enabled") or d not in auto.get("allowed_dims", []) or d in ("prompt_version", "cohorts"):
            continue
        if d in CUSTOMER_FACING:
            ps = by_dim.get(d, [])
            if not ps or not all(p.harm_reduction for p in ps):
                continue  # voluntary: customer-facing changes need a human unless they only remove harm
        if d == "schedule" and T["cohorts"] != C["cohorts"]:
            continue  # tables reference cohorts that don't exist yet; they ride with the cohort approval
        auto_dims.append(d)
    if auto_dims:
        A = copy.deepcopy(C)
        for d in auto_dims:
            if d == "schedule":
                A["schedule"] = copy.deepcopy(T["schedule"])
            elif d == "caller_id":
                A["allocations"]["default"]["caller_id"] = dict(T["allocations"]["default"]["caller_id"])
            elif d in ("welcome_message", "voice"):
                if T["cohorts"] == C["cohorts"]:
                    for k, v in T["allocations"].items():
                        if d in v:
                            A["allocations"].setdefault(k, {})[d] = dict(v[d])
                        elif k in A["allocations"]:
                            A["allocations"][k].pop(d, None)
                else:
                    A["allocations"]["default"][d] = dict(T["allocations"]["default"][d])
        A = normalise(A)
        sh = max_shift(C, A)
        limit = float(auto.get("max_shift", 0))
        if sh > limit + 1e-9:
            A = interpolate(C, A, max(limit / sh - 1e-3, 0), [d for d in auto_dims if d != "schedule"])
        adims = changed_dims(C, A)
        urgent = any(p.urgent for d in adims for p in by_dim.get(d, []))
        if adims and (not open_rows or urgent):
            aparts = [p for p in parts if p.dims & set(adims)]
            pid = _new_pid(ctx, wf, "auto")
            body = {"proposal_id": pid, "base_version": ver, "proposed_config": A,
                    "rationale": " | ".join(p.rationale for p in aparts)[:4000],
                    "evidence": {"parts": [p.brief() for p in aparts]}}
            rec = {**body, "change_summary": "; ".join(p.summary for p in aparts)[:500], "diff": diff(C, A),
                   "dry_run": {p.kind: p.dry_run for p in aparts}, "impact_score": _impact_score(ctx, wf, aparts),
                   "affected_leads": _affected(ctx, wf, aparts, adims)}
            _record(ctx, pid, wf, "auto", "submitting", urgent, ver, adims, rec, aparts)
            persist()
            r = ctx.api.put(f"/workflows/{wf}/config", body)
            _auto_result(ctx, wf, pid, r, A)
            persist()
            if r.ok:
                C, ver = ctx.configs[wf]["config"], ctx.configs[wf]["version"]
                dims = changed_dims(C, T)
        elif adims:
            ctx.decide(wf, MODULE, "defer_auto", f"Holding auto change to {adims} while an approval request is "
                       f"pending (applying it would make that request stale)", {"dims": adims})

    # ---- approval part (auto-eligible dims keep walking on the auto path, never double-filed)
    rest = [d for d in changed_dims(C, T) if d not in auto_dims]
    if not rest:
        return
    rparts = [p for p in parts if p.dims & set(rest)]
    if not any(p.material for p in rparts):
        return
    urgent = any(p.urgent for p in rparts)
    if open_rows and (pending_urgent or not urgent):
        return
    ok, why = window_ok(ctx, pol)
    if not ok:
        _note_wait(ctx, wf, gst, f"waiting: {why}", rest, urgent)
        return
    used = _requests_24h(ctx, wf)
    reserve = 1 if pol["max24"] >= 2 else 0
    cap = pol["max24"] - (0 if urgent else reserve)
    if used >= cap:
        _note_wait(ctx, wf, gst, f"waiting: {used}/{pol['max24']} requests used in 24h"
                   + ("" if urgent else " (one kept in reserve for incidents)"), rest, urgent)
        return
    if not urgent and gst.get("last_routine_request") is not None and \
            ctx.now - gst["last_routine_request"] < MIN_GAP_H * 60:
        return
    h = _content_hash(T, rest)
    cool = gst.get("cooldown", {})
    if h in cool and ctx.now < cool[h]:
        return
    pid = _new_pid(ctx, wf, "apr")
    ttl = pol["ttl_h"]
    evidence = {}
    for p in rparts:
        evidence.update({k: v for k, v in p.evidence.items() if k == "validation_run_id"})
    evidence["parts"] = [p.brief() for p in rparts]
    summary = " · ".join(p.summary for p in sorted(rparts, key=lambda p: (not p.urgent, -p.impact)))
    body = {"proposal_id": pid, "workflow_id": wf, "base_version": ver, "proposed_config": T,
            "change_summary": summary[:600], "diff": diff(C, T), "affected_leads": _affected(ctx, wf, rparts, rest),
            "dry_run": {p.kind: p.dry_run for p in rparts if p.dry_run} or {"note": "no measurable effect predicted"},
            "impact_score": _impact_score(ctx, wf, rparts),
            "rationale": "\n".join(f"[{p.kind}] {p.rationale}" for p in rparts)[:6000],
            "evidence": evidence, "expires_in_hours": ttl}
    _record(ctx, pid, wf, "approval", "submitting", urgent, ver, rest, body, rparts)
    gst["last_hash"] = h
    if not urgent:
        gst["last_routine_request"] = ctx.now
    gst.pop("waiting", None)
    if urgent:
        gst.pop("urgent_waiting_since", None)
        ctx.store.resolve_alert(ctx.now, wf, "urgent_change_waiting")
    persist()
    r = ctx.api.post("/approvals", body)
    _submit_result(ctx, wf, pid, r)
    persist()


URGENT_WAIT_TICKET_H = 18


def _note_wait(ctx, wf, gst, why, dims, urgent):
    """Waiting for approver hours is normal (info). An urgent change still unsent after
    URGENT_WAIT_TICKET_H is not: someone should ring the client's approver."""
    key = f"{why}|{sorted(dims)}"
    if gst.get("waiting") != key:
        gst["waiting"] = key
        ctx.decide(wf, MODULE, "wait", f"Change to {sorted(dims)} ready but {why}", {"urgent": urgent})
    if urgent:
        since = gst.setdefault("urgent_waiting_since", ctx.now)
        if ctx.now - since >= URGENT_WAIT_TICKET_H * 60:
            ctx.alert(wf, "ticket", "urgent_change_waiting", f"{wf}: urgent change to {sorted(dims)} unsent for "
                      f"{(ctx.now - since) // 60}h ({why}). Call the client's approver.")


def _submit_result(ctx: Ctx, wf: str, pid: str, r):
    if r.ok:
        b = r.body
        _set(ctx, pid, status="pending", approval_id=b["approval_id"])
        ctx.decide(wf, MODULE, "submitted", f"Approval request {b['approval_id']} filed: "
                   f"{ctx.store.rows('SELECT change_summary FROM proposals WHERE proposal_id=?', pid)[0]['change_summary']}",
                   {"approval_id": b["approval_id"], "expires_at": b.get("expires_at")}, pid)
    elif r.code == "duplicate_proposal_id":
        _set(ctx, pid, status="pending")  # reconciled on the next poll
    elif r.code == "stale_base_version":
        _set(ctx, pid, status="not_sent", note="stale base at submit")
    else:
        _set(ctx, pid, status="not_sent", note=dumps(r.body))
        ctx.decide(wf, MODULE, "submit_failed", f"Approval request refused: HTTP {r.status} {r.body}", {}, pid)
        ctx.alert(wf, "ticket", "submit_failed", f"{wf}: approval request {pid} refused with {r.status}: {r.body}")


def _auto_result(ctx: Ctx, wf: str, pid: str, r, A: dict):
    if r.ok:
        b = r.body
        _set(ctx, pid, status="applied", applied_version=b["version"], applied_minute=ctx.now)
        ctx.configs[wf] = {"version": b["version"], "config": A, "actor": "controller_auto"}
        ctx.decide(wf, MODULE, "auto_applied", f"Auto-applied v{b['version']} ({', '.join(b['changed_dims'])}) "
                   f"within policy", {"version": b["version"], "dims": b["changed_dims"]}, pid)
    elif r.code == "duplicate_proposal_id":
        _set(ctx, pid, status="applying")  # crash replay; reconcile reads config history
    else:
        _set(ctx, pid, status="refused", note=dumps(r.body))
        ctx.decide(wf, MODULE, "auto_refused", f"Auto-apply refused: HTTP {r.status} {r.body}", {}, pid)
        if r.status == 403:
            ctx.alert(wf, "ticket", "auto_apply_refused", f"{wf}: policy refused auto-apply {pid}: {r.body}. "
                      f"Our policy model disagrees with the platform's; check governance.auto_apply.")


# ------------------------------------------------------------------------------- lifecycle
def track(ctx: Ctx, wf: str, known_harmful, persist):
    """Polls open approval requests; applies approved ones. known_harmful(cfg)->str|None vetoes a stale idea."""
    for row in ctx.store.rows("SELECT * FROM proposals WHERE wf=? AND path='approval' AND status IN "
                              "('pending','approved','applying') ORDER BY created_minute", wf):
        pid, aid = row["proposal_id"], row["approval_id"]
        if not aid:
            continue
        r = ctx.api.get(f"/approvals/{aid}")
        if not r.ok:
            continue
        a = r.body
        st = a["status"]
        if st == "pending":
            continue
        if st in ("rejected", "expired", "lapsed") and row["status"] != st:
            _set(ctx, pid, status=st, decided_minute=ctx.now, reasons=a.get("reasons") or [])
            gst = ctx.mstate(MODULE, wf)
            h = _content_hash(json.loads(row["config"]), json.loads(row["dims"]))
            n = gst.setdefault("rejections", {}).get(h, 0) + 1
            gst["rejections"][h] = n
            gst.setdefault("cooldown", {})[h] = ctx.now + (REJECT_COOLDOWN_H if n == 1 else 24) * 60
            reasons = a.get("reasons") or []
            ctx.decide(wf, MODULE, st, f"{aid} {st}" + (f": {'; '.join(reasons)}" if reasons else "")
                       + ". Will re-derive from fresh data, never resubmit as-is.", {"reasons": reasons}, pid)
            if any("fatigue" in x for x in reasons) or any("must" in x for x in reasons):
                ctx.alert(wf, "ticket", "approval_rejected_by_rule", f"{wf}: {aid} rejected by rule ({reasons}); "
                          f"this is a controller bug, not a judgement call.")
            continue
        if st == "applied":
            if row["status"] != "applied":
                _set(ctx, pid, status="applied", applied_minute=ctx.now, applied_version=a.get("applied_version"))
            continue
        if st != "approved":
            continue
        if row["status"] == "pending":
            _set(ctx, pid, status="approved", decided_minute=ctx.now)
            ctx.decide(wf, MODULE, "approved", f"{aid} approved (apply by {a.get('apply_by')})", {}, pid)
        cur_ver = ctx.configs[wf]["version"]
        if row["base_version"] != cur_ver:
            _set(ctx, pid, status="stale", note=f"base v{row['base_version']} but live is v{cur_ver}")
            ctx.decide(wf, MODULE, "stale", f"{aid} approved but config moved v{row['base_version']} -> v{cur_ver} "
                       f"(by {ctx.configs[wf].get('actor')}); not applying, will re-propose on the new base", {}, pid)
            continue
        others = ctx.store.rows("SELECT proposal_id FROM proposals WHERE wf=? AND path='approval' AND urgent=1 AND "
                                "status IN ('pending','submitting') AND proposal_id<>?", wf, pid)
        if others and not row["urgent"]:
            continue  # let the urgent one land first; this one may lapse, and we re-propose
        veto = known_harmful(json.loads(row["config"]))
        if veto:
            _set(ctx, pid, status="withheld", note=veto)
            ctx.decide(wf, MODULE, "withheld", f"{aid} approved but not applied: {veto}", {}, pid)
            continue
        _set(ctx, pid, status="applying")
        persist()
        r2 = ctx.api.post(f"/workflows/{wf}/apply", {"approval_id": aid})
        if r2.ok:
            v = r2.body["version"]
            _set(ctx, pid, status="applied", applied_version=v, applied_minute=ctx.now)
            ctx.configs[wf] = {"version": v, "config": json.loads(row["config"]), "actor": "controller_approved"}
            ctx.decide(wf, MODULE, "applied", f"Applied {aid} as v{v} ({', '.join(r2.body['changed_dims'])})",
                       {"version": v}, pid)
        elif r2.status == 410:
            _set(ctx, pid, status="lapsed")
            ctx.decide(wf, MODULE, "lapsed", f"{aid} lapsed before apply", {}, pid)
        elif r2.code == "stale_base_version":
            _set(ctx, pid, status="stale")
        else:
            _set(ctx, pid, status="approved", note=dumps(r2.body))
        persist()


def reconcile(ctx: Ctx, persist):
    """Restart path: resolve every intent whose HTTP outcome we did not record.

    A workflow that already sent an HTTP write at the current sim minute is marked as decided for that
    minute: re-running its plan would compute the *next* step (e.g. another max_shift walk) early, so a
    crash would change behaviour. With the marker, a crashed run makes exactly the decisions an
    uninterrupted one would."""
    decided = ctx.state.setdefault("decided", {})
    for row in ctx.store.rows("SELECT wf FROM proposals WHERE created_minute=? AND status<>'not_sent'", ctx.now):
        decided[row["wf"]] = ctx.now
    for row in ctx.store.rows("SELECT * FROM proposals WHERE status IN ('submitting','applying')"):
        wf, pid = row["wf"], row["proposal_id"]
        if row["path"] == "auto":
            hist = ctx.api.must(ctx.api.get(f"/workflows/{wf}/config/history"), "history")
            hit = [h for h in hist if h.get("proposal_id") == pid]
            if hit:
                _set(ctx, pid, status="applied", applied_version=hit[0]["version"], note="reconciled after restart")
            else:
                _set(ctx, pid, status="not_sent", note="lost before send; planner will redo it")
            ctx.decide(wf, MODULE, "reconcile", f"Auto intent {pid}: {'applied' if hit else 'never reached the sim'}",
                       {}, pid)
            continue
        found = [a for a in ctx.api.must(ctx.api.get("/approvals", workflow_id=wf), "approvals")
                 if a["proposal_id"] == pid]
        if found:
            a = found[0]
            st = {"applied": "applied"}.get(a["status"], "pending" if row["status"] == "submitting" else "approved")
            _set(ctx, pid, status=st, approval_id=a["approval_id"], note="reconciled after restart")
            ctx.decide(wf, MODULE, "reconcile", f"Intent {pid} found in sim as {a['approval_id']} ({a['status']})", {}, pid)
        elif row["status"] == "submitting":
            body = {"proposal_id": pid, "workflow_id": wf, "base_version": row["base_version"],
                    "proposed_config": json.loads(row["config"]), "change_summary": row["change_summary"],
                    "diff": json.loads(row["diff"]), "affected_leads": row["affected_leads"],
                    "dry_run": json.loads(row["dry_run"]), "impact_score": row["impact_score"],
                    "rationale": row["rationale"], "evidence": json.loads(row["evidence"])}
            r = ctx.api.post("/approvals", body)
            _submit_result(ctx, wf, pid, r)
            ctx.decide(wf, MODULE, "reconcile", f"Intent {pid} had not reached the sim; re-sent with the same id "
                       f"(HTTP {r.status})", {}, pid)
    persist()


def on_supervisor_edit(ctx: Ctx, wf: str, dims: list[str], version: int):
    gst = ctx.mstate(MODULE, wf)
    fr = gst.setdefault("frozen", {})
    for d in dims:
        if d in ("schedule", "caller_id", "prompt_version", "cohorts"):
            fr[d] = ctx.now + FREEZE_H * 60
    ctx.decide(wf, MODULE, "supervisor_edit", f"Supervisor changed {dims} (v{version}). We keep their values for "
               f"{FREEZE_H}h; pending requests on the old base will be re-proposed, not forced.",
               {"dims": dims, "version": version})
