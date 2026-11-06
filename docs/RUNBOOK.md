# Controller runbook

For the engineer on call when the controller's author is away. Design rationale is in `SUBMISSION.md`. This
file covers how to run it, how to tell whether it is healthy, and what to do when it pages.

## 1. What it is
- **What:** one Python process (`python -m controller`) that drives every workflow through the platform
  HTTP API.
- **State:** all of it lives in one SQLite file, `state/controller.db` by default (override with
  `CONTROLLER_DB`).
- **What it is allowed to do:**
  - auto-apply within each workflow's policy;
  - file approval requests;
  - apply approved requests;
  - spend credits on validation runs and LLM suggestions during hallucination incidents.
- **What it must never do:** change config any other way, or overwrite a supervisor edit.

## 2. Run it
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # once
./run.sh http://127.0.0.1:8700                                       # runs until the scenario finishes
CONTROLLER_STEP=60 CONTROLLER_DB=state/x.db ./run.sh <URL>           # loop step (sim-min) / store path
scripts/run_scenario.sh public_02_incidents [port]                   # sim + controller + grade, end to end
.venv/bin/python -m pytest -q tests                                  # 27 tests, ~2 s
.venv/bin/python scripts/crash_test.py --scenario public_02_incidents # kill/restart proof, ~1 min
```
`run.sh` sets `PYTHONHASHSEED=0`. Do not remove it: deterministic iteration order is what makes a restart
reproduce the same decisions.

**Restarting is always safe.**
- Kill it however you like (SIGKILL included) and run the same command again.
- On start it logs `rebuilt lead map from N chunks`, then writes a `restart` row to `decisions`.
- It reconciles every request that was in flight, then continues.

**Starting over:** delete the DB file (`state/x.db` plus its `-wal` and `-shm` files). If the simulator's
world id differs from the one in the DB, the controller archives the old DB by itself and starts fresh.

## 3. Is it healthy?
```bash
.venv/bin/python -m controller.report --db state/controller.db health      # loop health
.venv/bin/python -m controller.report --db state/controller.db supervisor  # one screen per workflow
```

| signal | healthy | look at |
|---|---|---|
| wall per sim day | ≪ 600 s (public_03: 13.5–18.4 s) | `metrics.ingest_s` vs `decide_s`; ingest dominating means the event volume grew |
| events/s | ~160k on a laptop | a drop with flat volume means disk contention on the DB |
| decision lag | ≤ one loop step (60 sim-min) | if larger, the loop is behind; check the worst loop |
| peak RSS | ~800 MB at 12 workflows | grows with *active leads*; see §6 |
| credits left | stable outside incidents | `credit_spend` table: every spend with its workflow, kind and reference (validation or LLM run) |
| open alerts | only tickets and info | §4 |

Raw numbers, per loop: `sqlite3 state/controller.db "SELECT * FROM metrics ORDER BY id DESC LIMIT 5"`.

## 4. Alerts and what to do

Alerts live in the `alerts` table. Open ones have `resolved_minute IS NULL`.

### Pages (act now)
| code | meaning | do this |
|---|---|---|
| `did_quarantined:<D>` (page) | a caller ID carrying ≥ 50% of a workflow's traffic answers far below its siblings. On KYC-style policies (no auto-apply) the rotation needs an approver. | Check `report supervisor --wf <wf>` for the pending caller-ID request; chase the client approver; raise a carrier ticket for the number. |
| `no_healthy_caller_id` | every number on the workflow is quarantined; it is dialling the least-bad one | Carrier escalation. Ask the client for a new number. |
| `credits_exhausted` | a hallucination incident needs a validation or LLM call and credits can't cover it | Top up credits, or have the client revert the prompt by hand. The incident record shows the last good prompt. |
| `hallucination_fix_stuck` | an incident has been open > 6 h | `report why --wf <wf> --at "<now>"`. Usually the approval is waiting or was rejected; call the approver. |

### Tickets (today, not tonight)
| code | meaning | do this |
|---|---|---|
| `did_quarantined:<D>` (ticket) | a number was rotated out automatically | Raise a carrier ticket. Nothing else: the controller already moved the traffic. |
| `hallucination_spike` | a prompt's flag rate crossed the alarm. A validated fix is being sent. | Watch it resolve. It is auto-resolved by the verifier. |
| `fix_hurts_engagement` | the fix prompt is safe but people hang up more | Review the prompt with the client. Do not auto-revert. |
| `approval_rejected_by_rule` | the platform rejected a request for a rule reason (fatigue, missing evidence) | **This is a controller bug.** Collect the `proposals` row and its `decisions`, then file it. |
| `auto_apply_refused` | the platform refused an auto-apply that we believed policy allowed | The policy changed or our policy model is wrong. Compare `GET /workflows/<wf>` `governance.auto_apply` with the request. |
| `urgent_change_waiting` | an urgent change has been unsent for 18 h (approver hours or caps) | Contact the approver; the request is in the supervisor view. |

### Info (no action)
- supervisor edits;
- "waiting for approver hours";
- rollbacks;
- exploration timeouts.

These show on the supervisor screen.

## 5. Answering questions from the audit store
```bash
R=".venv/bin/python -m controller.report --db state/controller.db"
$R why   --wf ks_cancel --at "D3 18:00"     # what was live, why, who approved, what happened after
$R heard --wf nw_kyc --at "D3 14:00" --features lang=ta,age_band=60+   # what this customer could hear
$R weekly --wf nw_renewal                    # operational changes made under auto-apply (client weekly summary)
sqlite3 state/controller.db "SELECT ts, module, kind, summary FROM decisions WHERE wf='ks_cancel' ORDER BY id"
```
The shell variable above works in bash. In zsh, define a function instead.

| table | holds |
|---|---|
| `decisions` | every decision, human-readable `summary` plus JSON `detail` |
| `proposals` | every request with config, diff, evidence, impact, dry run, status |
| `config_versions` | who changed what, when |
| `config_snapshots` | the full config of each version we saw |

## 6. Common failures
- **`SimUnavailable` and the process exits non-zero.** The API kept failing after retries (5xx or transport).
  The state is consistent; fix the API and restart.
- **Memory climbing.** The lead map holds features for active leads. Concluded campaigns are evicted. A
  steady climb means campaigns are not concluding (check `model` campaign counters) or the tenant count grew;
  split tenants into separate processes, each with its own `CONTROLLER_DB`.
- **A workflow gets no proposals.** Check `decisions` for `wait` rows; the reason is in the summary (approver
  hours, TTL, cap, cooldown after a rejection, 48 h freeze after a supervisor edit). These are intentional.
- **"approved but not applied" (`withheld`).** The approved config would raise traffic to an arm we measured
  as harmful after the request was filed. It will be re-proposed without that arm.
- **`stale`.** Someone changed the config between request and approval. It will be re-proposed on the new
  base. Expected after supervisor edits.

## 7. Changing behaviour safely
- **Per-tenant or per-workflow restrictions** that are stricter than platform policy, such as Kestrel's
  2 requests per day and nothing before 11:00, go in `config/overrides.json`. Overrides can only tighten
  policy, never loosen it.
- **Statistical thresholds** are module constants:
  - `experiments.py`: `Z_PROMOTE`, `MIN_PROMOTE`, `Z_DROP`, `HOLDOUT`, `DECAY_WINDOW_H`;
  - `callerid.py`;
  - `incidents.py`.

  After changing any of them, re-run `scripts/calibrate.py` and `pytest`. The false-winner test fails if the
  rule exceeds 5%.
- **Before shipping any change:**
  1. Run `scripts/run_scenario.sh` on public_01 and public_02 and compare the reports: there must be no new
     harmful changes.
  2. Run `scripts/crash_test.py`: it must PASS.
