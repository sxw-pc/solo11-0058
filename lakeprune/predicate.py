"""Recursive-descent parser for the safe LakePrune predicate language."""

from __future__ import annotations

from typing import Mapping

from lakeprune.predicate_ast import (
    And,
    Between,
    Comparison,
    Expr,
    InList,
    IsNull,
    Literal,
    Not,
    Or,
    PredicateError,
    referenced_columns,
)
from lakeprune.predicate_lexer import Token, tokenize
from lakeprune.utils import TYPE_NAMES, ValueTypeError, coerce_value

__all__ = ["PredicateError", "parse_predicate", "referenced_columns"]

_COMPARISONS = {"=", "!=", "<", "<=", ">", ">="}
_ORDERING = {"<", "<=", ">", ">="}
_UNSUPPORTED_IDENTIFIERS = {
    "LIKE",
    "ILIKE",
    "REGEXP",
    "RLIKE",
    "GLOB",
    "MATCH",
    "SELECT",
    "FROM",
    "JOIN",
    "UNION",
    "CASE",
}
_RESERVED_START = {"AND", "OR", "IN", "BETWEEN", "IS", "NULL", "TRUE", "FALSE", "NOT"}

class _Parser:
    def __init__(self, text: str, schema: Mapping[str, str]) -> None:
        self.text = text
        self.schema = schema
        self.tokens = tokenize(text)
        self.index = 0

    def peek(self) -> Token:
        return self.tokens[self.index]

    def advance(self) -> Token:
        token = self.tokens[self.index]
        self.index += 1
        return token

    def error(self, code: str, message: str, token: Token | None = None) -> PredicateError:
        target = token or self.peek()
        return PredicateError(code, message, target.position)

    def keyword(self, word: str) -> bool:
        token = self.peek()
        return token.kind == "IDENT" and str(token.value).upper() == word

    def accept_keyword(self, word: str) -> bool:
        if self.keyword(word):
            self.advance()
            return True
        return False

    def expect_keyword(self, word: str) -> Token:
        if not self.keyword(word):
            raise self.error("E_EXPECTED_KEYWORD", f"expected {word}")
        return self.advance()

    def parse(self) -> Expr:
        if self.peek().kind == "EOF":
            raise self.error("E_EMPTY_EXPR", "predicate is empty")
        expression = self.parse_or()
        if self.peek().kind != "EOF":
            raise self.error(
                "E_UNEXPECTED_TOKEN",
                f"unexpected token: {self.peek().value!r}",
            )
        return expression

    def parse_or(self) -> Expr:
        children = [self.parse_and()]
        while self.accept_keyword("OR"):
            children.append(self.parse_and())
        return children[0] if len(children) == 1 else Or(tuple(children))

    def parse_and(self) -> Expr:
        children = [self.parse_not()]
        while self.accept_keyword("AND"):
            children.append(self.parse_not())
        return children[0] if len(children) == 1 else And(tuple(children))

    def parse_not(self) -> Expr:
        if self.accept_keyword("NOT"):
            return Not(self.parse_not())
        return self.parse_primary()

    def parse_primary(self) -> Expr:
        if self.peek().kind == "LPAREN":
            self.advance()
            expression = self.parse_or()
            if self.peek().kind != "RPAREN":
                raise self.error("E_EXPECTED_RPAREN", "expected ')'")
            self.advance()
            return expression
        return self.parse_predicate()

    def parse_predicate(self) -> Expr:
        token = self.peek()
        if token.kind != "IDENT":
            raise self.error("E_EXPECTED_COLUMN", "expected a column name")
        upper = str(token.value).upper()
        if upper in _RESERVED_START or upper in _UNSUPPORTED_IDENTIFIERS:
            code = "E_UNSUPPORTED_SYNTAX" if upper in _UNSUPPORTED_IDENTIFIERS else "E_EXPECTED_COLUMN"
            raise self.error(code, f"unsupported predicate start: {token.value!r}", token)
        column_token = self.advance()
        column = str(column_token.value)
        if self.peek().kind == "LPAREN":
            raise self.error(
                "E_UNSUPPORTED_FUNCTION",
                f"function calls are not supported: {column}(...)",
            )
        if column not in self.schema:
            raise self.error(
                "E_UNKNOWN_FIELD",
                f"unknown field: {column}",
                column_token,
            )
        column_type = self.schema[column]
        if column_type not in TYPE_NAMES:
            raise self.error("E_TYPE_MISMATCH", f"unsupported declared type: {column_type}")
        if self.keyword("IN"):
            return self.parse_in(column, column_type)
        if self.keyword("BETWEEN"):
            return self.parse_between(column, column_type)
        if self.keyword("IS"):
            return self.parse_is_null(column)
        if self.peek().kind == "IDENT" and str(self.peek().value).upper() in _UNSUPPORTED_IDENTIFIERS:
            raise self.error(
                "E_UNSUPPORTED_SYNTAX",
                f"unsupported operator: {self.peek().value}",
            )
        if self.peek().kind != "OP":
            raise self.error("E_EXPECTED_OPERATOR", "expected a comparison operator")
        operator = str(self.advance().value)
        if operator not in _COMPARISONS:
            raise self.error("E_UNSUPPORTED_SYNTAX", f"unsupported operator: {operator}")
        if operator in _ORDERING and column_type == "bool":
            raise self.error("E_TYPE_MISMATCH", "ordering comparisons are not valid for bool")
        literal = self.parse_typed_literal(column_type, column)
        return Comparison(column, operator, literal)

    def parse_in(self, column: str, column_type: str) -> Expr:
        self.advance()
        if self.peek().kind != "LPAREN":
            raise self.error("E_EXPECTED_PAREN", "IN requires a parenthesized value list")
        self.advance()
        if self.peek().kind == "RPAREN":
            raise self.error("E_EMPTY_IN", "IN list must not be empty")
        values = [self.parse_typed_literal(column_type, column)]
        while self.peek().kind == "COMMA":
            self.advance()
            if self.peek().kind == "RPAREN":
                raise self.error("E_EMPTY_IN", "IN list must not have trailing commas")
            values.append(self.parse_typed_literal(column_type, column))
        if self.peek().kind != "RPAREN":
            raise self.error("E_EXPECTED_RPAREN", "expected ')' after IN list")
        self.advance()
        return InList(column, tuple(values))

    def parse_between(self, column: str, column_type: str) -> Expr:
        if column_type == "bool":
            raise self.error("E_TYPE_MISMATCH", "BETWEEN is not valid for bool")
        self.advance()
        low = self.parse_typed_literal(column_type, column)
        self.expect_keyword("AND")
        high = self.parse_typed_literal(column_type, column)
        if low.value > high.value:  # type: ignore[operator]
            raise self.error("E_BETWEEN_REVERSED", "BETWEEN lower bound exceeds upper bound")
        return Between(column, low, high)

    def parse_is_null(self, column: str) -> Expr:
        self.advance()
        negated = self.accept_keyword("NOT")
        if not self.accept_keyword("NULL"):
            raise self.error("E_UNSUPPORTED_SYNTAX", "IS only supports NULL / NOT NULL")
        return IsNull(column, negated=negated)

    def parse_typed_literal(self, column_type: str, column: str) -> Literal:
        token = self.peek()
        if token.kind == "IDENT" and str(token.value).upper() == "NULL":
            raise self.error("E_NULL_COMPARISON", "use IS NULL instead of comparing with NULL")
        raw, raw_type, consumed = self.read_raw_literal()
        compatible = (
            (column_type == "string" and raw_type == "string")
            or (column_type in {"date", "datetime"} and raw_type == "string")
            or (column_type == "bool" and raw_type == "bool")
            or (column_type == "int" and raw_type == "number" and isinstance(raw, int))
            or (column_type == "float" and raw_type == "number")
        )
        if not compatible:
            raise self.error(
                "E_TYPE_MISMATCH",
                f"literal for {column} is incompatible with {column_type}: {raw!r}",
                consumed,
            )
        try:
            value = coerce_value(raw, column_type)
        except ValueTypeError as exc:
            raise self.error(
                "E_TYPE_MISMATCH",
                f"literal for {column} is incompatible with {column_type}: {raw!r}",
                consumed,
            ) from exc
        return Literal(value, column_type)

    def read_raw_literal(self) -> tuple[object, str, Token]:
        token = self.peek()
        if token.kind == "OP" and token.value == "-":
            self.advance()
            number = self.peek()
            if number.kind != "NUMBER":
                raise self.error("E_EXPECTED_LITERAL", "expected a number after '-'")
            self.advance()
            return _number(str(number.value), negative=True), "number", number
        if token.kind == "STRING":
            self.advance()
            return token.value, "string", token
        if token.kind == "NUMBER":
            self.advance()
            return _number(str(token.value), negative=False), "number", token
        if token.kind == "IDENT" and str(token.value).upper() in {"TRUE", "FALSE"}:
            self.advance()
            return str(token.value).upper() == "TRUE", "bool", token
        if token.kind == "LPAREN":
            raise self.error("E_UNSUPPORTED_SUBQUERY", "subqueries are not supported")
        raise self.error("E_EXPECTED_LITERAL", "expected a literal value")

def _number(raw: str, negative: bool) -> int | float:
    value: int | float = float(raw) if "." in raw else int(raw)
    return -value if negative else value

def parse_predicate(text: str, schema: Mapping[str, str]) -> Expr:
    """Parse and type-check a predicate without using eval or exec."""
    if not isinstance(text, str) or not text.strip():
        raise PredicateError("E_EMPTY_EXPR", "predicate is empty", 0)
    expression = _Parser(text, schema).parse()
    if not referenced_columns(expression):
        raise PredicateError("E_EMPTY_EXPR", "predicate has no column references", 0)
    return expression
