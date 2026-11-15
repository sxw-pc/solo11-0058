from __future__ import annotations

import json
import sqlite3

import pytest

from lakeprune.config import load_config
from lakeprune.replay import (
    CHANGED, FIELDS, NEWLY_PRUNED, NEWLY_UNTRUSTED, STILL_KEPT, UNCHANGED,
    ReplayError, replay_file,
)
from lakeprune.runner import run_audit

from conftest import make_table, write_config

COLUMNS = {"entry_id": "string", "amount": "int", "dt": "date"}
QUERY = {"query_id": "q_amount", "table": "clean", "select": ["entry_id", "amount"],
         "expected_columns": ["entry_id", "amount"], "where": "amount >= 5"}
TARGET = "dt=2024-02-02/part.csv"


def _workspace(tmp_path, files, queries=None):
    lake = tmp_path / "lake"
    make_table(lake, "clean", COLUMNS, ["dt"], files)
    config_path = write_config(tmp_path, lake, queries or [QUERY], ["clean"])
    result = run_audit(load_config(config_path), database=tmp_path / "ledger.db",
                       report_dir=tmp_path / "reports", fail_on="none")
    return tmp_path / "ledger.db", result.run_id, lake


def _two_files(tmp_path):
    return _workspace(tmp_path, [
        {"path": "dt=2024-02-01/part.csv", "partition": {"dt": "2024-02-01"},
         "rows": [{"entry_id": "a", "amount": 10}, {"entry_id": "b", "amount": 20}]},
        {"path": TARGET, "partition": {"dt": "2024-02-02"},
         "rows": [{"entry_id": "c", "amount": 6}, {"entry_id": "d", "amount": 7}]}])


def _rewrite(db, run_id, mutate):
    with sqlite3.connect(db) as con:
        report = json.loads(con.execute(
            "SELECT report_json FROM runs WHERE run_id=?", (run_id,)).fetchone()[0])
        mutate(report)
        con.execute("UPDATE runs SET report_json=? WHERE run_id=?",
                    (json.dumps(report), run_id))


def _bounds(report, suffix, low, high, untrusted=False, **extra):
    def file_item(item):
        item["statistics"]["amount"].update({"min": low, "max": high})
        item.update(extra)
        if untrusted:
            item.update({"untrusted_columns": ["amount"], "stats_decision": "unknown",
                         "kept": True, "declared_stats_decision": "must_not_match",
                         "would_declared_prune": True})

    def check_item(check):
        if check["column"] == "amount":
            check["declared"].update({"min": low, "max": high})
            if untrusted:
                check["status"] = "stats_mismatch"

    for plan in report["queries"]:
        for item in plan["files"]:
            if item["path"].endswith(suffix):
                file_item(item)
    for check in report["stats_audit"]["checks"]:
        if check["file_path"].endswith(suffix) and check["column"] != "*":
            check_item(check)


def _write(lake, text):
    (lake / "clean" / TARGET).write_text(text, encoding="utf-8")


def test_unchanged_replay_matches_historical_baseline(tmp_path):
    db, run_id, _ = _two_files(tmp_path)
    result = replay_file(db, run_id, "clean", TARGET)
    query = result["queries"][0]
    assert result["status"] == UNCHANGED and result["newly_untrusted_columns"] == []
    assert query["status"] == UNCHANGED and query["differences"] == []
    assert query["recorded"] == query["replayed"]
    assert result["baselines"]["declared_source"] == "runs.report_json snapshot"


def test_newly_pruned_when_replaced_data_falls_outside_predicate(tmp_path):
    db, run_id, lake = _two_files(tmp_path)
    _write(lake, "entry_id,amount,dt\nx,1,\ny,2,\n")
    _rewrite(db, run_id, lambda r: _bounds(r, TARGET, 1, 2))
    query = replay_file(db, run_id, "clean", TARGET)["queries"][0]
    assert query["status"] == NEWLY_PRUNED
    assert query["recorded"]["kept"] is True and query["replayed"]["kept"] is False
    assert query["replayed"]["stats_decision"] == "must_not_match"
    fields = [d["field"] for d in query["differences"]]
    assert fields == sorted(fields, key=FIELDS.index)
    assert all(d["direction"] == "recorded->replayed" for d in query["differences"])


def test_revived_misprune_is_still_kept_not_newly_pruned(tmp_path):
    db, run_id, lake = _two_files(tmp_path)
    _rewrite(db, run_id, lambda r: _bounds(
        r, TARGET, 1, 3, stats_decision="must_not_match", kept=False))
    _write(lake, "entry_id,amount,dt\nx,100,\ny,200,\n")
    result = replay_file(db, run_id, "clean", TARGET)
    query = result["queries"][0]
    assert query["status"] == STILL_KEPT
    assert query["recorded"]["kept"] is False and query["replayed"]["kept"] is True
    check = next(c for c in result["actual_stats_checks"] if c["column"] == "amount")
    assert check["matches"] is False and check["actual"]["max"] == 200


def test_would_declared_prune_stays_anchored_to_historical_stats(tmp_path):
    db, run_id, lake = _two_files(tmp_path)
    _rewrite(db, run_id, lambda r: _bounds(r, TARGET, 1, 3, untrusted=True))
    _write(lake, "entry_id,amount,dt\nx,100,\ny,200,\n")
    query = replay_file(db, run_id, "clean", TARGET)["queries"][0]
    assert query["recorded"]["would_declared_prune"] is True
    # Historical declaration (max=3) still says prune; truth (max=200) must not leak.
    assert query["replayed"]["declared_stats_decision"] == "must_not_match"
    assert query["replayed"]["would_declared_prune"] is True


def test_newly_untrusted_status_for_trusted_stats_that_now_drift(tmp_path):
    db, run_id, lake = _two_files(tmp_path)
    target = lake / "clean" / "dt=2024-02-01" / "part.csv"
    target.write_text("entry_id,amount,dt\na,10,\nb,20,\n", encoding="utf-8")
    assert replay_file(db, run_id, "clean", "dt=2024-02-01/part.csv")["status"] == UNCHANGED
    target.write_text("entry_id,amount,dt\na,10,\nb,99,\n", encoding="utf-8")
    result = replay_file(db, run_id, "clean", "dt=2024-02-01/part.csv")
    assert result["newly_untrusted_columns"] == ["amount"]
    assert result["queries"][0]["replayed"]["stats_decision"] == "unknown"
    assert result["queries"][0]["status"] in {NEWLY_UNTRUSTED, CHANGED}


def test_file_absent_from_historical_query_plans_is_not_an_error(tmp_path):
    db, run_id, _ = _two_files(tmp_path)
    _rewrite(db, run_id, lambda r: [
        plan.update({"files": [f for f in plan["files"] if not f["path"].endswith(TARGET)]})
        for plan in r["queries"]])
    query = replay_file(db, run_id, "clean", TARGET)["queries"][0]
    assert query["recorded"] is None and query["differences"] == []
    assert query["status"] in {STILL_KEPT, NEWLY_PRUNED}


def test_partition_decision_is_inherited_not_recomputed(tmp_path):
    db, run_id, _ = _workspace(tmp_path, [
        {"path": "dt=2024-02-01/part.csv", "partition": {"dt": "2024-02-01"},
         "rows": [{"entry_id": "a", "amount": 10}]},
        {"path": TARGET, "partition": {"dt": "2024-02-02"},
         "rows": [{"entry_id": "c", "amount": 6}]}],
        [{"query_id": "q_partition", "table": "clean", "select": ["entry_id"],
          "expected_columns": ["entry_id"], "where": "dt = '2024-02-01'"}])
    query = replay_file(db, run_id, "clean", TARGET)["queries"][0]
    assert query["recorded"]["partition_decision"] == "pruned"
    assert query["replayed"]["partition_decision"] == "pruned"
    assert query["replayed"]["stats_decision"] == "skipped_partition"


@pytest.mark.parametrize("kwargs,code", [
    ({"run_id": "missing-run"}, "E_REPLAY_RUN_NOT_FOUND"),
    ({"table": "ghost"}, "E_REPLAY_TABLE_NOT_IN_REPORT"),
    ({"file_path": "dt=2099-01-01/part.csv"}, "E_REPLAY_FILE_NOT_IN_REPORT")])
def test_snapshot_error_codes(tmp_path, kwargs, code):
    db, run_id, _ = _two_files(tmp_path)
    params = {"db_path": db, "run_id": run_id, "table": "clean", "file_path": TARGET}
    params.update(kwargs)
    with pytest.raises(ReplayError) as raised:
        replay_file(**params)
    assert raised.value.code == code


def test_missing_and_corrupt_disk_file_errors(tmp_path):
    db, run_id, lake = _two_files(tmp_path)
    (lake / "clean" / TARGET).unlink()
    with pytest.raises(ReplayError) as raised:
        replay_file(db, run_id, "clean", TARGET)
    assert raised.value.code == "E_REPLAY_DATA_MISSING"
    jlake = tmp_path / "jlake"
    make_table(jlake, "j", {"id": "string", "v": "int"}, [], [
        {"path": "p.jsonl", "partition": {}, "rows": [{"id": "a", "v": 1}]}], fmt="jsonl")
    config_path = write_config(tmp_path, jlake, [
        {"query_id": "qj", "table": "j", "select": ["id", "v"],
         "expected_columns": ["id", "v"], "where": "v >= 0"}], ["j"])
    result = run_audit(load_config(config_path), database=tmp_path / "j.db",
                       report_dir=tmp_path / "jr", fail_on="none")
    (jlake / "j" / "p.jsonl").write_text('{"id":"a","v":1}\n{broken\n', encoding="utf-8")
    with pytest.raises(ReplayError) as raised:
        replay_file(tmp_path / "j.db", result.run_id, "j", "p.jsonl")
    assert raised.value.code == "E_REPLAY_DATA_READ"


def test_all_table_queries_are_replayed_in_stable_order(tmp_path):
    db, run_id, _ = _workspace(tmp_path, [
        {"path": "dt=2024-02-01/part.csv", "partition": {"dt": "2024-02-01"},
         "rows": [{"entry_id": "a", "amount": 10}]}], [
        {"query_id": "q_z", "table": "clean", "select": ["entry_id"],
         "expected_columns": ["entry_id"], "where": "amount >= 1"},
        {"query_id": "q_a", "table": "clean", "select": ["entry_id"],
         "expected_columns": ["entry_id"], "where": "amount >= 2"}])
    result = replay_file(db, run_id, "clean", "dt=2024-02-01/part.csv")
    assert [q["query_id"] for q in result["queries"]] == ["q_a", "q_z"]
