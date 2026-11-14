"""Typed domain objects used across the audit pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from lakeprune.utils import TYPE_NAMES, canonical_json, canonical_scalar

class LakePruneError(Exception):
    """Base class with a stable error code."""

    code = "E_LAKEPRUNE"

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}

class ManifestError(LakePruneError):
    code = "E_MANIFEST"

class ConfigError(LakePruneError):
    code = "E_CONFIG"

class QuerySpecError(LakePruneError):
    code = "E_QUERY_SPEC"

class StatsAuditError(LakePruneError):
    code = "E_STATS_AUDIT"

@dataclass(frozen=True)
class ColumnStats:
    """Declared column statistics from a table manifest."""

    type: str
    min: Any = None
    max: Any = None
    null_count: int | None = None

    def normalized(self) -> dict[str, Any]:
        if self.type in TYPE_NAMES:
            try:
                low = canonical_scalar(self.min, self.type)
                high = canonical_scalar(self.max, self.type)
            except (TypeError, ValueError):
                low, high = self.min, self.max
        else:
            low, high = self.min, self.max
        return {
            "type": self.type,
            "min": low,
            "max": high,
            "null_count": self.null_count,
        }

    def to_dict(self) -> dict[str, Any]:
        return self.normalized()

@dataclass(frozen=True)
class FileEntry:
    path: str
    partition: Mapping[str, Any]
    row_count: int
    size_bytes: int
    sha256: str
    columns: Mapping[str, ColumnStats] = field(default_factory=dict)

@dataclass(frozen=True)
class TableManifest:
    table: str
    format: str
    partition_columns: tuple[str, ...]
    columns: Mapping[str, str]
    files: tuple[FileEntry, ...]
    partitions: tuple[Mapping[str, Any], ...] = ()
    manifest_path: str = "_manifest.json"

    @property
    def total_rows(self) -> int:
        return sum(item.row_count for item in self.files)

    @property
    def total_bytes(self) -> int:
        return sum(item.size_bytes for item in self.files)

    def partition_types(self) -> dict[str, str]:
        return {name: self.columns.get(name, "string") for name in self.partition_columns}

    def signature(self) -> dict[str, Any]:
        files = []
        for item in sorted(self.files, key=lambda entry: entry.path):
            files.append(
                {
                    "path": item.path,
                    "partition": dict(item.partition),
                    "row_count": item.row_count,
                    "size_bytes": item.size_bytes,
                    "sha256": item.sha256,
                    "columns": {
                        name: stats.to_dict()
                        for name, stats in sorted(item.columns.items())
                    },
                }
            )
        return {
            "table": self.table,
            "format": self.format,
            "partition_columns": list(self.partition_columns),
            "columns": dict(sorted(self.columns.items())),
            "partitions": [dict(sorted(p.items())) for p in self.partitions],
            "files": files,
        }

    def fingerprint(self) -> str:
        from lakeprune.utils import sha256_text

        return sha256_text(canonical_json(self.signature()))

    def to_summary(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "format": self.format,
            "partition_columns": list(self.partition_columns),
            "file_count": len(self.files),
            "partition_count": len(self.partitions),
            "row_count": self.total_rows,
            "size_bytes": self.total_bytes,
            "fingerprint": self.fingerprint(),
            "manifest_path": self.manifest_path,
        }

@dataclass(frozen=True)
class ManifestIssue:
    code: str
    level: str
    table: str
    file_path: str
    column: str | None
    description: str
    evidence: Mapping[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class QuerySpec:
    query_id: str
    table: str
    select: tuple[str, ...]
    expected_columns: tuple[str, ...]
    where: str
    description: str = ""
    source_path: str = ""

@dataclass(frozen=True)
class StatsCheck:
    table: str
    file_path: str
    column: str
    status: str
    declared: Mapping[str, Any]
    actual: Mapping[str, Any]
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "file_path": self.file_path,
            "column": self.column,
            "status": self.status,
            "declared": dict(self.declared),
            "actual": dict(self.actual),
            "message": self.message,
        }

@dataclass(frozen=True)
class Risk:
    risk_id: str
    code: str
    level: str
    query_id: str | None
    table: str
    file_path: str | None
    description: str
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def sort_key(self) -> tuple[Any, ...]:
        from lakeprune.utils import level_rank

        return (
            level_rank(self.level),
            self.code,
            self.query_id or "",
            self.table,
            self.file_path or "",
            self.description,
            self.risk_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "risk_id": self.risk_id,
            "code": self.code,
            "level": self.level,
            "query_id": self.query_id,
            "table": self.table,
            "file_path": self.file_path,
            "description": self.description,
            "evidence": dict(self.evidence),
        }

@dataclass
class AuditResult:
    report: dict[str, Any]
    exit_code: int
    run_id: str
    report_dir: str
    json_path: str
    markdown_path: str
    html_path: str
