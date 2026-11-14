"""Deterministic Markdown audit report."""

from __future__ import annotations

from typing import Any

from lakeprune.risks import summarize_risks

def _cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")

def _table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(_cell(item) for item in row) + " |")
    return lines

def render_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    risk_summary = summarize_risks(report["risks"])
    lines = [
        f"# {report['project']} Audit Report",
        "",
        f"- Project slug: `{report['tool']}`",
        f"- Schema version: `{report['schema_version']}`",
        f"- Manifest fingerprint: `{report['manifest_fingerprint']}`",
        "",
        "## Summary",
        "",
    ]
    lines.extend(
        _table(
            ["Metric", "Value"],
            [
                ["Tables", summary["table_count"]],
                ["Data files", summary["file_count"]],
                ["Queries", summary["query_count"]],
                ["Candidate files (all queries)", summary["candidate_files"]],
                ["Estimated scan bytes", summary["estimated_scan_bytes"]],
                ["High risks", risk_summary["level_counts"].get("high", 0)],
                ["Warnings", risk_summary["level_counts"].get("warn", 0)],
            ],
        )
    )
    lines.extend(["", "## Tables", ""])
    lines.extend(
        _table(
            ["Table", "Format", "Manifest", "Files", "Rows", "Bytes", "Fingerprint"],
            [
                [
                    item["table"],
                    item["format"],
                    item["manifest_path"],
                    item["file_count"],
                    item["row_count"],
                    item["size_bytes"],
                    item["fingerprint"],
                ]
                for item in report["manifests"]
            ],
        )
    )
    lines.extend(["", "## Query plans", ""])
    for plan in report["queries"]:
        plan_summary = plan["summary"]
        lines.extend(
            [
                f"### {plan['query_id']} ({plan['table']})",
                "",
                plan["description"],
                "",
                f"- Predicate: `{plan['predicate']['rendered']}`",
                f"- Files: {plan_summary['candidate_files']} / {plan_summary['total_files']} candidates",
                f"- Bytes: {plan_summary['estimated_scan_bytes']} / {plan_summary['total_bytes']}",
                f"- Prune ratio: {plan_summary['prune_ratio']}",
                f"- Partitions: {plan['partitions']}",
                "",
            ]
        )
        lines.extend(
            _table(
                ["File", "Partition", "Partition decision", "Stats decision", "Kept", "Reasons"],
                [
                    [
                        item["path"],
                        item["partition"],
                        item["partition_decision"],
                        item["stats_decision"],
                        item["kept"],
                        "; ".join(item["partition_reasons"] + item["stats_reasons"]),
                    ]
                    for item in plan["files"]
                ],
            )
        )
        lines.append("")
    lines.extend(["## Risks", ""])
    if report["risks"]:
        lines.extend(
            _table(
                ["Level", "Code", "Query", "Table", "File", "Risk ID", "Description"],
                [
                    [
                        risk["level"],
                        risk["code"],
                        risk["query_id"] or "-",
                        risk["table"],
                        risk["file_path"] or "-",
                        risk["risk_id"],
                        risk["description"],
                    ]
                    for risk in report["risks"]
                ],
            )
        )
    else:
        lines.append("No risks were detected.")
    lines.extend(["", "## Statistics audit", ""])
    stats_summary = report["stats_audit"]["summary"]
    lines.extend(
        _table(
            ["Metric", "Value"],
            [
                ["Files checked", stats_summary["files_checked"]],
                ["Columns checked", stats_summary["columns_checked"]],
                ["Status counts", stats_summary["status_counts"]],
                ["Untrusted columns", stats_summary["untrusted_columns"]],
            ],
        )
    )
    problem_checks = [item for item in report["stats_audit"]["checks"] if item["status"] != "ok"]
    if problem_checks:
        lines.extend(["", "### Non-OK statistics checks", ""])
        lines.extend(
            _table(
                ["Table", "File", "Column", "Status", "Message"],
                [
                    [
                        item["table"],
                        item["file_path"],
                        item["column"],
                        item["status"],
                        item["message"],
                    ]
                    for item in problem_checks
                ],
            )
        )
    lines.extend(["", "## Evidence notes", ""])
    lines.append(
        "All file paths are manifest-relative POSIX paths. Every pruning reason "
        "comes from the explicit predicate AST and the manifest fingerprint above. "
        "Untrusted statistics are downgraded to `unknown` before file pruning."
    )
    lines.append("")
    return "\n".join(lines)

def write_markdown(path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(render_markdown(report).encode("utf-8"))
