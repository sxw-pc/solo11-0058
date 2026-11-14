from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest
import yaml

from lakeprune.config import load_config
from lakeprune.runner import run_audit
from lakeprune.utils import canonical_json, coerce_value, sha256_bytes

PROJECT_ROOT = Path(__file__).resolve().parents[1]

def _cell(value):
    return "" if value is None else ("true" if value else "false") if isinstance(value, bool) else str(value)

def write_data(path: Path, rows: list[dict], columns: list[str], fmt: str = "csv") -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "csv":
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(columns)
        writer.writerows([_cell(row.get(name)) for name in columns] for row in rows)
        data = buffer.getvalue().encode()
    else:
        data = b"".join((json.dumps({name: row.get(name) for name in columns}, sort_keys=True) + "\n").encode() for row in rows)
    path.write_bytes(data)
    return data

def column_stats(rows: list[dict], column: str, column_type: str) -> dict:
    values = [coerce_value(row[column], column_type) for row in rows if row.get(column) not in (None, "")]
    return {"type": column_type, "min": min(values) if values else None, "max": max(values) if values else None,
            "null_count": len(rows) - len(values)}

def make_table(lake: Path, table: str, columns: dict[str, str], partition_columns: list[str],
               files: list[dict], fmt: str = "csv") -> None:
    root = lake / table
    root.mkdir(parents=True, exist_ok=True)
    entries, partitions = [], []
    for item in sorted(files, key=lambda value: value["path"]):
        data = write_data(root / item["path"], item["rows"], list(columns), fmt)
        stats = {name: column_stats(item["rows"], name, kind) for name, kind in columns.items()
                 if name not in partition_columns and name not in item.get("missing_stats", set())}
        entries.append({"path": item["path"], "partition": item["partition"], "row_count": len(item["rows"]),
                        "size_bytes": len(data), "sha256": sha256_bytes(data), "columns": stats})
        partitions.append(item["partition"])
    manifest = {"table": table, "format": fmt, "partition_columns": partition_columns,
                "columns": columns, "partitions": partitions, "files": entries}
    (root / "_manifest.json").write_text(canonical_json(manifest), encoding="utf-8")

def write_config(tmp_path: Path, lake: Path, queries: list[dict], tables: list[str]) -> Path:
    query_path = tmp_path / "queries.yaml"
    query_path.write_text(yaml.safe_dump({"queries": queries}, sort_keys=False), encoding="utf-8")
    config = {"lake_root": str(lake), "queries": str(query_path), "tables": tables,
              "database": str(tmp_path / "ledger.db"), "reports": str(tmp_path / "reports"), "fail_on": "high"}
    config_path = tmp_path / "audit.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return config_path

@pytest.fixture()
def clean_config(tmp_path: Path) -> Path:
    make_table(tmp_path / "lake", "clean", {"entry_id": "string", "amount": "int", "dt": "date"}, ["dt"], [
        {"path": "dt=2024-02-01/part.csv", "partition": {"dt": "2024-02-01"},
         "rows": [{"entry_id": "a", "amount": 1}, {"entry_id": "b", "amount": 2}]},
        {"path": "dt=2024-02-02/part.csv", "partition": {"dt": "2024-02-02"},
         "rows": [{"entry_id": "c", "amount": 6}, {"entry_id": "d", "amount": 7}]},
    ])
    query = {"query_id": "q_clean", "table": "clean", "select": ["entry_id", "amount"],
             "expected_columns": ["entry_id", "amount"], "where": "amount >= 5"}
    return write_config(tmp_path, tmp_path / "lake", [query], ["clean"])

@pytest.fixture(scope="session")
def demo_audit(tmp_path_factory):
    base = tmp_path_factory.mktemp("demo-audit")
    result = run_audit(load_config(PROJECT_ROOT / "configs" / "demo.yaml"), database=base / "ledger.db",
                       report_dir=base / "reports", fail_on="high")
    return {"result": result, "db": base / "ledger.db", "base": base}

@pytest.fixture(scope="session")
def demo_report(demo_audit):
    return demo_audit["result"].report
