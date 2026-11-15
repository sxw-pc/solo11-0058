from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from lakeprune.config import load_config
from lakeprune.models import QuerySpecError
from lakeprune.runner import collect_stats, run_audit

from conftest import PROJECT_ROOT

def test_demo_report_structure_counts_and_plans(demo_report):
    assert demo_report["project"] == "湖裁验台"
    assert demo_report["summary"]["query_count"] == 7
    assert demo_report["summary"]["risk_counts"] == {"high": 3, "info": 0, "warn": 12}
    assert len(demo_report["manifest_fingerprint"]) == 64 and "generated_at" not in demo_report
    plans = {plan["query_id"]: plan for plan in demo_report["queries"]}
    assert plans["q_readings_hot_day"]["summary"]["candidate_files"] == 1
    assert plans["q_readings_null_humidity"]["summary"]["candidate_files"] == 1
    assert plans["q_events_battery"]["summary"]["candidate_files"] == 2
    assert plans["q_readings_window"]["summary"]["candidate_files"] == 4
    assert plans["q_readings_upper_region"]["summary"]["candidate_files"] == 0
    assert plans["q_stations_active"]["summary"]["full_scan"] is True

def test_query_plans_are_explainable(demo_report):
    hot = {plan["query_id"]: plan for plan in demo_report["queries"]}["q_readings_hot_day"]
    assert hot["predicate"]["rendered"].startswith("(") and hot["partitions"]["pruned"] == 2
    files = {item["path"]: item for item in hot["files"]}
    pruned = files["dt=2024-01-01/region=r1/part-0001.csv"]
    assert pruned["partition_decision"] == "pruned" and pruned["partition_reasons"]
    kept = files["dt=2024-01-02/region=r1/part-0001.csv"]
    assert kept["kept"] is True and kept["stats_decision"] == "unknown"
    assert kept["untrusted_columns"] == ["value"] and kept["would_declared_prune"] is True

def test_stats_validation_entrypoint():
    config = load_config(PROJECT_ROOT / "configs" / "demo.yaml")
    manifests, issues, audit, fingerprint = collect_stats(config)
    assert set(manifests) == {"readings", "events", "stations"}
    assert [issue.code for issue in issues] == ["EMPTY_PARTITION"]
    assert audit.summary["files_checked"] == 11 and len(fingerprint) == 64

def test_clean_fixture_exits_zero_with_high_gate(clean_config):
    config = load_config(clean_config)
    result = run_audit(config, fail_on="high")
    assert result.exit_code == 0 and result.report["summary"]["risk_counts"]["high"] == 0
    assert result.report["queries"][0]["summary"]["candidate_files"] == 1
    assert all(Path(path).exists() for path in (result.json_path, result.markdown_path, result.html_path))
    assert config.database.exists()

def test_high_risk_fails_none_override_passes_and_reports_are_stable(tmp_path: Path):
    config = load_config(PROJECT_ROOT / "configs" / "demo.yaml")
    first = run_audit(config, database=tmp_path / "a.db", report_dir=tmp_path / "a", fail_on="high")
    second = run_audit(config, database=tmp_path / "b.db", report_dir=tmp_path / "b", fail_on="none")
    assert first.exit_code == 2 and second.exit_code == 0 and first.report == second.report
    assert Path(first.json_path).read_bytes() == Path(second.json_path).read_bytes()
    assert Path(first.markdown_path).read_bytes() == Path(second.markdown_path).read_bytes()

def test_invalid_query_raises_stable_code(clean_config, tmp_path: Path):
    config = load_config(clean_config)
    data = yaml.safe_load(config.queries_path.read_text(encoding="utf-8"))
    data["queries"][0]["where"] = "amount IN ()"
    config.queries_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(QuerySpecError) as raised:
        run_audit(config, database=tmp_path / "bad.db", report_dir=tmp_path / "bad")
    assert raised.value.code == "E_EMPTY_IN"
