"""The controller's only durable state: one SQLite file (WAL mode).

Two kinds of state live here:

* **Learned state** (aggregates + per-module state machines) is one compressed JSON document in
  `learn_state`. It is rewritten in the *same transaction* as the ingest watermark and as any
  proposal rows created by a decision, so after a crash the store always describes a consistent
  point: "chunks <= N ingested, these decisions taken, these HTTP intents outstanding".
* **Audit/decision records** are ordinary tables (`decisions`, `proposals`, `config_versions`,
  `alerts`, `metrics`, `cohorts`, `credit_spend`) so they can be queried with plain SQL.

In production this schema moves to the Postgres SRE already runs; nothing here needs SQLite.
"""
from __future__ import annotations

import json
import sqlite3
import zlib
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS learn_state (id INTEGER PRIMARY KEY CHECK (id = 1), blob BLOB NOT NULL,
    watermark_chunk INTEGER NOT NULL, sim_minute INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS chunks_seen (chunk INTEGER PRIMARY KEY, kind TEXT, until_minute INTEGER,
    events INTEGER, first_event_id INTEGER, last_event_id INTEGER);
CREATE TABLE IF NOT EXISTS config_versions (wf TEXT, version INTEGER, actor TEXT, changed_dims TEXT,
    proposal_id TEXT, approval_id TEXT, sim_minute INTEGER, ts TEXT, event_id INTEGER,
    PRIMARY KEY (wf, version));
CREATE TABLE IF NOT EXISTS config_snapshots (wf TEXT, version INTEGER, actor TEXT, config TEXT NOT NULL,
    PRIMARY KEY (wf, version));
CREATE TABLE IF NOT EXISTS proposals (proposal_id TEXT PRIMARY KEY, wf TEXT NOT NULL, path TEXT NOT NULL,
    status TEXT NOT NULL, urgent INTEGER NOT NULL DEFAULT 0, base_version INTEGER, dims TEXT,
    change_summary TEXT, rationale TEXT, impact_score REAL, affected_leads INTEGER,
    diff TEXT, dry_run TEXT, evidence TEXT, config TEXT, parts TEXT,
    approval_id TEXT, created_minute INTEGER, decided_minute INTEGER, applied_minute INTEGER,
    applied_version INTEGER, reasons TEXT, note TEXT);
CREATE INDEX IF NOT EXISTS proposals_wf ON proposals (wf, status);
CREATE TABLE IF NOT EXISTS decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, sim_minute INTEGER, ts TEXT,
    wf TEXT, module TEXT, kind TEXT, summary TEXT, detail TEXT, proposal_id TEXT);
CREATE INDEX IF NOT EXISTS decisions_wf ON decisions (wf, sim_minute);
CREATE TABLE IF NOT EXISTS alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, sim_minute INTEGER, ts TEXT,
    wf TEXT, severity TEXT, code TEXT, message TEXT, resolved_minute INTEGER);
CREATE TABLE IF NOT EXISTS metrics (id INTEGER PRIMARY KEY AUTOINCREMENT, wall_ts REAL, sim_minute INTEGER,
    events INTEGER, ingest_s REAL, decide_s REAL, advance_s REAL, events_per_s REAL, credits REAL,
    pending_approvals INTEGER, rss_mb REAL, decision_lag_min REAL, state_kb REAL);
CREATE TABLE IF NOT EXISTS cohorts (wf TEXT, cohort_id TEXT, where_json TEXT, first_minute INTEGER,
    last_minute INTEGER, PRIMARY KEY (wf, cohort_id));
CREATE TABLE IF NOT EXISTS credit_spend (id INTEGER PRIMARY KEY AUTOINCREMENT, sim_minute INTEGER, wf TEXT,
    kind TEXT, credits REAL, ref TEXT);
"""


def dumps(o) -> str:
    return json.dumps(o, separators=(",", ":"), sort_keys=True, default=str)


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self._in_tx = False

    @contextmanager
    def tx(self):
        """One atomic unit. Nested use joins the outer transaction."""
        if self._in_tx:
            yield self.db
            return
        self.db.execute("BEGIN IMMEDIATE")
        self._in_tx = True
        try:
            yield self.db
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        finally:
            self._in_tx = False

    def begin(self):
        """Explicit long transaction for the decision phase; persist points call checkpoint()."""
        if not self._in_tx:
            self.db.execute("BEGIN IMMEDIATE")
            self._in_tx = True

    def commit(self):
        if self._in_tx:
            self.db.execute("COMMIT")
            self._in_tx = False

    # meta ---------------------------------------------------------------------------
    def meta(self, k: str, default=None):
        r = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return json.loads(r[0]) if r else default

    def set_meta(self, k: str, v):
        self.db.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, dumps(v)))

    # learned state --------------------------------------------------------------------
    def load_state(self):
        r = self.db.execute("SELECT blob, watermark_chunk, sim_minute FROM learn_state WHERE id=1").fetchone()
        if not r:
            return None, -1, None
        return json.loads(zlib.decompress(r[0])), r[1], r[2]

    def save_state(self, state: dict, watermark: int, sim_minute: int) -> int:
        blob = zlib.compress(dumps(state).encode(), 3)
        self.db.execute("INSERT INTO learn_state(id, blob, watermark_chunk, sim_minute) VALUES(1,?,?,?) "
                        "ON CONFLICT(id) DO UPDATE SET blob=excluded.blob, watermark_chunk=excluded.watermark_chunk, "
                        "sim_minute=excluded.sim_minute", (blob, watermark, sim_minute))
        return len(blob)

    # audit -----------------------------------------------------------------------------
    def decision(self, sim_minute: int, ts: str, wf: str | None, module: str, kind: str, summary: str,
                 detail: dict | None = None, proposal_id: str | None = None) -> int:
        cur = self.db.execute("INSERT INTO decisions(sim_minute, ts, wf, module, kind, summary, detail, proposal_id) "
                              "VALUES(?,?,?,?,?,?,?,?)", (sim_minute, ts, wf, module, kind, summary,
                                                          dumps(detail or {}), proposal_id))
        return cur.lastrowid

    def alert(self, sim_minute: int, ts: str, wf: str | None, severity: str, code: str, message: str):
        open_ = self.db.execute("SELECT id FROM alerts WHERE wf IS ? AND code=? AND resolved_minute IS NULL",
                                (wf, code)).fetchone()
        if open_:
            return open_[0]
        return self.db.execute("INSERT INTO alerts(sim_minute, ts, wf, severity, code, message) VALUES(?,?,?,?,?,?)",
                               (sim_minute, ts, wf, severity, code, message)).lastrowid

    def resolve_alert(self, sim_minute: int, wf: str | None, code: str):
        self.db.execute("UPDATE alerts SET resolved_minute=? WHERE wf IS ? AND code=? AND resolved_minute IS NULL",
                        (sim_minute, wf, code))

    def rows(self, sql: str, *args) -> list[dict]:
        cur = self.db.execute(sql, args)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
