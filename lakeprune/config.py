"""Configuration and declarative query loading."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from lakeprune.models import ConfigError, QuerySpec, QuerySpecError, TableManifest

@dataclass(frozen=True)
class AuditConfig:
    path: str
    lake_root: Path
    queries_path: Path
    database: Path
    reports_dir: Path
    tables: tuple[str, ...]
    fail_on: str

    def summary(self) -> dict[str, Any]:
        return {
            "tables": list(self.tables),
            "fail_on": self.fail_on,
        }

def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{label} must be an object", "E_CONFIG_FIELD")
    return value

def _strings(value: Any, label: str, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ConfigError(f"{label} must be a list of non-empty strings", "E_CONFIG_FIELD")
    if not value and not allow_empty:
        raise ConfigError(f"{label} must not be empty", "E_CONFIG_FIELD")
    return tuple(value)

def load_config(path: str | Path) -> AuditConfig:
    config_path = Path(path).resolve()
    try:
        raw_text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config: {config_path}: {exc}", "E_CONFIG_IO") from exc
    try:
        raw = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML config: {exc}", "E_CONFIG_YAML") from exc
    data = _mapping(raw, "config")
    lake_raw = data.get("lake_root")
    queries_raw = data.get("queries")
    if not isinstance(lake_raw, str) or not lake_raw:
        raise ConfigError("lake_root must be a non-empty path", "E_CONFIG_FIELD")
    if not isinstance(queries_raw, str) or not queries_raw:
        raise ConfigError("queries must be a non-empty path", "E_CONFIG_FIELD")
    base = config_path.parent
    lake_root = (base / lake_raw).resolve()
    queries_path = (base / queries_raw).resolve()
    if not lake_root.is_dir():
        raise ConfigError(f"lake_root is not a directory: {lake_root}", "E_CONFIG_LAKE")
    tables = _strings(data.get("tables"), "tables")
    if len(set(tables)) != len(tables):
        raise ConfigError("tables contains duplicates", "E_CONFIG_FIELD")
    database = (base / data.get("database", "runtime/lakeprune.db")).resolve()
    reports_dir = (base / data.get("reports", "runtime/reports")).resolve()
    fail_on = data.get("fail_on", "high")
    if fail_on not in {"none", "info", "warn", "high"}:
        raise ConfigError("fail_on must be one of none, info, warn, high", "E_CONFIG_FIELD")
    return AuditConfig(
        path=str(config_path),
        lake_root=lake_root,
        queries_path=queries_path,
        database=database,
        reports_dir=reports_dir,
        tables=tables,
        fail_on=fail_on,
    )

def load_queries(
    path: Path, manifests: Mapping[str, TableManifest]
) -> tuple[QuerySpec, ...]:
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read queries file: {path}: {exc}", "E_QUERY_IO") from exc
    try:
        raw = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid queries YAML: {exc}", "E_QUERY_YAML") from exc
    data = _mapping(raw, "queries file")
    items = data.get("queries")
    if not isinstance(items, list) or not items:
        raise ConfigError("queries must be a non-empty list", "E_QUERY_FIELD")
    seen: set[str] = set()
    result: list[QuerySpec] = []
    for index, item_raw in enumerate(items):
        item = _mapping(item_raw, f"queries[{index}]")
        query_id = item.get("query_id")
        table = item.get("table")
        where = item.get("where")
        if not isinstance(query_id, str) or not query_id:
            raise ConfigError(f"queries[{index}].query_id is required", "E_QUERY_FIELD")
        if query_id in seen:
            raise ConfigError(f"duplicate query_id: {query_id}", "E_QUERY_FIELD")
        seen.add(query_id)
        if not isinstance(table, str) or table not in manifests:
            raise ConfigError(
                f"query {query_id} references unknown table: {table!r}",
                "E_QUERY_TABLE",
            )
        if not isinstance(where, str) or not where.strip():
            raise ConfigError(f"query {query_id} has an empty where clause", "E_QUERY_FIELD")
        select = _strings(item.get("select"), f"query {query_id} select")
        expected = _strings(
            item.get("expected_columns", list(select)),
            f"query {query_id} expected_columns",
        )
        manifest = manifests[table]
        for column in select:
            if column not in manifest.columns:
                raise QuerySpecError(
                    f"query {query_id} selects unknown column {column!r}",
                    "E_UNKNOWN_FIELD",
                )
        for column in expected:
            if column not in manifest.columns:
                raise QuerySpecError(
                    f"query {query_id} expects unknown column {column!r}",
                    "E_UNKNOWN_FIELD",
                )
        description = item.get("description", "")
        if not isinstance(description, str):
            raise ConfigError(f"query {query_id} description must be a string", "E_QUERY_FIELD")
        result.append(
            QuerySpec(
                query_id=query_id,
                table=table,
                select=select,
                expected_columns=expected,
                where=where,
                description=description,
                source_path=str(path),
            )
        )
    return tuple(result)
