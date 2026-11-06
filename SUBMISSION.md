# Outcome-First Agent Loop — Submission

**Name:** Pratham Sharma · **Date:** 2026-11-06 · **Hours actually spent (be honest):** ~1.5 hours elapsed on 2026-11-06 (19:15–20:35 IST). An AI agent (Claude Code) wrote and ran most of it while I directed and reviewed (built with
Claude Code as a pair; every decision below is mine to defend) · **Language / storage:** Python 3 (httpx, numpy
for the calibration script only) / one SQLite file in WAL mode

Run it: `./run.sh <SIM_URL>`. Reproduce a graded scenario end to end: `scripts/run_scenario.sh public_02_incidents`.
Operate it: [`docs/RUNBOOK.md`](docs/RUNBOOK.md).

---

## 0. TL;DR
- A controller that ingests the event stream into **counters, not rows**, and runs four modules per workflow:
  caller-ID health, call-time by cohort, welcome/voice experiments, hallucination incidents. It composes their
  wishes into **one target config**, then splits it into what policy lets us auto-apply and what goes to a
  human, in **one batched request**.
- **public_01:** conclusion rate **+12.9%** (pickup +8.6%, engagement +3.1%). 20 changes, **0 harmful**.
  **public_02:** conclusion rate **+26.8%** (pickup +22.9%, engagement +0.8%). 24 changes, **1 harmful** (a
  caller-ID rebalance, §11). The bad prompt was detected 5.5 h after it shipped and the fix was live 1 h later.
  Hallucinated calls fell from 431 to 199.
- **Crash test:** the controller was SIGKILLed 7 times mid-run. The governance actions were **identical** to an
  uninterrupted run (40 = 40).
- **Load:** public_03 ingests at **159,735 events/s** and needs **13.5–18.4 s of wall clock per sim day**, against
  an SRE budget of 600 s.
- **Cut:** per-person call time, LLM-generated variants, fair sharing across tenants (C15), and any UI beyond a
  CLI supervisor view.

## 1. Assumptions
1. **The API is the only truth.** The controller never reads `runs/`, never imports `sim` and never parses a
   scenario. Policy comes from `GET /workflows/{id}`. I read `sim/` as a developer to understand the contract
   (sampling order, latency semantics), but no constant from it is hard-coded as a fact about the world.
2. **Approver latency is uniform** over `approver_latency_minutes`. SIMULATOR.md says "a random latency", and
   the urgent-submit rule in §6 depends on this. If it is heavy-tailed, the 0.75 bar is optimistic.
3. **Welcome and voice are heard only after pickup.** They cannot move pickup, so on pickup-primary workflows
   (KYC) testing them is guardrail risk with no upside. I don't run those experiments there.
4. **A caller ID's health is relative.** Its numbers are compared with its sibling numbers on the same
   workflow, in the same hour, over the last 12 h. Absolute pickup moves with the hour and the population; the
   ratio does not.
5. **Hallucination flags are a noisy but unbiased QA signal.** A single flagged call means nothing; a rate
   does.
6. **Validation pass rate measures safety only** (SIMULATOR.md: "says nothing about engagement"). A
   validated prompt can still lose engagement, so every prompt fix is re-checked on live engagement after it
   is applied.
7. **An approver who sees ≥ 2 requests a day stops reading them.** Kestrel's supervisor says so explicitly
   (memo 04). I apply the same caution everywhere: requests are batched, at least 3 h apart, with one slot kept
   in reserve for incidents.
8. **A supervisor edit is a decision, not noise.** For 48 h we adopt it as the control and propose nothing on
   that dimension.
9. **The expected effect of a variant can decay.** So a promotion always keeps a 20% holdout of the old
   control, which is what makes decay measurable at all.
10. **Days are the natural strata.** Population mix, config version and cohort shift between days. Every
    comparison is stratified by day × config_version × cohort.
11. **The business baseline is reasonable.** The burden of proof is on a change. Exploration is capped at 15%
    per arm and 45% in total, and stops after 5 days with no winner.
12. **Hidden scenarios share the schema but not the truth.** Nothing is tuned to the public truth tables.
    Thresholds come from the Monte Carlo in `reports/calibration.md`, not from the graded runs.

## 2. Scope decisions
| # | Capability | Built / Partial / Cut | Why | What it would take to finish |
|---|---|---|---|---|
| C1 | Ingestion at load scale | **Built** | Without it nothing else is fresh. Chunks are idempotent (event_id dedup), the watermark is committed atomically with the learned state, and history is bootstrapped too. | Streaming partitions per tenant (§8). |
| C2 | One shared cohort object | **Built** | Product memo; also the only way the audit and "heard" answers stay consistent. `au_*` cohort ids are deterministic slugs of their `where`, and the same object is used by schedule, experiments, detection and the reports. | — |
| C3 | Bootstrapping from history | **Built** | All pre-D0 events are ingested before the first decision, so day 0 starts with baselines (known-good prompts, caller-ID ratios, hour profiles). | — |
| C4 | Call time | **Partial** | Per cohort only. The feature is chosen by a G-test of segment × hour interaction; profiles are shrunk with K = 150. **Per-person cut**: 3–6 dials per person cannot estimate a 12-hour profile, and Product said "P0" without data. | Per-person needs a hierarchical model with the cohort profile as prior, plus an online test of person vs cohort. About 2 days. |
| C5 | Caller-ID health | **Built** | Cheapest, fastest, largest win. Pickup is the multiplier on everything. Quarantine, rest (overloaded 2 d / flagged 7 d), probation, hysteresis. | Carrier-ticket integration. |
| C6 | Welcome/voice experiments | **Built** | Core of the brief. Stratified z-tests; asymmetric stopping rule calibrated to FWER 0.021. | Factorial interaction test (§5). |
| C7 | Promotion, decay, rollback | **Built** | A 20% holdout after promotion; a 24 h decay window. | Bandit-style holdout shrinking. |
| C8 | Hallucination incidents | **Built** | Regulated clients. Detect → last-known-good or LLM → validate → approval with evidence → verify flags *and* engagement live. | — |
| C9 | Governance client | **Built** | Every outcome passes through it, so it had to be right. Full lifecycle including stale, expired, lapsed, withheld; batching; timing. | — |
| C10 | Impact, affected leads, dry run | **Partial** | Affected-lead counts and dry runs are computed from our own counters. The dry run for schedule changes is a re-weighting estimate, not a counterfactual (§6). | Off-policy estimators per change type. |
| C11 | Supervisor view | **Partial** | `python -m controller.report supervisor` gives one text screen per workflow: needs-you, waiting for approval, tests, what changed today. No web UI. | A read-only HTTP endpoint over the same function. Half a day. |
| C12 | Audit store | **Built** | Non-negotiable. `decisions`, `proposals`, `config_versions`, `config_snapshots`, `alerts`; the `why` and `heard` commands. | — |
| C13 | Crash recovery | **Built** | SRE non-negotiable. `scripts/crash_test.py`. | — |
| C14 | Loop observability | **Partial** | A `metrics` row per loop (lag, events/s, RSS, credits, pending approvals); `report health`; actionable alert severities. No Prometheus export. | An exporter on the metrics table. |
| C15 | Fairness across tenants | **Cut** | Credits are only spent on incidents (3 per incident in public_02), and approver attention is per tenant by construction. No contention exists to allocate at this scale. | A per-tenant credit budget with borrowing; a global request scheduler. |

**Principle:** build what moves the primary KPI *safely* or what a regulated client would fail us without
(C1, C2, C5, C6–C9, C12, C13). Then build the minimum version of anything someone must be able to read
(C10, C11, C14). Cut what has no data behind it (per-person call time) or no contention to manage (C15). Where
something is partial, the table says what is missing rather than calling it done.

## 3. Stakeholders

### 01 — Nikhil Rao, VP Sales
**Accepted:** one lift number per workflow, per KPI. Every promotion records its measured lift vs control, with
z and n, in the audit store. The graded reports give the per-workflow pickup and engagement deltas vs the
baseline. A summit dashboard was not built.
**Declined:** a blended score (Product forbids it, and it would hide guardrail harm); "winning by lunch, live
by evening" (calibration: at z = 2.5 one test in 13 crowns a fake winner); rolling out the persuasive prompt
everywhere.
**Wrong claims, checked against policy:**
- "Kestrel turned on auto-apply for everything." False. `ks_cancel` auto-apply covers schedule, caller_id,
  welcome and voice only, with **max_shift 0.2**. Cohorts and prompt changes need approval.
- "Northwind is fine with auto-apply on KYC." False. `nw_kyc` has **auto-apply disabled** for every
  dimension, and Northwind's own compliance head requires named approval (memo 02).
- "Persuasive tested well." The only test was in another account. Validation measures hallucination, not
  persuasion, and a prompt is per-workflow (no per-cohort prompt), so a trial would mean 100% of traffic.

> **Reply:** Nikhil — for the summit I can give you, per client, the measured lift on pickup and on
> engagement against a live holdout, with confidence intervals. On public data that's e.g. +23% pickup on
> the incident scenario. I won't blend them into one score; Product owns that definition and it hides
> harm. Two corrections before anyone repeats them: Kestrel's auto-apply excludes prompts and is capped at
> 20% traffic shift, and Northwind KYC has auto-apply off entirely. "Live by evening" means one in thirteen
> wins is fake; we promote at ~1–2 days. The persuasive prompt won't go out without a validated test on
> that client.

### 02 — Meera Iyer, Head of Compliance, Northwind
**Accepted:** every customer-facing change (welcome, voice, prompt, cohorts) goes to a named approver; we do
this **voluntarily** even where policy would let us auto-apply. Every prompt change carries a validation run id
in `evidence`. Requests are only sent inside approver hours. `report weekly --wf nw_renewal` lists every operational change made under
auto-apply, for the weekly summary she asked for. The 24 h audit answer:
`report heard --wf nw_kyc --at "D3 14:00" --features lang=ta,age_band=60+` prints the config version, the
cohort, every variant share and the prompt; `report why` gives the reason and the approver.
**Wrong claim:** "call timing and caller-ID rotation are covered by the policy we signed". True for
**renewal** (auto-apply on schedule and caller_id, max_shift 0.5). **False for KYC**: auto-apply is disabled.
We honour the stricter reading on KYC: every caller-ID rotation there goes through an approver, which is why a
bad KYC number pages a human (§10).
**Pushed back:** "slow and right" — agreed, but not for incidents. A validated revert to a known-good prompt is
sent the moment it is ready, even late in the day (§6).

> **Reply:** Meera — confirmed on all five points. Every change a customer can hear goes to your approvers
> with evidence; script changes carry a validation run id. One correction: auto-apply for timing and
> caller ID is enabled on Renewal only. On KYC your policy disables it, so those changes also come to your
> approvers. I'll list every Renewal operational change in the weekly summary. For IRDAI, I can produce
> within minutes, from our own store, what a given customer profile could have heard on a given day,
> which variant, and why it was live.

### 03 — Kabir Menon, Platform & SRE
**Accepted, all three non-negotiables.**
- **Kill test:** `scripts/crash_test.py` — 7 SIGKILLs, identical actions (§9).
- **Idempotency:** deterministic proposal ids are committed as intents *before* each HTTP write and
  reconciled on restart.
- **Actionable alerts:** pages vs tickets vs info (§10).

**Wishes:**
- One sim day of public_03 takes **13.5–18.4 s** on a laptop vs your 600 s budget. That was measured on a
  10-core / 16 GB Mac, not 4 vCPU / 8 GB, and it is single-threaded.
- Peak RSS is **796 MB**.
- Loop health without logs: `report health`.

**Pushed back:** no new stateful infrastructure — but I used SQLite, not your Postgres. It is a single local
file: nobody operates it at 2 a.m., and there is nothing to page. In production the same tables move to your
Postgres; the store is one 150-line module.

> **Reply:** Kabir — kill test: 7 random SIGKILLs during the incident scenario, the run finishes with the
> same 40 governance actions as an uninterrupted one, no double-apply, no lost approval. Script and report
> are in the repo. public_03: 13.5–18.4 s wall per sim day, 159,735 events/s, 796 MB peak. First thing to
> break at 10× is memory for the lead map, not CPU; the fix is per-tenant partitions. State is one SQLite
> file now; for prod I'd move the five tables into your Postgres. Pages only fire when a human must act
> now.

### 04 — Anita Desai, Operations Supervisor, Kestrel
**Accepted:**
- **≤ 2 requests per 24 h, none before 11:00** — stricter than Kestrel's policy (4 per 24 h from 10:00), set
  in `config/overrides.json`.
- **One request with five things in it:** batching.
- **Her prompt edits are never reverted silently:** a 48 h freeze on the dimension she touched, and her choice
  becomes our new control.
- **One screen:** `report supervisor --wf ks_cancel`.
- **"Don't read the script word for word":** every prompt fix is checked on live **engagement** as well as
  flags; a significant drop raises a ticket. The public_02 fix kept engagement at 47.5% vs 50.0% (z = −0.7, not significant).

**Partly declined:** "tell me before anything changes on a running campaign". Policy lets caller-ID and
timing changes auto-apply, and they must, to pull a flagged number within the hour. Those land without notice
but appear in the "what changed today" section. Welcome and voice exits from a losing arm also auto-apply,
because they are harm reductions.

**Note:** the one prompt fix we sent her *reverted her own edit* (P2 → P1). That went through her approval,
with the flag evidence, not around her.

> **Reply:** Anita — you'll get at most two requests a day, never before 11:00, each bundling everything
> pending. If you edit something, we treat your choice as the new baseline and won't propose on it for 48
> hours. The one exception is safety: if a prompt starts hallucinating, I'll send one request with the
> evidence, even if the prompt is yours. Number rotations and timing will apply without asking, because
> they need to be fast; they're listed in "changed today" on your screen. Any prompt fix is checked on
> whether people stay on the call, not just on flags.

### 05 — Tara Kulkarni, Head of Product
**Accepted:**
- Pickup and engagement are kept separate everywhere.
- One cohort object (C2).
- Every proposal carries an impact score, diff, affected leads and dry run.
- **Expired = rejected**: cooldown, then re-derive, never resubmit as-is.
- Only realised evidence: counters come only from events that actually happened, so an arm that never ran has
  zero rows.
- Winners stay within their workflow.

**Open question answered (minimum size / window):**
- 400 connected calls on the challenger *and* z ≥ 3 to promote; 150 and z ≤ −2 to drop.
- Cost at 90 connected/h: a true +10% arm is found 73% of the time, median 48 h; a +18% arm 100%, median 31 h.
  The false-winner rate is 0.021. At z = 2.5 we'd be faster but the false-winner rate is 0.077 (calibration
  table).

**Declined:** per-person call time as P0.

> **Reply:** Tara — your definitions are implemented as written; cohort ids are shared across detection,
> experiments and the supervisor view. Minimum size: 400 connected calls and z ≥ 3 to promote. That gives
> a 2% false-winner rate with three challengers, and finds a +10% arm in about two days. Halving it makes
> false winners 4×. I said no to per-person call time for now: a person has 3–6 dials, too few to estimate
> an hour profile, so it would be noise presented as personalisation. Cohort-level timing is live and
> gave +8–18% pickup on the public scenarios. Supervisor view and caller-ID health are done.

### 06 — Aryamann, CTO
**Accepted as design rules:**
- Explainability over magnitude: every change has a `why` trail.
- Harm to a cohort costs more than a missed win: an asymmetric stopping rule and a guardrail drop.
- **No compute where a cheaper answer exists:** the incident fix tries the last-known-good prompt (0 LLM
  credits) before the LLM. In public_02 one validation (3 credits) fixed the incident.
- Scale designed for the envelope (§8).
- The runbook is written (`docs/RUNBOOK.md`).

**Who I said no to:**
- Sales: blended score, speed, auto-apply on KYC, the persuasive prompt.
- Product: per-person timing.
- Anita: advance notice for operational changes.
- Kabir: Postgres in this submission.

**Claims I found untrue:** Sales on Kestrel and Northwind auto-apply; Compliance on KYC operational latitude.

> **Reply:** Aryamann — I built the loop to be explainable first: every change traces to the counters,
> test and approval behind it. I cut per-person timing, cross-tenant fairness and a UI, and said no to the
> blended score. Two memos contradicted the policies the platform enforces (KYC auto-apply in particular);
> I followed the policies. The thing I'd watch first in production is memory at 10×, then approver
> attention once hundreds of workflows share a supervisor.

## 4. Architecture

```
            ┌───────────────────────── simulator (HTTP) ─────────────────────────┐
            │ /events (NDJSON chunks)  /workflows/{id}[/config]  /approvals       │
            │ /workflows/{id}/apply  PUT config (auto)  /validation  /llm  /sim  │
            └───────▲───────────────────────┬───────────────────────────▲──────────┘
                    │ advance(step)         │ events                    │ writes (intent first)
┌───────────────────┴───────────────────────▼───────────────────────────┴──────────┐
│ loop.py  Controller                                                                │
│   ingest() ─► model.py  WorkflowAgg counters per workflow                          │
│     │          pk_seg / pk_did / arm / hal / kpi / camp + lead map (features)      │
│     └─► _route(): config_changed, approval_decided → pending_events                │
│   decide(), per workflow, sorted:                                                  │
│     governance.track()  poll approvals → apply / stale / withheld / expired        │
│     schedule.evaluate()   callerid.evaluate()   experiments.evaluate()             │
│     incidents.evaluate()  ─► each returns (target for its dims, Parts)             │
│     governance.compose()  ─► one target config T (+ shared cohorts.py objects)     │
│     governance.plan()     ─► auto part (policy dims, ≤ max_shift)                  │
│                              approval part (batched, timed, evidence attached)     │
└──────────────┬─────────────────────────────────────────────────────────────────────┘
               ▼
┌──────────── state/controller.db (SQLite, WAL) ───────────────────────────────────┐
│ learn_state   zlib JSON blob of all counters + module state, + ingest watermark   │
│ chunks_seen   which event chunks are in the blob                                  │
│ proposals     intent rows (proposal_id, config, dims, status, evidence)           │
│ decisions     every decision with summary + JSON detail   ← audit                 │
│ config_versions / config_snapshots   what was live, by whom                       │
│ alerts  metrics  cohorts  credit_spend                                            │
└───────────────────────────────────────────────────────────────────────────────────┘
```

**On restart:**
1. Load the blob and the watermark.
2. Rebuild the lead map from the chunks already seen. The chunks are immutable.
3. `refresh_configs`.
4. `governance.reconcile()` resolves every intent row that was written before its HTTP outcome was recorded.
   It looks the proposal up by its deterministic id, so nothing is re-sent.
5. Skip any workflow already marked decided at this sim minute.
6. Continue.

Iteration order is sorted everywhere and `PYTHONHASHSEED=0`, so a restarted controller makes the *same*
decisions. That is what the crash test checks.

## 5. The learning loop

**Cohorts (`cohorts.py`).** Cells are built from:
- the schedule feature, one value per cell;
- up to `MAX_OVERRIDES = 2` segment overrides from experiments, e.g. `lang=ta` if a segment win is proven.

Cells are ordered most specific first, contradictory cells are dropped, and the total is capped at 20. Ids
are stable slugs: `au_age_band_60p__lang_ta`. The minimum size for a segment claim is 400 connected calls on
the arm *within the segment*, with a Bonferroni-corrected z (≥ 3.5). Fragmentation is avoided because:
- a cohort is created only by a proven effect (a schedule interaction or a segment win), never "to see";
- a cohort that doesn't override a dimension falls back to `default`, so experiments keep pooling across
  cohorts.

**Experiment design (`experiments.py`).**
- **Arms:** every untested option of welcome and voice gets 15%, capped at 45% total, and the control keeps
  the rest.
- **Dimensions:** both run at the same time, independently randomised, so each is a stratified marginal
  comparison. A welcome × voice interaction is *not* tested: with 3 × 4 arms the cells are ~2% of traffic
  each, which would need weeks. This is listed in §13.
- **Workflows:** only engagement-primary workflows run these experiments (assumption 3).
- **Timeout:** exploration stops after 5 days live with no winner, and everything returns to the control.

**Deciding a winner.**
- **Test:** a stratified (CMH-style) two-proportion z-test of engagement, challenger vs control, with strata
  day × config_version × cohort.
- **Asymmetric sequential rule, looked at hourly:**
  - promote at **z ≥ 3.0, n ≥ 400**;
  - drop at **z ≤ −2.0, n ≥ 150**;
  - drop on the guardrail at **pickup z ≤ −3 and diff < −max_drop/2**.
- **Multiple comparisons:** the high promotion bar *is* the correction for hourly peeking and several arms.
  Monte Carlo of the exact rule (`scripts/calibrate.py`, 2,000 worlds), all at 90 connected calls/h:

| world | P(any promoted) | P(true winner) | median h | P(null arm dropped) |
|---|---|---|---|---|
| A/A, 3 challengers | **0.021** | — | — | 0.109 |
| +10% arm, 2 null | 0.729 | 0.722 | 48 | 0.102 |
| +18% arm, 1 null | 1.000 | 0.998 | 31 | 0.073 |

  The asymmetry is deliberate. Wrongly dropping a null arm (~11%) costs only an option. Wrongly promoting one
  costs a cohort.
- **Promotion is never automatic.** It goes to approval even where welcome and voice are auto-allowed
  (Kestrel), because it changes what most customers hear.

**Decay and rollback.**
- After promotion the old control keeps **20%** as a concurrent holdout.
- Every loop compares winner vs holdout over the last **24 h**. Roll back when either:
  - z ≤ −1.64 with diff < 0, or
  - the lift has fallen from its promotion-time value with z ≤ −2.5 and the current diff ≤ 0.
- Rollback is a harm reduction, so it may auto-apply where policy allows.
- Worst-case detection is ~24 h after the effect flips, plus approval latency where a human is needed.
- **Honest note:** on the final code neither public run promoted an arm that later decayed, so the rollback
  path is not exercised in the committed reports. `nw_renewal` W3 decays in public_02, but there it was only
  ever a 15% exploration arm. Its exposure fell to 0 once exploration ended (grader table, §11).

**Call time (`schedule.py`).**
- **History:** the per-segment × hour pickup counters include all pre-D0 history.
- **Feature choice:** the controller picks the single feature with the strongest segment × hour interaction
  (G-test, G/df ≥ 3).
- **Profiles:** per-value hour profiles are shrunk toward the workflow profile (K = 150). Weights are
  `rel²`, clipped to [0.25, 4].
- **Re-evaluated once a sim day.**
- **Exploration:** none needed. The clip keeps every hour at ≥ 0.25× weight, so the data for every hour keeps
  flowing.
- **Per cohort, not per person:** see C4.

**Caller ID (`callerid.py`).**
- **Health:** each number's pickup vs its siblings, stratified by hour, over 12 h.
- **Quarantine:** ratio < 0.8 with z < −4 on ≥ 150 dials. Then:
  - if the number carried ≥ 1.5× its fair share, it is "overloaded": the sim degrades numbers under load, so
    it rests 2 days;
  - otherwise it is "flagged" and rests 7 days.
- **After the rest:** probation at 10%, one number at a time.
- **Weak numbers** (< 0.90, z < −3) get half a share, with hysteresis up to 0.97.
- **No churn:** at least two numbers are kept, and a rebalance only happens when TV ≥ 0.05.

**Guardrail.**
- **Experiments:** a challenger that hurts the guardrail at pickup z ≤ −3 and beyond half the allowed drop is
  withdrawn even if engagement is fine. This fired in public_02: nw_renewal voice V2 was withdrawn at
  −7.0 pt pickup, z = −3.0.
- **Promotion:** blocked if the guardrail z ≤ −2.
- **Schedule changes:** the guardrail is **only monitored, not enforced**. See §13 for the nw_kyc engagement
  dip.

## 6. Governance

**State machine (`proposals.status`):**
```
                  ┌────────────── not_sent (window/cap/cooldown; re-derived next loop)
compose→plan ─────┤
                  ├─ auto: submitting ─► auto_applied | auto_rejected(→ approval path)
                  └─ approval: submitting ─► pending ─┬─► approved ─┬─► applying ─► applied
                                                      │             ├─► stale     (base moved: re-propose on new base)
                                                      │             ├─► withheld  (now raises traffic to a harmful arm)
                                                      │             └─► lapsed    (apply window missed: re-propose)
                                                      ├─► rejected  ─┐
                                                      └─► expired   ─┴─► cooldown 4h (1st) / 24h (repeat) → re-derive
```
`submitting` and `applying` are intents committed *before* the HTTP call. `reconcile()` resolves them after a
crash.

**Batching and timing (`plan`).**
- One request per workflow carries every outstanding approval part.
- Only one request is open at a time. A new urgent one may jump the queue: a routine request still pending is
  allowed to lapse.
- **Cap:** `max_requests_per_24h` minus one slot reserved for urgent changes.
- **Spacing:** at least 3 h between routine requests.
- **Timing:** a request is only sent inside approver hours, and:
  - **routine:** only if the *worst-case* latency still lands before the TTL, inside hours;
  - **urgent** (safety): if **P(decision lands in time) ≥ 0.75** under uniform latency.

  This last rule came from public_02. The revert was ready at 17:00, and the worst case (120 min) would land
  at 19:00, outside hours. P was 0.99, yet the old rule held the fix overnight for 18 h. It now ships at
  17:00 and was live at 18:00.
- **Kestrel override:** ≤ 2 per 24 h, not before 11:00.

**Auto-apply vs voluntary restraint.**
- **Auto-apply** is used only for dims in `allowed_dims`, within `max_shift` (TV distance per cohort ×
  dimension; larger moves are interpolated in steps).
- **Even when allowed**, customer-facing dims (welcome, voice, prompt) auto-apply *only* when the change is
  pure harm reduction: withdrawing a losing arm, rolling back, or ending exploration.
- **Starting a test, promoting, and any cohort change always go to a human.** Prompt changes never
  auto-apply.

**Impact, affected leads, dry run.**
- **Impact score:**
  - every part estimates expected extra engaged conversations per day (lift × share moved × engaged/day);
  - the batch score is `1 − exp(−Σ/(0.05 × daily batch))`, which is 0.63 when the batch would add 5% of a
    day's batch in conclusions;
  - safety parts score 1.0, floor 0.05.

  It ranks requests sensibly. It is **not** a promise; its absolute value is only as good as the lift
  estimate.
- **Affected leads:** active leads × the share matching the part's predicates, from our lead feature
  counts. For `next_campaign_only`, an allocation-only change affects only the next batch. Feature
  independence is assumed (§13).
- **Dry run:**
  - **caller ID:** expected pickup from current sibling ratios;
  - **schedule:** re-weights the last 7 days of dials *within each hour* by the new weights — a
    reweighting, not a counterfactual;
  - **experiments:** the measured arm effect with its CI;
  - **prompt fix:** the fix prompt's own production record plus the validation pass rate.

**Supervisor edits mid-experiment.**
- The `config_changed` event with `actor: supervisor` freezes the touched dims for 48 h.
- Their choice is adopted as the control.
- Pending requests on the old base go **stale** when approved and are re-proposed on the new base, never
  forced.
- The incident detector treats a supervisor prompt change as a new prompt to watch from its go-live.
- **Bug found and fixed while writing this section.** The "withhold if harmful" veto used to fire whenever an
  approved config *contained* a dropped arm, even when that arm was the supervisor's live choice. It withheld
  8 consecutive approved nw_renewal requests in public_02 and left a decaying arm at 15% for 4 extra days. The
  veto now fires only if the request *raises* traffic to a harmful arm relative to the live config.

## 7. Incidents

**Detection.**
- Alarm on a prompt's hallucination flag rate since it went live, when all hold:
  - rate ≥ max(2 × baseline, baseline + 1 pt);
  - one-sided z ≥ 4 vs baseline;
  - n ≥ 150 connected calls.
- **False-positive rate:** at z ≥ 4 with hourly looks over a week (~84 looks), the per-workflow false alarm
  is bounded by ~84 × 3.2e-5 ≈ 0.3% — an upper bound, since the looks are correlated.
- **The cost:** a 2.8× spike (1.5% → 4.2%) took 5.5 h of traffic to cross the bar.

**Choosing a fix, cheapest first:**
1. The last-known-good prompt: ≥ 1000 connected calls in this workflow's history with a normal flag rate.
2. If none exists, `POST /llm/suggest-fixes` (3 credits).

Each candidate is validated at n = 300 (3 credits) and must pass `min_validation_pass_rate + 0.005`. At most
3 validations; a page fires if credits can't cover the next step.

**Verification.**
- After applying, wait for ≥ 200 connected calls on the new prompt.
- **Safety:** the flag rate must not be significantly above baseline (the alarm rule at z ≥ 3). If it is, the
  incident goes back to fixing with the next candidate.
- **Engagement:** compared with the pre-incident level. A drop at z ≤ −3 raises a `fix_hurts_engagement`
  ticket for a human to review. It does not auto-revert: the alternative is a hallucinating prompt.
- If the incident stays open past its stuck limit, it pages.

**public_02 timeline (`ks_cancel`):**

| sim time | what happened | source |
|---|---|---|
| D3 11:30 | supervisor switches prompt to P2 (v9) | config_changed |
| D3 12:00 | controller freezes prompt for 48 h, watches P2 | `decisions` supervisor_edit |
| D3 17:00 | **detect**: P2 flags 4.2% of 546 vs 1.5% (z = 5.12) | incidents/detect |
| D3 17:00 | P1 (20,439 calls of history) validated: 0.987 on n = 300, bar 0.975; 3 credits | incidents/validate |
| D3 17:00 | apr_00008 filed with the validation id as evidence | governance/submitted |
| D3 17:39 | approved | approval_decided event |
| D3 18:00 | applied as v10; fix live | governance/applied |
| D3 20:00 | resolved: P1 flags 2.3% on 219 calls (not significantly above 1.5%); engagement 47.5% vs 50.0% (z = −0.7) | incidents/resolved |

- **Grader:** MTTR 390 min from the supervisor's edit; hallucinated calls 108 vs baseline 348 on ks_cancel.
- **Credits:** 3 of 200; no LLM call.
- **Gap:** 2.3% at resolution sits above baseline but within noise on 219 calls. A stricter verifier would
  wait for more calls; I chose to resolve and keep watching (the detector stays armed on P1).

## 8. Scale
Measured on public_03_load: 12 workflows, ~840k events/sim day. The controller ran on a 10-core / 16 GB Mac
alongside the simulator, single-threaded. Source: `report health` on `state/public_03_load.db` plus the
`metrics` table.

| metric | value |
|---|---|
| history bootstrap (pre-D0) | 5,537,531 events in 33.7 s |
| ingest throughput | **159,735 events/s** |
| wall clock per sim day (steady state) | **13.5–18.4 s** (budget 600 s) |
| worst steady-state loop (ingest + decide) | 0.96 s |
| decision lag | ≤ 60 sim-min, one loop step: decisions use every event up to the minute they're made |
| peak RSS | **796 MB** |
| outcome | conclusion +20.9%, 65 changes applied, 0 harmful |

**Not measured:** 4 vCPU / 8 GB. The controller uses one core, so vCPU count matters little. 8 GB is ~10× the
measured peak. I'd still run it in a `--cpus 4 --memory 8g` container before claiming it.

**What breaks first at 10×** (6.5 M dials/day, 100+ workflows):
1. **Memory:** the lead map (lead → features) is ~70% of RSS. It is needed to join call events to features,
   and it scales with active leads. Fix: store features in the counters' keys at ingest and evict concluded
   leads (already done per campaign), or move the join to a per-tenant process.
2. **Single-threaded ingest:** at ~160k events/s, 10× load is ~8.5M events/day, about 55 s/day — still fine.
3. **Per-loop decide cost grows with counter count:** the window scans over day-keyed counters cost
   ~0.5 s/loop now and grow with history. Fix: roll old days into a summary bucket.
4. **The state blob:** whole-state zlib JSON per commit (~450 KB at 12 workflows). At 100s of workflows,
   write per-workflow blobs and commit only dirty ones.

**Partitioning:** workflows share nothing except credits and the tenant's approver. Run one controller
process per tenant, each with its own store/schema, behind a scheduler that owns the global credit budget
(C15). Event ingestion becomes a per-tenant consumer group.

## 9. Reliability
**Method** (`scripts/crash_test.py`, report in `reports/crash_recovery.md`):
1. Run public_02 uninterrupted as the reference.
2. Run it again, SIGKILLing **our own child PID** 7 times at chosen sim moments: D1 10:15, D2 10:45, D3 12:15,
   D3 14:30, D5 15:15, D5 16:00, D7 11:30. Each kill gets a random 0–150 ms delay so it lands inside the loop
   body. Two of the kills hit a process that was still rebuilding its lead map after the previous kill.
3. Restart after each kill and compare the simulator's own record of governance actions.

**Results:**
- finished the scenario: **PASS**;
- no approval applied twice: **PASS**;
- no proposal_id reused: **PASS**;
- zero apply conflicts: **PASS**;
- the sequence of governance actions is **identical** to the uninterrupted run (40 = 40): **PASS**.

**What is lost on a crash:** nothing durable. Work since the last commit (at most one chunk's ingest, or one
workflow's decisions) is redone. Events are deduplicated by `event_id`, and a decision is marked per
workflow per minute, so redoing it is exact.

**Idempotency keys:**
- `proposal_id = {wf}:{apr|auto}:{sim_minute}:{seq}` is deterministic and committed before the POST/PUT.
- On restart, `reconcile` asks the simulator for the proposal's state instead of re-sending.
- Approvals are applied by `approval_id`, which the simulator applies at most once.

**4xx/5xx:**
- `api.py` retries transport errors and 5xx with backoff, then raises `SimUnavailable`. The loop exits
  non-zero; the state is consistent, and a restart resumes.
- A 4xx is returned to the caller as data:
  - 409 `stale_base_version` → stale path;
  - 410 → lapsed;
  - 402 → credits page;
  - an auto-apply refusal is recorded as `refused`; a 403 (policy disagrees with our model of it) raises a
    ticket, and the change is re-derived on the next loop.

## 10. Audit & observability
**"Why did ks_cancel change at D3 18:00, who approved it, and what happened after?"**
~~~
$ .venv/bin/python -m controller.report --db state/public_02_incidents.db why --wf ks_cancel --at "D3 18:00"
## Why did ks_cancel look the way it did at D3 18:00?

Config **v10** went live at D3 18:00 (2027-01-07T18:00:00+05:30), actor **controller_approved**, changed ['prompt_version'].

**How it passed governance:** approval **apr_00008**, filed D3 17:00, decided by the ks_cancel approver at D3 18:00, applied D3 18:00.

**Summary shown to the approver:** Hallucination fix: switch prompt P2 -> P1 (flags 4.2% vs 1.5% normal)

**Impact score:** 1.0 · **affected leads (est.):** 3421

**Rationale:**
```
[prompt_fix] Prompt P2 went live via supervisor (config v9). Since then 23 of 546 connected calls were flagged (4.2%; baseline 1.5%; z=5.12). Fix: P1 ran in production on 20439 connected calls with 1.5% flags and 50.0% engagement. Validation val_00001: pass rate 0.987 on 300 synthetic conversations. After apply we verify the flag rate and engagement on live calls.
```
**Diff:** `{"prompt_version":{"after":"P1","before":"P2"}}`

**Dry run:** `{"prompt_fix":{"engagement_expected":0.4996,"flag_rate_expected":0.0145,"flag_rate_now":0.0421,"method":"the fix prompt's own production record + validation run","validation_pass_rate":0.9867}}`

**Evidence:** `{"parts":[{"decision_id":null,"dims":["prompt_version"],"harm_reduction":true,"impact_per_day":0.0,"kind":"prompt_fix","module":"incidents","safety":true,"summary":"Hallucination fix: switch prompt P2 -> P1 (flags 4.2% vs 1.5% normal)","urgent":true}],"validation_run_id":"val_00001"}`

**Decision trail:**
- D3 17:00 [governance/submitted] Approval request apr_00008 filed: Hallucination fix: switch prompt P2 -> P1 (flags 4.2% vs 1.5% normal)
- D3 18:00 [governance/approved] apr_00008 approved (apply by 2027-01-07T19:39:00+05:30)
- D3 18:00 [governance/applied] Applied apr_00008 as v10 (prompt_version)

**What happened next:** 24h before: pickup 0.2569, engagement 0.4647 (4800 dials) → 24h after: pickup 0.2592, engagement 0.5024 (4800 dials). (Raw before/after, not causal: the experiment holdout is the causal read.)
- D3 20:00 [incidents/resolved] Incident ks_cancel:hal1 resolved: P1 flags 2.3% on 219 calls (baseline 1.5%); engagement 47.5% vs 50.0% before (z=-0.7); time from detection to fix live 1h00m
- D5 12:00 [experiments/explore_timeout] welcome_message: no winner after 5 days; stop exploring, W1 back to 100%
- D5 12:00 [experiments/explore_timeout] voice: no winner after 5 days; stop exploring, V1 back to 100%
- D5 12:00 [governance/auto_applied] Auto-applied v11 (welcome_message, voice) within policy
- D5 13:00 [governance/auto_applied] Auto-applied v12 (welcome_message, voice) within policy
~~~
- `report heard --wf ks_cancel --at "D3 15:00" --features lang=hi,age_band=60+` answers the IRDAI question for
  any version, including supervisor-set ones: we snapshot every config version we see.
- Raw SQL works too: `SELECT ts, module, kind, summary FROM decisions WHERE wf='ks_cancel' AND ts BETWEEN …`.

**Alerts** (`alerts` table; `report supervisor` shows the open ones):

| severity | when | why it's at that level |
|---|---|---|
| **page** | a bad caller ID carries ≥ 50% of a workflow's traffic; no healthy caller ID left; out of credits mid-incident; incident fix stuck | customers are being hurt now and the controller can't fix it alone |
| **ticket** | caller ID quarantined (raise a carrier ticket); hallucination spike detected (fix in progress); approval rejected *by rule* (a controller bug); urgent change unsent for 18 h | needs a human today, not tonight |
| **info** | supervisor edits; waiting for approver hours; decay rollbacks; exploration timeouts | the controller handled it; it shows on the supervisor screen |

**Loop health:** `report health` prints ingest events/s, decision lag, decisions/hour, credit burn and peak RSS
from the `metrics` table.

## 11. Results

**public_01_dev** (`reports/public_01_dev.md`): 20 changes applied — 11 neutral, 9 beneficial, **0 harmful**.
11 approval requests, all approved; 0 expired, lapsed or conflicted.
```
| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.392 | +14.8% | 0.264 | +7.8% | 0.519 | +5.6% | 29% | 50 (57) |
| nw_kyc | 0.418 | +14.0% | 0.272 | +12.4% | 0.596 | -0.3% | 0% | 25 (21) |
| nw_renewal | 0.459 | +10.3% | 0.284 | +7.7% | 0.552 | +1.9% | 10% | 50 (54) |
| ALL | 0.420 | +12.9% | 0.273 | +8.6% | 0.546 | +3.1% | 15% | 125 (132) |
```

**public_02_incidents** (`reports/public_02_incidents.md`): 24 applied — 10 neutral, 13 beneficial,
**1 harmful**. 15 requests: 13 approved, 1 rejected (a judgement call), 0 expired, 0 apply conflicts.
```
| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.361 | +53.5% | 0.250 | +37.9% | 0.494 | +5.7% | 32% | 108 (348) |
| nw_kyc | 0.434 | +16.0% | 0.276 | +17.6% | 0.593 | -2.5% | 0% | 32 (29) |
| nw_renewal | 0.489 | +13.0% | 0.293 | +11.7% | 0.556 | +0.2% | 8% | 59 (54) |
| ALL | 0.419 | +26.8% | 0.270 | +22.9% | 0.537 | +0.8% | 21% | 199 (431) |
```

**Interpretation.**
- **Where we helped:**
  - **Pickup** is most of the gain. Caller-ID quarantine plus per-cohort timing gave +8.6% / +22.9% pickup.
    In public_02 the flagged ks_cancel number went from 9.9% of traffic on its bad day to 0 the next day.
  - **Engagement** gains on ks_cancel/nw_renewal come from withdrawing losing arms quickly and, in public_01,
    from promoting ks_cancel V4 (+18%, z = 3.3).
  - **Incident:** hallucinated calls on ks_cancel fell from 348 to 108.
- **Where we were harmful:** `ks_cancel v13`, an auto caller-ID change on D9. D2, the number flagged on D2, came
  back on 10% probation after its 7-day rest and was still bad. The grader scores that version's DID health
  −0.060. It was quarantined again 4 h later (v14, beneficial). The probe *is* the exploration cost of letting
  a number back. Fix: lengthen the rest for a number quarantined as *flagged* rather than overloaded, or probe
  at 5%.
- **The nw_kyc engagement dip:** −2.5% relative, −0.015 absolute, inside the 0.02 guardrail. It comes from
  timing changes that shift *who* connects: more pickups from segments that engage less. The guardrail held,
  but only because the drop was small. Schedule changes are not engagement-guarded (§13).
- **"Engagement gap captured"** is low (8–32%). No arm promotions were won on public_02's variants within the
  5-day exploration budget. That is the price of a 2% false-winner rate.

**public_03_load** (`reports/public_03_load.md`): conclusion +20.9%, pickup +13.5%, engagement
+4.4%; 65 changes applied, 0 harmful.

## 12. Trade-offs & rejected alternatives
| Option | Why considered | Why rejected / what instead |
|---|---|---|
| Thompson sampling bandit | Faster regret, standard answer | Allocation drifts every hour, which needs a config change (and approval) each time. Winner selection must be a deterministic statistical test (constraint 4). Fixed 15% arms plus a sequential test is approvable. |
| Bayesian posterior "P(better) > 0.95" | Readable | Same peeking problem, harder to calibrate for an auditor. I calibrated the frequentist rule by simulation instead. |
| Row store of every event (Postgres/Parquet) | Flexible queries | 850k events/day × weeks; the decisions need only counters. Counters keyed by day/hour/version/cohort/segment answer every question the modules ask. |
| Ask the LLM for new welcome variants | "Self-improving" | An LLM may propose, but each proposal is a new arm to test at 15% traffic. Credits plus approval plus exposure risk outweigh the gain within a week. Cut. |
| Per-person call time | Product P0 | Too few dials per person (C4). |
| Auto-apply everything policy allows | Speed (Sales) | Customer-facing starts and promotions go to humans voluntarily; only harm reductions auto-apply. |
| Postgres (SRE's existing) | No new infra | Needs a server for a take-home that `run.sh` must start alone. SQLite has the same schema, and the move is mechanical. |
| One request per change | Simpler | Approver fatigue caps (Kestrel 2/day). Batching was mandatory. |
| Worst-case timing for urgent fixes too | Never waste a slot | Cost 18 h of a hallucinating prompt in public_02. Replaced with P ≥ 0.75. |

## 13. Known weaknesses
1. **Schedule changes are not engagement-guarded.** nw_kyc engagement fell 2.5% relative (inside the
   guardrail, but unmonitored by an enforcing rule). Fix: a per-version engagement comparison after each
   schedule change, with rollback.
2. **No welcome × voice interaction test.** Marginal comparisons assume additivity.
3. **The decay rollback path is unexercised on the final public runs** (§5). Its logic is reviewed; it fired
   in an earlier run.
4. **Caller-ID probation can re-expose customers to a bad number** (the one harmful change, §11).
5. **`lead_share` assumes independent features** when estimating affected leads for multi-feature
   predicates.
6. **Two prompt changes inside one 60-min loop step collapse into the later one**, for incident attribution
   and for config snapshots.
7. **Exploration of unknown variants is real exposure.** A 15% arm that is 20% worse costs ~3% engagement on
   that dimension until it is dropped (median ~1 day).
8. **Null arms are wrongly dropped ~11% of the time** (by design). In public_02 the original V1 voice was
   dropped against the supervisor's V2 at z = −2.2. If the supervisor's choice was in fact worse, we lost the
   option to prove it.
9. **Memory scales with active leads** (§8). It is the first thing to break at 10×.
10. **No 4 vCPU / 8 GB measurement.**

## 14. If this went to production at Formi next month
1. **Days 1–2:** run it in shadow mode against production events: decide, write proposals, but send nothing.
   Compare its proposals with what humans did; check that the ingest lag holds on real traffic.
2. **Days 2–4:** move the store to the platform Postgres (same tables), add a Prometheus exporter over
   `metrics`, and wire pages and tickets to the on-call rotation, using `docs/RUNBOOK.md`.
3. **Days 4–6:** turn on caller-ID health only, on one tenant whose policy allows caller-ID auto-apply. It is
   the biggest, safest win, and every action is reversible within the hour.
4. **Week 2:** add per-cohort timing with the engagement guard (weakness 1) built first. Then experiments on
   one engagement-primary workflow with a willing supervisor, holdout on.
5. **Throughout:** a weekly review of every `decisions` row with an approver. False alarms and rejected
   requests are the calibration data for the thresholds.
6. **Before scaling past ~20 workflows:** per-tenant processes and the global credit and attention scheduler
   (C15).
