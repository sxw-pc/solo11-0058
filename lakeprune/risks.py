"""Stable risk classification for unsafe or unexplainable pruning."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from lakeprune.models import ManifestIssue, Risk
from lakeprune.predicate_ast import Between, Comparison, Expr, IsNull, iter_leaves
from lakeprune.prune import QueryPlan
from lakeprune.stats_audit import (
    STATUS_DRIFT,
    STATUS_MISMATCH,
    STATUS_MISSING,
    STATUS_READ_ERROR,
    StatsAuditResult,
)
from lakeprune.utils import canonical_json, stable_id

RISK_CODES = (
    "MIS_PRUNE_RISK",
    "FULL_SCAN_RISK",
    "MISSING_STATS",
    "STATS_MISMATCH",
    "TYPE_DRIFT",
    "NULL_SEMANTICS",
    "BOUNDARY_RISK",
    "EMPTY_PARTITION",
)

LEVEL_ORDER = {"high": 0, "warn": 1, "info": 2}

def make_risk(code: str, level: str, query_id: str | None, table: str, file_path: str | None,
              description: str, evidence: Mapping[str, Any] | None = None) -> Risk:
    payload = dict(evidence or {})
    risk_id = stable_id("R", code, level, query_id or "", table, file_path or "",
                        description, canonical_json(payload))
    return Risk(risk_id, code, level, query_id, table, file_path, description, payload)

def from_manifest_issues(issues: Iterable[ManifestIssue]) -> list[Risk]:
    return [make_risk(issue.code, issue.level, None, issue.table, issue.file_path or None,
                      issue.description, issue.evidence) for issue in issues]

def from_stats_audit(audit: StatsAuditResult) -> list[Risk]:
    result: list[Risk] = []
    for check in audit.checks:
        evidence = {"column": check.column, "declared": dict(check.declared),
                    "actual": dict(check.actual), "check_message": check.message}
        if check.status == STATUS_MISSING:
            code, level, message = "MISSING_STATS", "warn", "missing file statistics for column"
        elif check.status == STATUS_DRIFT:
            code, level, message = "TYPE_DRIFT", "high", "actual data violates declared type for column"
        elif check.status in {STATUS_MISMATCH, STATUS_READ_ERROR, "invalid_stats"}:
            code, level, message = "STATS_MISMATCH", "high", "declared statistics are not reliable for column"
        else:
            continue
        result.append(make_risk(code, level, None, check.table, check.file_path,
                                f"{message} {check.column}", evidence))
    return result

def _is_null_query(expression: Expr) -> bool:
    return any(isinstance(leaf, IsNull) for leaf in iter_leaves(expression))

def _boundary_fragments(expression: Expr) -> tuple[list[str], list[str]]:
    date_fragments: list[str] = []
    string_or_float: list[str] = []
    for leaf in iter_leaves(expression):
        if isinstance(leaf, Between) and leaf.low.type_name in {"date", "datetime"}:
            date_fragments.append(leaf.render())
        elif isinstance(leaf, Comparison):
            if leaf.literal.type_name in {"date", "datetime"}:
                date_fragments.append(leaf.render())
            elif leaf.literal.type_name == "float" and leaf.op in {"=", "!="}:
                string_or_float.append(leaf.render())
            elif leaf.literal.type_name == "string" and any(
                char.isupper() for char in str(leaf.literal.value)
            ):
                string_or_float.append(leaf.render())
    return date_fragments, string_or_float

def from_query_plan(plan: QueryPlan) -> list[Risk]:
    result: list[Risk] = []
    query = plan.query
    for item in plan.misprune_candidates():
        result.append(make_risk(
            "MIS_PRUNE_RISK", "high", query.query_id, query.table, item.path,
            "untrusted statistics would have pruned a file that remains a scan candidate",
            {"declared_stats_decision": item.declared_stats_decision,
             "effective_stats_decision": item.stats_decision,
             "untrusted_columns": list(item.untrusted_columns),
             "stats_reasons": list(item.stats_reasons), "statistics": dict(item.statistics)},
        ))
    if plan.is_full_scan:
        result.append(make_risk(
            "FULL_SCAN_RISK", "warn", query.query_id, query.table, None,
            "predicate cannot prune any file; the entire table will be scanned",
            {"total_files": plan.total_files, "candidate_files": plan.candidate_files,
             "total_bytes": plan.total_bytes, "partitions": dict(plan.partitions)},
        ))
    if _is_null_query(plan.expression):
        for item in plan.files:
            if item.untrusted_columns:
                result.append(make_risk(
                    "NULL_SEMANTICS", "warn", query.query_id, query.table, item.path,
                    "NULL predicate cannot be resolved because null_count statistics are missing or untrusted",
                    {"predicate": plan.expression.render(),
                     "untrusted_columns": list(item.untrusted_columns),
                     "stats_decision": item.stats_decision},
                ))
    dates, others = _boundary_fragments(plan.expression)
    if dates or others:
        result.append(make_risk(
            "BOUNDARY_RISK", "warn", query.query_id, query.table, None,
            "date/time, floating-point or case-sensitive comparison may have boundary semantics",
            {"date_or_datetime_fragments": dates, "float_or_case_fragments": others},
        ))
    return result

def build_risks(
    manifest_issues: Iterable[ManifestIssue],
    audit: StatsAuditResult,
    plans: Iterable[QueryPlan],
) -> list[Risk]:
    risks: list[Risk] = []
    risks.extend(from_manifest_issues(manifest_issues))
    risks.extend(from_stats_audit(audit))
    for plan in plans:
        risks.extend(from_query_plan(plan))
    deduplicated = {risk.risk_id: risk for risk in risks}
    return sorted(deduplicated.values(), key=lambda risk: risk.sort_key())

def summarize_risks(risks: Iterable[Risk]) -> dict[str, Any]:
    by_level: dict[str, int] = {"high": 0, "warn": 0, "info": 0}
    by_code: dict[str, int] = {}
    for risk in risks:
        level = risk["level"] if isinstance(risk, Mapping) else risk.level
        code = risk["code"] if isinstance(risk, Mapping) else risk.code
        by_level[level] = by_level.get(level, 0) + 1
        by_code[code] = by_code.get(code, 0) + 1
    return {
        "level_counts": {key: by_level[key] for key in sorted(by_level)},
        "code_counts": {key: by_code[key] for key in sorted(by_code)},
        "total": sum(by_level.values()),
    }

def exit_code_for_level(level: str, risks: Iterable[Risk]) -> int:
    """Return 2 when a risk at or above the gate exists, otherwise 0."""
    if level == "none":
        return 0
    threshold = LEVEL_ORDER.get(level)
    if threshold is None:
        return 0
    for risk in risks:
        if LEVEL_ORDER.get(risk.level, 99) <= threshold:
            return 2
    return 0
