from __future__ import annotations

import pytest

from lakeprune.predicate import PredicateError, parse_predicate
from lakeprune.predicate_ast import And, IsNull, Not, Or, and_truth, not_truth, or_truth

SCHEMA = {"status": "string", "amount": "int", "ratio": "float", "active": "bool",
          "dt": "date", "observed_at": "datetime"}

def test_comparison_precedence_and_render():
    expression = parse_predicate("amount = 1 OR amount != 2 OR amount > 3 OR amount >= 4 OR amount < 5 OR amount <= 6", SCHEMA)
    assert isinstance(expression, Or) and len(expression.children) == 6
    assert "amount = 1" in expression.render()
    grouped = parse_predicate("status = 'ok' OR amount = 2 AND active = true", SCHEMA)
    assert isinstance(grouped, Or) and isinstance(grouped.children[1], And)
    explicit = parse_predicate("(status = 'ok' OR amount = 2) AND active = true", SCHEMA)
    assert isinstance(explicit, And) and isinstance(explicit.children[0], Or)

def test_not_in_between_null_and_typed_literals():
    assert isinstance(parse_predicate("NOT status = 'ok'", SCHEMA), Not)
    assert "IN" in parse_predicate("status IN ('ok', 'retry')", SCHEMA).render()
    assert "BETWEEN" in parse_predicate("amount BETWEEN 1 AND 4", SCHEMA).render()
    assert isinstance(parse_predicate("status IS NULL", SCHEMA), IsNull)
    assert isinstance(parse_predicate("status IS NOT NULL", SCHEMA), IsNull)
    expression = parse_predicate("dt >= '2024-01-01' AND observed_at < '2024-01-02T03:04:05'", SCHEMA)
    assert "2024-01-01" in expression.render()
    assert "O''Brien" in parse_predicate("status = 'O''Brien'", SCHEMA).render()
    assert parse_predicate("active = false", SCHEMA).render() == "active = false"
    assert parse_predicate("ratio = -1.5", SCHEMA).render() == "ratio = -1.5"
    assert parse_predicate("ratio = 2", SCHEMA).render() == "ratio = 2.0"
    with pytest.raises(PredicateError) as raised:
        parse_predicate("amount = 1.5", SCHEMA)
    assert raised.value.code == "E_TYPE_MISMATCH"

@pytest.mark.parametrize(("text", "code"), [
    ("", "E_EMPTY_EXPR"), ("amount IN ()", "E_EMPTY_IN"), ("amount IN (1,)", "E_EMPTY_IN"),
    ("amount BETWEEN 5 AND 2", "E_BETWEEN_REVERSED"), ("missing = 1", "E_UNKNOWN_FIELD"),
    ("lower(status) = 'ok'", "E_UNSUPPORTED_FUNCTION"), ("status LIKE 'ok%'", "E_UNSUPPORTED_SYNTAX"),
    ("(SELECT status)", "E_UNSUPPORTED_SYNTAX"), ("status = NULL", "E_NULL_COMPARISON"),
    ("amount = 'three'", "E_TYPE_MISMATCH"), ("active > false", "E_TYPE_MISMATCH"),
    ("dt > '2024-13-01'", "E_TYPE_MISMATCH"), ("amount = 1 AND", "E_EXPECTED_COLUMN"),
    ("amount = 1; DROP TABLE x", "E_LEX_CHARACTER"), ("status = 'unterminated", "E_LEX_STRING"),
    ("amount BETWEEN 1", "E_EXPECTED_KEYWORD"), ("status IS true", "E_UNSUPPORTED_SYNTAX"),
])
def test_stable_parse_error_codes(text, code):
    with pytest.raises(PredicateError) as raised:
        parse_predicate(text, SCHEMA)
    assert raised.value.code == code and raised.value.message
    assert raised.value.position >= 0

def test_error_position_three_valued_logic_and_no_eval():
    with pytest.raises(PredicateError) as raised:
        parse_predicate("amount = 1 AND missing = 2", SCHEMA)
    assert raised.value.position > 0
    truth, false, unknown = frozenset({True}), frozenset({False}), frozenset({None})
    assert and_truth(truth, false) == false and and_truth(truth, unknown) == unknown
    assert or_truth(false, unknown) == unknown and or_truth(truth, unknown) == truth
    assert not_truth(unknown) == unknown and not_truth(truth) == false
    text = open(__import__("lakeprune.predicate", fromlist=["__file__"]).__file__, encoding="utf-8").read()
    assert "eval(" not in text and "exec(" not in text
