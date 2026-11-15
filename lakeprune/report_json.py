"""Stable JSON report rendering and semantic golden comparison."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from lakeprune.utils import canonical_json

def render_json(report: dict[str, Any]) -> str:
    return canonical_json(report)

def write_json(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(render_json(report).encode("utf-8"))

def load_report(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))

def deep_diff(expected: Any, actual: Any, path: str = "$") -> list[str]:
    """Return deterministic, human-readable differences."""
    if type(expected) is not type(actual):
        return [
            f"- {path}: type {type(expected).__name__} == {expected!r}",
            f"+ {path}: type {type(actual).__name__} == {actual!r}",
        ]
    if isinstance(expected, dict):
        lines: list[str] = []
        for key in sorted(set(expected) | set(actual), key=str):
            child = f"{path}.{key}"
            if key not in expected:
                lines.append(f"+ {child}: {actual[key]!r}")
            elif key not in actual:
                lines.append(f"- {child}: {expected[key]!r}")
            else:
                lines.extend(deep_diff(expected[key], actual[key], child))
        return lines
    if isinstance(expected, list):
        lines = []
        common = min(len(expected), len(actual))
        for index in range(common):
            lines.extend(deep_diff(expected[index], actual[index], f"{path}[{index}]"))
        for index in range(common, len(expected)):
            lines.append(f"- {path}[{index}]: {expected[index]!r}")
        for index in range(common, len(actual)):
            lines.append(f"+ {path}[{index}]: {actual[index]!r}")
        return lines
    if expected != actual:
        return [f"- {path}: {expected!r}", f"+ {path}: {actual!r}"]
    return []

def compare_golden(expected_path: str | Path, actual_path: str | Path) -> tuple[bool, list[str]]:
    try:
        expected = load_report(expected_path)
    except (OSError, json.JSONDecodeError) as exc:
        return False, [f"- cannot read expected report: {exc}"]
    try:
        actual = load_report(actual_path)
    except (OSError, json.JSONDecodeError) as exc:
        return False, [f"- cannot read actual report: {exc}"]
    differences = deep_diff(expected, actual)
    return not differences, differences
