import csv
import json
from datetime import date
from decimal import Decimal

import pytest

from dbnero.compare.data_diff import DataDiff, DataMethod, RowSample, TableDataDiff
from dbnero.compare.report import SENSITIVE_WARNING, CompareReport, export_report
from dbnero.compare.schema_diff import Category, ChangeKind, DiffItem, SchemaDiff


def _report():
    schema = SchemaDiff("a", "b", [DiffItem(Category.TABLE, ChangeKind.ADDED, "public.<x>")])
    data = DataDiff(
        "a",
        "b",
        [
            TableDataDiff(
                "public.t",
                DataMethod.KEYED,
                3,
                3,
                changed=1,
                identical=False,
                samples=[
                    RowSample(
                        "changed",
                        (1,),
                        (1, Decimal("1.5"), date(2026, 1, 1)),
                        (1, b"\x00\x01", None),
                    )
                ],
            )
        ],
    )
    return CompareReport("Rapor", schema, data)


def test_json_excludes_row_data_by_default(tmp_path):
    p = tmp_path / "r.json"
    export_report(_report(), p)
    data = json.loads(p.read_text("utf-8"))
    assert data["contains_row_data"] is False
    assert "samples" not in data["data"]["tables"][0]
    export_report(_report(), p, include_row_data=True)
    sample = json.loads(p.read_text("utf-8"))["data"]["tables"][0]["samples"][0]
    assert sample["left"] == [1, "1.5", "2026-01-01"] and sample["right"][1] == "0x0001"


def test_html_escapes_and_warns(tmp_path):
    p = tmp_path / "r.html"
    export_report(_report(), p, include_row_data=True)
    text = p.read_text("utf-8")
    assert "public.&lt;x&gt;" in text and SENSITIVE_WARNING in text
    export_report(_report(), p)
    assert SENSITIVE_WARNING not in p.read_text("utf-8")


def test_csv(tmp_path):
    p = tmp_path / "r.csv"
    export_report(_report(), p)
    rows = list(csv.reader(p.open(encoding="utf-8")))
    assert rows[0][0] == "section" and rows[1][:3] == ["schema", "table", "added"]
    assert rows[2][0] == "data"


def test_unknown_format(tmp_path):
    with pytest.raises(ValueError):
        export_report(_report(), tmp_path / "r.pdf")
