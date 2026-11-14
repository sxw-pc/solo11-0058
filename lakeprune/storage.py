"""Local SQLite audit ledger with read-only query helpers."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Mapping

from lakeprune.models import Risk
from lakeprune.prune import QueryPlan
from lakeprune.stats_audit import StatsAuditResult
from lakeprune.utils import canonical_json, utc_now_iso

_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS runs (
      run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT NOT NULL,
      config_path TEXT NOT NULL, lake_root TEXT NOT NULL, manifest_fingerprint TEXT NOT NULL,
      query_count INTEGER NOT NULL, fail_on TEXT NOT NULL, exit_code INTEGER NOT NULL,
      report_json TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS queries (
      run_id TEXT NOT NULL, query_id TEXT NOT NULL, table_name TEXT NOT NULL,
      plan_json TEXT NOT NULL, PRIMARY KEY (run_id, query_id),
      FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE)""",
    """CREATE TABLE IF NOT EXISTS prune_results (
      run_id TEXT NOT NULL, query_id TEXT NOT NULL, file_path TEXT NOT NULL,
      partition_decision TEXT NOT NULL, stats_decision TEXT NOT NULL, kept INTEGER NOT NULL,
      size_bytes INTEGER NOT NULL, reason TEXT NOT NULL,
      PRIMARY KEY (run_id, query_id, file_path),
      FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE)""",
    """CREATE TABLE IF NOT EXISTS risks (
      run_id TEXT NOT NULL, risk_id TEXT NOT NULL, code TEXT NOT NULL, level TEXT NOT NULL,
      query_id TEXT, table_name TEXT NOT NULL, file_path TEXT, evidence_json TEXT NOT NULL,
      description TEXT NOT NULL, PRIMARY KEY (run_id, risk_id),
      FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE)""",
    """CREATE TABLE IF NOT EXISTS stats_checks (
      run_id TEXT NOT NULL, table_name TEXT NOT NULL, file_path TEXT NOT NULL,
      column_name TEXT NOT NULL, status TEXT NOT NULL, declared_json TEXT NOT NULL,
      actual_json TEXT NOT NULL, message TEXT NOT NULL,
      PRIMARY KEY (run_id, table_name, file_path, column_name),
      FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE)""",
    "CREATE INDEX IF NOT EXISTS idx_risks_run_level ON risks(run_id, level)",
    "CREATE INDEX IF NOT EXISTS idx_prune_run ON prune_results(run_id, query_id)",
]
SCHEMA = ";\n".join(_SCHEMA) + ";"

_INSERT_RUN = """INSERT INTO runs (run_id, started_at, finished_at, config_path, lake_root,
  manifest_fingerprint, query_count, fail_on, exit_code, report_json)
  VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
_INSERT_QUERY = "INSERT INTO queries (run_id, query_id, table_name, plan_json) VALUES (?, ?, ?, ?)"
_INSERT_PRUNE = """INSERT INTO prune_results (run_id, query_id, file_path, partition_decision,
  stats_decision, kept, size_bytes, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"""
_INSERT_RISK = """INSERT INTO risks (run_id, risk_id, code, level, query_id,
  table_name, file_path, evidence_json, description) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"""
_INSERT_CHECK = """INSERT INTO stats_checks (run_id, table_name, file_path, column_name,
  status, declared_json, actual_json, message) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"""

def connect(db_path: str | Path, *, readonly: bool = False) -> sqlite3.Connection:
    path = Path(db_path)
    if not readonly:
        path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    if readonly:
        connection.execute("PRAGMA query_only = ON")
    return connection

@contextmanager
def _session(db_path: str | Path, *, readonly: bool = False):
    connection = connect(db_path, readonly=readonly)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def init_db(db_path: str | Path) -> None:
    with _session(db_path) as connection:
        connection.executescript(SCHEMA)

def new_run_id() -> str:
    stamp = utc_now_iso().replace("-", "").replace(":", "")
    return f"run-{stamp}-{uuid.uuid4().hex[:8]}"

def _rows(connection: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(sql, params).fetchall()]

def record_run(
    db_path: str | Path,
    run_id: str,
    config_path: str,
    lake_root: str,
    report: Mapping[str, Any],
    plans: Iterable[QueryPlan],
    risks: Iterable[Risk],
    audit: StatsAuditResult,
    exit_code: int,
    fail_on: str,
) -> None:
    init_db(db_path)
    plans, risks = list(plans), list(risks)
    started_at = utc_now_iso()
    with _session(db_path) as connection:
        connection.execute(_INSERT_RUN, (
            run_id, started_at, utc_now_iso(), config_path, lake_root,
            report["manifest_fingerprint"], len(plans), fail_on, exit_code,
            canonical_json(dict(report)),
        ))
        for plan in plans:
            query = plan.query
            connection.execute(_INSERT_QUERY, (
                run_id, query.query_id, query.table, canonical_json(plan.to_dict()),
            ))
            for item in plan.files:
                reason = "; ".join(item.partition_reasons + item.stats_reasons)
                connection.execute(_INSERT_PRUNE, (
                    run_id, query.query_id, item.path, item.partition_decision,
                    item.stats_decision, int(item.kept), item.size_bytes, reason,
                ))
        for risk in risks:
            connection.execute(_INSERT_RISK, (
                run_id, risk.risk_id, risk.code, risk.level, risk.query_id, risk.table,
                risk.file_path, canonical_json(dict(risk.evidence)), risk.description,
            ))
        for check in audit.checks:
            connection.execute(_INSERT_CHECK, (
                run_id, check.table, check.file_path, check.column, check.status,
                canonical_json(dict(check.declared)), canonical_json(dict(check.actual)),
                check.message,
            ))

def list_runs(db_path: str | Path, limit: int = 100) -> list[dict[str, Any]]:
    if not Path(db_path).is_file():
        return []
    try:
        with _session(db_path, readonly=True) as connection:
            rows = _rows(connection, """
                SELECT runs.*,
                  (SELECT COUNT(*) FROM risks WHERE risks.run_id = runs.run_id AND risks.level = 'high') AS high_risks,
                  (SELECT COUNT(*) FROM risks WHERE risks.run_id = runs.run_id AND risks.level = 'warn') AS warn_risks
                FROM runs ORDER BY started_at DESC, run_id DESC LIMIT ?
                """, (limit,))
    except sqlite3.Error:
        return []
    for row in rows:
        row.pop("report_json", None)
    return rows

def get_run(db_path: str | Path, run_id: str, include_report: bool = False) -> dict[str, Any] | None:
    if not Path(db_path).is_file():
        return None
    try:
        with _session(db_path, readonly=True) as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    data = dict(row)
    if include_report:
        data["report"] = json.loads(data["report_json"])
    data.pop("report_json", None)
    return data

def get_report(db_path: str | Path, run_id: str) -> dict[str, Any] | None:
    if not Path(db_path).is_file():
        return None
    try:
        with _session(db_path, readonly=True) as connection:
            row = connection.execute("SELECT report_json FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    except sqlite3.Error:
        return None
    return None if row is None else json.loads(row["report_json"])

def get_plans(db_path: str | Path, run_id: str) -> list[dict[str, Any]]:
    with _session(db_path, readonly=True) as connection:
        rows = _rows(connection, "SELECT query_id, table_name, plan_json FROM queries WHERE run_id = ? ORDER BY query_id", (run_id,))
    return [{"query_id": row["query_id"], "table_name": row["table_name"], "plan": json.loads(row["plan_json"])} for row in rows]

def get_risks(db_path: str | Path, run_id: str) -> list[dict[str, Any]]:
    with _session(db_path, readonly=True) as connection:
        rows = _rows(connection, """
            SELECT risk_id, code, level, query_id, table_name, file_path, evidence_json, description
            FROM risks WHERE run_id = ?
            ORDER BY CASE level WHEN 'high' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END,
                     code, query_id, table_name, file_path, risk_id
            """, (run_id,))
    for row in rows:
        row["evidence"] = json.loads(row.pop("evidence_json"))
    return rows

def get_prune_results(db_path: str | Path, run_id: str) -> list[dict[str, Any]]:
    with _session(db_path, readonly=True) as connection:
        return _rows(connection, "SELECT * FROM prune_results WHERE run_id = ? ORDER BY query_id, file_path", (run_id,))

def get_stats_checks(db_path: str | Path, run_id: str) -> list[dict[str, Any]]:
    with _session(db_path, readonly=True) as connection:
        rows = _rows(connection, "SELECT * FROM stats_checks WHERE run_id = ? ORDER BY table_name, file_path, column_name", (run_id,))
    for row in rows:
        row["declared"] = json.loads(row.pop("declared_json"))
        row["actual"] = json.loads(row.pop("actual_json"))
    return rows

def run_summary(db_path: str | Path, run_id: str) -> dict[str, Any] | None:
    run = get_run(db_path, run_id)
    if run is None:
        return None
    run["risks"] = get_risks(db_path, run_id)
    run["stats_checks"] = get_stats_checks(db_path, run_id)
    run["prune_results"] = get_prune_results(db_path, run_id)
    return run
