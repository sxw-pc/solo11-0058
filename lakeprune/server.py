"""Read-only loopback HTTP viewer for the local audit ledger."""

from __future__ import annotations

import json
import re
import sqlite3
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from lakeprune import __project_name__
from lakeprune import storage

_RUN_ID = re.compile(r"^[A-Za-z0-9._-]+$")
_CSS = """
:root { color-scheme:light dark; --bg:#f6f3ea; --panel:#fffdf6; --ink:#1b2421; --muted:#5d6b63; --accent:#235e52; --line:#d9d4c7; --high:#9b2c2c; --warn:#8a5a12; }
@media (prefers-color-scheme:dark) { :root { --bg:#171b18; --panel:#202721; --ink:#e8eee8; --muted:#a8b8ad; --accent:#8fd0bd; --line:#39443b; --high:#ff9b95; --warn:#f1c36d; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:14px/1.55 ui-monospace, SFMono-Regular, Consolas, monospace; }
main { max-width:1180px; margin:0 auto; padding:24px 18px 56px; }
a,h1,h2,h3 { color:var(--accent); line-height:1.25; }
.panel { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:14px 16px; margin:16px 0; overflow:auto; }
table { width:100%; border-collapse:collapse; font-size:12px; }
th,td { border-bottom:1px solid var(--line); padding:6px 7px; text-align:left; vertical-align:top; }
th,.muted { color:var(--muted); }
code { font-size:12px; }
.level-high { color:var(--high); font-weight:700; }
.level-warn { color:var(--warn); font-weight:700; }
.kv { display:flex; flex-wrap:wrap; gap:8px 22px; }
"""

class LakePruneServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, db_path: Path):
        self.db_path = db_path
        super().__init__(address, _Handler)

class Html(str):
    """A small trusted-markup marker for cells built inside this module."""

def _cell_html(cell: object) -> str:
    return str(cell) if isinstance(cell, Html) else escape(str(cell))

def _table(headers: list[str], rows: list[list[object]]) -> str:
    head = "".join(f"<th>{escape(item)}</th>" for item in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{_cell_html(cell)}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"

def _layout(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title><style>{_CSS}</style></head>
<body><main><h1>{escape(__project_name__)} / lakeprune</h1>
<p class="muted">Local read-only pruning audit ledger. No write endpoints.</p>
{body}</main></body></html>
"""

def _index_body(db_path: Path) -> str:
    runs = storage.list_runs(db_path, limit=100)
    rows = [
        [
            Html(f'<a href="/run/{escape(run["run_id"])}">{escape(run["run_id"])}</a>'),
            run["started_at"],
            run["manifest_fingerprint"][:16],
            run["query_count"],
            run["exit_code"],
            run["high_risks"],
            run["warn_risks"],
        ]
        for run in runs
    ]
    content = _table(
        ["Run", "Created (UTC)", "Fingerprint", "Queries", "Exit", "High", "Warn"], rows
    )
    if not runs:
        content = "<p>No audit runs have been recorded yet.</p>"
    return f'<section class="panel"><h2>Runs</h2>{content}</section>'

def _run_body(db_path: Path, run_id: str) -> str | None:
    run = storage.get_run(db_path, run_id)
    report = storage.get_report(db_path, run_id)
    if run is None or report is None:
        return None
    summary = report["summary"]
    risk_rows = [
        [
            Html(f'<span class="level-{escape(risk["level"])}">{escape(risk["level"])}</span>'),
            risk["code"],
            risk["query_id"] or "-",
            risk["table"],
            risk["file_path"] or "-",
            risk["description"],
        ]
        for risk in report["risks"]
    ]
    query_rows = [
        [
            plan["query_id"],
            plan["table"],
            plan["summary"]["total_files"],
            plan["summary"]["candidate_files"],
            plan["summary"]["estimated_scan_bytes"],
            plan["summary"]["prune_ratio"],
            Html(f"<code>{escape(plan['predicate']['rendered'])}</code>"),
        ]
        for plan in report["queries"]
    ]
    files = []
    for plan in report["queries"]:
        for item in plan["files"]:
            files.append(
                [
                    plan["query_id"],
                    item["path"],
                    item["partition_decision"],
                    item["stats_decision"],
                    item["kept"],
                    "; ".join(item["partition_reasons"] + item["stats_reasons"]),
                ]
            )
    problems = [
        [
            check["table"],
            check["file_path"],
            check["column"],
            check["status"],
            check["message"],
        ]
        for check in report["stats_audit"]["checks"]
        if check["status"] != "ok"
    ]
    body = [
        '<section class="panel"><h2>Run</h2><div class="kv">',
        f"<span>id <code>{escape(run_id)}</code></span>",
        f"<span>started {escape(run['started_at'])}</span>",
        f"<span>finished {escape(run['finished_at'])}</span>",
        f"<span>exit {run['exit_code']}</span>",
        f"<span>high {summary['risk_counts'].get('high', 0)}</span>",
        f"<span>warn {summary['risk_counts'].get('warn', 0)}</span>",
        f"<span>fingerprint <code>{escape(report['manifest_fingerprint'])}</code></span>",
        "</div></section>",
        '<section class="panel"><h2>Query plans</h2>',
        _table(
            ["Query", "Table", "Files", "Candidates", "Bytes", "Prune ratio", "Predicate"],
            query_rows,
        ),
        "</section>",
        '<section class="panel"><h2>File decisions</h2>',
        _table(["Query", "File", "Partition", "Stats", "Kept", "Reasons"], files),
        "</section>",
        '<section class="panel"><h2>Risks</h2>',
        _table(["Level", "Code", "Query", "Table", "File", "Description"], risk_rows)
        if risk_rows
        else "<p>No risks.</p>",
        "</section>",
        '<section class="panel"><h2>Non-OK statistics checks</h2>',
        _table(["Table", "File", "Column", "Status", "Message"], problems)
        if problems
        else "<p>No inconsistent statistics.</p>",
        "</section>",
    ]
    return "".join(body)

class _Handler(BaseHTTPRequestHandler):
    server: LakePruneServer

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        return

    def _send(self, status: int, content_type: str, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def _html(self, status: int, text: str) -> None:
        self._send(status, "text/html; charset=utf-8", text.encode("utf-8"))

    def _json(self, status: int, data: dict) -> None:
        payload = (json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        self._send(status, "application/json; charset=utf-8", payload)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path == "/healthz":
                storage.get_run(self.server.db_path, "__health__")
                self._json(200, {"status": "ok", "tool": "lakeprune"})
                return
            if path == "/favicon.ico":
                self._send(204, "image/x-icon", b"")
                return
            if path == "/":
                self._html(200, _layout("Runs", _index_body(self.server.db_path)))
                return
            match = re.fullmatch(r"/run/([^/]+)", path)
            if match:
                run_id = match.group(1)
                if not _RUN_ID.fullmatch(run_id):
                    self._html(404, _layout("Not found", "<p>Unknown run.</p>"))
                    return
                body = _run_body(self.server.db_path, run_id)
                if body is None:
                    self._html(404, _layout("Not found", "<p>Unknown run.</p>"))
                else:
                    self._html(200, _layout(run_id, body))
                return
            self._html(404, _layout("Not found", "<p>No such read-only page.</p>"))
        except sqlite3.Error:
            self._html(500, _layout("Error", "<p>The local ledger could not be read.</p>"))

    def _read_only(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if 0 < length <= 1_048_576:
            self.rfile.read(length)
        self.send_response(405)
        self.send_header("Allow", "GET")
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_HEAD = _read_only
    do_POST = _read_only
    do_PUT = _read_only
    do_PATCH = _read_only
    do_DELETE = _read_only
    do_OPTIONS = _read_only

def create_server(db_path: str | Path, port: int = 0, host: str = "127.0.0.1") -> LakePruneServer:
    if host not in {"127.0.0.1", "::1"}:
        raise ValueError("the viewer may only bind to a loopback address")
    path = Path(db_path)
    storage.init_db(path)
    return LakePruneServer((host, int(port)), path)

def serve(db_path: str | Path, port: int = 0, host: str = "127.0.0.1") -> None:
    server = create_server(db_path, port, host)
    address = server.server_address
    print(f"lakeprune viewer listening on http://{address[0]}:{address[1]}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
