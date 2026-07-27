"""Prompt templates and default prompt registry for AI generation.

All file types flow through the two-phase pipeline (test-item extraction → TC
generation), so prompt content lives here in one place to keep wording
consistent and easy to tweak.
"""

# Two-phase pipeline prompts (all file types use this pipeline)

FP_EXTRACT_PROMPT = """你是资深的软件测试工程师。请仔细阅读需求文档（含文字与图片），按文档结构提取**可测测试项**。

【测试项定义 — 比「功能点」更细】
- 测试项 = 一条可独立设计用例的能力、交互、校验规则或业务约束。
- 不要把多个场景揉成一个粗功能点；例如「登录」应拆成：正确账号密码登录、密码错误、账号锁定、验证码过期等测试项。
- 文档中的原型图/截图也要提取可见控件、流程与校验相关测试项。

【提取规则】
1. 识别文档中的模块/章节标题，作为 module 字段原样使用，不要自行改名。
2. 将每个模块下的能力拆成可独立测试的测试项；不要把功能名拼进模块名。
3. 覆盖 UI 交互、数据操作、状态流转、权限、业务规则、通知与跨模块联动。
4. 只有确实无法归属的全局能力才使用 module="通用"。

【优先级】
- P0：核心流程，缺失则系统不可用
- P1：重要功能，影响主要场景
- P2：辅助/体验类功能

【输出要求 — 必须严格遵守】
- 只输出一个 JSON 对象，不要输出 Markdown 标题、表格或解释性文字
- 不要使用 ``` 代码块包裹
- 字段名必须使用英文：module / name / category / desc / priority
- name 为测试项名称（简洁、可测试）

输出格式：
{
  "functional_points": [
    {
      "module": "所属业务模块名称",
      "name": "测试项名称（简洁、可测试）",
      "category": "增删改查 | 校验规则 | 交互反馈 | 权限控制 | 数据展示 | 其他",
      "desc": "测试项简明描述（1-2句，含关键约束/边界）",
      "priority": "P0 | P1 | P2"
    }
  ]
}

示例：
{"functional_points":[{"module":"登录注册","name":"手机号验证码登录成功","category":"增删改查","desc":"用户输入正确手机号和验证码完成登录","priority":"P0"},{"module":"登录注册","name":"验证码错误提示","category":"校验规则","desc":"输入错误验证码时页面提示错误且不登录","priority":"P0"},{"module":"登录注册","name":"登录失败锁定","category":"校验规则","desc":"同一账号密码错误超过5次后锁定30分钟","priority":"P0"}]}
"""

TC_GENERATE_PROMPT = """你是资深的软件测试工程师。请为以下**测试项**生成**功能测试用例**（业务场景验证，不限于 UI 操作细节）。

测试项详情：
{fp_descriptions}

【设计要求 — 必须严格遵守】
- module 必须使用测试项详情中的模块名，不要自行修改
- **每个测试项必须至少生成 3 条用例：1 条正常流程 + 1 条异常流程 + 1 条边界场景；禁止只生成 1～2 条**
- 步骤描述业务操作与规则校验；可用【】标注关键界面元素
- 预期结果必须可验证；steps 与 expected 数量尽量一一对应
- 信息不足时可合理推断，但不要遗漏明显场景

【输出要求 — 必须严格遵守】
- 只输出一个 JSON 数组，不要输出 Markdown 表格、标题或解释性文字
- 不要使用 ``` 代码块包裹
- 字段名必须使用英文

输出格式：
[
  {
    "title": "用例标题（简洁，建议含测试项意图）",
    "module": "所属业务模块",
    "priority": "P0 | P1 | P2",
    "precondition": "前置条件",
    "steps": ["步骤1", "步骤2", "步骤3"],
    "expected": ["预期1", "预期2", "预期3"],
    "scenario_type": "正常流程 | 异常流程 | 边界场景"
  }
]
"""

TC_GENERATE_UI_PROMPT = """你是资深的 UI 自动化测试工程师。请为以下**测试项**生成**浏览器可执行的 UI 自动化用例**。

测试项详情：
{fp_descriptions}

【设计要求 — UI 自动化专用 — 必须严格遵守】
- module 必须使用测试项详情中的模块名
- **每个测试项必须至少生成 3 条用例：1 条主路径成功 + 1 条异常/校验提示 + 1 条边界或状态类；禁止只生成 1～2 条**
- 每一步必须是单一浏览器操作，使用客观可执行语言，并用【】标注控件可见文本或角色名
- 允许的操作类型语义：打开页面、点击、输入、选择、勾选、等待出现、断言文案/元素可见/URL
- 禁止：无法在页面观察的内部状态、纯接口调用、数据库校验、后台批处理
- **steps 与 expected 数组长度必须完全相等**；中间步骤也要写可观测预期（如“输入框显示已填内容”），不要把最终断言只写在 expected 前几项
- expected 必须是页面可观测结果（文案、按钮状态、跳转 URL、元素出现/消失）
- precondition 写清起始页面或登录态（如：已打开登录页 / 已登录普通用户）

【输出要求 — 必须严格遵守】
- 只输出一个 JSON 数组，不要输出 Markdown 表格、标题或解释性文字
- 不要使用 ``` 代码块包裹
- 字段名必须使用英文

输出格式：
[
  {
    "title": "用例标题（简洁，含页面/操作意图）",
    "module": "所属业务模块",
    "priority": "P0 | P1 | P2",
    "precondition": "前置条件（页面/登录态）",
    "steps": ["打开【登录】页面", "在【用户名】输入框输入 admin", "点击【登录】按钮"],
    "expected": ["登录页加载完成", "用户名输入框显示 admin", "跳转到首页且右上角显示用户名"],
    "scenario_type": "正常流程 | 异常流程 | 边界场景"
  }
]
"""

# Number of test items to bundle into a single Phase-2 batch.
FP_BATCH_SIZE = 3
# Minimum test cases expected per test item (normal + exception + boundary).
MIN_TCS_PER_ITEM = 3


def get_default_prompts() -> dict:
    """返回默认提示词模板字典（key → {label, content}）。"""
    return {
        "fp_extract": {
            "label": "测试项提取",
            "content": FP_EXTRACT_PROMPT.strip(),
        },
        "tc_generate": {
            "label": "功能用例生成",
            "content": TC_GENERATE_PROMPT.strip(),
        },
        "tc_generate_ui": {
            "label": "UI自动化用例生成",
            "content": TC_GENERATE_UI_PROMPT.strip(),
        },
    }


def pick_tc_prompt_key(skills: list | None) -> str:
    """Choose TC prompt key from Agent skills (UI skill wins when present)."""
    skills = skills or []
    if "tc_generate_ui" in skills:
        return "tc_generate_ui"
    return "tc_generate"


__all__ = [
    "FP_EXTRACT_PROMPT",
    "TC_GENERATE_PROMPT",
    "TC_GENERATE_UI_PROMPT",
    "FP_BATCH_SIZE",
    "MIN_TCS_PER_ITEM",
    "get_default_prompts",
    "pick_tc_prompt_key",
]
