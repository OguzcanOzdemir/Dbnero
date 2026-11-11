"""Export comparison results as JSON, CSV or a self-contained HTML report.

Row samples can contain sensitive data; they are only exported when
``include_row_data`` is explicitly enabled.
"""

from __future__ import annotations

import csv
import html
import io
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from dbnero import __version__
from dbnero.compare.data_diff import DataDiff
from dbnero.compare.schema_diff import SchemaDiff
from dbnero.core.models import utc_now

SENSITIVE_WARNING = (
    "This report contains sample rows from the databases and may include sensitive data."
)


@dataclass(slots=True)
class CompareReport:
    title: str
    schema: SchemaDiff | None = None
    data: DataDiff | None = None
    created_at_utc: datetime | None = None
    notes: list[str] | None = None


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, bytes | bytearray | memoryview):
        return "0x" + bytes(value).hex()
    return str(value)


def report_to_dict(report: CompareReport, include_row_data: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "title": report.title,
        "generated_by": f"dbnero {__version__}",
        "created_at_utc": (report.created_at_utc or utc_now()).isoformat(),
        "notes": list(report.notes or []),
        "contains_row_data": include_row_data,
    }
    if report.schema is not None:
        out["schema"] = {
            "left": report.schema.left_label,
            "right": report.schema.right_label,
            "identical": report.schema.identical,
            "summary": report.schema.summary(),
            "items": [i.to_dict() for i in report.schema.items],
        }
    if report.data is not None:
        tables = []
        for t in report.data.tables:
            entry: dict[str, Any] = {
                "table": t.table,
                "method": t.method.value,
                "left_rows": t.left_rows,
                "right_rows": t.right_rows,
                "added": t.added,
                "removed": t.removed,
                "changed": t.changed,
                "identical": t.identical,
                "key_columns": t.key_columns,
                "note": t.note,
            }
            if include_row_data:
                entry["columns"] = t.columns
                entry["samples"] = [
                    {
                        "kind": s.kind,
                        "key": _jsonable(s.key),
                        "left": _jsonable(s.left),
                        "right": _jsonable(s.right),
                    }
                    for s in t.samples
                ]
            tables.append(entry)
        out["data"] = {
            "left": report.data.left_label,
            "right": report.data.right_label,
            "identical": report.data.identical,
            "skipped_tables": report.data.skipped,
            "tables": tables,
        }
    return out


def export_json(report: CompareReport, path: Path, include_row_data: bool = False) -> None:
    data = report_to_dict(report, include_row_data)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")


def export_csv(report: CompareReport, path: Path, include_row_data: bool = False) -> None:
    """One row per schema difference and per table data result."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["section", "category", "change", "object", "detail", "left", "right"])
    if report.schema:
        for i in report.schema.items:
            w.writerow(
                [
                    "schema",
                    i.category.value,
                    i.kind.value,
                    i.object_name,
                    i.detail,
                    i.left or "",
                    i.right or "",
                ]
            )
    if report.data:
        for t in report.data.tables:
            detail = (
                f"method={t.method.value}; added={t.added}; removed={t.removed}; "
                f"changed={t.changed}; identical={t.identical}"
            )
            w.writerow(["data", "table", "rows", t.table, detail, t.left_rows, t.right_rows])
            if include_row_data:
                for s in t.samples:
                    w.writerow(
                        [
                            "data",
                            "row",
                            s.kind,
                            t.table,
                            repr(_jsonable(s.key)),
                            repr(_jsonable(s.left)),
                            repr(_jsonable(s.right)),
                        ]
                    )
    path.write_text(buf.getvalue(), "utf-8", newline="")


_CSS = """
body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:24px;color:#1d1d1f}
h1{font-size:22px}h2{font-size:18px;margin-top:28px}
table{border-collapse:collapse;width:100%;font-size:13px;margin:8px 0}
th,td{border:1px solid #d0d0d7;padding:4px 8px;text-align:left;vertical-align:top}
th{background:#f2f2f5}.added{color:#0a7d32}.removed{color:#b3261e}.changed{color:#8a5a00}
.warn{background:#fff4e5;border:1px solid #f0c36d;padding:8px;margin:12px 0}
code{white-space:pre-wrap;font-size:12px}.ok{color:#0a7d32}.bad{color:#b3261e}
"""


def export_html(report: CompareReport, path: Path, include_row_data: bool = False) -> None:
    e = html.escape
    data = report_to_dict(report, include_row_data)
    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        f"<title>{e(report.title)}</title><style>{_CSS}</style></head><body>",
        f"<h1>{e(report.title)}</h1>",
        f"<p>Created {e(data['created_at_utc'])} · {e(data['generated_by'])}</p>",
    ]
    if include_row_data:
        parts.append(f"<div class='warn'>{e(SENSITIVE_WARNING)}</div>")
    for note in data["notes"]:
        parts.append(f"<p>{e(note)}</p>")

    if "schema" in data:
        s = data["schema"]
        status = (
            "<span class='ok'>Identical</span>"
            if s["identical"]
            else (f"<span class='bad'>{len(s['items'])} differences</span>")
        )
        parts.append(f"<h2>Schema: {e(s['left'])} ↔ {e(s['right'])} — {status}</h2>")
        if s["items"]:
            parts.append(
                "<table><tr><th>Category</th><th>Change</th><th>Object</th><th>Detail</th>"
                "<th>Left</th><th>Right</th></tr>"
            )
            for i in s["items"]:
                parts.append(
                    f"<tr><td>{e(i['category'])}</td><td class='{e(i['kind'])}'>{e(i['kind'])}"
                    f"</td><td>{e(i['object_name'])}</td><td>{e(i['detail'] or '')}</td>"
                    f"<td><code>{e(i['left'] or '')}</code></td>"
                    f"<td><code>{e(i['right'] or '')}</code></td></tr>"
                )
            parts.append("</table>")

    if "data" in data:
        d = data["data"]
        parts.append(f"<h2>Data: {e(d['left'])} ↔ {e(d['right'])}</h2>")
        parts.append(
            "<table><tr><th>Table</th><th>Method</th><th>Left rows</th><th>Right rows</th>"
            "<th>Added</th><th>Removed</th><th>Changed</th><th>Result</th><th>Note</th></tr>"
        )
        for t in d["tables"]:
            res = {
                True: "<span class='ok'>Identical</span>",
                False: "<span class='bad'>Different</span>",
                None: "?",
            }[t["identical"]]
            parts.append(
                f"<tr><td>{e(t['table'])}</td><td>{e(t['method'])}</td><td>{t['left_rows']}</td>"
                f"<td>{t['right_rows']}</td><td class='added'>{t['added']}</td>"
                f"<td class='removed'>{t['removed']}</td><td class='changed'>{t['changed']}</td>"
                f"<td>{res}</td><td>{e(t['note'])}</td></tr>"
            )
        parts.append("</table>")
        if d["skipped_tables"]:
            parts.append(
                "<p>Tables present on one side only (data not compared): "
                + e(", ".join(d["skipped_tables"]))
                + "</p>"
            )
        if include_row_data:
            for t in d["tables"]:
                if not t.get("samples"):
                    continue
                parts.append(f"<h3>{e(t['table'])} — sample rows</h3><table>")
                parts.append("<tr><th>Change</th><th>Key</th><th>Left</th><th>Right</th></tr>")
                for smp in t["samples"]:
                    parts.append(
                        f"<tr><td class='{e(smp['kind'])}'>{e(smp['kind'])}</td>"
                        f"<td>{e(str(smp['key']))}</td><td><code>{e(str(smp['left']))}</code>"
                        f"</td><td><code>{e(str(smp['right']))}</code></td></tr>"
                    )
                parts.append("</table>")
    parts.append("</body></html>")
    path.write_text("\n".join(parts), "utf-8")


EXPORTERS = {".json": export_json, ".csv": export_csv, ".html": export_html}


def export_report(report: CompareReport, path: Path, include_row_data: bool = False) -> None:
    exporter = EXPORTERS.get(path.suffix.lower())
    if exporter is None:
        raise ValueError(f"Unsupported report format: {path.suffix}")
    exporter(report, path, include_row_data)
