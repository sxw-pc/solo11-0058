from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from conftest import PROJECT_ROOT

def run_cli(*args: str, timeout: int = 90) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "lakeprune", *args], cwd=PROJECT_ROOT,
                          capture_output=True, text=True, timeout=timeout)

def test_demo_creates_reports_ledger_and_fixtures(tmp_path: Path):
    out = tmp_path / "demo"
    completed = run_cli("demo", "--out", str(out))
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert all(Path(payload[key]).exists() for key in ("json_report", "markdown_report", "html_report", "database"))
    assert payload["risk_counts"]["high"] >= 1 and (out / "lake" / "readings" / "_manifest.json").exists()

def test_init_demo_and_validate_stats(tmp_path: Path):
    out = tmp_path / "fixture"
    completed = run_cli("init-demo", "--out", str(out))
    assert completed.returncode == 0 and (out / "events" / "_manifest.json").exists()
    validated = run_cli("validate-stats", "--config", "configs/demo.yaml")
    assert validated.returncode == 0
    payload = json.loads(validated.stdout)
    assert payload["stats_summary"]["files_checked"] == 11 and "readings" in payload["untrusted"]

def test_audit_gate_report_export_and_golden(tmp_path: Path):
    out = tmp_path / "demo"
    demo = run_cli("demo", "--out", str(out))
    assert demo.returncode == 0
    demo_payload = json.loads(demo.stdout)
    audit = run_cli("audit", "--config", str(out / "demo.yaml"), "--out", str(tmp_path / "reports"),
                    "--db", str(tmp_path / "ledger.db"), "--fail-on", "high")
    assert audit.returncode == 2, audit.stderr
    run_id = json.loads(audit.stdout)["run_id"]
    exported = run_cli("report", "--run-id", run_id, "--format", "json", "--db", str(tmp_path / "ledger.db"))
    assert exported.returncode == 0 and json.loads(exported.stdout)["summary"]["query_count"] == 7
    markdown = run_cli("report", "--run-id", run_id, "--format", "md", "--db", str(tmp_path / "ledger.db"))
    assert markdown.returncode == 0 and "# 湖裁验台 Audit Report" in markdown.stdout
    assert demo_payload["run_id"] != run_id
    actual = demo_payload["json_report"]
    assert run_cli("golden", "--expected", actual, "--actual", actual).returncode == 0
    expected = PROJECT_ROOT / "tests" / "golden" / "expected_report.json"
    assert run_cli("golden", "--expected", str(expected), "--actual", actual).returncode == 0

def test_report_missing_run_returns_nonzero():
    completed = run_cli("report", "--run-id", "missing", "--db", "runtime/no-such.db")
    assert completed.returncode == 1 and "run not found" in completed.stderr


def test_replay_subcommand_reports_and_errors(tmp_path: Path):
    out = tmp_path / "demo"
    demo = run_cli("demo", "--out", str(out))
    assert demo.returncode == 0
    run_id = json.loads(demo.stdout)["run_id"]
    db = str(out / "lakeprune.db")
    target = "dt=2024-01-03/region=r1/part-0001.csv"
    ok = run_cli("replay", "--run-id", run_id, "--table", "readings",
                 "--file-path", target, "--db", db)
    assert ok.returncode == 0
    payload = json.loads(ok.stdout)
    assert payload["status"] == "unchanged" and payload["file_path"] == target
    assert payload["baselines"]["declared_source"] == "runs.report_json snapshot"
    missing = run_cli("replay", "--run-id", run_id, "--table", "readings",
                      "--file-path", "dt=1999-01-01/part.csv", "--db", db)
    assert missing.returncode == 1 and "E_REPLAY_FILE_NOT_IN_REPORT" in missing.stderr
    no_run = run_cli("replay", "--run-id", "no-such-run", "--table", "readings",
                     "--file-path", target, "--db", db)
    assert no_run.returncode == 1 and "E_REPLAY_RUN_NOT_FOUND" in no_run.stderr
