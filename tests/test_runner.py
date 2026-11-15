from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from lakeprune.config import load_config
from lakeprune.models import QuerySpecError
from lakeprune.runner import collect_stats, run_audit
from lakeprune.utils import canonical_json, sha256_bytes

from conftest import PROJECT_ROOT, write_config, write_data

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

def _drift_readings_config(tmp_path: Path) -> Path:
    """One file hides a float->string TYPE_DRIFT on value; one drifts a partition key."""
    columns = {"station_id": "string", "value": "float", "dt": "date"}
    root = tmp_path / "lake" / "readings"
    files = [
        ("dt=2024-01-02/part-0001.csv", {"dt": "2024-01-02"},
         [{"station_id": "s1", "value": 5.0}, {"station_id": "s2", "value": "hot"}],
         {"station_id": {"type": "string", "min": "s1", "max": "s2", "null_count": 0},
          "value": {"type": "float", "min": 5.0, "max": 5.0, "null_count": 0}}),
        ("dt=2024-01-02/dt=2024-01-03/part-0001.csv", {"dt": "2024-01-03"},
         [{"station_id": "s1", "value": 1.5}],
         {"station_id": {"type": "string", "min": "s1", "max": "s1", "null_count": 0},
          "value": {"type": "float", "min": 1.5, "max": 1.5, "null_count": 0}}),
    ]
    entries = []
    for rel_path, partition, rows, stats in files:
        data = write_data(root / rel_path, rows, list(columns))
        entries.append({"path": rel_path, "partition": partition, "row_count": len(rows),
                        "size_bytes": len(data), "sha256": sha256_bytes(data), "columns": stats})
    manifest = {"table": "readings", "format": "csv", "partition_columns": ["dt"],
                "columns": columns, "partitions": [item[1] for item in files], "files": entries}
    (root / "_manifest.json").write_text(canonical_json(manifest), encoding="utf-8")
    queries = [
        {"query_id": "q_readings_hot_day", "table": "readings", "select": ["value"],
         "expected_columns": ["value"], "where": "dt >= '2024-01-02' AND value > 40.0"},
        {"query_id": "q_readings_calm_day", "table": "readings", "select": ["station_id"],
         "expected_columns": ["station_id"], "where": "station_id = 's1'"},
        {"query_id": "q_readings_not_hot", "table": "readings", "select": ["value"],
         "expected_columns": ["value"], "where": "dt >= '2024-01-02' AND NOT value > 40.0"},
        {"query_id": "q_readings_in_or_null", "table": "readings", "select": ["value"],
         "expected_columns": ["value"], "where": "value IN (40.0, 41.0) OR station_id IS NULL"},
    ]
    return write_config(tmp_path, tmp_path / "lake", queries, ["readings"])

def test_drift_propagation_tracks_prune_path(tmp_path: Path):
    config = load_config(_drift_readings_config(tmp_path))
    result = run_audit(config, fail_on="high")
    assert result.exit_code == 2  # existing TYPE_DRIFT risks still gate the run
    plans = {plan["query_id"]: plan for plan in result.report["queries"]}
    hot = {item["path"]: item for item in plans["q_readings_hot_day"]["files"]}
    drifted = hot["dt=2024-01-02/part-0001.csv"]
    assert drifted["kept"] is True and drifted["stats_decision"] == "unknown"
    prop = drifted["drift_propagation"]
    assert prop["propagated_columns"] == ["value"] and prop["non_propagated_columns"] == []
    assert prop["conclusion"] == {"pruning_trustworthy": False, "retained_only_as_unknown": True}
    assert prop["decisions"]["trusted_without_drift"] == "must_not_match"
    leaf = prop["leaves"][0]
    assert leaf["fragment"] == "value > 40.0" and leaf["kind"] == "comparison"
    assert leaf["effective_values"] == [None, False, True] and leaf["trusted_values"] == [False]
    branch = leaf["branches"][0]
    assert (branch["operator"], branch["side"], branch["sibling_affected"]) == ("and", "right", False)
    assert branch["sibling_fragments"] == ["dt >= '2024-01-02'"]
    assert prop["asymmetric_nodes"][0]["operator"] == "and"
    exact = hot["dt=2024-01-02/dt=2024-01-03/part-0001.csv"]["drift_propagation"]
    assert exact["propagated_columns"] == [] and exact["conclusion"]["pruning_trustworthy"] is True
    assert exact["non_propagated_columns"][0]["reason"] == "partition_exact"
    assert exact["non_propagated_columns"][0]["exact_leaves"][0]["exact_values"] == [True]
    negated = {item["path"]: item for item in plans["q_readings_not_hot"]["files"]}
    negated = negated["dt=2024-01-02/part-0001.csv"]["drift_propagation"]
    not_evidence = negated["leaves"][0]["not_evidence"]
    assert negated["leaves"][0]["under_not"] is True
    assert not_evidence["degraded_subtree_values"] == [None, False, True]
    assert not_evidence["contributed_to_parent"] == [None, False, True]
    assert negated["conclusion"]["pruning_trustworthy"] is True
    or_prop = {item["path"]: item for item in plans["q_readings_in_or_null"]["files"]}
    or_prop = or_prop["dt=2024-01-02/part-0001.csv"]["drift_propagation"]
    assert or_prop["leaves"][0]["kind"] == "in"
    assert or_prop["asymmetric_nodes"][0]["operator"] == "or"
    calm = plans["q_readings_calm_day"]
    assert all(item["drift_propagation"] is None for item in calm["files"])
    records = result.report["drift_propagation"]["records"]
    assert not [item for item in records if item["query_id"] == "q_readings_calm_day"]
    assert result.report["drift_propagation"]["summary"] == {
        "records": 5, "queries_affected": 3, "files_affected": 2,
        "propagated_columns": 3, "untrustworthy_retentions": 2}

def test_invalid_query_raises_stable_code(clean_config, tmp_path: Path):
    config = load_config(clean_config)
    data = yaml.safe_load(config.queries_path.read_text(encoding="utf-8"))
    data["queries"][0]["where"] = "amount IN ()"
    config.queries_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(QuerySpecError) as raised:
        run_audit(config, database=tmp_path / "bad.db", report_dir=tmp_path / "bad")
    assert raised.value.code == "E_EMPTY_IN"
