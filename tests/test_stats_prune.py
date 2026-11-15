from __future__ import annotations

from lakeprune.models import ColumnStats, FileEntry
from lakeprune.predicate import parse_predicate
from lakeprune.stats import (MUST_MATCH, MUST_NOT_MATCH, UNKNOWN, declared_stats_decision,
                             evaluate_file, validate_stats)

SCHEMA = {"amount": "int", "dt": "date", "ratio": "float", "status": "string"}

def entry(stats: dict[str, ColumnStats], rows: int = 4, partition=None) -> FileEntry:
    return FileEntry("part.csv", partition or {}, rows, 100, "0" * 64, stats)

def decide(expression: str, file_entry: FileEntry, untrusted=None, partition_columns=()):
    return evaluate_file(parse_predicate(expression, SCHEMA), file_entry, SCHEMA, untrusted,
                         partition_columns, file_entry.partition)

def test_range_in_between_not_and_boundaries():
    item = entry({"amount": ColumnStats("int", 10, 20, 0)})
    assert decide("amount < 5", item).decision == MUST_NOT_MATCH
    assert decide("amount >= 5", item).decision == MUST_MATCH
    assert decide("amount > 15", item).decision in {"maybe_match", MUST_MATCH}
    assert decide("amount > 20", item).decision == MUST_NOT_MATCH
    maybe = decide("amount = 15", item)
    assert maybe.decision == "maybe_match" and not any("amount = 15" in reason for reason in maybe.reasons)
    assert decide("amount IN (1, 2, 3)", item).decision == MUST_NOT_MATCH
    assert decide("amount IN (10, 12)", item).decision != MUST_NOT_MATCH
    assert decide("amount BETWEEN 1 AND 5", item).decision == MUST_NOT_MATCH
    assert decide("NOT amount < 5", item).decision == MUST_MATCH
    assert decide("amount BETWEEN 10 AND 20", item).decision == MUST_MATCH
    strings = entry({"status": ColumnStats("string", "alpha", "gamma", 0)})
    assert decide("status > 'zzz'", strings).decision == MUST_NOT_MATCH
    assert decide("status < 'alpha'", strings).decision == MUST_NOT_MATCH
    dates = entry({"dt": ColumnStats("date", "2024-01-01", "2024-01-03", 0)})
    assert decide("dt > '2024-01-03'", dates).decision == MUST_NOT_MATCH
    assert decide("dt BETWEEN '2023-01-01' AND '2023-12-31'", dates).decision == MUST_NOT_MATCH
    floats = entry({"ratio": ColumnStats("float", 0.1, 0.3, 0)})
    assert decide("ratio > 0.3", floats).decision == MUST_NOT_MATCH
    assert decide("ratio = 0.3", floats).decision == "maybe_match"

def test_null_counts_is_null_and_empty_file():
    no_nulls = entry({"amount": ColumnStats("int", 1, 5, 0)})
    assert decide("amount IS NULL", no_nulls).decision == MUST_NOT_MATCH
    assert decide("amount IS NOT NULL", no_nulls).decision == MUST_MATCH
    all_null = entry({"amount": ColumnStats("int", None, None, 4)})
    assert decide("amount IS NULL", all_null).decision == MUST_MATCH
    assert decide("amount IS NOT NULL", all_null).decision == MUST_NOT_MATCH
    mixed = entry({"amount": ColumnStats("int", 1, 5, 1)})
    assert decide("amount IS NULL", mixed).decision == "maybe_match"
    assert decide("amount > 0", mixed).decision == "maybe_match"
    assert decide("amount > 5", entry({}, rows=0)).decision == MUST_NOT_MATCH

def test_missing_invalid_wrong_type_and_untrusted_stats_are_unknown():
    assert decide("amount > 5", entry({})).decision == UNKNOWN
    assert decide("amount > 5", entry({"amount": ColumnStats("int", 20, 10, 0)})).decision == UNKNOWN
    assert decide("amount > 5", entry({"amount": ColumnStats("string", "a", "z", 0)})).decision == UNKNOWN
    assert decide("amount > 5", entry({"amount": ColumnStats("int", 0, 1, 0)}), {"amount"}).decision == UNKNOWN
    assert decide("amount > 5", entry({"amount": ColumnStats("int", 0, 1, None)})).decision == UNKNOWN

def test_partition_column_uses_exact_assignment_and_misprune_evidence():
    item = entry({"dt": ColumnStats("date", None, None, 2), "amount": ColumnStats("int", 1, 9, 0)},
                 rows=2, partition={"dt": "2024-01-01"})
    assert evaluate_file(parse_predicate("dt >= '2024-01-02'", SCHEMA), item, SCHEMA, set(),
                         ("dt",), item.partition).decision == MUST_NOT_MATCH
    assert evaluate_file(parse_predicate("dt = '2024-01-01'", SCHEMA), item, SCHEMA, set(),
                         ("dt",), item.partition).decision == MUST_MATCH
    declared = declared_stats_decision(parse_predicate("amount > 10", SCHEMA), item, SCHEMA)
    assert declared.decision == MUST_NOT_MATCH
    assert decide("amount > 10", item, {"amount"}).decision == UNKNOWN

def test_validate_stats_reports_reasons():
    assert validate_stats(ColumnStats("int", 1, 2, 0), "int", 2) == (True, "")
    assert not validate_stats(ColumnStats("int", 1, 2, 3), "int", 2)[0]
    assert "min" in validate_stats(ColumnStats("int", 2, 1, 0), "int", 2)[1]
    assert not validate_stats(ColumnStats("float", 1.0, 2.0, 0), "int", 2)[0]

MARGIN_SCHEMA = {"v": "float", "d": "date", "ts": "datetime", "s": "string", "n": "int"}
MARGIN_CASES = [
    ("v > 40.0", "v", ColumnStats("float", 40.0, 40.0, 0), MUST_NOT_MATCH, "exact", False),
    ("v >= 40.00000000001", "v", ColumnStats("float", 40.0, 40.0, 0), "maybe_match", "tight", True),
    ("v >= 40.000001", "v", ColumnStats("float", 40.0, 40.0, 0), MUST_NOT_MATCH, "safe", False),
    ("v < 39.99999999999", "v", ColumnStats("float", 40.0, 41.0, 0), "maybe_match", "tight", True),
    ("d > '2024-01-03'", "d", ColumnStats("date", "2024-01-01", "2024-01-03", 0), MUST_NOT_MATCH, "exact", False),
    ("d >= '2024-01-04'", "d", ColumnStats("date", "2024-01-01", "2024-01-03", 0), MUST_NOT_MATCH, "safe", False),
    ("ts > '2024-01-01T00:00:00'", "ts", ColumnStats("datetime", "2024-01-01T00:00:00", "2024-01-01T00:00:00", 0), MUST_NOT_MATCH, "exact", False),
    ("ts >= '2024-01-01T00:00:01'", "ts", ColumnStats("datetime", "2024-01-01T00:00:00", "2024-01-01T00:00:00", 0), MUST_NOT_MATCH, "safe", False),
    ("s > 'gammb'", "s", ColumnStats("string", "alpha", "gamma", 0), MUST_NOT_MATCH, "safe", False),
    ("n >= 21", "n", ColumnStats("int", 10, 20, 0), MUST_NOT_MATCH, "safe", False),
]

def test_boundary_margins_classify_and_tighten_conservatively():
    for expression, column, stats, decision, classification, tightened in MARGIN_CASES:
        item = FileEntry("p.csv", {}, 4, 100, "0" * 64, {column: stats})
        outcome = evaluate_file(parse_predicate(expression, MARGIN_SCHEMA), item, MARGIN_SCHEMA)
        assert (outcome.decision, outcome.raw_decision, outcome.tightened) == (decision, MUST_NOT_MATCH, tightened), expression
        (margin,) = outcome.margins
        assert margin.classification == classification and margin.margin_units >= 0, expression
        assert ("margin tightened" in "; ".join(outcome.reasons)) == tightened, expression

def test_must_match_and_maybe_match_are_never_tightened():
    item = FileEntry("p.csv", {}, 4, 100, "0" * 64, {"v": ColumnStats("float", 40.0, 40.0, 0)})
    must = evaluate_file(parse_predicate("v <= 40.0", MARGIN_SCHEMA), item, MARGIN_SCHEMA)
    assert must.decision == MUST_MATCH and not must.tightened and must.margins == ()
    wide = FileEntry("p.csv", {}, 4, 100, "0" * 64, {"v": ColumnStats("float", 40.0, 41.0, 0)})
    maybe = evaluate_file(parse_predicate("v = 40.5", MARGIN_SCHEMA), wide, MARGIN_SCHEMA)
    assert maybe.decision == "maybe_match" and not maybe.tightened
