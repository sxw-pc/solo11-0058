"""Self-contained, offline HTML audit report with no external assets."""

from __future__ import annotations

from html import escape
from typing import Any, Iterable

from lakeprune.risks import summarize_risks

_STYLE = """
:root { color-scheme:light dark; --bg:#f6f3ea; --panel:#fffdf6; --ink:#1b2421; --muted:#5d6b63; --accent:#235e52; --line:#d9d4c7; --high:#9b2c2c; --warn:#8a5a12; --info:#315d83; }
@media (prefers-color-scheme:dark) { :root { --bg:#171b18; --panel:#202721; --ink:#e8eee8; --muted:#a8b8ad; --accent:#8fd0bd; --line:#39443b; --high:#ff9b95; --warn:#f1c36d; --info:#9fc9ef; } }
body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 ui-monospace, SFMono-Regular, Consolas, monospace; }
main { max-width:1180px; margin:0 auto; padding:28px 20px 64px; }
h1,h2,h3 { color:var(--accent); line-height:1.25; }
h1 { margin-bottom:.2em; }
.meta { color:var(--muted); }
.panel { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:16px; margin:18px 0; overflow:auto; }
table { border-collapse:collapse; width:100%; font-size:13px; }
th,td { border-bottom:1px solid var(--line); text-align:left; padding:6px 8px; vertical-align:top; }
th { color:var(--muted); font-weight:600; }
code, pre { font:12px/1.5 ui-monospace, SFMono-Regular, Consolas, monospace; }
pre { white-space:pre-wrap; word-break:break-word; }
.level-high { color:var(--high); font-weight:700; }
.level-warn { color:var(--warn); font-weight:700; }
.level-info { color:var(--info); font-weight:700; }
.kv { display:flex; flex-wrap:wrap; gap:12px 28px; }
.kv span { color:var(--muted); }
footer { margin-top:32px; color:var(--muted); font-size:12px; }
"""

class Html(str):
    """Trusted markup produced inside this module."""

def _rows(items: Iterable[Iterable[Any]]) -> str:
    rendered = []
    for row in items:
        cells = "".join(
            f"<td>{str(item) if isinstance(item, Html) else escape(str(item))}</td>"
            for item in row
        )
        rendered.append(f"<tr>{cells}</tr>")
    return "".join(rendered)

def render_html(report: dict[str, Any]) -> str:
    risk_summary = summarize_risks(report["risks"])
    summary = report["summary"]
    table_rows = _rows(
        [
            [item["table"], item["format"], item["manifest_path"], item["file_count"],
             item["row_count"], item["size_bytes"], item["fingerprint"]]
            for item in report["manifests"]
        ]
    )
    risk_rows = _rows(
        [
            [
                Html(f'<span class="level-{escape(item["level"])}">{escape(item["level"])}</span>'),
                item["code"], item["query_id"] or "-", item["table"],
                item["file_path"] or "-", item["risk_id"], item["description"],
            ]
            for item in report["risks"]
        ]
    )
    problem_checks = [
        item for item in report["stats_audit"]["checks"] if item["status"] != "ok"
    ]
    check_rows = _rows(
        [
            [item["table"], item["file_path"], item["column"], item["status"], item["message"]]
            for item in problem_checks
        ]
    )
    query_html: list[str] = []
    for plan in report["queries"]:
        file_rows = []
        for item in plan["files"]:
            file_rows.append(
                f"<tr><td>{escape(item['path'])}</td>"
                f"<td>{escape(str(item['partition']))}</td>"
                f"<td>{escape(item['partition_decision'])}</td>"
                f"<td>{escape(item['stats_decision'])}</td>"
                f"<td>{escape(str(item['kept']))}</td>"
                f"<td>{escape('; '.join(item['partition_reasons'] + item['stats_reasons']))}</td></tr>"
            )
        query_html.append(
            f"<section class=\"panel\"><h3>{escape(plan['query_id'])} "
            f"&#183; {escape(plan['table'])}</h3>"
            f"<p>{escape(plan['description'])}</p>"
            f"<p>Predicate: <code>{escape(plan['predicate']['rendered'])}</code></p>"
            f"<div class=\"kv\"><span>files {plan['summary']['candidate_files']} / "
            f"{plan['summary']['total_files']}</span>"
            f"<span>bytes {plan['summary']['estimated_scan_bytes']} / "
            f"{plan['summary']['total_bytes']}</span>"
            f"<span>prune ratio {plan['summary']['prune_ratio']}</span>"
            f"<span>partitions {escape(str(plan['partitions']))}</span></div>"
            "<table><thead><tr><th>File</th><th>Partition</th><th>Partition decision</th>"
            "<th>Stats decision</th><th>Kept</th><th>Reasons</th></tr></thead><tbody>"
            + "".join(file_rows)
            + "</tbody></table></section>"
        )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(report['project'])} Audit Report</title>
<style>{_STYLE}</style>
</head>
<body>
<main>
<h1>{escape(report['project'])} Audit Report</h1>
<p class="meta">Offline, read-only audit &#183; schema {escape(report['schema_version'])} &#183;
fingerprint <code>{escape(report['manifest_fingerprint'])}</code></p>
<section class="panel">
<h2>Summary</h2>
<div class="kv">
<span>tables {summary['table_count']}</span><span>files {summary['file_count']}</span>
<span>queries {summary['query_count']}</span>
<span>candidates {summary['candidate_files']}</span>
<span>scan bytes {summary['estimated_scan_bytes']}</span>
<span>high {risk_summary['level_counts'].get('high', 0)}</span>
<span>warn {risk_summary['level_counts'].get('warn', 0)}</span>
</div>
</section>
<section class="panel"><h2>Tables</h2>
<table><thead><tr><th>Table</th><th>Format</th><th>Manifest</th><th>Files</th><th>Rows</th>
<th>Bytes</th><th>Fingerprint</th></tr></thead><tbody>{table_rows}</tbody></table></section>
<h2>Query plans</h2>
{''.join(query_html)}
<section class="panel"><h2>Risks</h2>
<table><thead><tr><th>Level</th><th>Code</th><th>Query</th><th>Table</th>
<th>File</th><th>Risk ID</th><th>Description</th></tr></thead><tbody>{risk_rows}</tbody></table>
</section>
<section class="panel"><h2>Statistics audit</h2>
<div class="kv"><span>files {report['stats_audit']['summary']['files_checked']}</span>
<span>columns {report['stats_audit']['summary']['columns_checked']}</span>
<span>status {escape(str(report['stats_audit']['summary']['status_counts']))}</span>
<span>untrusted columns {report['stats_audit']['summary']['untrusted_columns']}</span></div>
<table><thead><tr><th>Table</th><th>File</th><th>Column</th><th>Status</th>
<th>Message</th></tr></thead><tbody>{check_rows}</tbody></table></section>
<footer>Generated locally by {escape(report['tool'])}. No external resources were loaded.</footer>
</main>
</body>
</html>
"""

def write_html(path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(render_html(report).encode("utf-8"))
