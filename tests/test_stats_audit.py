from __future__ import annotations

from pathlib import Path

from lakeprune.config import load_config
from lakeprune.demo_data import write_lake
from lakeprune.manifest import load_lake
from lakeprune.stats_audit import (STATUS_DRIFT, STATUS_MISSING, STATUS_MISMATCH, STATUS_OK,
                                   audit_stats, read_data_rows, recompute_column)

from conftest import PROJECT_ROOT

def _demo_audit():
    config = load_config(PROJECT_ROOT / "configs" / "demo.yaml")
    manifests, issues, fingerprint = load_lake(config.lake_root, config.tables)
    return manifests, issues, audit_stats(config.lake_root, manifests), fingerprint

def test_recompute_csv_jsonl_and_type_drift(tmp_path: Path):
    rows = [{"amount": "1", "name": "a"}, {"amount": "", "name": "b"}, {"amount": "3", "name": "c"}]
    assert recompute_column(rows, "csv", "amount", "int") == {"type": "int", "min": 1, "max": 3, "null_count": 1}
    assert recompute_column(rows, "csv", "name", "string")["null_count"] == 0
    drift = recompute_column([{"battery_pct": "20"}, {"battery_pct": "retry"}], "csv", "battery_pct", "int")
    assert drift["type_drift_count"] == 1 and drift["min"] == 20 and drift["max"] == 20
    csv_path = tmp_path / "a.csv"
    csv_path.write_text("id,name\n1,alpha\n2,beta\n", encoding="utf-8")
    assert read_data_rows(csv_path, "csv") == ([{"id": "1", "name": "alpha"}, {"id": "2", "name": "beta"}], ["id", "name"])
    jsonl_path = tmp_path / "b.jsonl"
    jsonl_path.write_text('{"id":1}\n{"id":2}\n', encoding="utf-8")
    assert read_data_rows(jsonl_path, "jsonl") == ([{"id": 1}, {"id": 2}], ["id"])

def test_demo_status_classes_trust_and_sorting():
    manifests, issues, audit, fingerprint = _demo_audit()
    assert len(fingerprint) == 64
    assert audit.summary["status_counts"] == {STATUS_DRIFT: 1, STATUS_MISSING: 3, STATUS_MISMATCH: 1, STATUS_OK: 40}
    assert audit.summary["files_checked"] == 11 and audit.summary["columns_checked"] == 45
    assert "value" in audit.untrusted_columns("readings", "dt=2024-01-02/region=r1/part-0001.csv")
    assert "battery_pct" in audit.untrusted_columns("events", "dt=2024-01-02/region=r1/part-0001.csv")
    assert "humidity" in audit.untrusted_columns("readings", "dt=2024-01-02/region=r2/part-0001.csv")
    assert "notes" in audit.untrusted_columns("stations", "data.jsonl")
    assert [issue.code for issue in issues] == ["EMPTY_PARTITION"]
    missing = [check for check in audit.checks if check.status == STATUS_MISSING]
    assert {(check.table, check.column) for check in missing} == {
        ("events", "battery_pct"), ("readings", "humidity"), ("stations", "notes")}
    _, _, second, _ = _demo_audit()
    assert [(c.table, c.file_path, c.column) for c in audit.checks] == [(c.table, c.file_path, c.column) for c in second.checks]
    assert audit.summary == second.summary

def test_tampered_data_file_is_excluded_from_trust(tmp_path: Path):
    lake = tmp_path / "lake"
    write_lake(lake)
    target = lake / "readings" / "dt=2024-01-03" / "region=r1" / "part-0001.csv"
    target.write_bytes(target.read_bytes().replace(b"20.0", b"99.0"))
    manifests, _, _ = load_lake(lake, ["readings", "events", "stations"])
    audit = audit_stats(lake, manifests)
    assert audit.summary["untrusted_columns"] > 0
    assert "dt=2024-01-03/region=r1/part-0001.csv" in audit.untrusted["readings"]
    messages = [check.message for check in audit.checks if check.file_path.endswith("part-0001.csv")]
    assert any("sha256" in message or "declared" in message for message in messages)
