"""Manifest loading, validation and stable fingerprints."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

from lakeprune.models import (
    ColumnStats,
    FileEntry,
    ManifestError,
    ManifestIssue,
    TableManifest,
)
from lakeprune.partition import parse_partition_path, partition_label
from lakeprune.utils import (
    TYPE_NAMES,
    canonical_json,
    canonical_scalar,
    safe_join,
    sha256_text,
)

MANIFEST_NAME = "_manifest.json"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

def _field_error(message: str) -> ManifestError:
    return ManifestError(message, "E_MANIFEST_FIELD")

def _issue(code: str, table: str, path: str, column: str | None, message: str,
           evidence: Mapping[str, Any]) -> ManifestIssue:
    return ManifestIssue(code, "warn", table, path, column, message, evidence)

def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise _field_error(f"{label} must be an object")
    return value

def _require_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise _field_error(f"{label} must be a non-empty string")
    return value

def _require_int(value: Any, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise _field_error(f"{label} must be an integer >= {minimum}")
    return value

def _read_column_stats(column: str, raw: Any) -> ColumnStats:
    data = _require_mapping(raw, f"file stats for {column}")
    declared_type = data.get("type")
    if not isinstance(declared_type, str):
        declared_type = "unknown"
    null_count = data.get("null_count")
    if isinstance(null_count, bool) or not isinstance(null_count, int):
        null_count = None
    return ColumnStats(
        type=declared_type,
        min=data.get("min"),
        max=data.get("max"),
        null_count=null_count,
    )

def _normalize_partition(
    raw: Mapping[str, Any],
    partition_columns: tuple[str, ...],
    columns: Mapping[str, str],
    source: str,
) -> dict[str, Any]:
    unknown = sorted(set(raw) - set(partition_columns))
    if unknown:
        raise _field_error(f"{source} has undeclared partition keys: {', '.join(unknown)}")
    result: dict[str, Any] = {}
    for name in partition_columns:
        raw_value = raw.get(name)
        column_type = columns.get(name, "string")
        try:
            result[name] = canonical_scalar(raw_value, column_type)
        except (TypeError, ValueError):
            result[name] = raw_value
    return result

def _empty_partition_issues(
    table: str,
    all_partitions: Iterable[dict[str, Any]],
    files: list[FileEntry],
    partition_columns: tuple[str, ...],
) -> list[ManifestIssue]:
    grouped: dict[str, list[FileEntry]] = {}
    for item in files:
        grouped.setdefault(partition_label(item.partition, partition_columns), []).append(item)
    issues: list[ManifestIssue] = []
    seen_labels: set[str] = set()
    for partition in all_partitions:
        label = partition_label(partition, partition_columns)
        if label in seen_labels:
            continue
        seen_labels.add(label)
        members = grouped.get(label, [])
        if members and any(entry.row_count > 0 for entry in members):
            continue
        paths = sorted(entry.path for entry in members)
        issues.append(_issue("EMPTY_PARTITION", table, paths[0] if paths else "", None,
                             f"partition has no scannable rows: {label}",
                             {"partition": partition, "files": paths, "row_count": 0}))
    return issues

def load_manifest(lake_root: Path, table: str) -> tuple[TableManifest, list[ManifestIssue]]:
    relative = f"{table}/{MANIFEST_NAME}"
    manifest_path = safe_join(lake_root, relative)
    try:
        raw_text = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"cannot read manifest for table {table}: {exc}", "E_MANIFEST_IO") from exc
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"invalid manifest JSON for {table}: {exc}", "E_MANIFEST_JSON") from exc
    data = _require_mapping(raw, "manifest root")
    if data.get("table") != table:
        raise _field_error(f"manifest table mismatch: expected {table!r}, got {data.get('table')!r}")
    table_format = _require_str(data.get("format"), "format")
    if table_format not in {"csv", "jsonl"}:
        raise _field_error(f"unsupported table format: {table_format}")
    columns_raw = _require_mapping(data.get("columns"), "columns")
    columns: dict[str, str] = {}
    for name in sorted(columns_raw):
        column_type = _require_str(columns_raw[name], f"type for column {name}")
        if column_type not in TYPE_NAMES:
            raise _field_error(f"unsupported type for column {name}: {column_type}")
        columns[name] = column_type
    partition_raw = data.get("partition_columns", [])
    if not isinstance(partition_raw, list) or not all(
        isinstance(item, str) and item for item in partition_raw
    ):
        raise _field_error("partition_columns must be a list of non-empty strings")
    partition_columns = tuple(partition_raw)
    if len(set(partition_columns)) != len(partition_columns):
        raise _field_error("partition_columns contains duplicates")
    for name in partition_columns:
        if name not in columns:
            raise _field_error(f"partition column is absent from schema: {name}")
    files_raw = data.get("files")
    if not isinstance(files_raw, list):
        raise _field_error("files must be a list")
    issues: list[ManifestIssue] = []
    files: list[FileEntry] = []
    seen_paths: set[str] = set()
    for index, item_raw in enumerate(files_raw):
        item = _require_mapping(item_raw, f"files[{index}]")
        rel_path = _require_str(item.get("path"), f"files[{index}].path")
        if rel_path in seen_paths:
            raise _field_error(f"duplicate manifest file path: {rel_path}")
        seen_paths.add(rel_path)
        try:
            safe_join(lake_root, rel_path)
        except ValueError as exc:
            raise ManifestError(str(exc), "E_PATH_UNSAFE") from exc
        declared_partition = _require_mapping(
            item.get("partition", {}), f"files[{index}].partition"
        )
        expected_partition = _normalize_partition(
            declared_partition, partition_columns, columns, f"files[{index}].partition"
        )
        parsed = parse_partition_path(
            rel_path,
            partition_columns,
            {name: columns[name] for name in partition_columns},
            table,
            declared=expected_partition,
        )
        issues.extend(parsed.issues)
        columns_raw_for_file = _require_mapping(
            item.get("columns", {}), f"files[{index}].columns"
        )
        stats: dict[str, ColumnStats] = {}
        for column_name in sorted(columns_raw_for_file):
            if column_name not in columns:
                issues.append(_issue("TYPE_DRIFT", table, rel_path, column_name,
                                     f"statistics reference an unknown column: {column_name}",
                                     {"column": column_name}))
                continue
            stats[column_name] = _read_column_stats(column_name, columns_raw_for_file[column_name])
        sha = _require_str(item.get("sha256"), f"files[{index}].sha256")
        if not _SHA256.fullmatch(sha):
            raise _field_error(f"files[{index}].sha256 must be lowercase sha256")
        files.append(FileEntry(rel_path, parsed.values,
                               _require_int(item.get("row_count"), f"files[{index}].row_count"),
                               _require_int(item.get("size_bytes"), f"files[{index}].size_bytes"),
                               sha, stats))
    files.sort(key=lambda entry: entry.path)
    declared_partitions: list[dict[str, Any]] = []
    partitions_raw = data.get("partitions", [])
    if not isinstance(partitions_raw, list):
        raise _field_error("partitions must be a list")
    for index, partition_raw in enumerate(partitions_raw):
        partition_obj = _require_mapping(partition_raw, f"partitions[{index}]")
        declared_partitions.append(
            _normalize_partition(partition_obj, partition_columns, columns, f"partitions[{index}]")
        )
    file_partitions: list[dict[str, Any]] = []
    seen_labels: set[str] = set()
    for item in files:
        label = partition_label(item.partition, partition_columns)
        if label not in seen_labels:
            file_partitions.append(dict(item.partition))
            seen_labels.add(label)
    all_partitions = declared_partitions + [
        part
        for part in file_partitions
        if partition_label(part, partition_columns)
        not in {partition_label(item, partition_columns) for item in declared_partitions}
    ]
    issues.extend(_empty_partition_issues(table, all_partitions, files, partition_columns))
    manifest = TableManifest(
        table=table,
        format=table_format,
        partition_columns=partition_columns,
        columns=columns,
        files=tuple(files),
        partitions=tuple(all_partitions),
        manifest_path=f"{table}/{MANIFEST_NAME}",
    )
    return manifest, issues

def combined_manifest_fingerprint(manifests: Mapping[str, TableManifest]) -> str:
    payload = {
        table: manifests[table].fingerprint()
        for table in sorted(manifests)
    }
    return sha256_text(canonical_json(payload))

def load_lake(
    lake_root: Path, tables: Iterable[str]
) -> tuple[dict[str, TableManifest], list[ManifestIssue], str]:
    manifests: dict[str, TableManifest] = {}
    issues: list[ManifestIssue] = []
    for table in sorted(set(tables)):
        manifest, table_issues = load_manifest(lake_root, table)
        manifests[table] = manifest
        issues.extend(table_issues)
    return manifests, issues, combined_manifest_fingerprint(manifests)
