from __future__ import annotations

import json
from pathlib import Path

import pytest

from lakeprune.manifest import load_manifest
from lakeprune.models import ManifestError
from lakeprune.partition import evaluate_partition, parse_partition_path, partition_label
from lakeprune.predicate import parse_predicate

TYPES = {"dt": "date", "region": "string"}
SCHEMA = {"dt": "date", "region": "string", "amount": "int"}

def test_parse_multilevel_partition_null_sentinel_and_issues():
    parsed = parse_partition_path("dt=2024-01-02/region=r1/part.csv", ("dt", "region"), TYPES, "t")
    assert parsed.values == {"dt": "2024-01-02", "region": "r1"} and not parsed.issues
    null_partition = parse_partition_path("dt=2024-01-02/region=__HIVE_DEFAULT_PARTITION__/part.csv",
                                          ("dt", "region"), TYPES, "t")
    assert null_partition.values["region"] is None and null_partition.issues == ()
    missing = parse_partition_path("dt=2024-01-02/part.csv", ("dt", "region"), TYPES, "t")
    assert any(issue.code == "FULL_SCAN_RISK" for issue in missing.issues)
    drift = parse_partition_path("dt=2024-01-02/region=north/part.csv", ("dt", "region"),
                                 {"dt": "date", "region": "int"}, "t")
    assert any(issue.code == "TYPE_DRIFT" for issue in drift.issues)
    duplicate = parse_partition_path("dt=2024-01-01/dt=2024-01-02/region=r1/part.csv",
                                     ("dt", "region"), TYPES, "t")
    assert any("duplicate" in issue.description for issue in duplicate.issues)

def test_manifest_partition_mismatch_label_and_unpartitioned():
    parsed = parse_partition_path("dt=2024-01-02/region=r1/part.csv", ("dt", "region"), TYPES, "t",
                                  declared={"dt": "2024-01-03", "region": "r1"})
    assert any("disagrees" in issue.description for issue in parsed.issues)
    assert partition_label(parsed.values, ("dt", "region")) == "dt=2024-01-02;region=r1"
    assert partition_label({}, ()) == "unpartitioned"

def test_exact_partition_match_prune_three_valued_and_null_logic():
    expression = parse_predicate("region = 'r1'", SCHEMA)
    assert evaluate_partition(expression, {"region": "r1"}, ("region",)).decision == "match"
    miss = evaluate_partition(expression, {"region": "r2"}, ("region",))
    assert miss.decision == "pruned" and any("region" in reason for reason in miss.reasons)
    assert evaluate_partition(expression, {"region": None}, ("region",)).decision == "pruned"
    mixed = parse_predicate("region = 'r1' AND amount > 5", SCHEMA)
    assert evaluate_partition(mixed, {"region": "r1"}, ("region",)).decision == "undetermined"
    assert evaluate_partition(mixed, {"region": "r2"}, ("region",)).decision == "pruned"
    disjunction = parse_predicate("region = 'r1' OR amount > 5", SCHEMA)
    assert evaluate_partition(disjunction, {"region": "r2"}, ("region",)).decision == "undetermined"
    is_null = parse_predicate("region IS NULL", SCHEMA)
    assert evaluate_partition(is_null, {"region": None}, ("region",)).decision == "match"
    assert evaluate_partition(is_null, {"region": "r1"}, ("region",)).decision == "pruned"
    negated = parse_predicate("NOT region = 'r1'", SCHEMA)
    assert evaluate_partition(negated, {"region": "r2"}, ("region",)).decision == "match"
    assert evaluate_partition(negated, {"region": "r1"}, ("region",)).decision == "pruned"

def _write_manifest(lake: Path, path_value: str) -> None:
    table = lake / "bad"
    table.mkdir(parents=True, exist_ok=True)
    payload = {"table": "bad", "format": "csv", "partition_columns": [], "columns": {"id": "string"},
               "files": [{"path": path_value, "partition": {}, "row_count": 1, "size_bytes": 1,
                          "sha256": "0" * 64, "columns": {}}]}
    (table / "_manifest.json").write_text(json.dumps(payload), encoding="utf-8")

@pytest.mark.parametrize("unsafe", ["../outside.csv", "/tmp/outside.csv", "..\\outside.csv"])
def test_manifest_rejects_path_escape(tmp_path: Path, unsafe: str):
    _write_manifest(tmp_path, unsafe)
    with pytest.raises(ManifestError) as raised:
        load_manifest(tmp_path, "bad")
    assert raised.value.code in {"E_PATH_UNSAFE", "E_MANIFEST_FIELD"}

def test_manifest_missing_required_field(tmp_path: Path):
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "_manifest.json").write_text('{"table":"bad"}', encoding="utf-8")
    with pytest.raises(ManifestError) as raised:
        load_manifest(tmp_path, "bad")
    assert raised.value.code == "E_MANIFEST_FIELD"

def test_manifest_rejects_symlink_escape(tmp_path: Path):
    outside = tmp_path / "outside.csv"
    outside.write_text("id\nx\n", encoding="utf-8")
    (tmp_path / "bad").mkdir()
    link = tmp_path / "bad" / "link.csv"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")
    _write_manifest(tmp_path, "link.csv")
    with pytest.raises(ManifestError) as raised:
        load_manifest(tmp_path, "bad")
    assert raised.value.code == "E_PATH_UNSAFE"
