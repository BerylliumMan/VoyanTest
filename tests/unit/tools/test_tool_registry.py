"""T008：工具注册表单测（红→绿）。"""

import pytest

from core.tools.registry import ToolSpec, ToolRegistry


def _spec(name="tool_a", **over):
    base = dict(
        name=name,
        description="演示工具",
        parameters={
            "type": "object",
            "properties": {"limit": {"type": "integer"}, "q": {"type": "string"}},
            "required": ["limit"],
        },
        side_effect="read",
        requires_confirmation=False,
        handler=lambda ctx, **a: None,
    )
    base.update(over)
    return ToolSpec(**base)


class TestToolSpec:
    def test_invalid_side_effect_rejected(self):
        with pytest.raises(ValueError):
            _spec(side_effect="write")  # 允许值：read/mutate/execute

    def test_parameters_must_be_object_schema(self):
        with pytest.raises(ValueError):
            _spec(parameters={"properties": {}})  # 缺 type=object

    def test_valid_spec_fields(self):
        spec = _spec(name="search_cases", side_effect="read", requires_confirmation=False)
        assert spec.name == "search_cases"
        assert spec.side_effect == "read"
        assert spec.requires_confirmation is False


class TestToolRegistry:
    def test_register_and_get(self, registry, make_tool):
        spec = make_tool(name="list_projects")
        registry.register(spec)
        assert registry.get("list_projects") is spec
        assert registry.get("nope") is None
        assert registry.names() == ["list_projects"]

    def test_duplicate_name_rejected(self, registry, make_tool):
        registry.register(make_tool(name="dup"))
        with pytest.raises(ValueError):
            registry.register(make_tool(name="dup"))

    def test_registration_order_stable(self, registry, make_tool):
        registry.register(make_tool(name="b_tool"))
        registry.register(make_tool(name="a_tool"))
        assert registry.names() == ["b_tool", "a_tool"]  # 注册序，非字典序

    def test_as_openai_tools_format(self, registry, make_tool):
        registry.register(make_tool(
            name="list_projects",
            description="项目清单",
            parameters={"type": "object", "properties": {"limit": {"type": "integer"}}, "required": []},
        ))
        tools = registry.as_openai_tools()
        assert len(tools) == 1
        t = tools[0]
        assert t["type"] == "function"
        assert t["function"]["name"] == "list_projects"
        assert t["function"]["description"] == "项目清单"
        assert t["function"]["parameters"]["type"] == "object"

    def test_as_openai_tools_allow_filter(self, registry, make_tool):
        registry.register(make_tool(name="r1", side_effect="read"))
        registry.register(make_tool(name="m1", side_effect="mutate"))
        registry.register(make_tool(name="e1", side_effect="execute"))
        names = [t["function"]["name"] for t in registry.as_openai_tools(allow=lambda s: s.side_effect == "read")]
        assert names == ["r1"]

    def test_describe_markdown(self, registry, make_tool):
        registry.register(make_tool(
            name="list_projects",
            description="项目清单（权限内）",
            parameters={
                "type": "object",
                "properties": {"limit": {"type": "integer"}, "q": {"type": "string"}},
                "required": ["limit"],
            },
        ))
        registry.register(make_tool(name="edit_case", side_effect="mutate", requires_confirmation=True))
        md = registry.describe_markdown()
        assert "`list_projects`（只读）" in md
        assert "项目清单（权限内）" in md
        assert "limit(integer,必填)" in md
        assert "q(string,可选)" in md
        assert "`edit_case`（变更，需确认）" in md

    def test_describe_markdown_allow_filter(self, registry, make_tool):
        registry.register(make_tool(name="r1", side_effect="read"))
        registry.register(make_tool(name="m1", side_effect="mutate"))
        md = registry.describe_markdown(allow=lambda s: s.side_effect == "read")
        assert "`r1`" in md
        assert "`m1`" not in md
