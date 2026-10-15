"""T008：工具结果契约单测（红→绿）。"""

from core.tools.result import DEFAULT_MAX_BYTES, ToolResult, error_result, result_bytes, shape_result


class TestToolResult:
    def test_to_dict_shape(self):
        r = ToolResult(ok=True, summary="s", data={"a": 1})
        d = r.to_dict()
        assert set(d.keys()) == {"ok", "summary", "data", "truncated", "next_cursor", "error"}

    def test_error_result(self):
        r = error_result("未找到用例 #999")
        assert r.ok is False
        assert r.error == "未找到用例 #999"
        assert r.summary == "未找到用例 #999"


class TestShapeResult:
    def test_small_result_passthrough(self):
        data = {"items": [{"id": 1}], "total": 1, "offset": 0}
        r = shape_result(data, summary="1 条")
        assert r.ok and not r.truncated
        assert r.data == data
        assert r.next_cursor is None

    def test_none_result(self):
        r = shape_result(None, summary="")
        assert r.ok and r.data is None
        assert r.summary == "空结果"

    def test_items_slicing_truncated(self):
        items = [{"id": i, "name": f"用例-{i:03d}"} for i in range(100)]
        data = {"items": items, "total": 100, "offset": 10}
        r = shape_result(data, summary="分页", max_bytes=600)
        assert r.truncated is True
        kept = r.data["items"]
        assert 0 < len(kept) < 100
        assert result_bytes(r.data) <= 600
        assert r.next_cursor == 10 + len(kept)
        # 截断后保留元信息
        assert r.data["total"] == 100

    def test_oversize_non_items_dropped(self):
        data = {"blob": "x" * 5000}
        r = shape_result(data, summary="大对象", max_bytes=1000)
        assert r.truncated is True
        assert r.data is None
        assert r.summary == "大对象"

    def test_single_oversize_item(self):
        items = [{"blob": "y" * 5000}]
        data = {"items": items, "total": 1, "offset": 0}
        r = shape_result(data, summary="单项超限", max_bytes=500)
        assert r.truncated is True
        assert r.data["items"] == []
        assert r.next_cursor == 0

    def test_default_budget_constant(self):
        assert DEFAULT_MAX_BYTES == 16384
