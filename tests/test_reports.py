from __future__ import annotations

import json
from pathlib import Path

from lakeprune.report_html import render_html
from lakeprune.report_json import compare_golden, compare_golden_detailed, diff_reports, render_json
from lakeprune.report_markdown import render_markdown

GOLDEN = Path(__file__).parent / "golden"
EXPECTED = GOLDEN / "expected_report.json"

def test_reports_are_canonical_complete_and_self_contained(demo_report):
    text = render_json(demo_report)
    assert text.endswith("\n") and text == render_json(json.loads(text))
    loaded = json.loads(text)
    assert loaded["manifest_fingerprint"] == demo_report["manifest_fingerprint"]
    assert loaded["stats_audit"]["checks"] and loaded["queries"][0]["files"][0]["path"].endswith(".csv")
    assert any(risk["code"] == "MIS_PRUNE_RISK" for risk in loaded["risks"])
    for fragment in ("# 湖裁验台 Audit Report", "q_readings_hot_day", "MIS_PRUNE_RISK",
                     "dt=2024-01-01/region=r1/part-0001.csv", "Statistics audit",
                     demo_report["manifest_fingerprint"], "Prune ratio"):
        assert fragment in render_markdown(demo_report)
    html = render_html(demo_report)
    assert html.startswith("<!doctype html>") and "<style>" in html and "<script" not in html
    assert "https://" not in html and "http://" not in html
    assert "q_readings_hot_day" in html and "level-high" in html

def test_golden_files_match_and_mismatch_is_detected(demo_audit, tmp_path: Path):
    result = demo_audit["result"]
    ok_json, differences = compare_golden(EXPECTED, result.json_path)
    assert ok_json, "\n".join(differences)
    assert (GOLDEN / "expected_report.md").read_bytes() == Path(result.markdown_path).read_bytes()
    left, right = {"b": [1, 2], "a": {"x": True}}, {"b": [1, 3], "a": {"x": False}, "c": None}
    differences, notes = diff_reports(left, right)
    assert differences and not notes and diff_reports(left, left) == ([], [])
    report = json.loads(Path(result.json_path).read_text(encoding="utf-8"))
    report["summary"]["query_count"] = 999
    actual = tmp_path / "actual.json"
    actual.write_text(json.dumps(report), encoding="utf-8")
    ok, differences = compare_golden(EXPECTED, actual)
    assert not ok and any("query_count" in line for line in differences)

def _check(mutate, tmp_path: Path) -> tuple[list[str], list[str]]:
    report = json.loads(EXPECTED.read_text(encoding="utf-8"))
    mutate(report)
    (tmp_path / "actual.json").write_text(json.dumps(report), encoding="utf-8")
    return compare_golden_detailed(EXPECTED, tmp_path / "actual.json")[1:]

def _files(report: dict) -> list:
    return next(q for q in report["queries"] if len(q["files"]) > 2)["files"]

def test_golden_aligns_keyed_collections_and_keeps_list_order_visible(tmp_path: Path):
    def reorder(report: dict) -> None:
        report["queries"][0:2] = reversed(report["queries"][0:2])
        report["risks"][2:4] = reversed(report["risks"][2:4])
        files = _files(report)
        files[0], files[2] = files[2], files[0]

    differences, notes = _check(reorder, tmp_path)
    assert not differences, "\n".join(differences)
    for prefix in ("~ $.queries:", "~ $.queries[query_id=", "~ $.risks:"):
        assert any(note.startswith(prefix) and "order differs" in note for note in notes)
    differences, _ = _check(lambda r: _files(r)[0].__setitem__("kept", True), tmp_path)
    assert any(".files[path=" in line and ".kept" in line for line in differences)

def test_golden_duplicate_keys_and_ordered_lists_mismatch(tmp_path: Path):
    def dup(collection: str, field: str):
        return lambda r: r[collection][1].__setitem__(field, r[collection][0][field])

    cases = {
        "duplicate primary key query_id=": dup("queries", "query_id"),
        "duplicate primary key risk_id=": dup("risks", "risk_id"),
        "duplicate primary key path=": lambda r: _files(r)[1].__setitem__("path", _files(r)[0]["path"]),
        "$.stats_audit.checks[0]": lambda r: r["stats_audit"].update(
            checks=r["stats_audit"]["checks"][1:] + r["stats_audit"]["checks"][:1]),
        "type_drift_samples[1]": lambda r: next(c for c in r["stats_audit"]["checks"] if c["actual"].get(
            "type_drift_samples"))["actual"]["type_drift_samples"].append(None),
        "date_or_datetime_fragments[1]": lambda r: next(x for x in r["risks"] if x["evidence"].get(
            "date_or_datetime_fragments"))["evidence"]["date_or_datetime_fragments"].append(None),
    }
    for marker, mutate in cases.items():
        differences, _ = _check(mutate, tmp_path)
        assert any(marker in line for line in differences), (marker, differences)
