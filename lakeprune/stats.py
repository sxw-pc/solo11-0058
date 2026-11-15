"""File-level pruning using manifest column statistics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from lakeprune.models import ColumnStats, FileEntry
from lakeprune.partition import exact_leaf_evaluation
from lakeprune.predicate_ast import (
    ALL_TRUTH,
    Between,
    Comparison,
    Evaluation,
    Expr,
    InList,
    IsNull,
    LeafEvaluator,
    evaluate_tree,
)
from lakeprune.utils import TYPE_NAMES, canonical_scalar

MUST_MATCH = "must_match"
MUST_NOT_MATCH = "must_not_match"
MAYBE_MATCH = "maybe_match"
UNKNOWN = "unknown"

@dataclass(frozen=True)
class StatsOutcome:
    decision: str
    reasons: tuple[str, ...]
    unknown_columns: tuple[str, ...]
    truth_values: tuple[bool | None, ...]

def validate_stats(
    stats: ColumnStats, column_type: str, row_count: int
) -> tuple[bool, str]:
    """Validate a declared statistic without reading the data file."""
    if column_type not in TYPE_NAMES:
        return False, f"unsupported column type: {column_type}"
    if stats.type != column_type:
        return False, f"declared statistic type {stats.type!r} != schema type {column_type!r}"
    if stats.null_count is None:
        return False, "null_count is missing"
    if stats.null_count < 0 or stats.null_count > row_count:
        return False, f"null_count {stats.null_count} is outside row count {row_count}"
    null_count = stats.null_count
    if row_count == 0:
        return True, ""
    has_bounds = stats.min is not None or stats.max is not None
    if null_count == row_count:
        if has_bounds:
            return False, "all-null column declares non-null bounds"
        return True, ""
    if stats.min is None or stats.max is None:
        return False, "non-null rows exist but min/max is missing"
    try:
        low = canonical_scalar(stats.min, column_type)
        high = canonical_scalar(stats.max, column_type)
    except (TypeError, ValueError) as exc:
        return False, f"min/max cannot be typed: {exc}"
    if low > high:  # type: ignore[operator]
        return False, "min is greater than max"
    if null_count == 0:
        return True, ""
    return True, ""

def _can_any_nonnull(
    low: object, high: object, node: Expr
) -> tuple[bool, bool]:
    """Return (true_possible, false_possible) for non-null values."""
    if isinstance(node, Comparison):
        literal = node.literal.value
        op = node.op
        if op == "=":
            return low <= literal <= high, not (low == high == literal)
        if op == "!=":
            return not (low == high == literal), low <= literal <= high
        if op == ">":
            return high > literal, low <= literal
        if op == ">=":
            return high >= literal, low < literal
        if op == "<":
            return low < literal, high >= literal
        return low <= literal, high > literal
    if isinstance(node, InList):
        accepted = {item.value for item in node.values}
        true_possible = any(low <= item <= high for item in accepted)
        false_possible = not (
            low == high and low in accepted and len(accepted) == 1
        )
        return true_possible, false_possible
    if isinstance(node, Between):
        low_bound = node.low.value
        high_bound = node.high.value
        true_possible = high >= low_bound and low <= high_bound
        false_possible = not (low >= low_bound and high <= high_bound)
        return true_possible, false_possible
    return True, True

def _leaf_stats(
    node: Expr,
    stats: ColumnStats,
    row_count: int,
    column_type: str,
) -> Evaluation:
    valid, reason = validate_stats(stats, column_type, row_count)
    if not valid:
        return Evaluation(ALL_TRUTH, (node.render(),), unknown_used=True)
    assert stats.null_count is not None
    null_possible = stats.null_count > 0
    if isinstance(node, IsNull):
        all_null = stats.null_count == row_count
        no_null = stats.null_count == 0
        if node.negated:
            values = {True} if no_null else ({False} if all_null else {True, False})
        else:
            values = {False} if no_null else ({True} if all_null else {True, False})
        return Evaluation(frozenset(values))
    if stats.null_count == row_count:
        values = {None}
        return Evaluation(frozenset(values), (node.render(),))
    assert stats.min is not None and stats.max is not None
    low = canonical_scalar(stats.min, column_type)
    high = canonical_scalar(stats.max, column_type)
    true_possible, false_possible = _can_any_nonnull(low, high, node)
    values: set[bool | None] = set()
    if true_possible:
        values.add(True)
    if false_possible:
        values.add(False)
    if null_possible:
        values.add(None)
    reasons = (node.render(),) if True not in values else ()
    return Evaluation(frozenset(values), reasons)

def make_leaf_evaluator(
    file_entry: FileEntry,
    column_types: Mapping[str, str],
    untrusted_columns: set[str],
    partition_columns: tuple[str, ...],
    partition_values: Mapping[str, object],
    unknown: set[str] | None = None,
) -> LeafEvaluator:
    """Build the per-leaf evaluator used by ``evaluate_file``; shared with drift tracing."""
    def leaf(node: Expr) -> Evaluation:
        column = getattr(node, "column", None)
        if column is None or column not in column_types:
            if unknown is not None:
                unknown.add(str(column))
            return Evaluation(ALL_TRUTH, (node.render(),), unknown_used=True)
        if column in partition_columns:
            return exact_leaf_evaluation(node, partition_values, partition_columns)
        if column in untrusted_columns:
            if unknown is not None:
                unknown.add(column)
            return Evaluation(ALL_TRUTH, (node.render(),), unknown_used=True)
        stats = file_entry.columns.get(column)
        if stats is None:
            if unknown is not None:
                unknown.add(column)
            return Evaluation(ALL_TRUTH, (node.render(),), unknown_used=True)
        valid, _ = validate_stats(stats, column_types[column], file_entry.row_count)
        if not valid and unknown is not None:
            unknown.add(column)
        return _leaf_stats(node, stats, file_entry.row_count, column_types[column])
    return leaf

def evaluate_file(
    expression: Expr,
    file_entry: FileEntry,
    column_types: Mapping[str, str],
    untrusted_columns: set[str] | None = None,
    partition_columns: tuple[str, ...] = (),
    partition_values: Mapping[str, object] | None = None,
) -> StatsOutcome:
    """Classify one data file conservatively; untrusted stats become unknown."""
    if file_entry.row_count == 0:
        return StatsOutcome(MUST_NOT_MATCH, ("file has zero rows",), (), (False,))
    unknown: set[str] = set()
    leaf = make_leaf_evaluator(
        file_entry, column_types, untrusted_columns or set(),
        partition_columns, partition_values or {}, unknown,
    )
    evaluation = evaluate_tree(expression, leaf)
    if True not in evaluation.values:
        decision = MUST_NOT_MATCH
    elif evaluation.values == {True}:
        decision = MUST_MATCH
    elif unknown or evaluation.unknown_used:
        decision = UNKNOWN
    else:
        decision = MAYBE_MATCH
    ordered = tuple(sorted(evaluation.values, key=lambda item: (item is not None, bool(item))))
    return StatsOutcome(
        decision,
        tuple(evaluation.reasons),
        tuple(sorted(unknown)),
        ordered,
    )

def declared_stats_decision(
    expression: Expr,
    file_entry: FileEntry,
    column_types: Mapping[str, str],
    partition_columns: tuple[str, ...] = (),
) -> StatsOutcome:
    """Evaluate raw declared stats, used only to surface mis-prune evidence."""
    return evaluate_file(
        expression, file_entry, column_types, untrusted_columns=None,
        partition_columns=partition_columns, partition_values=file_entry.partition,
    )
