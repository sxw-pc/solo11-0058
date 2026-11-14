"""Hive-style partition path parsing and exact partition pruning."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Mapping

from lakeprune.models import ManifestIssue
from lakeprune.predicate_ast import (
    ALL_TRUTH,
    Between,
    Comparison,
    Evaluation,
    Expr,
    InList,
    IsNull,
    evaluate_tree,
)
from lakeprune.utils import HIVE_NULL, ValueTypeError, canonical_scalar, coerce_value

@dataclass(frozen=True)
class PartitionParse:
    values: Mapping[str, Any]
    issues: tuple[ManifestIssue, ...]
    dirty: bool

@dataclass(frozen=True)
class PartitionResult:
    decision: str
    reasons: tuple[str, ...]
    truth_values: tuple[bool | None, ...]

def _issue(
    code: str,
    table: str,
    file_path: str,
    column: str | None,
    message: str,
    evidence: Mapping[str, Any],
) -> ManifestIssue:
    return ManifestIssue(
        code=code,
        level="high" if code == "TYPE_DRIFT" else "warn",
        table=table,
        file_path=file_path,
        column=column,
        description=message,
        evidence=evidence,
    )

def parse_partition_path(
    rel_path: str,
    partition_columns: tuple[str, ...],
    column_types: Mapping[str, str],
    table: str,
    declared: Mapping[str, Any] | None = None,
) -> PartitionParse:
    """Parse ``key=value`` directories without trusting any path component."""
    issues: list[ManifestIssue] = []
    values: dict[str, Any] = {}
    seen: set[str] = set()
    directories = list(PurePosixPath(rel_path).parts[:-1])
    for component in directories:
        if "=" not in component:
            issues.append(
                _issue(
                    "TYPE_DRIFT",
                    table,
                    rel_path,
                    None,
                    f"partition path component has no key=value form: {component}",
                    {"component": component},
                )
            )
            continue
        key, raw = component.split("=", 1)
        if key in seen:
            issues.append(
                _issue(
                    "TYPE_DRIFT",
                    table,
                    rel_path,
                    key,
                    f"duplicate partition key: {key}",
                    {"key": key},
                )
            )
        seen.add(key)
        if key not in partition_columns:
            issues.append(
                _issue(
                    "TYPE_DRIFT",
                    table,
                    rel_path,
                    key,
                    f"undeclared partition key in path: {key}",
                    {"key": key},
                )
            )
            continue
        declared_type = column_types.get(key, "string")
        if raw == HIVE_NULL:
            values[key] = None
            continue
        try:
            values[key] = coerce_value(raw, declared_type)
        except ValueTypeError:
            values[key] = raw
            issues.append(
                _issue(
                    "TYPE_DRIFT",
                    table,
                    rel_path,
                    key,
                    f"partition value {raw!r} is not a valid {declared_type}",
                    {"raw": raw, "declared_type": declared_type},
                )
            )
    for key in partition_columns:
        if key not in values:
            issues.append(
                _issue(
                    "FULL_SCAN_RISK",
                    table,
                    rel_path,
                    key,
                    f"partition key is absent from path: {key}",
                    {"missing_key": key},
                )
            )
    if declared is not None:
        for key in partition_columns:
            path_value = values.get(key)
            declared_value = declared.get(key)
            declared_type = column_types.get(key, "string")
            try:
                left = canonical_scalar(path_value, declared_type)
                right = canonical_scalar(declared_value, declared_type)
            except (TypeError, ValueError):
                left, right = path_value, declared_value
            if left != right:
                issues.append(
                    _issue(
                        "TYPE_DRIFT",
                        table,
                        rel_path,
                        key,
                        f"partition value disagrees with manifest: {left!r} != {right!r}",
                        {"path_value": left, "manifest_value": right},
                    )
                )
    return PartitionParse(values=values, issues=tuple(issues), dirty=bool(issues))

def partition_label(values: Mapping[str, Any], partition_columns: tuple[str, ...]) -> str:
    if not partition_columns:
        return "unpartitioned"
    parts = []
    for name in partition_columns:
        value = values.get(name)
        parts.append(f"{name}={HIVE_NULL if value is None else value}")
    return ";".join(parts)

def _compare(value: object, literal: object, op: str) -> bool:
    if op == "=":
        return value == literal
    if op == "!=":
        return value != literal
    if op == ">":
        return value > literal  # type: ignore[operator]
    if op == ">=":
        return value >= literal  # type: ignore[operator]
    if op == "<":
        return value < literal  # type: ignore[operator]
    return value <= literal  # type: ignore[operator]

def exact_leaf_evaluation(
    node: Expr,
    assignment: Mapping[str, Any],
    partition_columns: tuple[str, ...],
) -> Evaluation:
    if isinstance(node, IsNull):
        if node.column not in partition_columns:
            return Evaluation(ALL_TRUTH, unknown_used=True)
        result = assignment.get(node.column) is None
        if node.negated:
            result = not result
        return Evaluation(frozenset({result}), (node.render(),) if not result else ())
    if not isinstance(node, (Comparison, InList, Between)):
        return Evaluation(ALL_TRUTH, unknown_used=True)
    if node.column not in partition_columns:
        return Evaluation(ALL_TRUTH, unknown_used=True)
    value = assignment.get(node.column)
    if value is None:
        return Evaluation(frozenset({None}), (node.render(),))
    if isinstance(node, Comparison):
        result = _compare(value, node.literal.value, node.op)
    elif isinstance(node, InList):
        result = value in {item.value for item in node.values}
    else:
        result = node.low.value <= value <= node.high.value
    return Evaluation(frozenset({result}), (node.render(),) if not result else ())

def evaluate_partition(
    expression: Expr,
    assignment: Mapping[str, Any],
    partition_columns: tuple[str, ...],
) -> PartitionResult:
    """Return a safe partition decision and per-leaf explanation."""
    evaluation = evaluate_tree(
        expression,
        lambda node: exact_leaf_evaluation(node, assignment, partition_columns),
    )
    if True not in evaluation.values:
        decision = "pruned"
    elif evaluation.values == {True} and not evaluation.unknown_used:
        decision = "match"
    else:
        decision = "undetermined"
    ordered = tuple(sorted(evaluation.values, key=lambda item: (item is not None, bool(item))))
    return PartitionResult(decision, evaluation.reasons, ordered)
