"""Recompute on-disk column statistics and mark untrusted pruning inputs."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from lakeprune.manifest import combined_manifest_fingerprint
from lakeprune.models import StatsAuditError, StatsCheck, TableManifest
from lakeprune.stats import validate_stats
from lakeprune.utils import canonical_scalar, coerce_value, safe_join, sha256_file

STATUS_OK = "ok"
STATUS_MISSING = "missing_stats"
STATUS_MISMATCH = "stats_mismatch"
STATUS_INVALID = "invalid_stats"
STATUS_DRIFT = "type_drift"
STATUS_READ_ERROR = "read_error"

@dataclass(frozen=True)
class StatsAuditResult:
    checks: tuple[StatsCheck, ...]
    untrusted: Mapping[str, Mapping[str, tuple[str, ...]]]
    summary: Mapping[str, Any]

    def untrusted_columns(self, table: str, file_path: str) -> set[str]:
        return set(self.untrusted.get(table, {}).get(file_path, ()))

def read_data_rows(path: Path, table_format: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Read a local CSV/JSONL file; malformed JSON is a hard audit error."""
    if table_format == "csv":
        try:
            with path.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                if reader.fieldnames is None:
                    raise StatsAuditError(f"CSV file has no header: {path}", "E_STATS_CSV")
                return [dict(row) for row in reader], list(reader.fieldnames)
        except OSError as exc:
            raise StatsAuditError(f"cannot read data file: {path}: {exc}", "E_STATS_IO") from exc
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise StatsAuditError(
                        f"invalid JSONL at {path}:{line_number}: {exc}",
                        "E_STATS_JSONL",
                    ) from exc
                if not isinstance(item, dict):
                    raise StatsAuditError(
                        f"JSONL row is not an object at {path}:{line_number}",
                        "E_STATS_JSONL",
                    )
                rows.append(item)
    except OSError as exc:
        raise StatsAuditError(f"cannot read data file: {path}: {exc}", "E_STATS_IO") from exc
    columns = sorted({key for row in rows for key in row})
    return rows, columns

def _raw_is_compatible(raw: Any, column_type: str) -> bool:
    if column_type == "string":
        return isinstance(raw, str)
    if column_type == "bool":
        return isinstance(raw, bool) or (
            isinstance(raw, str) and raw.lower() in {"true", "false"}
        )
    if column_type == "int":
        return not isinstance(raw, bool) and (
            isinstance(raw, int)
            or (isinstance(raw, str) and raw.strip().lstrip("-").isdigit())
        )
    if column_type == "float":
        return not isinstance(raw, bool) and isinstance(raw, (int, float, str))
    return isinstance(raw, str)

def recompute_column(
    rows: list[dict[str, Any]],
    fmt: str,
    column: str,
    column_type: str,
) -> dict[str, Any]:
    values: list[Any] = []
    null_count = 0
    drift_samples: list[dict[str, Any]] = []
    for row_number, row in enumerate(rows, start=1):
        raw = row.get(column)
        if raw is None or (fmt == "csv" and raw == ""):
            null_count += 1
            continue
        if not _raw_is_compatible(raw, column_type):
            if len(drift_samples) < 3:
                drift_samples.append({"row": row_number, "value": raw})
            continue
        try:
            values.append(coerce_value(raw, column_type))
        except (TypeError, ValueError):
            if len(drift_samples) < 3:
                drift_samples.append({"row": row_number, "value": raw})
    actual: dict[str, Any] = {
        "type": column_type,
        "min": None,
        "max": None,
        "null_count": null_count,
    }
    if values:
        actual["min"] = canonical_scalar(min(values), column_type)
        actual["max"] = canonical_scalar(max(values), column_type)
    if drift_samples:
        actual["type_drift_samples"] = drift_samples
        actual["type_drift_count"] = len(drift_samples)
    return actual

def _compare_declared(
    declared: Mapping[str, Any], actual: Mapping[str, Any]
) -> list[str]:
    differences: list[str] = []
    for field in ("type", "min", "max", "null_count"):
        if declared.get(field) != actual.get(field):
            differences.append(
                f"{field}: declared={declared.get(field)!r} actual={actual.get(field)!r}"
            )
    return differences

def _invalidate_all_columns(manifest: TableManifest, path: str) -> tuple[str, ...]:
    return tuple(sorted(manifest.columns))

def audit_stats(
    lake_root: Path,
    manifests: Mapping[str, TableManifest],
) -> StatsAuditResult:
    checks: list[StatsCheck] = []
    untrusted: dict[str, dict[str, list[str]]] = {}
    files_checked = 0
    columns_checked = 0
    for table in sorted(manifests):
        manifest = manifests[table]
        file_untrusted = untrusted.setdefault(table, {})
        for file_entry in manifest.files:
            files_checked += 1
            try:
                path = safe_join(lake_root, f"{table}/{file_entry.path}")
                rows, _ = read_data_rows(path, manifest.format)
                actual_size = path.stat().st_size
                actual_sha = sha256_file(path)
            except (StatsAuditError, OSError, ValueError) as exc:
                checks.append(
                    StatsCheck(
                        table,
                        file_entry.path,
                        "*",
                        STATUS_READ_ERROR,
                        {"size_bytes": file_entry.size_bytes, "sha256": file_entry.sha256},
                        {},
                        str(exc),
                    )
                )
                file_untrusted[file_entry.path] = list(_invalidate_all_columns(manifest, file_entry.path))
                continue
            integrity_errors = []
            if actual_size != file_entry.size_bytes:
                integrity_errors.append(
                    f"size_bytes: manifest={file_entry.size_bytes} actual={actual_size}"
                )
            if actual_sha != file_entry.sha256:
                integrity_errors.append("sha256 mismatch")
            if len(rows) != file_entry.row_count:
                integrity_errors.append(
                    f"row_count: manifest={file_entry.row_count} actual={len(rows)}"
                )
            if integrity_errors:
                checks.append(
                    StatsCheck(
                        table,
                        file_entry.path,
                        "*",
                        STATUS_MISMATCH,
                        {
                            "row_count": file_entry.row_count,
                            "size_bytes": file_entry.size_bytes,
                            "sha256": file_entry.sha256,
                        },
                        {
                            "row_count": len(rows),
                            "size_bytes": actual_size,
                            "sha256": actual_sha,
                        },
                        "; ".join(integrity_errors),
                    )
                )
                file_untrusted[file_entry.path] = list(_invalidate_all_columns(manifest, file_entry.path))
            for column in sorted(manifest.columns):
                if column in manifest.partition_columns:
                    continue
                columns_checked += 1
                actual = recompute_column(rows, manifest.format, column, manifest.columns[column])
                declared_stats = file_entry.columns.get(column)
                if declared_stats is None:
                    status = STATUS_MISSING
                    message = "declared file statistics are missing for this column"
                    declared = {"type": manifest.columns[column], "min": None, "max": None, "null_count": None}
                    file_untrusted.setdefault(file_entry.path, []).append(column)
                else:
                    declared = declared_stats.to_dict()
                    valid, reason = validate_stats(
                        declared_stats, manifest.columns[column], file_entry.row_count
                    )
                    if actual.get("type_drift_count"):
                        status = STATUS_DRIFT
                        message = (
                            f"actual values violate declared {manifest.columns[column]} type; "
                            f"samples={actual.get('type_drift_samples')}"
                        )
                        file_untrusted.setdefault(file_entry.path, []).append(column)
                    elif not valid:
                        status = STATUS_INVALID
                        message = f"declared statistics invalid: {reason}"
                        file_untrusted.setdefault(file_entry.path, []).append(column)
                    else:
                        differences = _compare_declared(declared, actual)
                        if differences:
                            status = STATUS_MISMATCH
                            message = "; ".join(differences)
                            file_untrusted.setdefault(file_entry.path, []).append(column)
                        else:
                            status = STATUS_OK
                            message = "declared statistics match recomputed values"
                checks.append(
                    StatsCheck(
                        table,
                        file_entry.path,
                        column,
                        status,
                        declared,
                        actual,
                        message,
                    )
                )
    normalized_untrusted = {
        table: {
            path: tuple(sorted(set(columns)))
            for path, columns in sorted(files.items())
        }
        for table, files in sorted(untrusted.items())
    }
    status_counts: dict[str, int] = {}
    for check in checks:
        status_counts[check.status] = status_counts.get(check.status, 0) + 1
    summary = {
        "files_checked": files_checked,
        "columns_checked": columns_checked,
        "status_counts": {key: status_counts[key] for key in sorted(status_counts)},
        "untrusted_files": sum(1 for files in normalized_untrusted.values() for _ in files),
        "untrusted_columns": sum(
            len(columns) for files in normalized_untrusted.values() for columns in files.values()
        ),
        "manifest_fingerprint": combined_manifest_fingerprint(manifests),
    }
    return StatsAuditResult(tuple(checks), normalized_untrusted, summary)
