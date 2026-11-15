"""Stable JSON report rendering and semantic golden comparison."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from lakeprune.utils import canonical_json

# Collections whose order is not contractual; all other lists, including
# stats_audit.checks and lists nested in evidence/declared/actual, stay ordered.
IDENTITY = {("$", "queries"): "query_id", ("$", "risks"): "risk_id"}

def render_json(report: dict[str, Any]) -> str:
    return canonical_json(report)

def write_json(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(render_json(report).encode("utf-8"))

def load_report(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))

def _groups(items: Any, primary: str) -> dict[str, list[dict[str, Any]]] | None:
    """Group keyed elements; None when the value is not a keyed collection."""
    if not (isinstance(items, list) and all(
            isinstance(item, dict) and isinstance(item.get(primary), str) for item in items)):
        return None
    found: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        found.setdefault(item[primary], []).append(item)
    return found

def _keyed(expected: Any, actual: Any, path: str, primary: str, notes: list[str]) -> list[str]:
    left, right = _groups(expected, primary), _groups(actual, primary)
    if left is None or right is None:
        return _diff(expected, actual, path, notes)
    dups = {key for side in (left, right) for key, items in side.items() if len(items) > 1}
    lines = [f"{sign} {path}[{primary}={key}]: duplicate primary key {primary}={key} "
             f"in {side} ({len(items)} occurrences)" for side, sign, found
             in (("expected", "-", left), ("actual", "+", right))
             for key, items in sorted(found.items()) if len(items) > 1]
    for key in sorted((set(left) | set(right)) - dups):
        child = f"{path}[{primary}={key}]"
        if key not in right or key not in left:
            sign, absent = ("-", "actual") if key not in right else ("+", "expected")
            lines.append(f"{sign} {child}: missing in {absent}")
        else:
            lines += _diff(left[key][0], right[key][0], child, notes)
    if not dups and set(left) == set(right) and list(left) != list(right):
        notes.append(f"~ {path}: order differs: expected {list(left)}, actual {list(right)}")
    return lines

def _diff(expected: Any, actual: Any, path: str, notes: list[str]) -> list[str]:
    if type(expected) is not type(actual):
        return [f"- {path}: type {type(expected).__name__} == {expected!r}",
                f"+ {path}: type {type(actual).__name__} == {actual!r}"]
    if isinstance(expected, dict):
        lines: list[str] = []
        for key in sorted(set(expected) | set(actual), key=str):
            child = f"{path}.{key}"
            primary = ("path" if key == "files" and path.startswith("$.queries[")
                       and path.count("[") == 1 else IDENTITY.get((path, key)))
            if key not in expected or key not in actual:
                sign, source = ("+", actual) if key not in expected else ("-", expected)
                lines.append(f"{sign} {child}: {source[key]!r}")
            elif primary:
                lines += _keyed(expected[key], actual[key], child, primary, notes)
            else:
                lines += _diff(expected[key], actual[key], child, notes)
        return lines
    if isinstance(expected, list):
        common = min(len(expected), len(actual))
        lines = [line for i in range(common)
                 for line in _diff(expected[i], actual[i], f"{path}[{i}]", notes)]
        return lines + [f"{sign} {path}[{i}]: {items[i]!r}"
                        for sign, items in (("-", expected), ("+", actual))
                        for i in range(common, len(items))]
    return [] if expected == actual else [f"- {path}: {expected!r}", f"+ {path}: {actual!r}"]

def diff_reports(expected: Any, actual: Any) -> tuple[list[str], list[str]]:
    """Return (field differences, order-only notices); order is never a field difference."""
    notes: list[str] = []
    return _diff(expected, actual, "$", notes), notes

def deep_diff(expected: Any, actual: Any, path: str = "$") -> list[str]:
    return _diff(expected, actual, path, [])

def compare_golden_detailed(expected_path: str | Path, actual_path: str | Path
                            ) -> tuple[bool, list[str], list[str]]:
    reports: list[Any] = []
    for label, path in (("expected", expected_path), ("actual", actual_path)):
        try:
            reports.append(load_report(path))
        except (OSError, json.JSONDecodeError) as exc:
            return False, [f"- cannot read {label} report: {exc}"], []
    differences, notes = diff_reports(*reports)
    return not differences, differences, notes

def compare_golden(expected_path: str | Path, actual_path: str | Path) -> tuple[bool, list[str]]:
    ok, differences, _ = compare_golden_detailed(expected_path, actual_path)
    return ok, differences
