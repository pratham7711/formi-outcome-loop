"""Hallucination incident response (C8): detect -> pick a fix -> validate -> propose -> verify.

Detection works on the flag rate of the *current* prompt since it went live, against the workflow's
own baseline flag rate from history. The detector is imperfect (false positives and negatives),
so the baseline already contains its false-positive floor; we only compare like with like.
Alarm: >= MIN_CONN connected calls, rate >= max(2 x base, base + 1pt), and one-sided z >= Z_ALARM (4.0).
Under the null that is ~3e-5 per check; with ~130 hourly checks per workflow-run the false-alarm
rate is ~0.4% per workflow-run.

Picking a fix, cheapest evidence first ("the best compute is no compute"):
  1. The last prompt this workflow ran in production with a normal flag rate and >= 1000 connected
     calls. That is real evidence of both safety and engagement, and costs no LLM credits.
  2. Only if none exists: ask the LLM for suggestions (3 credits) and try them in order.
Every candidate still needs a validation run (the approver requires it): n=300, 3 credits. A candidate
must clear min_validation_pass_rate + 0.5pt margin, so the approver's bar is not a coin flip.
At most MAX_VALIDATIONS per incident. Credits are only ever spent here.

After apply we verify on live calls: the flag rate must come back under the alarm bar on >= 200
connected calls, and engagement must not fall significantly below the pre-incident level (the
"strict script" failure mode: nobody hallucinates, nobody stays on the call).
"""
from __future__ import annotations

from .common import Ctx, Part
from .stats import binom_upper_z, stratified_diff

MODULE = "incidents"
MIN_CONN = 150
Z_ALARM = 4.0
KNOWN_GOOD_MIN_CONN = 1000
VALIDATION_N = 300
PASS_MARGIN = 0.005
MAX_VALIDATIONS = 3
VERIFY_CONN = 200
STUCK_MINUTES = 6 * 60


def _hours_since(key: str) -> int:
    day, hour, _ = key.split("|", 2)
    return int(day) * 24 + int(hour)


def prompt_stats(a, prompt: str, from_minute: int | None = None, to_minute: int | None = None):
    conn = flags = eng = 0
    lo = None if from_minute is None else from_minute // 60
    hi = None if to_minute is None else to_minute // 60
    for k, (c, f, e) in a.hal.items():
        if not k.endswith("|" + prompt):
            continue
        h = _hours_since(k)
        if (lo is not None and h < lo) or (hi is not None and h >= hi):
            continue
        conn += c
        flags += f
        eng += e
    return conn, flags, eng


def baseline(ctx: Ctx, wf: str) -> float:
    st = ctx.mstate(MODULE, wf)
    if "base" not in st:
        a = ctx.model.agg(wf)
        c = sum(v[0] for k, v in a.hal.items() if int(k.split("|", 1)[0]) < 0)
        f = sum(v[1] for k, v in a.hal.items() if int(k.split("|", 1)[0]) < 0)
        if c < 2000:  # thin history: pool every workflow's history
            for w in ctx.wfs:
                aa = ctx.model.agg(w)
                c += sum(v[0] for k, v in aa.hal.items() if int(k.split("|", 1)[0]) < 0)
                f += sum(v[1] for k, v in aa.hal.items() if int(k.split("|", 1)[0]) < 0)
        st["base"] = (f + 1) / (c + 50)
        st["base_n"] = c
    return st["base"]


def on_prompt_change(ctx: Ctx, wf: str, version: int, prompt: str, actor: str):
    st = ctx.mstate(MODULE, wf)
    hist = st.setdefault("prompt_epochs", [])
    if not hist or hist[-1]["prompt"] != prompt:
        hist.append({"prompt": prompt, "from": ctx.now, "version": version, "actor": actor})


def _alarm(ctx, wf, prompt, since) -> tuple[bool, dict]:
    a = ctx.model.agg(wf)
    b = baseline(ctx, wf)
    c, f, _ = prompt_stats(a, prompt, since)
    z = binom_upper_z(f, c, b)
    rate = f / c if c else 0.0
    fire = c >= MIN_CONN and rate >= max(2 * b, b + 0.01) and z >= Z_ALARM
    return fire, {"prompt": prompt, "conn": c, "flags": f, "rate": round(rate, 4), "baseline": round(b, 4), "z": round(z, 2)}


def evaluate(ctx: Ctx, wf: str) -> tuple[str | None, list[Part]]:
    """Returns (target prompt_version or None, parts)."""
    st = ctx.mstate(MODULE, wf)
    cfg = ctx.configs[wf]["config"]
    cur = cfg["prompt_version"]
    epochs = st.setdefault("prompt_epochs", [])
    if not epochs:
        epochs.append({"prompt": cur, "from": None, "version": ctx.configs[wf]["version"], "actor": "bootstrap"})
    since = epochs[-1]["from"]
    inc = st.get("incident")

    if inc and inc["state"] in ("verifying",):
        return _verify(ctx, wf, st, inc, cur)
    if inc and inc["state"] == "failed" and cur == inc["bad_prompt"]:
        return None, []  # paged already; a human owns it now
    if inc and inc["state"] in ("fixing", "proposed"):
        if cur == inc.get("candidate"):
            inc.update({"state": "verifying", "applied_at": ctx.now})
            ctx.decide(wf, MODULE, "fix_live", f"Prompt {cur} is live; verifying on live calls", {"incident": inc["id"]})
            return None, []
        if cur != inc["bad_prompt"]:
            fire, ev = _alarm(ctx, wf, cur, since)
            if not fire:
                inc["state"] = "superseded"
                ctx.decide(wf, MODULE, "superseded", f"Prompt changed to {cur} by someone else during the incident; "
                           f"standing down our fix", ev)
                ctx.store.resolve_alert(ctx.now, wf, "hallucination_spike")
                return None, []
        if ctx.now - inc["opened"] > STUCK_MINUTES:
            ctx.alert(wf, "page", "hallucination_fix_stuck", f"{wf}: hallucination incident open "
                      f"{(ctx.now - inc['opened']) // 60}h without a live fix (state={inc['state']}). Check "
                      f"`python -m controller.report --db ... why {wf}` and the approval queue.")
        if inc.get("candidate"):
            return inc["candidate"], [_part(ctx, wf, inc)]
        return _pick_fix(ctx, wf, st, inc)

    fire, ev = _alarm(ctx, wf, cur, since)
    if not fire:
        return None, []
    n = st.get("n_incidents", 0) + 1
    st["n_incidents"] = n
    epoch = epochs[-1]
    inc = {"id": f"{wf}:hal{n}", "state": "fixing", "opened": ctx.now, "bad_prompt": cur, "detect": ev,
           "epoch": epoch, "tried": [], "validations": 0, "credits": 0}
    st["incident"] = inc
    who = f" (set by {epoch['actor']} at v{epoch['version']})" if epoch.get("from") is not None else ""
    ctx.decide(wf, MODULE, "detect", f"Hallucination flags on {cur}{who}: {ev['rate']:.1%} of {ev['conn']} connected "
               f"calls vs baseline {ev['baseline']:.1%} (z={ev['z']})", ev)
    ctx.alert(wf, "ticket", "hallucination_spike", f"{wf}: prompt {cur} flags {ev['rate']:.1%} vs {ev['baseline']:.1%} "
              f"baseline on {ev['conn']} calls. Controller is preparing a validated fix for approval.")
    return _pick_fix(ctx, wf, st, inc)


def _known_good(ctx, wf, st, bad: str, tried: list) -> list[str]:
    a = ctx.model.agg(wf)
    b = baseline(ctx, wf)
    opts = ctx.wfs[wf]["options"]["prompt_version"]
    order = [e["prompt"] for e in reversed(st.get("prompt_epochs", []))]
    out = []
    for p in dict.fromkeys(order + opts):
        if p == bad or p in tried or p not in opts:
            continue
        c, f, _ = prompt_stats(a, p)
        if c >= KNOWN_GOOD_MIN_CONN and binom_upper_z(f, c, b) < 2.0 and f / c < max(2 * b, b + 0.01):
            out.append(p)
    return out


def _pick_fix(ctx, wf, st, inc):
    gov = ctx.wfs[wf]["governance"]
    bar = float(gov.get("min_validation_pass_rate", 0.97)) + PASS_MARGIN
    while inc["validations"] < MAX_VALIDATIONS:
        cands = [(p, "known_good") for p in _known_good(ctx, wf, st, inc["bad_prompt"], inc["tried"])]
        if not cands:
            if "llm" not in inc:
                if ctx.credits < 3 + 3:
                    ctx.alert(wf, "page", "credits_exhausted", f"{wf}: hallucination incident but only "
                              f"{ctx.credits:.0f} credits left; cannot get or validate a fix. Top up credits.")
                    return None, []
                res = ctx.api.post("/llm/suggest-fixes", {"workflow_id": wf, "problem": (
                    f"Prompt {inc['bad_prompt']} hallucination flag rate {inc['detect']['rate']:.1%} vs "
                    f"{inc['detect']['baseline']:.1%} baseline on {inc['detect']['conn']} calls. Need a grounded "
                    f"prompt that keeps engagement.")})
                if res.status == 402:
                    ctx.alert(wf, "page", "credits_exhausted", f"{wf}: out of credits mid-incident")
                    return None, []
                body = ctx.api.must(res, "suggest-fixes")
                ctx.credits = body.get("credits_remaining", ctx.credits - 3)
                ctx.store.db.execute("INSERT INTO credit_spend(sim_minute, wf, kind, credits, ref) VALUES(?,?,?,?,?)",
                                     (ctx.now, wf, "llm_suggest_fixes", 3, inc["id"]))
                inc["credits"] += 3
                inc["llm"] = [s["prompt_version"] for s in body["suggestions"]]
                ctx.decide(wf, MODULE, "llm_suggest", f"No known-good prompt in production history; LLM suggested "
                           f"{inc['llm']}", {"suggestions": body["suggestions"]})
            cands = [(p, "llm") for p in inc["llm"] if p not in inc["tried"] and p != inc["bad_prompt"]]
        if not cands:
            ctx.alert(wf, "page", "no_prompt_fix", f"{wf}: no safe prompt candidate left for incident {inc['id']}; "
                      f"needs a human prompt fix.")
            inc["state"] = "failed"
            return None, []
        p, source = cands[0]
        if ctx.credits < 3:
            ctx.alert(wf, "page", "credits_exhausted", f"{wf}: cannot validate fix {p}: {ctx.credits} credits")
            return None, []
        res = ctx.api.post("/validation/runs", {"workflow_id": wf, "prompt_version": p, "n": VALIDATION_N})
        if res.status == 402:
            ctx.alert(wf, "page", "credits_exhausted", f"{wf}: out of credits validating {p}")
            return None, []
        run = ctx.api.must(res, "validation")
        ctx.credits = run.get("credits_remaining", ctx.credits - 3)
        cost = -(-VALIDATION_N // 100)
        ctx.store.db.execute("INSERT INTO credit_spend(sim_minute, wf, kind, credits, ref) VALUES(?,?,?,?,?)",
                             (ctx.now, wf, "validation_run", cost, run["validation_run_id"]))
        inc["credits"] += cost
        inc["validations"] += 1
        inc["tried"].append(p)
        ok = run["pass_rate"] >= bar
        ctx.decide(wf, MODULE, "validate", f"Validated {p} ({source}): pass rate {run['pass_rate']:.3f} on n={run['n']} "
                   f"-> {'accept' if ok else 'reject'} (bar {bar:.3f})", {"run": run, "source": source})
        if ok:
            a = ctx.model.agg(wf)
            c, f, e = prompt_stats(a, p)
            inc.update({"candidate": p, "source": source, "validation": run, "state": "fixing",
                        "prod_record": {"conn": c, "flag_rate": round(f / c, 4) if c else None,
                                        "engagement": round(e / c, 4) if c else None}})
            return p, [_part(ctx, wf, inc)]
    ctx.alert(wf, "page", "no_prompt_fix", f"{wf}: {MAX_VALIDATIONS} validations failed for incident {inc['id']}")
    inc["state"] = "failed"
    return None, []


def _part(ctx, wf, inc) -> Part:
    d, run, rec = inc["detect"], inc["validation"], inc.get("prod_record") or {}
    ep = inc["epoch"]
    who = ep.get("actor", "?")
    src = (f"{inc['candidate']} ran in production on {rec.get('conn')} connected calls with "
           f"{rec.get('flag_rate', 0):.1%} flags and {rec.get('engagement', 0):.1%} engagement"
           if inc.get("source") == "known_good" else f"{inc['candidate']} suggested by the LLM (no production record)")
    return Part(
        module=MODULE, kind="prompt_fix", dims={"prompt_version"},
        summary=f"Hallucination fix: switch prompt {inc['bad_prompt']} -> {inc['candidate']} "
                f"(flags {d['rate']:.1%} vs {d['baseline']:.1%} normal)",
        rationale=(f"Prompt {inc['bad_prompt']} went live via {who} (config v{ep.get('version')}). Since then "
                   f"{d['flags']} of {d['conn']} connected calls were flagged ({d['rate']:.1%}; baseline "
                   f"{d['baseline']:.1%}; z={d['z']}). Fix: {src}. Validation {run['validation_run_id']}: pass rate "
                   f"{run['pass_rate']:.3f} on {run['n']} synthetic conversations. After apply we verify the flag rate "
                   f"and engagement on live calls."),
        urgent=True, material=True, auto_ok=False, harm_reduction=True,
        safety=True,
        evidence={"validation_run_id": run["validation_run_id"], "pass_rate": run["pass_rate"],
                  "incident": inc["id"], "detection": d, "production_record": rec},
        dry_run={"method": "the fix prompt's own production record + validation run",
                 "flag_rate_now": d["rate"], "flag_rate_expected": rec.get("flag_rate"),
                 "engagement_expected": rec.get("engagement"), "validation_pass_rate": run["pass_rate"]})


def _verify(ctx, wf, st, inc, cur):
    a = ctx.model.agg(wf)
    b = baseline(ctx, wf)
    c, f, e = prompt_stats(a, cur, inc["applied_at"])
    if cur != inc["candidate"]:
        inc["state"] = "superseded"
        return None, []
    if c < VERIFY_CONN:
        return None, []
    rate = f / c
    z = binom_upper_z(f, c, b)
    ok_safety = not (rate >= max(2 * b, b + 0.01) and z >= 3.0)
    pre_c, _, pre_e = prompt_stats(a, inc["candidate"], None, inc["opened"])
    eng_note = ""
    if pre_c >= 200:
        dd = stratified_diff([((e, c), (pre_e, pre_c))])
        eng_note = f"; engagement {e / c:.1%} vs {pre_e / pre_c:.1%} before (z={dd.z:.1f})"
        if dd.z <= -3:
            ctx.alert(wf, "ticket", "fix_hurts_engagement", f"{wf}: fix prompt {cur} is safe but engagement fell "
                      f"to {e / c:.1%} from {pre_e / pre_c:.1%}{eng_note}. Review the prompt.")
    if ok_safety:
        inc["state"] = "resolved"
        inc["resolved_at"] = ctx.now
        ctx.decide(wf, MODULE, "resolved", f"Incident {inc['id']} resolved: {cur} flags {rate:.1%} on {c} calls "
                   f"(baseline {b:.1%}){eng_note}; time from detection to fix live "
                   f"{(inc['applied_at'] - inc['opened']) // 60}h{(inc['applied_at'] - inc['opened']) % 60:02d}m",
                   {"conn": c, "flags": f, "rate": rate, "z": z})
        ctx.store.resolve_alert(ctx.now, wf, "hallucination_spike")
        ctx.store.resolve_alert(ctx.now, wf, "hallucination_fix_stuck")
        return None, []
    inc["state"] = "fixing"
    inc["bad_prompt"] = cur
    inc["candidate"] = None
    ctx.decide(wf, MODULE, "verify_failed", f"Fix {cur} still flags {rate:.1%} on {c} calls; trying the next candidate",
               {"conn": c, "rate": rate, "z": z})
    return _pick_fix(ctx, wf, st, inc)
