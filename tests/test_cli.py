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

def test_golden_cli_accepts_reordering_but_rejects_checks_and_duplicate_paths(tmp_path: Path):
    expected = Path(json.loads(run_cli("demo", "--out", str(tmp_path / "demo")).stdout)["json_report"])
    report = json.loads(expected.read_text(encoding="utf-8"))
    report["queries"][0:2] = reversed(report["queries"][0:2])
    report["queries"][0]["files"][0:2] = reversed(report["queries"][0]["files"][0:2])

    def golden(name: str) -> subprocess.CompletedProcess:
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        return run_cli("golden", "--expected", str(expected), "--actual", str(path))

    accepted = golden("reordered")
    assert accepted.returncode == 0 and "golden: OK" in accepted.stdout
    assert "order differs" in accepted.stdout
    report["stats_audit"]["checks"][0:2] = reversed(report["stats_audit"]["checks"][0:2])
    rejected = golden("checks")
    assert rejected.returncode == 1 and "golden: MISMATCH" in rejected.stdout
    report = json.loads(expected.read_text(encoding="utf-8"))
    files = report["queries"][0]["files"]
    files[1]["path"] = files[0]["path"]
    rejected = golden("duplicated")
    assert rejected.returncode == 1 and "duplicate primary key path=" in rejected.stdout

def test_report_missing_run_returns_nonzero():
    completed = run_cli("report", "--run-id", "missing", "--db", "runtime/no-such.db")
    assert completed.returncode == 1 and "run not found" in completed.stderr
