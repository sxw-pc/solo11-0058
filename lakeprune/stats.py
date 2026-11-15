"""File-level pruning using manifest column statistics."""

from __future__ import annotations

import datetime as _dt
import math
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
    evaluate_tree,
)
from lakeprune.utils import DATE_FORMAT, DATETIME_FORMAT, TYPE_NAMES, canonical_scalar

MUST_MATCH = "must_match"
MUST_NOT_MATCH = "must_not_match"
MAYBE_MATCH = "maybe_match"
UNKNOWN = "unknown"

@dataclass(frozen=True)
class LeafMargin:
    """Boundary margin of one prune-driving leaf, in type-resolution units."""
    predicate: str
    type_name: str
    separation: float
    resolution: float
    margin_units: float
    classification: str

@dataclass(frozen=True)
class StatsOutcome:
    decision: str
    reasons: tuple[str, ...]
    unknown_columns: tuple[str, ...]
    truth_values: tuple[bool | None, ...]
    margins: tuple[LeafMargin, ...] = ()
    raw_decision: str = ""
    tightened: bool = False

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
    if row_count == 0:
        return True, ""
    has_bounds = stats.min is not None or stats.max is not None
    if stats.null_count == row_count:
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
    return True, ""

def _resolution_units(pair: tuple[object, object], type_name: str) -> tuple[float, float]:
    """Return (separation, resolution) between two canonical scalars of one type.

    Resolution: 12-significant-digit canonical quantum (float), 1 unit (int/bool),
    1 day (date), 1 second (datetime) or 1 code point at first difference (string).
    """
    a, b = pair
    if type_name == "float":
        fa, fb = float(a), float(b)  # type: ignore[arg-type]
        scale = max(abs(fa), abs(fb))
        quantum = 10.0 ** (math.floor(math.log10(scale)) - 11) if scale else 1.0
        return abs(fa - fb), quantum
    if type_name in {"int", "bool"}:
        return float(abs(int(a) - int(b))), 1.0  # type: ignore[arg-type]
    if type_name in {"date", "datetime"}:
        fmt = DATE_FORMAT if type_name == "date" else DATETIME_FORMAT
        delta = _dt.datetime.strptime(str(a), fmt) - _dt.datetime.strptime(str(b), fmt)
        return float(abs(delta.days if type_name == "date" else delta.total_seconds())), 1.0
    differ = next(((x, y) for x, y in zip(str(a), str(b)) if x != y), None)
    return (float(abs(ord(differ[0]) - ord(differ[1]))) if differ else (0.0 if a == b else 1.0)), 1.0

def _margin_pairs(node: Expr, low: object, high: object) -> list[tuple[object, object]]:
    """Scalar pairs whose separation bounds a prune-driving leaf decision."""
    if isinstance(node, Comparison):
        lit, op = node.literal.value, node.op
        if op in {"=", "!="}:
            return [(low, lit)] if op == "!=" or lit < low else [(high, lit)]  # type: ignore[operator]
        return [(high, lit)] if op in {">", ">="} else [(low, lit)]
    if isinstance(node, InList):
        return [(low, v) if v < low else (high, v) for v in (item.value for item in node.values)]  # type: ignore[operator]
    if isinstance(node, Between):
        return [(high, node.low.value)] if high < node.low.value else [(low, node.high.value)]  # type: ignore[operator]
    return []

def leaf_margin(node: Expr, low: object, high: object, type_name: str) -> LeafMargin | None:
    """Boundary margin of a prune-driving leaf; zero separation means exact."""
    pairs = _margin_pairs(node, low, high)
    if not pairs:
        return None
    separation, resolution = min(
        (_resolution_units(pair, type_name) for pair in pairs),
        key=lambda item: item[0] / item[1],
    )
    units = separation / resolution
    classification = "exact" if separation == 0 else ("tight" if units < 1 else "safe")
    return LeafMargin(node.render(), type_name, separation, resolution, units, classification)

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
        return true_possible, not (low == high and low in accepted and len(accepted) == 1)
    if isinstance(node, Between):
        low_bound, high_bound = node.low.value, node.high.value
        return high >= low_bound and low <= high_bound, not (low >= low_bound and high <= high_bound)
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
        all_null, no_null = stats.null_count == row_count, stats.null_count == 0
        if node.negated:
            return Evaluation(frozenset({True} if no_null else ({False} if all_null else {True, False})))
        return Evaluation(frozenset({False} if no_null else ({True} if all_null else {True, False})))
    if stats.null_count == row_count:
        return Evaluation(frozenset({None}), (node.render(),))
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

def evaluate_file(
    expression: Expr,
    file_entry: FileEntry,
    column_types: Mapping[str, str],
    untrusted_columns: set[str] | None = None,
    partition_columns: tuple[str, ...] = (),
    partition_values: Mapping[str, object] | None = None,
) -> StatsOutcome:
    """Classify one data file conservatively; untrusted stats become unknown."""
    untrusted = untrusted_columns or set()
    unknown: set[str] = set()
    margins: list[LeafMargin] = []
    partition_values = partition_values or {}

    def leaf(node: Expr) -> Evaluation:
        column = getattr(node, "column", None)
        if column is None or column not in column_types:
            unknown.add(str(column))
            return Evaluation(ALL_TRUTH, (node.render(),), unknown_used=True)
        if column in partition_columns:
            return exact_leaf_evaluation(node, partition_values, partition_columns)
        if column in untrusted:
            unknown.add(column)
            return Evaluation(ALL_TRUTH, (node.render(),), unknown_used=True)
        stats = file_entry.columns.get(column)
        if stats is None:
            unknown.add(column)
            return Evaluation(ALL_TRUTH, (node.render(),), unknown_used=True)
        valid, _ = validate_stats(stats, column_types[column], file_entry.row_count)
        if not valid:
            unknown.add(column)
        evaluation = _leaf_stats(node, stats, file_entry.row_count, column_types[column])
        prune_driving = True not in evaluation.values and stats.min is not None and stats.max is not None
        if prune_driving and isinstance(node, (Comparison, InList, Between)):
            low = canonical_scalar(stats.min, column_types[column])
            high = canonical_scalar(stats.max, column_types[column])
            margin = leaf_margin(node, low, high, column_types[column])
            if margin is not None:
                margins.append(margin)
        return evaluation

    if file_entry.row_count == 0:
        return StatsOutcome(MUST_NOT_MATCH, ("file has zero rows",), (), (False,), raw_decision=MUST_NOT_MATCH)
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
    reasons, raw_decision, tightened = tuple(evaluation.reasons), decision, False
    tight = sorted(margin.predicate for margin in margins if margin.classification == "tight")
    if decision == MUST_NOT_MATCH and tight:
        decision, tightened = MAYBE_MATCH, True
        reasons += ("margin tightened: must_not_match -> maybe_match "
                    f"(below recording resolution: {', '.join(tight)})",)
    return StatsOutcome(decision, reasons, tuple(sorted(unknown)), ordered,
                        tuple(margins), raw_decision, tightened)

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
