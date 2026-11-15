"""Single-file replay against a historical run, with two separate baselines.

Declared statistics come exclusively from the ``runs.report_json`` ledger
snapshot (the current ``_manifest.json`` is never read); actual statistics are
recomputed from the data file on disk. Partition decisions cannot be recomputed
from rows (partition columns are path components) and are inherited from
history. ``declared_stats_decision`` / ``would_declared_prune`` always use the
historical declared stats, never the recomputed truth, so mis-prune semantics
do not drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from lakeprune.models import ColumnStats, FileEntry, LakePruneError
from lakeprune.partition import evaluate_partition, parse_partition_path
from lakeprune.predicate import parse_predicate
from lakeprune.predicate_ast import referenced_columns
from lakeprune.stats import MUST_NOT_MATCH, declared_stats_decision, evaluate_file
from lakeprune.stats_audit import StatsAuditError, read_data_rows, recompute_column
from lakeprune.storage import get_report, get_run
from lakeprune.utils import safe_join

UNCHANGED, CHANGED = "unchanged", "changed"
NEWLY_UNTRUSTED, STILL_KEPT, NEWLY_PRUNED = "newly_untrusted", "still_kept", "newly_pruned"
RANK = {UNCHANGED: 0, CHANGED: 1, NEWLY_UNTRUSTED: 2, STILL_KEPT: 3, NEWLY_PRUNED: 4}
FIELDS = ("partition_decision", "stats_decision", "kept",
          "declared_stats_decision", "would_declared_prune")
STAT_FIELDS = ("type", "min", "max", "null_count")


class ReplayError(LakePruneError):
    code = "E_REPLAY"


@dataclass(frozen=True)
class Snapshot:
    report: Mapping[str, Any]
    lake_root: Path
    table: str
    fmt: str
    partition_columns: tuple[str, ...]
    types: Mapping[str, str]
    historical: Mapping[str, Mapping[str, Any]]
    declared: Mapping[str, Mapping[str, Any]]
    row_count: int | None
    partition: Mapping[str, Any]


def _leaves(plan: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    out: list[Mapping[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("kind") in {"comparison", "in", "between", "is_null"}:
                out.append(node)
            for child in node.get("children", []):
                walk(child)

    walk(plan.get("predicate", {}).get("ast", {}))
    return out


def _leaf_type(leaf: Mapping[str, Any]) -> str:
    node = leaf.get("literal") or leaf.get("low")
    if node is not None:
        return str(node.get("type", "string"))
    values = leaf.get("values") or []
    return str(values[0].get("type", "string")) if values else "string"


def _load(db: str | Path, run_id: str, table: str, file_path: str) -> Snapshot:
    run = get_run(db, run_id)
    if run is None:
        raise ReplayError(f"historical run not found: {run_id}", "E_REPLAY_RUN_NOT_FOUND")
    report = get_report(db, run_id)
    if report is None:  # pragma: no cover - get_run already proves the row exists
        raise ReplayError(f"run has no report snapshot: {run_id}", "E_REPLAY_NO_REPORT")
    summary = next((s for s in report.get("manifests", []) if s.get("table") == table), None)
    if summary is None:
        raise ReplayError(f"report snapshot has no table: {table}", "E_REPLAY_TABLE_NOT_IN_REPORT")
    pcols = tuple(summary.get("partition_columns", ()))
    plans = [p for p in report.get("queries", []) if p.get("table") == table]
    checks = [c for c in report.get("stats_audit", {}).get("checks", [])
              if c.get("table") == table and c.get("file_path") == file_path]
    historical, hits = {}, []
    for plan in plans:
        for item in plan.get("files", []):
            if item.get("path") == file_path:
                historical[plan["query_id"]] = item
                hits.append(item)
    if not checks and not hits:
        raise ReplayError(
            f"report snapshot for run {run_id} has no file record: {table}/{file_path}",
            "E_REPLAY_FILE_NOT_IN_REPORT")
    types = {c["column"]: c["declared"].get("type", "string")
             for c in checks if c.get("column") != "*" and c.get("declared")}
    for plan in plans:
        for leaf in _leaves(plan):
            if leaf["column"] in pcols and leaf["column"] not in types:
                types[leaf["column"]] = _leaf_type(leaf)
    declared = {c["column"]: c["declared"] for c in checks
                if c.get("column") != "*" and c.get("declared")}
    first = hits[0] if hits else {}
    return Snapshot(report, Path(run["lake_root"]), table, str(summary.get("format", "csv")),
                    pcols, types, historical, declared, first.get("row_count"),
                    dict(first.get("partition", {})))


def _entry(file_path: str, snap: Snapshot, stats: Mapping[str, Mapping[str, Any]],
           row_count: int) -> FileEntry:
    return FileEntry(file_path, snap.partition, row_count, 0, "", {
        name: ColumnStats(str(d.get("type", "string")), d.get("min"), d.get("max"),
                          d.get("null_count")) for name, d in stats.items()})


def _differ(declared: Mapping[str, Any] | None, actual: ColumnStats) -> bool:
    return declared is None or any(
        declared.get(f) != getattr(actual, f) for f in STAT_FIELDS)


def _recompute(snap: Snapshot, file_path: str) -> tuple[FileEntry, tuple[dict[str, Any], ...]]:
    path = safe_join(snap.lake_root, f"{snap.table}/{file_path}")
    if not path.is_file():
        raise ReplayError(f"current data file is missing: {path}", "E_REPLAY_DATA_MISSING")
    try:
        rows, _ = read_data_rows(path, snap.fmt)
    except StatsAuditError as exc:
        raise ReplayError(f"cannot read current data file {path}: {exc.message}",
                          "E_REPLAY_DATA_READ") from exc
    except OSError as exc:
        raise ReplayError(f"current data file is unreadable: {path}: {exc}",
                          "E_REPLAY_DATA_READ") from exc
    stats, out = {}, []
    for column, ctype in sorted(snap.types.items()):
        if column in snap.partition_columns:
            continue
        actual = recompute_column(rows, snap.fmt, column, ctype)
        stats[column] = actual
        declared = snap.declared.get(column)
        out.append({"column": column, "declared": declared, "actual": actual,
                    "matches": declared is not None and all(
                        declared.get(f) == actual.get(f) for f in STAT_FIELDS)})
    return _entry(file_path, snap, stats, len(rows)), tuple(out)


def _partition(snap: Snapshot, file_path: str) -> dict[str, Any]:
    return dict(parse_partition_path(
        file_path, snap.partition_columns,
        {n: snap.types.get(n, "string") for n in snap.partition_columns}, snap.table).values)


def _replay(plan: Mapping[str, Any], file_path: str, snap: Snapshot,
            actual: FileEntry, declared_entry: FileEntry,
            newly: tuple[str, ...]) -> dict[str, Any]:
    expr = parse_predicate(plan["where"], snap.types)
    referenced = set(referenced_columns(expr))
    history = snap.historical.get(plan["query_id"])
    partition = snap.partition or _partition(snap, file_path)
    if history is not None and history.get("partition_decision") == "pruned":
        replayed = {"partition_decision": "pruned", "stats_decision": "skipped_partition",
                    "kept": False,
                    "declared_stats_decision": history.get("declared_stats_decision", ""),
                    "would_declared_prune": history.get("would_declared_prune", False)}
    else:
        presult = evaluate_partition(expr, partition, snap.partition_columns)
        if presult.decision == "pruned":
            replayed = {"partition_decision": "pruned", "stats_decision": "skipped_partition",
                        "kept": False, "declared_stats_decision": "",
                        "would_declared_prune": False}
        else:
            untrusted = {c for c in referenced if c not in snap.partition_columns
                         and _differ(snap.declared.get(c), actual.columns.get(c))}
            outcome = evaluate_file(expr, actual, snap.types, untrusted,
                                    snap.partition_columns, partition)
            kept = outcome.decision != MUST_NOT_MATCH
            hist_untrusted = set(history.get("untrusted_columns", ())) if history else set()
            relevant = tuple(sorted(referenced & hist_untrusted))
            ddecision, would = "", False
            if relevant:  # Gated like prune.py; always historical declared stats.
                d = declared_stats_decision(expr, declared_entry, snap.types,
                                            snap.partition_columns)
                ddecision, would = d.decision, kept and d.decision == MUST_NOT_MATCH
            replayed = {"partition_decision": presult.decision,
                        "stats_decision": outcome.decision, "kept": kept,
                        "declared_stats_decision": ddecision, "would_declared_prune": would}
    diffs = [] if history is None else [
        {"field": f, "direction": "recorded->replayed",
         "recorded": history.get(f), "replayed": replayed[f]}
        for f in FIELDS if history.get(f) != replayed[f]]
    return {"query_id": plan["query_id"], "where": plan["where"],
            "recorded": None if history is None else {f: history.get(f) for f in FIELDS},
            "replayed": replayed, "differences": diffs,
            "status": _status(history, replayed, newly)}


def _status(history: Mapping[str, Any] | None, replayed: Mapping[str, Any],
            newly: tuple[str, ...]) -> str:
    if history is None:
        return NEWLY_PRUNED if not replayed["kept"] else STILL_KEPT
    if all(history.get(f) == replayed[f] for f in FIELDS):
        return UNCHANGED
    if history.get("kept") is True and not replayed["kept"]:
        return NEWLY_PRUNED
    if history.get("kept") is False and replayed["kept"] is True:
        return STILL_KEPT
    return NEWLY_UNTRUSTED if newly else CHANGED


def replay_file(db_path: str | Path, run_id: str, table: str, file_path: str) -> dict[str, Any]:
    """Replay one data file against every query recorded for its table."""
    snap = _load(db_path, run_id, table, file_path)
    actual, checks = _recompute(snap, file_path)
    newly = tuple(sorted(c["column"] for c in checks
                         if c["declared"] is not None and not c["matches"]))
    declared_entry = _entry(file_path, snap, snap.declared,
                            snap.row_count if snap.row_count is not None else actual.row_count)
    queries = [_replay(plan, file_path, snap, actual, declared_entry, newly)
               for plan in sorted(snap.report.get("queries", []), key=lambda p: p["query_id"])
               if plan.get("table") == table]
    statuses = [q["status"] for q in queries]
    return {"run_id": run_id, "table": table, "file_path": file_path,
            "lake_root": str(snap.lake_root),
            "status": max(statuses, key=lambda s: RANK[s]) if statuses else UNCHANGED,
            "newly_untrusted_columns": list(newly),
            "baselines": {"declared_source": "runs.report_json snapshot",
                          "actual_source": "recomputed from current disk data",
                          "historical_row_count": snap.row_count,
                          "actual_row_count": actual.row_count},
            "actual_stats_checks": list(checks), "queries": queries}
