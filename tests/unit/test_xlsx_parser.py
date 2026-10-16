"""xlsx 解析器单测：多表 / 行截断 / 空表 / 空值处理。"""
import io

from openpyxl import Workbook

from app.gen.xlsx_parser import extract_text_from_xlsx


def _wb_bytes(build):
    wb = Workbook()
    build(wb)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def test_multi_sheet_and_values():
    def build(wb):
        ws = wb.active
        ws.title = "SheetA"
        ws.append(["编号", "名称"])
        ws.append(["1", "登录"])
        ws2 = wb.create_sheet("SheetB")
        ws2.append(["x", None, "z"])

    text = extract_text_from_xlsx(_wb_bytes(build))
    assert "工作表：SheetA" in text and "工作表：SheetB" in text
    assert "1\t登录" in text
    assert "x\t\tz" in text  # 空单元格保留位（制表符分隔）


def test_row_cap_marks_truncation():
    def build(wb):
        ws = wb.active
        for i in range(20):
            ws.append([f"r{i}"])

    text = extract_text_from_xlsx(_wb_bytes(build), max_rows=5)
    assert "r4" in text and "r5" not in text
    assert "已截断" in text


def test_empty_workbook_returns_headers_only():
    def build(wb):
        pass

    text = extract_text_from_xlsx(_wb_bytes(build))
    assert "工作表" in text
    # 只有标题行，无数据行
    assert "\t" not in text
