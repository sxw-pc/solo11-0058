"""Small deterministic helpers shared by LakePrune modules."""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import math
from pathlib import Path, PurePosixPath, PureWindowsPath

class ValueTypeError(ValueError):
    """Raised when a value cannot be represented by a declared column type."""

TYPE_NAMES = ("string", "int", "float", "date", "datetime", "bool")
DATE_FORMAT = "%Y-%m-%d"
DATETIME_FORMAT = "%Y-%m-%dT%H:%M:%S"
HIVE_NULL = "__HIVE_DEFAULT_PARTITION__"

def canonical_json(value: object) -> str:
    """Serialize a value with stable key ordering and a trailing newline."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        separators=(",", ": "),
    ) + "\n"

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()

def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))

def stable_id(prefix: str, *parts: object) -> str:
    payload = canonical_json([str(item) for item in parts])
    return f"{prefix}-{sha256_text(payload)[:12]}"

def parse_date(value: str) -> str:
    try:
        parsed = _dt.datetime.strptime(value, DATE_FORMAT)
    except (TypeError, ValueError) as exc:
        raise ValueTypeError(f"invalid date: {value!r}") from exc
    return parsed.strftime(DATE_FORMAT)

def parse_datetime(value: str) -> str:
    try:
        parsed = _dt.datetime.strptime(value, DATETIME_FORMAT)
    except (TypeError, ValueError) as exc:
        raise ValueTypeError(f"invalid datetime: {value!r}") from exc
    return parsed.strftime(DATETIME_FORMAT)

def coerce_value(value: object, type_name: str) -> object:
    """Convert a JSON/CSV scalar according to a declared lake type."""
    if value is None:
        return None
    if type_name not in TYPE_NAMES:
        raise ValueTypeError(f"unknown type: {type_name!r}")
    if type_name == "string":
        if isinstance(value, (dict, list)):
            raise ValueTypeError("string value must be scalar")
        return str(value)
    if type_name == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in {"true", "false"}:
            return value.lower() == "true"
        raise ValueTypeError(f"invalid bool: {value!r}")
    if type_name == "int":
        if isinstance(value, bool):
            raise ValueTypeError(f"invalid int: {value!r}")
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value)
        raise ValueTypeError(f"invalid int: {value!r}")
    if type_name == "float":
        if isinstance(value, bool):
            raise ValueTypeError(f"invalid float: {value!r}")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueTypeError(f"invalid float: {value!r}") from exc
        if not math.isfinite(number):
            raise ValueTypeError(f"non-finite float: {value!r}")
        return number
    if type_name == "date":
        return parse_date(str(value))
    return parse_datetime(str(value))

def canonical_scalar(value: object, type_name: str) -> object:
    """Return a stable, comparable representation used in reports and checks."""
    if value is None:
        return None
    normalized = coerce_value(value, type_name)
    if type_name == "float":
        number = float(normalized)
        if number == 0.0:
            return 0.0
        return float(f"{number:.12g}")
    return normalized

def safe_join(root: Path, relative: str) -> Path:
    """Join a manifest-relative POSIX path and reject escapes."""
    if not isinstance(relative, str) or not relative:
        raise ValueError("empty manifest path")
    if "\\" in relative:
        raise ValueError("backslash is not allowed in manifest paths")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or PureWindowsPath(relative).drive or ".." in pure.parts:
        raise ValueError(f"unsafe manifest path: {relative!r}")
    candidate = root.joinpath(*pure.parts)
    root_resolved = root.resolve()
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(root_resolved)
    except (OSError, ValueError) as exc:
        raise ValueError(f"path escapes lake root: {relative!r}") from exc
    return candidate

def level_rank(level: str) -> int:
    return {"high": 0, "warn": 1, "info": 2}.get(level, 3)

def utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
