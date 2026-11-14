from __future__ import annotations

import sqlite3

import pytest

from lakeprune import storage

def test_ledger_schema_run_queries_and_round_trip(demo_audit):
    db = demo_audit["db"]
    with storage.connect(db, readonly=True) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"runs", "queries", "prune_results", "risks", "stats_checks"} <= tables
    runs = storage.list_runs(db)
    assert runs and runs[0]["query_count"] == 7 and runs[0]["high_risks"] == 3
    run_id = runs[0]["run_id"]
    plans, risks = storage.get_plans(db, run_id), storage.get_risks(db, run_id)
    checks, prune_rows = storage.get_stats_checks(db, run_id), storage.get_prune_results(db, run_id)
    assert len(plans) == 7 and plans[0]["plan"]["files"]
    assert len(risks) == 15 and risks[0]["level"] == "high"
    assert len(checks) == 45 and len(prune_rows) == 30
    assert {"partition_decision", "stats_decision", "reason"} <= set(prune_rows[0])
    run = storage.get_run(db, run_id)
    assert run is not None and run["exit_code"] == 2
    assert storage.get_report(db, run_id) == demo_audit["result"].report
    summary = storage.run_summary(db, run_id)
    assert summary["risks"] and summary["stats_checks"] and summary["prune_results"]
    assert storage.get_run(db, "does-not-exist") is None
    assert storage.get_report(db, "does-not-exist") is None
    assert storage.list_runs(db / "missing.db") == []

def test_readonly_connection_rejects_writes_and_run_ids_are_unique(demo_audit):
    with storage.connect(demo_audit["db"], readonly=True) as connection:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("INSERT INTO runs (run_id, created_at, config_path, lake_root, "
                               "manifest_fingerprint, query_count, fail_on, exit_code, report_json) "
                               "VALUES ('x', 'x', 'x', 'x', 'x', 0, 'none', 0, '{}')")
    assert storage.new_run_id() != storage.new_run_id()
    assert storage.new_run_id().startswith("run-")
