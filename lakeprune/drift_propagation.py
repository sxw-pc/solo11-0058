"""Trace how TYPE_DRIFT columns propagate into file-level pruning decisions."""

from __future__ import annotations

from typing import Any, Mapping

from lakeprune.models import FileEntry
from lakeprune.predicate_ast import (ALL_TRUTH, And, Between, Comparison, Expr, InList, IsNull,
                                     Not, Or, and_truth, not_truth, or_truth, referenced_columns)
from lakeprune.stats import MUST_NOT_MATCH, UNKNOWN, evaluate_file, make_leaf_evaluator

_KINDS = ((Comparison, "comparison"), (InList, "in"), (Between, "between"), (IsNull, "is_null"))

def _kind(node: Expr) -> str:
    return next((name for cls, name in _KINDS if isinstance(node, cls)), "other")

def _truth(values: Any) -> list[Any]:
    return sorted(values, key=lambda item: (item is not None, bool(item)))

def trace_drift_propagation(
    expression: Expr,
    file_entry: FileEntry,
    column_types: Mapping[str, str],
    drift_columns: set[str],
    untrusted_columns: set[str],
    partition_columns: tuple[str, ...] = (),
    partition_values: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """Explain, leaf by leaf, how drifted columns reach one (query, file) prune evaluation.

    The effective evaluation degrades untrusted drift columns to ALL_TRUTH; the trusted
    counterfactual re-evaluates with drift columns removed from the untrusted set.  Drift
    columns that are unreferenced, or that coincide with partition columns (where
    ``exact_leaf_evaluation`` overrides the drift signal), do not propagate.
    """
    partition_values = partition_values or {}
    drift = frozenset(drift_columns)
    referenced = frozenset(referenced_columns(expression))
    exact_columns = drift & referenced & frozenset(partition_columns)
    propagated = drift & referenced - frozenset(partition_columns)
    untrusted = set(untrusted_columns)
    effective_leaf = make_leaf_evaluator(
        file_entry, column_types, untrusted, partition_columns, partition_values)
    trusted_leaf = make_leaf_evaluator(
        file_entry, column_types, untrusted - set(drift), partition_columns, partition_values)
    values: dict[int, tuple[Any, Any]] = {}
    below: dict[int, frozenset[str]] = {}
    hits: list[tuple[Expr, tuple[tuple[Expr, int], ...]]] = []
    exact_hits: list[Expr] = []
    asymmetric: list[Expr] = []

    def walk(node: Expr, ancestors: tuple[tuple[Expr, int], ...]) -> tuple[Any, Any]:
        if isinstance(node, Not):
            child = walk(node.child, ancestors + ((node, 0),))
            pair = not_truth(child[0]), not_truth(child[1])
            found = below[id(node.child)]
        elif isinstance(node, (And, Or)):
            combine = and_truth if isinstance(node, And) else or_truth
            pairs = [walk(child, ancestors + ((node, index),))
                     for index, child in enumerate(node.children)]
            found = frozenset().union(*(below[id(c)] for c in node.children)) if pairs else frozenset()
            pair = (ALL_TRUTH, ALL_TRUTH) if not pairs else pairs[0]
            for item in pairs[1:]:
                pair = combine(pair[0], item[0]), combine(pair[1], item[1])
            if True not in pair[1] and True in pair[0]:
                asymmetric.append(node)
        else:
            pair = effective_leaf(node).values, trusted_leaf(node).values
            column = getattr(node, "column", None)
            found = frozenset({column}) & propagated
            if column in propagated:
                hits.append((node, ancestors))
            elif column in exact_columns:
                exact_hits.append(node)
        values[id(node)] = pair
        below[id(node)] = found
        return pair

    walk(expression, ())

    def leaf_evidence(node: Expr, ancestors: tuple[tuple[Expr, int], ...]) -> dict[str, Any]:
        branches: list[dict[str, Any]] = []
        not_evidence = None
        for parent, index in ancestors:
            if isinstance(parent, Not):
                not_evidence = {
                    "fragment": parent.render(),
                    "degraded_subtree_values": _truth(values[id(parent.child)][0]),
                    "contributed_to_parent": _truth(values[id(parent)][0]),
                }
            else:
                siblings = [child for i, child in enumerate(parent.children) if i != index]
                branches.append({
                    "operator": "and" if isinstance(parent, And) else "or",
                    "index": index,
                    "side": "left" if index == 0 else (
                        "right" if index == len(parent.children) - 1 else "middle"),
                    "fragment": parent.render(),
                    "sibling_fragments": [child.render() for child in siblings],
                    "sibling_affected": any(below[id(child)] for child in siblings),
                })
        effective_values, trusted_values = values[id(node)]
        return {
            "fragment": node.render(),
            "column": node.column,
            "kind": _kind(node),
            "effective_values": _truth(effective_values),
            "trusted_values": _truth(trusted_values),
            "under_not": not_evidence is not None,
            "not_evidence": not_evidence,
            "branches": branches,
        }

    effective = evaluate_file(expression, file_entry, column_types, untrusted,
                              partition_columns, partition_values)
    trusted = evaluate_file(expression, file_entry, column_types, untrusted - set(drift),
                            partition_columns, partition_values)
    changed = trusted.decision == MUST_NOT_MATCH and effective.decision != MUST_NOT_MATCH
    if effective.decision == MUST_NOT_MATCH:
        asymmetric = []
    return {
        "file_path": file_entry.path,
        "drift_columns": sorted(drift),
        "propagated_columns": sorted(propagated),
        "non_propagated_columns": [
            {"column": column, "reason": "not_referenced", "exact_leaves": []}
            for column in sorted(drift - referenced)
        ] + [
            {"column": column, "reason": "partition_exact", "exact_leaves": [
                {"fragment": hit.render(), "kind": _kind(hit),
                 "exact_values": _truth(values[id(hit)][0])}
                for hit in exact_hits if hit.column == column]}
            for column in sorted(exact_columns)
        ],
        "leaves": [leaf_evidence(node, ancestors) for node, ancestors in hits],
        "asymmetric_nodes": [
            {"operator": "and" if isinstance(node, And) else "or", "fragment": node.render(),
             "trusted_values": _truth(values[id(node)][1]),
             "effective_values": _truth(values[id(node)][0])}
            for node in asymmetric
        ],
        "decisions": {
            "effective": effective.decision,
            "trusted_without_drift": trusted.decision,
            "effective_values": _truth(effective.truth_values),
            "trusted_values": _truth(trusted.truth_values),
            "retained": effective.decision != MUST_NOT_MATCH,
            "changed_by_drift": changed,
        },
        "conclusion": {
            "pruning_trustworthy": not changed,
            "retained_only_as_unknown": changed and effective.decision == UNKNOWN,
        },
    }
