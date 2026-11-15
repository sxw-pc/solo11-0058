"""Predicate AST, rendering and SQL-style three-valued logic."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

from lakeprune.models import LakePruneError

TruthSet = frozenset[bool | None]

class PredicateError(LakePruneError):
    """Stable, user-facing predicate parse/validation error."""

    code = "E_PREDICATE"

    def __init__(self, code: str, message: str, position: int = 0) -> None:
        super().__init__(message, code)
        self.position = position
ALL_TRUTH: TruthSet = frozenset({True, False, None})

def and_truth(left: TruthSet, right: TruthSet) -> TruthSet:
    values: set[bool | None] = set()
    for first in left:
        for second in right:
            if first is False or second is False:
                values.add(False)
            elif first is None or second is None:
                values.add(None)
            else:
                values.add(True)
    return frozenset(values)

def or_truth(left: TruthSet, right: TruthSet) -> TruthSet:
    values: set[bool | None] = set()
    for first in left:
        for second in right:
            if first is True or second is True:
                values.add(True)
            elif first is None or second is None:
                values.add(None)
            else:
                values.add(False)
    return frozenset(values)

def not_truth(values: TruthSet) -> TruthSet:
    return frozenset(None if item is None else not item for item in values)

def render_literal(value: object, type_name: str) -> str:
    if type_name == "bool":
        return "true" if value else "false"
    if type_name in {"int", "float"}:
        return repr(value)
    if type_name == "string":
        return "'" + str(value).replace("'", "''") + "'"
    if type_name in {"date", "datetime"}:
        return "'" + str(value) + "'"
    return repr(value)

@dataclass(frozen=True)
class Expr:
    def render(self) -> str:
        raise NotImplementedError

@dataclass(frozen=True)
class Literal(Expr):
    value: object
    type_name: str

    def render(self) -> str:
        return render_literal(self.value, self.type_name)

@dataclass(frozen=True)
class Column(Expr):
    name: str

    def render(self) -> str:
        return self.name

@dataclass(frozen=True)
class Comparison(Expr):
    column: str
    op: str
    literal: Literal

    def render(self) -> str:
        return f"{self.column} {self.op} {self.literal.render()}"

@dataclass(frozen=True)
class InList(Expr):
    column: str
    values: tuple[Literal, ...]

    def render(self) -> str:
        rendered = ", ".join(item.render() for item in self.values)
        return f"{self.column} IN ({rendered})"

@dataclass(frozen=True)
class Between(Expr):
    column: str
    low: Literal
    high: Literal

    def render(self) -> str:
        return f"{self.column} BETWEEN {self.low.render()} AND {self.high.render()}"

@dataclass(frozen=True)
class IsNull(Expr):
    column: str
    negated: bool = False

    def render(self) -> str:
        return f"{self.column} IS {'NOT ' if self.negated else ''}NULL"

@dataclass(frozen=True)
class And(Expr):
    children: tuple[Expr, ...]

    def render(self) -> str:
        return "(" + " AND ".join(child.render() for child in self.children) + ")"

@dataclass(frozen=True)
class Or(Expr):
    children: tuple[Expr, ...]

    def render(self) -> str:
        return "(" + " OR ".join(child.render() for child in self.children) + ")"

@dataclass(frozen=True)
class Not(Expr):
    child: Expr

    def render(self) -> str:
        return f"(NOT {self.child.render()})"

def referenced_columns(expr: Expr) -> tuple[str, ...]:
    found: set[str] = set()

    def visit(node: Expr) -> None:
        if isinstance(node, (Comparison, InList, Between, IsNull)):
            found.add(node.column)
        elif isinstance(node, (And, Or)):
            for child in node.children:
                visit(child)
        elif isinstance(node, Not):
            visit(node.child)

    visit(expr)
    return tuple(sorted(found))

def iter_leaves(expr: Expr) -> Iterable[Expr]:
    if isinstance(expr, (Comparison, InList, Between, IsNull)):
        yield expr
    elif isinstance(expr, (And, Or)):
        for child in expr.children:
            yield from iter_leaves(child)
    elif isinstance(expr, Not):
        yield from iter_leaves(expr.child)

@dataclass(frozen=True)
class Evaluation:
    values: TruthSet
    reasons: tuple[str, ...] = ()
    unknown_used: bool = False

LeafEvaluator = Callable[[Expr], Evaluation]

def evaluate_tree(expr: Expr, leaf_eval: LeafEvaluator) -> Evaluation:
    """Evaluate an AST using callbacks and collect stable prune evidence."""
    if not isinstance(expr, (And, Or, Not)):
        return leaf_eval(expr)
    if isinstance(expr, Not):
        child = evaluate_tree(expr.child, leaf_eval)
        result = Evaluation(not_truth(child.values), child.reasons, child.unknown_used)
        if True not in result.values and not result.reasons:
            result = Evaluation(result.values, (expr.render(),), result.unknown_used)
        return result
    if not expr.children:
        return Evaluation(ALL_TRUTH)
    children = [evaluate_tree(child, leaf_eval) for child in expr.children]
    values = children[0].values
    for child in children[1:]:
        values = (
            and_truth(values, child.values)
            if isinstance(expr, And)
            else or_truth(values, child.values)
        )
    unknown_used = any(child.unknown_used for child in children)
    reasons: list[str] = []
    if True not in values:
        if isinstance(expr, And):
            relevant = [child for child, ev in zip(expr.children, children) if True not in ev.values]
            selected = relevant or list(expr.children)
        else:
            selected = list(expr.children)
        for child, ev in zip(expr.children, children):
            if child in selected:
                reasons.extend(ev.reasons or (child.render(),))
        if not reasons:
            reasons.append(expr.render())
    return Evaluation(values, tuple(dict.fromkeys(reasons)), unknown_used)

def ast_to_dict(expr: Expr) -> dict[str, object]:
    """A stable JSON-friendly AST representation for evidence."""
    if isinstance(expr, Literal):
        return {"kind": "literal", "value": expr.value, "type": expr.type_name}
    if isinstance(expr, Column):
        return {"kind": "column", "name": expr.name}
    if isinstance(expr, Comparison):
        return {
            "kind": "comparison",
            "column": expr.column,
            "op": expr.op,
            "literal": ast_to_dict(expr.literal),
        }
    if isinstance(expr, InList):
        return {
            "kind": "in",
            "column": expr.column,
            "values": [ast_to_dict(item) for item in expr.values],
        }
    if isinstance(expr, Between):
        return {
            "kind": "between",
            "column": expr.column,
            "low": ast_to_dict(expr.low),
            "high": ast_to_dict(expr.high),
        }
    if isinstance(expr, IsNull):
        return {"kind": "is_null", "column": expr.column, "negated": expr.negated}
    if isinstance(expr, (And, Or)):
        return {
            "kind": "and" if isinstance(expr, And) else "or",
            "children": [ast_to_dict(child) for child in expr.children],
        }
    if isinstance(expr, Not):
        return {"kind": "not", "child": ast_to_dict(expr.child)}
    return {"kind": "unknown"}
