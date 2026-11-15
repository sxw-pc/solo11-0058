"""Command-line interface for LakePrune."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from lakeprune.config import load_config
from lakeprune.demo_data import prepare_workspace, write_lake
from lakeprune.models import LakePruneError
from lakeprune.replay import replay_file
from lakeprune.report_html import render_html
from lakeprune.report_json import compare_golden, render_json
from lakeprune.report_markdown import render_markdown
from lakeprune.runner import collect_stats, run_audit
from lakeprune.server import serve
from lakeprune.storage import get_report
from lakeprune.utils import canonical_json

def _print_json(data: object) -> None:
    sys.stdout.write(canonical_json(data))

def _cmd_demo(args: argparse.Namespace) -> int:
    out = Path(args.out).resolve()
    config_path = prepare_workspace(out)
    config = load_config(config_path)
    result = run_audit(config, fail_on="none")
    payload = {
        "workspace": str(out),
        "config": str(config_path),
        "run_id": result.run_id,
        "json_report": result.json_path,
        "markdown_report": result.markdown_path,
        "html_report": result.html_path,
        "database": str(config.database),
        "risk_counts": result.report["summary"]["risk_counts"],
        "note": "demo always exits 0; use audit --fail-on high for CI gating",
    }
    _print_json(payload)
    return 0

def _cmd_init_demo(args: argparse.Namespace) -> int:
    out = Path(args.out).resolve()
    write_lake(out)
    _print_json({"lake_root": str(out), "tables": ["events", "readings", "stations"]})
    return 0

def _cmd_validate_stats(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    manifests, issues, audit, fingerprint = collect_stats(config)
    payload = {
        "manifest_fingerprint": fingerprint,
        "tables": [manifests[name].to_summary() for name in sorted(manifests)],
        "stats_summary": dict(audit.summary),
        "untrusted": {
            table: {path: list(columns) for path, columns in sorted(files.items())}
            for table, files in sorted(audit.untrusted.items())
        },
        "manifest_issues": [
            {
                "code": issue.code,
                "table": issue.table,
                "file_path": issue.file_path,
                "description": issue.description,
            }
            for issue in issues
        ],
    }
    _print_json(payload)
    return 0

def _cmd_audit(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    result = run_audit(
        config,
        database=args.db,
        report_dir=args.out,
        fail_on=args.fail_on,
    )
    payload = {
        "run_id": result.run_id,
        "exit_code": result.exit_code,
        "report_dir": result.report_dir,
        "json_report": result.json_path,
        "markdown_report": result.markdown_path,
        "html_report": result.html_path,
        "risk_counts": result.report["summary"]["risk_counts"],
    }
    _print_json(payload)
    return result.exit_code

def _render_for_format(fmt: str, report: dict) -> str:
    if fmt == "json":
        return render_json(report)
    if fmt == "md":
        return render_markdown(report)
    return render_html(report)

def _cmd_report(args: argparse.Namespace) -> int:
    report = get_report(args.db, args.run_id)
    if report is None:
        print(f"run not found: {args.run_id}", file=sys.stderr)
        return 1
    rendered = _render_for_format(args.format, report)
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(rendered.encode("utf-8"))
        print(str(path))
    else:
        sys.stdout.write(rendered if rendered.endswith("\n") else rendered + "\n")
    return 0

def _cmd_golden(args: argparse.Namespace) -> int:
    ok, differences = compare_golden(args.expected, args.actual)
    if ok:
        print("golden: OK")
        return 0
    print(f"golden: MISMATCH ({len(differences)} difference lines)")
    for line in differences:
        print(line)
    return 1


def _cmd_replay(args: argparse.Namespace) -> int:
    payload = replay_file(args.db, args.run_id, args.table, args.file_path)
    rendered = canonical_json(payload)
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
        print(
            f"replay: {payload['status']} ({len(payload['queries'])} queries) -> {path}",
            file=sys.stderr,
        )
    else:
        sys.stdout.write(rendered)
    return 0

def _cmd_serve(args: argparse.Namespace) -> int:
    serve(args.db, args.port)
    return 0

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m lakeprune")
    subparsers = parser.add_subparsers(dest="command", required=True)
    demo = subparsers.add_parser("demo", help="generate fixtures, audit, reports and ledger")
    demo.add_argument("--out", default="runtime", help="demo workspace directory")
    demo.set_defaults(func=_cmd_demo)
    init = subparsers.add_parser("init-demo", help="generate only the synthetic lake fixture")
    init.add_argument("--out", required=True, help="fixture output directory")
    init.set_defaults(func=_cmd_init_demo)
    validate = subparsers.add_parser("validate-stats", help="recompute and validate statistics")
    validate.add_argument("--config", required=True)
    validate.set_defaults(func=_cmd_validate_stats)
    audit = subparsers.add_parser("audit", help="run a full pruning audit")
    audit.add_argument("--config", required=True)
    audit.add_argument("--out", default=None, help="report output directory")
    audit.add_argument("--db", default=None, help="SQLite ledger path")
    audit.add_argument("--fail-on", choices=["none", "info", "warn", "high"], default=None)
    audit.set_defaults(func=_cmd_audit)
    report = subparsers.add_parser("report", help="export a stored report")
    report.add_argument("--run-id", required=True)
    report.add_argument("--format", choices=["json", "md", "html"], default="json")
    report.add_argument("--db", default="runtime/lakeprune.db")
    report.add_argument("--out", default=None, help="optional output file")
    report.set_defaults(func=_cmd_report)
    golden = subparsers.add_parser("golden", help="compare a JSON report with a golden")
    golden.add_argument("--expected", required=True)
    golden.add_argument("--actual", required=True)
    golden.set_defaults(func=_cmd_golden)
    replay = subparsers.add_parser(
        "replay", help="re-audit one replaced data file against a historical run"
    )
    replay.add_argument("--run-id", required=True)
    replay.add_argument("--table", required=True)
    replay.add_argument("--file-path", required=True, help="manifest-relative data file path")
    replay.add_argument("--db", default="runtime/lakeprune.db")
    replay.add_argument("--out", default=None, help="optional output file for the replay JSON")
    replay.set_defaults(func=_cmd_replay)
    serve_parser = subparsers.add_parser("serve", help="start the loopback read-only viewer")
    serve_parser.add_argument("--db", default="runtime/lakeprune.db")
    serve_parser.add_argument("--port", type=int, default=0)
    serve_parser.set_defaults(func=_cmd_serve)
    return parser

def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except LakePruneError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return 1
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"E_RUNTIME: {exc}", file=sys.stderr)
        return 1
