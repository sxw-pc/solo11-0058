"""Audit orchestration: load, validate, prune, classify, report and persist."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from lakeprune.config import AuditConfig, load_queries
from lakeprune.manifest import load_lake
from lakeprune.models import AuditResult, QuerySpecError
from lakeprune.predicate import PredicateError, parse_predicate
from lakeprune.prune import QueryPlan, build_query_plan
from lakeprune.report_html import write_html
from lakeprune.report_json import write_json
from lakeprune.report_markdown import write_markdown
from lakeprune.risks import build_risks, exit_code_for_level, summarize_risks
from lakeprune.stats_audit import StatsAuditResult, audit_stats
from lakeprune.storage import new_run_id, record_run

def build_report(
    manifests: dict[str, Any],
    plans: list[QueryPlan],
    audit: StatsAuditResult,
    risks: list[Any],
    fingerprint: str,
) -> dict[str, Any]:
    risk_summary = summarize_risks(risks)
    table_summaries = [manifests[name].to_summary() for name in sorted(manifests)]
    summary = {
        "table_count": len(manifests),
        "file_count": sum(item["file_count"] for item in table_summaries),
        "table_bytes": sum(item["size_bytes"] for item in table_summaries),
        "query_count": len(plans),
        "candidate_files": sum(plan.candidate_files for plan in plans),
        "estimated_scan_bytes": sum(plan.estimated_scan_bytes for plan in plans),
        "risk_counts": risk_summary["level_counts"],
        "risk_codes": risk_summary["code_counts"],
        "risk_total": risk_summary["total"],
        "stats_status_counts": dict(audit.summary["status_counts"]),
    }
    return {
        "schema_version": "1.0",
        "project": "湖裁验台",
        "tool": "lakeprune",
        "manifest_fingerprint": fingerprint,
        "manifests": table_summaries,
        "stats_audit": {
            "summary": dict(audit.summary),
            "checks": [check.to_dict() for check in audit.checks],
        },
        "queries": [plan.to_dict() for plan in plans],
        "risks": [risk.to_dict() for risk in risks],
        "summary": summary,
    }

def run_audit(
    config: AuditConfig,
    *,
    database: str | Path | None = None,
    report_dir: str | Path | None = None,
    fail_on: str | None = None,
    write_reports: bool = True,
    stats_audit: StatsAuditResult | None = None,
) -> AuditResult:
    manifests, manifest_issues, fingerprint = load_lake(config.lake_root, config.tables)
    queries = load_queries(config.queries_path, manifests)
    audit = stats_audit or audit_stats(config.lake_root, manifests)
    plans: list[QueryPlan] = []
    for query in sorted(queries, key=lambda item: item.query_id):
        manifest = manifests[query.table]
        try:
            expression = parse_predicate(query.where, manifest.columns)
        except PredicateError as exc:
            raise QuerySpecError(
                f"query {query.query_id}: {exc.message}",
                exc.code,
            ) from exc
        untrusted = audit.untrusted.get(query.table, {})
        plans.append(build_query_plan(query, expression, manifest, untrusted))
    all_risks = build_risks(manifest_issues, audit, plans)
    effective_fail_on = fail_on if fail_on is not None else config.fail_on
    exit_code = exit_code_for_level(effective_fail_on, all_risks)
    report = build_report(manifests, plans, audit, all_risks, fingerprint)
    run_id = new_run_id()
    out_dir = Path(report_dir) if report_dir is not None else config.reports_dir
    json_path = out_dir / "report.json"
    markdown_path = out_dir / "report.md"
    html_path = out_dir / "report.html"
    if write_reports:
        write_json(json_path, report)
        write_markdown(markdown_path, report)
        write_html(html_path, report)
    db_path = Path(database) if database is not None else config.database
    record_run(
        db_path,
        run_id,
        config_path=config.path,
        lake_root=str(config.lake_root),
        report=report,
        plans=plans,
        risks=all_risks,
        audit=audit,
        exit_code=exit_code,
        fail_on=effective_fail_on,
    )
    return AuditResult(
        report=report,
        exit_code=exit_code,
        run_id=run_id,
        report_dir=str(out_dir),
        json_path=str(json_path),
        markdown_path=str(markdown_path),
        html_path=str(html_path),
    )

def collect_stats(config: AuditConfig):
    manifests, manifest_issues, fingerprint = load_lake(config.lake_root, config.tables)
    audit = audit_stats(config.lake_root, manifests)
    return manifests, manifest_issues, audit, fingerprint
