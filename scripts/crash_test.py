"""Crash-recovery test: kill -9 the controller at chosen sim moments, restart it, let it finish, and
compare against an uninterrupted run of the same scenario.

    python scripts/crash_test.py --scenario public_02_incidents [--port 8721]

Pass criteria (all must hold):
  1. the crashed run finishes the scenario;
  2. no approval is applied twice, no proposal_id is reused, zero apply conflicts;
  3. no approval request is left pending/approved-but-never-applied by us because of the crash;
  4. the sequence of governance actions (requests, applies, auto-applies: workflow, version, dims) is
     identical to the uninterrupted run. The controller is deterministic given the sim's state,
     so any divergence means a crash changed a decision.
Writes reports/crash_recovery.md and logs/crash_*.log.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
PY = os.environ.get("PYTHON", str(ROOT / ".venv" / "bin" / "python"))
KILL_AT = ["D1 10:15", "D2 10:45", "D3 12:15", "D3 14:30", "D5 15:15", "D5 16:00", "D7 11:30"]


def minute(s: str) -> int:
    d, hm = s.split()
    h, m = hm.split(":")
    return int(d[1:]) * 1440 + int(h) * 60 + int(m)


def start_sim(scenario: str, run_dir: str, port: int, log):
    p = subprocess.Popen([PY, "-m", "sim", "serve", "--scenario", f"scenarios/{scenario}.yaml", "--run-dir", run_dir,
                          "--port", str(port), "--force"], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    for _ in range(600):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200:
                return p
        except httpx.HTTPError:
            pass
        if p.poll() is not None:
            raise SystemExit("simulator exited")
        time.sleep(0.2)
    raise SystemExit("simulator did not start")


def controller(port: int, db: str, log):
    return subprocess.Popen(["./run.sh", f"http://127.0.0.1:{port}"], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                            env={**os.environ, "CONTROLLER_DB": db, "PYTHON": PY})


def actions(run_dir: str) -> list[tuple]:
    out = []
    for line in (ROOT / run_dir / "sim_log.jsonl").read_text().splitlines():
        r = json.loads(line)
        if r["kind"] in ("approval_requested", "approval_applied", "auto_applied", "apply_conflict", "approval_lapsed",
                         "auto_apply_rejected", "validation_run", "llm_suggest_fixes"):
            out.append((r["sim_minute"], r["kind"], r.get("workflow_id"), r.get("proposal_id") or r.get("approval_id"),
                        tuple(r.get("dims") or [])))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="public_02_incidents")
    ap.add_argument("--port", type=int, default=8721)
    ap.add_argument("--seed", type=int, default=11)
    a = ap.parse_args()
    rnd = random.Random(a.seed)
    (ROOT / "logs").mkdir(exist_ok=True)
    (ROOT / "state").mkdir(exist_ok=True)
    lines = [f"# Crash-recovery test — {a.scenario}", ""]

    # reference: uninterrupted
    ref_dir, ref_db = f"runs/crashref_{a.scenario}", f"state/crashref_{a.scenario}.db"
    for suf in ("", "-wal", "-shm"):
        Path(ROOT / (ref_db + suf)).unlink(missing_ok=True)
    with open(ROOT / "logs" / "crash_ref.log", "w") as log:
        sim = start_sim(a.scenario, ref_dir, a.port, log)
        try:
            c = controller(a.port, ref_db, log)
            c.wait()
        finally:
            sim.terminate()
            sim.wait()

    # crashed run
    run_dir, db = f"runs/crash_{a.scenario}", f"state/crash_{a.scenario}.db"
    for suf in ("", "-wal", "-shm"):
        Path(ROOT / (db + suf)).unlink(missing_ok=True)
    kills = []
    with open(ROOT / "logs" / "crash_run.log", "w") as log:
        sim = start_sim(a.scenario, run_dir, a.port + 1, log)
        try:
            pending = [minute(k) for k in KILL_AT]
            c = controller(a.port + 1, db, log)
            restarts = 0
            while True:
                if c.poll() is not None:
                    if c.returncode == 0:
                        break
                    if c.returncode != -signal.SIGKILL:
                        raise SystemExit(f"controller exited with {c.returncode}; see logs/crash_run.log")
                try:
                    st = httpx.get(f"http://127.0.0.1:{a.port + 1}/sim/state", timeout=5).json()
                except httpx.HTTPError:
                    time.sleep(0.05)
                    continue
                if pending and st["sim_minute"] >= pending[0] and c.poll() is None:
                    time.sleep(rnd.uniform(0.0, 0.15))  # land somewhere inside the loop body
                    os.kill(c.pid, signal.SIGKILL)       # our own child PID only
                    c.wait()
                    kills.append((pending.pop(0), st["sim_minute"]))
                    log.write(f"\n=== KILLED controller pid {c.pid} at sim minute {st['sim_minute']} ===\n")
                    log.flush()
                    c = controller(a.port + 1, db, log)
                    restarts += 1
                    continue
                if st["finished"] and c.poll() is not None:
                    break
                time.sleep(0.02)
            c.wait()
        finally:
            sim.terminate()
            sim.wait()

    ref, got = actions(ref_dir), actions(run_dir)
    applied = [x[3] for x in got if x[1] == "approval_applied"]
    pids = [x[3] for x in got if x[1] in ("approval_requested", "auto_applied")]
    conflicts = [x for x in got if x[1] == "apply_conflict"]
    finished = json.loads((ROOT / run_dir / "_truth" / "aggregates.json").read_text())["sim_minute"]
    end = json.loads((ROOT / ref_dir / "_truth" / "aggregates.json").read_text())["sim_minute"]
    same = ref == got
    first_diff = next((i for i, (x, y) in enumerate(zip(ref, got)) if x != y), None)
    checks = {
        "finished the scenario": finished == end,
        "no approval applied twice": len(applied) == len(set(applied)),
        "no proposal_id reused": len(pids) == len(set(pids)),
        "zero apply conflicts": not conflicts,
        "governance actions identical to uninterrupted run": same,
    }
    lines += [f"Kills (target sim minute → actual sim minute when SIGKILL landed): "
              + ", ".join(f"{t // 1440}d{(t % 1440) // 60:02d}:{t % 60:02d}→{m // 1440}d{(m % 1440) // 60:02d}:{m % 60:02d}"
                          for t, m in kills), "",
              f"Governance actions: reference {len(ref)}, crashed run {len(got)}.", ""]
    lines += [f"- {'PASS' if ok else 'FAIL'} — {k}" for k, ok in checks.items()]
    if not same:
        lines += ["", f"First divergence at index {first_diff}:", f"- reference: {ref[first_diff] if first_diff is not None and first_diff < len(ref) else None}",
                  f"- crashed:   {got[first_diff] if first_diff is not None and first_diff < len(got) else None}"]
    restarts_logged = subprocess.run(["sqlite3", str(ROOT / db), "SELECT ts, summary FROM decisions WHERE kind IN "
                                      "('restart','reconcile') ORDER BY id"], capture_output=True, text=True).stdout
    lines += ["", "Restart/reconcile records from the controller's own audit store:", "", "```", restarts_logged.strip(), "```"]
    (ROOT / "reports").mkdir(exist_ok=True)
    (ROOT / "reports" / "crash_recovery.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    sys.exit(0 if all(checks.values()) else 1)


if __name__ == "__main__":
    main()
