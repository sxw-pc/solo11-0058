from __future__ import annotations

import json
from pathlib import Path

from lakeprune.config import load_config
from lakeprune.predicate import parse_predicate
from lakeprune.predicate_ast import are_complementary, complementary_pairs
from lakeprune.report_json import render_json
from lakeprune.runner import run_audit

from conftest import make_table, write_config

SCHEMA = {"id": "string", "v": "int"}

def _query(query_id: str, where: str, table: str = "t") -> dict:
    return {"query_id": query_id, "table": table, "select": ["id"],
            "expected_columns": ["id"], "where": where}

def _run(tmp_path: Path, queries: list[dict], files: list[dict], columns: dict | None = None,
         partition_columns: list[str] | None = None, name: str = "run"):
    lake = tmp_path / "lake"
    make_table(lake, "t", columns or SCHEMA, partition_columns or [], files)
    config = load_config(write_config(tmp_path, lake, queries, ["t"]))
    return run_audit(config, database=tmp_path / f"{name}.db", report_dir=tmp_path / name)

def _entry(section: dict, path: str) -> dict:
    matches = [item for item in section["files"] if item["path"] == path]
    assert len(matches) == 1
    return matches[0]

def test_complementary_certain_decisions_form_true_conflict(tmp_path: Path):
    result = _run(tmp_path, [_query("q_high", "v >= 10"), _query("q_low", "v < 10")],
                  [{"path": "part.csv", "partition": {},
                    "rows": [{"id": "a", "v": 10}, {"id": "b", "v": 20}]}])
    section = result.report["consistency"]
    assert section["summary"] == {"files": 1, "queries": 2, "conflicts": 1, "hints": 0,
                                  "files_with_conflicts": 1, "files_with_hints": 0}
    conflict = section["conflicts"][0]
    assert conflict["finding_id"].startswith("C-") and conflict["cause"] == ""
    assert conflict["table"] == "t" and conflict["path"] == "part.csv" and conflict["column"] == "v"
    assert conflict["query_ids"] == ["q_high", "q_low"]
    assert conflict["decisions"] == {"q_high": "must_match", "q_low": "must_not_match"}
    assert conflict["predicates"] == {"q_high": "v >= 10", "q_low": "v < 10"}
    assert conflict["evidence"]["statistics"] == {"type": "int", "min": 10, "max": 20, "null_count": 0}
    assert conflict["evidence"]["full_predicates"] == {"q_high": "v >= 10", "q_low": "v < 10"}
    assert conflict["recommendation"] == (
        "queries q_high and q_low apply complementary predicates to column 'v' yet reach "
        "must_match and must_not_match on the same file; the divergence is certain and cannot "
        "be explained by partition values, so verify the column statistics and predicate "
        "boundaries before trusting either plan")
    assert result.exit_code == 0 and result.report["summary"]["risk_counts"]["high"] == 0

def test_partition_pruning_is_no_decision_and_never_a_conflict(tmp_path: Path):
    columns = {"id": "string", "v": "int", "dt": "date"}
    files = [
        {"path": "dt=2024-01-01/part.csv", "partition": {"dt": "2024-01-01"}, "rows": [{"id": "a", "v": 10}]},
        {"path": "dt=2024-01-02/part.csv", "partition": {"dt": "2024-01-02"}, "rows": [{"id": "b", "v": 20}]},
    ]
    queries = [_query("q_first", "dt = '2024-01-01' AND v >= 10"),
               _query("q_second", "dt = '2024-01-02' AND v < 10")]
    result = _run(tmp_path, queries, files, columns, ["dt"])
    section = result.report["consistency"]
    assert section["conflicts"] == [] and section["hints"] == []
    observations = {obs["query_id"]: obs for obs in _entry(section, "dt=2024-01-01/part.csv")["observations"]}
    assert observations["q_first"]["stats_decision"] == "must_match"
    assert observations["q_second"]["partition_decision"] == "pruned"
    assert observations["q_second"]["stats_decision"] == "skipped_partition"
    assert all(obs["stats_decision"] != "unknown" for obs in observations.values())

def test_unknown_decision_downgrades_to_hint(tmp_path: Path):
    columns = {"id": "string", "v": "int", "w": "int"}
    files = [{"path": "part.csv", "partition": {}, "missing_stats": {"w"},
              "rows": [{"id": "a", "v": 10, "w": 1}, {"id": "b", "v": 20, "w": 2}]}]
    queries = [_query("q_cover", "v >= 10 AND w = 1"), _query("q_drop", "v < 10")]
    result = _run(tmp_path, queries, files, columns)
    section = result.report["consistency"]
    assert section["conflicts"] == [] and len(section["hints"]) == 1
    hint = section["hints"][0]
    assert hint["finding_id"].startswith("H-") and hint["cause"] == "unknown_decision"
    assert hint["decisions"] == {"q_cover": "unknown", "q_drop": "must_not_match"}
    assert hint["predicates"] == {"q_cover": "v >= 10", "q_drop": "v < 10"}
    assert hint["recommendation"] == (
        "queries q_cover and q_drop apply complementary predicates to column 'v' and diverge "
        "on the same file, but at least one decision is unknown; recompute or repair the "
        "column statistics before treating the divergence as a confirmed conflict")

def test_untrusted_statistics_downgrade_to_hint(tmp_path: Path):
    columns = {"id": "string", "v": "int", "w": "int"}
    files = [{"path": "part.csv", "partition": {}, "missing_stats": {"w"},
              "rows": [{"id": "a", "v": 10, "w": 1}, {"id": "b", "v": 20, "w": 2}]}]
    queries = [_query("q_cover", "v >= 10"), _query("q_drop", "v < 10 AND w = 1")]
    result = _run(tmp_path, queries, files, columns)
    section = result.report["consistency"]
    assert section["conflicts"] == [] and len(section["hints"]) == 1
    hint = section["hints"][0]
    assert hint["cause"] == "untrusted_statistics"
    assert hint["decisions"] == {"q_cover": "must_match", "q_drop": "must_not_match"}
    assert hint["evidence"]["untrusted_columns"] == {"q_cover": [], "q_drop": ["w"]}

def test_non_complementary_divergence_is_not_a_conflict(tmp_path: Path):
    result = _run(tmp_path, [_query("q_keep", "v >= 10"), _query("q_drop", "v <= 5")],
                  [{"path": "part.csv", "partition": {},
                    "rows": [{"id": "a", "v": 10}, {"id": "b", "v": 20}]}])
    section = result.report["consistency"]
    assert section["conflicts"] == [] and section["hints"] == []
    decisions = {obs["query_id"]: obs["stats_decision"]
                 for obs in _entry(section, "part.csv")["observations"]}
    assert decisions == {"q_keep": "must_match", "q_drop": "must_not_match"}

def test_consistency_section_is_byte_stable_across_runs(tmp_path: Path):
    queries = [_query("q_high", "v >= 10"), _query("q_low", "v < 10")]
    files = [{"path": "part.csv", "partition": {},
              "rows": [{"id": "a", "v": 10}, {"id": "b", "v": 20}]}]
    lake = tmp_path / "lake"
    make_table(lake, "t", SCHEMA, [], files)
    config_path = write_config(tmp_path, lake, queries, ["t"])
    config = load_config(config_path)
    first = run_audit(config, database=tmp_path / "a.db", report_dir=tmp_path / "a")
    second = run_audit(config, database=tmp_path / "b.db", report_dir=tmp_path / "b")
    assert first.report["consistency"] == second.report["consistency"]
    assert render_json(first.report) == render_json(second.report)
    assert json.loads(render_json(first.report))["consistency"]["summary"]["conflicts"] == 1

def test_complementary_predicate_detection():
    schema = {"v": "int", "s": "string", "b": "bool"}
    greater = parse_predicate("v > 40", schema)
    not_greater = parse_predicate("NOT (v > 40)", schema)
    less_equal = parse_predicate("v <= 40", schema)
    other_literal = parse_predicate("v <= 41", schema)
    is_null = parse_predicate("s IS NULL", schema)
    not_null = parse_predicate("s IS NOT NULL", schema)
    boolean = parse_predicate("b = true", schema)
    not_boolean = parse_predicate("b != true", schema)
    assert are_complementary(greater, not_greater)
    assert are_complementary(greater, less_equal)
    assert are_complementary(is_null, not_null)
    assert are_complementary(boolean, not_boolean)
    assert not are_complementary(greater, other_literal)
    assert not are_complementary(greater, boolean)
    pairs = complementary_pairs(parse_predicate("v > 40 AND s IS NULL", schema),
                                parse_predicate("v <= 40 AND s IS NOT NULL", schema))
    assert [column for column, _, _ in pairs] == ["s", "v"]

def test_demo_report_has_sorted_consistency_section(demo_report):
    section = demo_report["consistency"]
    keys = [(entry["table"], entry["path"]) for entry in section["files"]]
    assert keys == sorted(keys) and len(keys) == 11
    decisions_seen = set()
    for entry in section["files"]:
        query_ids = [obs["query_id"] for obs in entry["observations"]]
        assert query_ids == sorted(query_ids)
        for obs in entry["observations"]:
            decisions_seen.add(obs["stats_decision"])
            if obs["partition_decision"] == "pruned":
                assert obs["stats_decision"] == "skipped_partition"
            else:
                assert obs["stats_decision"] != "skipped_partition"
    assert {"unknown", "skipped_partition", "must_match", "must_not_match"} <= decisions_seen
    assert section["conflicts"] == [] and section["hints"] == []
    assert section["summary"] == {"files": 11, "queries": 7, "conflicts": 0, "hints": 0,
                                  "files_with_conflicts": 0, "files_with_hints": 0}
    assert render_json(demo_report) == render_json(json.loads(render_json(demo_report)))
