from __future__ import annotations

import json
from pathlib import Path

from lakeprune.report_html import render_html
from lakeprune.report_json import compare_golden, deep_diff, render_json
from lakeprune.report_markdown import render_markdown

GOLDEN = Path(__file__).parent / "golden"

def test_reports_are_canonical_complete_and_self_contained(demo_report):
    text = render_json(demo_report)
    assert text.endswith("\n") and text == render_json(json.loads(text))
    loaded = json.loads(text)
    assert loaded["manifest_fingerprint"] == demo_report["manifest_fingerprint"]
    assert loaded["stats_audit"]["checks"] and loaded["queries"][0]["files"][0]["path"].endswith(".csv")
    assert any(risk["code"] == "MIS_PRUNE_RISK" for risk in loaded["risks"])
    markdown = render_markdown(demo_report)
    for fragment in ("# 湖裁验台 Audit Report", "q_readings_hot_day", "MIS_PRUNE_RISK",
                     "dt=2024-01-01/region=r1/part-0001.csv", "Statistics audit",
                     demo_report["manifest_fingerprint"], "Prune ratio"):
        assert fragment in markdown
    html = render_html(demo_report)
    assert html.startswith("<!doctype html>") and "<style>" in html and "<script" not in html
    assert "https://" not in html and "http://" not in html
    assert "q_readings_hot_day" in html and "level-high" in html

def test_golden_files_match_and_mismatch_is_detected(demo_audit, tmp_path: Path):
    result = demo_audit["result"]
    ok_json, differences = compare_golden(GOLDEN / "expected_report.json", result.json_path)
    assert ok_json, "\n".join(differences)
    assert (GOLDEN / "expected_report.md").read_bytes() == Path(result.markdown_path).read_bytes()
    left, right = {"b": [1, 2], "a": {"x": True}}, {"b": [1, 3], "a": {"x": False}, "c": None}
    assert deep_diff(left, right) == deep_diff(left, right) and any("$." in line for line in deep_diff(left, right))
    assert deep_diff(left, left) == []
    report = json.loads(Path(result.json_path).read_text(encoding="utf-8"))
    report["summary"]["query_count"] = 999
    actual = tmp_path / "actual.json"
    actual.write_text(json.dumps(report), encoding="utf-8")
    ok, differences = compare_golden(GOLDEN / "expected_report.json", actual)
    assert not ok and any("query_count" in line for line in differences)
