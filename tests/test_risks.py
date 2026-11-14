from __future__ import annotations

from lakeprune.risks import RISK_CODES, exit_code_for_level, make_risk, summarize_risks
from lakeprune.utils import level_rank

def test_demo_risks_are_complete_sorted_and_stable(demo_report):
    risks = demo_report["risks"]
    assert set(RISK_CODES) <= {risk["code"] for risk in risks}
    assert demo_report["summary"]["risk_codes"] == {
        code: sum(1 for risk in risks if risk["code"] == code) for code in sorted({risk["code"] for risk in risks})}
    keys = [(level_rank(risk["level"]), risk["code"], risk["query_id"] or "", risk["table"], risk["file_path"] or "")
            for risk in risks]
    assert keys == sorted(keys) and risks[0]["level"] == "high"
    summary = summarize_risks(risks)
    assert summary["total"] == len(risks) and summary["level_counts"]["high"] == 3
    assert summary["level_counts"]["warn"] == 12 and summary["code_counts"]["MISSING_STATS"] == 3

def test_risk_evidence_descriptions_and_ids(demo_report):
    by_code = {risk["code"]: risk for risk in demo_report["risks"]}
    misprune = by_code["MIS_PRUNE_RISK"]
    assert misprune["query_id"] == "q_readings_hot_day" and "untrusted_columns" in misprune["evidence"]
    assert by_code["TYPE_DRIFT"]["evidence"]["actual"]["type_drift_count"] == 1
    assert by_code["EMPTY_PARTITION"]["evidence"]["row_count"] == 0
    full_scan = by_code["FULL_SCAN_RISK"]
    assert full_scan["evidence"]["candidate_files"] == full_scan["evidence"]["total_files"]
    first = make_risk("MISSING_STATS", "warn", "q1", "t", "a.csv", "missing", {"column": "x"})
    second = make_risk("MISSING_STATS", "warn", "q1", "t", "a.csv", "missing", {"column": "x"})
    other = make_risk("MISSING_STATS", "warn", "q1", "t", "a.csv", "missing", {"column": "y"})
    assert first.risk_id == second.risk_id and first.risk_id != other.risk_id and first.risk_id.startswith("R-")

def test_exit_code_gate_levels():
    high = make_risk("MIS_PRUNE_RISK", "high", "q", "t", "a", "high")
    warn = make_risk("FULL_SCAN_RISK", "warn", "q", "t", None, "warn")
    info = make_risk("BOUNDARY_RISK", "info", "q", "t", None, "info")
    assert exit_code_for_level("high", [high, warn, info]) == 2
    assert exit_code_for_level("high", [warn, info]) == 0
    assert exit_code_for_level("warn", [warn, info]) == 2
    assert exit_code_for_level("info", [info]) == 2
    assert exit_code_for_level("none", [high]) == 0
