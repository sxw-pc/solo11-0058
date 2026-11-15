from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _python_files():
    return sorted(path for folder in ("lakeprune", "tests") for path in (ROOT / folder).rglob("*.py")
                  if "__pycache__" not in path.parts)


def test_python_line_budget_and_per_file_limit():
    counts = {path: len(path.read_text(encoding="utf-8").splitlines()) for path in _python_files()}
    assert all(count <= 300 for count in counts.values()), {str(p): n for p, n in counts.items() if n > 300}
    assert 2500 <= sum(counts.values()) <= 5000


def test_predicate_parser_never_uses_eval_or_exec():
    text = (ROOT / "lakeprune" / "predicate.py").read_text(encoding="utf-8")
    assert "eval(" not in text and "exec(" not in text
