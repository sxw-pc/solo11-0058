"""Explainable two-stage partition and column-statistics pruning."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from lakeprune.drift_propagation import trace_drift_propagation
from lakeprune.models import FileEntry, QuerySpec, TableManifest
from lakeprune.partition import evaluate_partition, partition_label
from lakeprune.predicate_ast import Expr, ast_to_dict, referenced_columns
from lakeprune.stats import (
    MUST_NOT_MATCH,
    StatsOutcome,
    declared_stats_decision,
    evaluate_file,
)

@dataclass(frozen=True)
class FilePlan:
    path: str
    partition: Mapping[str, Any]
    partition_decision: str
    partition_reasons: tuple[str, ...]
    stats_decision: str
    stats_reasons: tuple[str, ...]
    kept: bool
    size_bytes: int
    row_count: int
    untrusted_columns: tuple[str, ...]
    declared_stats_decision: str
    would_declared_prune: bool
    statistics: Mapping[str, Any]
    drift_propagation: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "partition": dict(self.partition),
            "partition_decision": self.partition_decision,
            "partition_reasons": list(self.partition_reasons),
            "stats_decision": self.stats_decision,
            "stats_reasons": list(self.stats_reasons),
            "kept": self.kept,
            "size_bytes": self.size_bytes,
            "row_count": self.row_count,
            "untrusted_columns": list(self.untrusted_columns),
            "declared_stats_decision": self.declared_stats_decision,
            "would_declared_prune": self.would_declared_prune,
            "statistics": dict(self.statistics),
            "drift_propagation": (
                dict(self.drift_propagation) if self.drift_propagation is not None else None
            ),
        }

@dataclass(frozen=True)
class QueryPlan:
    query: QuerySpec
    expression: Expr
    files: tuple[FilePlan, ...]
    partitions: Mapping[str, int]
    total_files: int
    partition_pruned_files: int
    stats_pruned_files: int
    candidate_files: int
    total_bytes: int
    estimated_scan_bytes: int
    stats_counts: Mapping[str, int]

    @property
    def prune_ratio(self) -> float:
        if self.total_bytes == 0:
            return 0.0
        return round((self.total_bytes - self.estimated_scan_bytes) / self.total_bytes, 6)

    @property
    def is_full_scan(self) -> bool:
        return self.total_files > 0 and self.candidate_files == self.total_files

    def misprune_candidates(self) -> list[FilePlan]:
        return [item for item in self.files if item.would_declared_prune]

    def to_dict(self) -> dict[str, Any]:
        query = self.query
        return {
            "query_id": query.query_id,
            "table": query.table,
            "description": query.description,
            "select": list(query.select),
            "expected_columns": list(query.expected_columns),
            "where": query.where,
            "predicate": {
                "rendered": self.expression.render(),
                "ast": ast_to_dict(self.expression),
                "referenced_columns": list(referenced_columns(self.expression)),
            },
            "summary": {
                "total_files": self.total_files,
                "partition_pruned_files": self.partition_pruned_files,
                "partition_candidate_files": self.total_files - self.partition_pruned_files,
                "stats_pruned_files": self.stats_pruned_files,
                "candidate_files": self.candidate_files,
                "total_bytes": self.total_bytes,
                "estimated_scan_bytes": self.estimated_scan_bytes,
                "prune_ratio": self.prune_ratio,
                "stats_decisions": {
                    key: self.stats_counts[key] for key in sorted(self.stats_counts)
                },
                "full_scan": self.is_full_scan,
            },
            "partitions": {
                "total": self.partitions["total"],
                "hit": self.partitions["hit"],
                "pruned": self.partitions["pruned"],
                "undetermined": self.partitions["undetermined"],
            },
            "files": [item.to_dict() for item in self.files],
        }

def _statistics_snapshot(
    entry: FileEntry, columns: tuple[str, ...]
) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    for column in columns:
        stats = entry.columns.get(column)
        snapshot[column] = stats.to_dict() if stats else None
    return snapshot

def build_query_plan(
    query: QuerySpec,
    expression: Expr,
    manifest: TableManifest,
    untrusted: Mapping[str, set[str]] | None = None,
    drift_columns: Mapping[str, set[str]] | None = None,
) -> QueryPlan:
    untrusted = untrusted or {}
    drift_columns = drift_columns or {}
    referenced = referenced_columns(expression)
    partition_columns = manifest.partition_columns
    grouped: dict[str, list[FileEntry]] = {}
    for entry in manifest.files:
        grouped.setdefault(partition_label(entry.partition, partition_columns), []).append(entry)
    partition_eval: dict[str, Any] = {}
    partitions = {"total": len(grouped), "hit": 0, "pruned": 0, "undetermined": 0}
    for label, entries in grouped.items():
        result = evaluate_partition(
            expression, entries[0].partition, partition_columns
        )
        partition_eval[label] = result
        if result.decision == "pruned":
            partitions["pruned"] += 1
        elif result.decision == "match":
            partitions["hit"] += 1
        else:
            partitions["undetermined"] += 1
    file_plans: list[FilePlan] = []
    stats_counts = {
        "must_match": 0,
        "must_not_match": 0,
        "maybe_match": 0,
        "unknown": 0,
        "skipped_partition": 0,
    }
    partition_pruned_files = 0
    stats_pruned_files = 0
    candidate_files = 0
    estimated_scan_bytes = 0
    for entry in manifest.files:
        label = partition_label(entry.partition, partition_columns)
        partition_result = partition_eval[label]
        relevant_untrusted = tuple(
            sorted(set(referenced) & set(untrusted.get(entry.path, set())))
        )
        if partition_result.decision == "pruned":
            partition_pruned_files += 1
            stats_counts["skipped_partition"] += 1
            file_plans.append(
                FilePlan(
                    entry.path,
                    dict(entry.partition),
                    "pruned",
                    partition_result.reasons,
                    "skipped_partition",
                    (),
                    False,
                    entry.size_bytes,
                    entry.row_count,
                    relevant_untrusted,
                    "",
                    False,
                    _statistics_snapshot(entry, referenced),
                )
            )
            continue
        outcome: StatsOutcome = evaluate_file(
            expression,
            entry,
            manifest.columns,
            set(relevant_untrusted),
            manifest.partition_columns,
            entry.partition,
        )
        stats_counts[outcome.decision] += 1
        kept = outcome.decision != MUST_NOT_MATCH
        if not kept:
            stats_pruned_files += 1
        else:
            candidate_files += 1
            estimated_scan_bytes += entry.size_bytes
        declared_decision = ""
        would_declared_prune = False
        if relevant_untrusted:
            declared = declared_stats_decision(
                expression, entry, manifest.columns, manifest.partition_columns
            )
            declared_decision = declared.decision
            would_declared_prune = kept and declared.decision == MUST_NOT_MATCH
        propagation = None
        file_drift = set(drift_columns.get(entry.path, ()))
        if file_drift & set(referenced):
            propagation = trace_drift_propagation(
                expression, entry, manifest.columns, file_drift,
                set(relevant_untrusted), manifest.partition_columns, entry.partition,
            )
        file_plans.append(
            FilePlan(
                entry.path,
                dict(entry.partition),
                partition_result.decision,
                partition_result.reasons,
                outcome.decision,
                outcome.reasons,
                kept,
                entry.size_bytes,
                entry.row_count,
                relevant_untrusted,
                declared_decision,
                would_declared_prune,
                _statistics_snapshot(entry, referenced),
                propagation,
            )
        )
    file_plans.sort(key=lambda item: item.path)
    return QueryPlan(
        query=query,
        expression=expression,
        files=tuple(file_plans),
        partitions=partitions,
        total_files=len(manifest.files),
        partition_pruned_files=partition_pruned_files,
        stats_pruned_files=stats_pruned_files,
        candidate_files=candidate_files,
        total_bytes=manifest.total_bytes,
        estimated_scan_bytes=estimated_scan_bytes,
        stats_counts=stats_counts,
    )
