"""Deterministic, entirely synthetic fixture generation for LakePrune."""

from __future__ import annotations

import csv
import io
import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

import yaml

from lakeprune.models import ColumnStats
from lakeprune.utils import canonical_json, canonical_scalar, coerce_value, sha256_bytes

@lru_cache(maxsize=1)
def _spec() -> dict[str, Any]:
    path = Path(__file__).with_name("demo_spec.json")
    return json.loads(path.read_text(encoding="utf-8"))

def _compute_stats(rows: list[dict[str, Any]], column: str, column_type: str) -> ColumnStats:
    values, null_count = [], 0
    for row in rows:
        raw = row.get(column)
        if raw is None:
            null_count += 1
        else:
            values.append(coerce_value(raw, column_type))
    low = canonical_scalar(min(values), column_type) if values else None
    high = canonical_scalar(max(values), column_type) if values else None
    return ColumnStats(column_type, low, high, null_count)

def _render_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)

def _write_rows(
    path: Path, table_format: str, columns: Iterable[str], rows: list[dict[str, Any]]
) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    names = list(columns)
    if table_format == "csv":
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(names)
        for row in rows:
            writer.writerow([_render_cell(row.get(name)) for name in names])
        data = buffer.getvalue().encode("utf-8")
    else:
        rendered = [
            json.dumps({name: row.get(name) for name in names}, ensure_ascii=False, sort_keys=True)
            for row in rows
        ]
        data = ("\n".join(rendered) + "\n").encode("utf-8") if rendered else b""
    path.write_bytes(data)
    return data

def _build_table(root: Path, table: str, spec: dict[str, Any]) -> None:
    table_dir = root / table
    table_dir.mkdir(parents=True, exist_ok=True)
    columns, table_format = spec["columns"], spec["format"]
    partition_columns = list(spec["partition_columns"])
    manifest_files, partitions = [], []
    for file_spec in sorted(spec["files"], key=lambda item: item["path"]):
        rel_path = file_spec["path"]
        data_rows = file_spec.get("data_rows", file_spec["rows"])
        data = _write_rows(root / table / rel_path, table_format, columns, data_rows)
        missing = set(file_spec.get("missing_stats", ()))
        corrupt = file_spec.get("corrupt_stats", {})
        file_stats = {}
        for column in sorted(columns):
            if column in partition_columns or column in missing:
                continue
            data_stats = _compute_stats(file_spec["rows"], column, columns[column]).to_dict()
            data_stats.update(corrupt.get(column, {}))
            file_stats[column] = data_stats
        manifest_files.append(
            {
                "path": rel_path,
                "partition": dict(file_spec["partition"]),
                "row_count": len(data_rows),
                "size_bytes": len(data),
                "sha256": sha256_bytes(data),
                "columns": file_stats,
            }
        )
        partitions.append(dict(file_spec["partition"]))
    manifest = {
        "table": table,
        "format": table_format,
        "partition_columns": partition_columns,
        "columns": dict(columns),
        "partitions": partitions,
        "files": manifest_files,
    }
    (table_dir / "_manifest.json").write_text(
        canonical_json(manifest), encoding="utf-8", newline="\n"
    )

def write_lake(root: Path) -> None:
    """Write all three synthetic tables under ``root``."""
    root.mkdir(parents=True, exist_ok=True)
    for table in sorted(_spec()["tables"]):
        _build_table(root, table, _spec()["tables"][table])

def write_queries(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "# Synthetic, offline-only queries for the LakePrune demo.\n"
    text += yaml.safe_dump({"queries": _spec()["queries"]}, sort_keys=False, allow_unicode=True)
    path.write_text(text, encoding="utf-8", newline="\n")

def _config_text(lake_root: str, database: str, reports: str) -> str:
    data = {
        "lake_root": lake_root,
        "queries": "queries.yaml",
        "tables": ["readings", "events", "stations"],
        "database": database,
        "reports": reports,
        "fail_on": "high",
    }
    return "# LakePrune demo configuration.\n" + yaml.safe_dump(data, sort_keys=False)

def prepare_workspace(out: Path) -> Path:
    """Create a complete self-contained demo workspace and return its config path."""
    out.mkdir(parents=True, exist_ok=True)
    write_lake(out / "lake")
    write_queries(out / "queries.yaml")
    config_path = out / "demo.yaml"
    config_path.write_text(
        _config_text("lake", "lakeprune.db", "reports"), encoding="utf-8", newline="\n"
    )
    return config_path

def write_repository_assets(project_root: Path) -> None:
    """Regenerate committed fixtures and configs from the deterministic spec."""
    write_lake(project_root / "fixtures" / "lake")
    write_queries(project_root / "configs" / "queries.yaml")
    config_path = project_root / "configs" / "demo.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        _config_text("../fixtures/lake", "../runtime/lakeprune.db", "../runtime/reports"),
        encoding="utf-8",
        newline="\n",
    )
