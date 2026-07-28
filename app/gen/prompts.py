"""Prompt templates and default prompt registry for AI generation.

All file types flow through the two-phase pipeline (test-item extraction → TC
generation), so prompt content lives here in one place to keep wording
consistent and easy to tweak.
"""

# Two-phase pipeline prompts (all file types use this pipeline)

FP_EXTRACT_PROMPT = """你是资深的软件测试工程师。请仔细阅读需求文档（含文字与图片），按文档结构提取**可测测试项**。

【测试项定义 — 必须比「功能点」更细】
- 测试项 = 一条可独立设计用例的能力、交互、校验规则或业务约束。
- 禁止把多个场景揉成一个粗功能点。例如「登录」必须拆成：正确账号密码登录、密码错误提示、账号锁定、验证码过期等独立测试项。
- 文档中的原型图/截图也要提取可见控件、流程与校验相关测试项。

【密度要求 — 必须遵守】
- 每个二级章节 / 每个主要界面：至少提取 3 个测试项（正常 + 异常/校验 + 边界或权限）。
- 列表/表单/图表/权限/状态流转：分别拆项，不要合并成一条。
- 扫一遍分类清单（有则必出）：增删改查、必填与格式校验、空态/超长/特殊字符、权限与越权、重复提交、提示文案与跳转、跨模块联动。

【提取规则】
1. 识别文档中的**一级 / 二级**章节或业务模块标题，module 必须与文档标题语义一致，不要自造无关名称。
2. module 格式固定为「一级」或「一级——二级」（中间用中文破折号 ——）；有二级章节时必须写成两级路径。
3. 将每个模块下的能力拆成可独立测试的测试项；不要把功能名拼进模块名。
4. 只有确实无法归属的全局能力才使用 module="通用"。
5. 信息不足时可合理推断，但不要遗漏文档已写明的规则与异常路径。
6. **禁止**把上传文件名、图片文件名、`文件N` / `===== … =====` 分隔标记当作 module；分隔条不是业务模块。

【优先级】
- P0：核心流程，缺失则系统不可用
- P1：重要功能，影响主要场景
- P2：辅助/体验类功能

【输出要求 — 必须严格遵守】
- 只输出一个 JSON 对象，不要 Markdown、表格、解释文字，也不要用 ``` 包裹
- 字段：module / name / category / desc / priority（英文键名）
- **优先写全量测试项**：宁可 desc 极短，也不要漏项；输出被截断等于失败
- desc 控制在一句内（约 20 字），勿展开步骤
- name 简洁可测，建议含场景意图
- module 示例：`登录注册` 或 `登录注册——验证码登录`

输出格式（紧凑单行亦可）：
{"functional_points":[{"module":"一级——二级","name":"测试项名","category":"增删改查|校验规则|交互反馈|权限控制|数据展示|其他","desc":"一句说明","priority":"P0|P1|P2"}]}

示例：
{"functional_points":[{"module":"登录注册——验证码登录","name":"手机号验证码登录成功","category":"增删改查","desc":"正确手机号验证码可登录","priority":"P0"},{"module":"登录注册——验证码登录","name":"验证码错误提示","category":"校验规则","desc":"错误验证码提示且不登录","priority":"P0"},{"module":"登录注册——账号安全","name":"登录失败锁定","category":"校验规则","desc":"密码错超5次锁30分钟","priority":"P0"}]}
"""

TC_GENERATE_PROMPT = """你是资深的软件测试工程师。请为以下**测试项**生成**功能测试用例**（业务场景验证，不限于 UI 操作细节）。

测试项详情：
{fp_descriptions}

【数量硬指标 — 必须严格遵守】
- 本批有 N 个测试项时，JSON 数组**至少**包含 N×3 条用例；禁止只生成 1～2 条。
- **每个测试项必须恰好覆盖三类**：1 条正常流程 + 1 条异常流程 + 1 条边界场景。
- 每条用例必须填写 scenario_type，且带上 fp_name（与测试项名称一致），便于核对覆盖。

【设计要求】
- module 必须与测试项详情中的模块路径一致（「一级」或「一级——二级」），不要自行修改或改用文件名
- title 建议含测试项意图 + 场景类型（如「…-正常」「…-密码错误」）
- 步骤描述业务操作与规则校验；可用【】标注关键界面元素
- steps 与 expected 数组长度尽量相等；预期结果必须可验证
- 信息不足时可合理推断，但不要遗漏明显场景

【输出要求 — 必须严格遵守】
- 只输出一个 JSON 数组，不要输出 Markdown 表格、标题或解释性文字
- 不要使用 ``` 代码块包裹
- 字段名必须使用英文

输出格式：
[
  {
    "fp_name": "对应测试项名称",
    "title": "用例标题（简洁，建议含测试项意图）",
    "module": "所属业务模块",
    "priority": "P0 | P1 | P2",
    "precondition": "前置条件",
    "steps": ["步骤1", "步骤2", "步骤3"],
    "expected": ["预期1", "预期2", "预期3"],
    "scenario_type": "正常流程 | 异常流程 | 边界场景"
  }
]

单测试项示例（必须输出 3 条）：
[
  {"fp_name":"手机号验证码登录成功","title":"正确手机号验证码登录-正常","module":"登录注册——验证码登录","priority":"P0","precondition":"已打开登录页且可收验证码","steps":["输入合法手机号","获取并输入正确验证码","点击【登录】"],"expected":["手机号校验通过","验证码校验通过","进入首页"],"scenario_type":"正常流程"},
  {"fp_name":"手机号验证码登录成功","title":"验证码错误无法登录-异常","module":"登录注册——验证码登录","priority":"P0","precondition":"已打开登录页","steps":["输入合法手机号","输入错误验证码","点击【登录】"],"expected":["提示验证码错误","停留在登录页","不进入首页"],"scenario_type":"异常流程"},
  {"fp_name":"手机号验证码登录成功","title":"验证码过期后登录-边界","module":"登录注册——验证码登录","priority":"P1","precondition":"验证码已超过有效期","steps":["输入合法手机号","输入过期验证码","点击【登录】"],"expected":["提示验证码过期或失效","可重新获取验证码","不进入首页"],"scenario_type":"边界场景"}
]
"""

TC_GENERATE_UI_PROMPT = """你是资深的 UI 自动化测试工程师。请为以下**测试项**生成**浏览器可执行的 UI 自动化用例**。

测试项详情：
{fp_descriptions}

【数量硬指标 — 必须严格遵守】
- 本批有 N 个测试项时，JSON 数组**至少**包含 N×3 条用例；禁止只生成 1～2 条。
- **每个测试项必须恰好覆盖三类**：1 条主路径成功 + 1 条异常/校验提示 + 1 条边界或状态类。
- 每条用例必须填写 scenario_type，且带上 fp_name（与测试项名称一致）。

【设计要求 — UI 自动化专用】
- module 必须与测试项详情中的模块路径一致（「一级」或「一级——二级」）
- 每一步必须是单一浏览器操作，使用客观可执行语言，并用【】标注**页面上真实可见**的控件文案或角色名
- 允许的操作类型语义：打开页面、点击、输入、选择、勾选、等待出现、断言文案/元素可见/URL
- 禁止：无法在页面观察的内部状态、纯接口调用、数据库校验、后台批处理；禁止一步多操作
- **steps 与 expected 数组长度必须完全相等**；中间步骤也要写可观测预期
- expected 必须是页面可观测结果（文案、按钮状态、跳转 URL、元素出现/消失）
- precondition 写清起始页面或登录态（如：已打开登录页 / 已登录普通用户）

【步骤写法 — 必须可被自动化执行】
- 【】内只写页面可见文案（按钮名、字段标签、选项文案、菜单项），**禁止**把「下拉框」「输入框」「按钮」「选择器」「文本框」写进【】或紧挨字段名拼成伪文案。
- 下拉/选择（好）：「在【单位】中选择【汉东省院】」或拆成两步「点击【单位】」→「选择【汉东省院】」。
- 下拉/选择（坏，禁止）：「单位下拉框选择【汉东省院】」「点击【单位下拉框】」「选择下拉框中的汉东省院」。
- 输入（好）：「在【用户名】输入 admin」「在【手机号】输入 13800138000」。
- 输入（坏，禁止）：「用户名输入框输入 admin」「在用户名输入框中填写…」（执行端会误找「输入框」文案）。
- 点击（好）：「点击【登录】」「点击【提交反馈】」。
- 点击（坏，禁止）：「点击登录按钮」（若页面按钮文案就是「登录」，必须写成【登录】）。
- 打开页面写清入口：「打开【问题反馈】页面」或「进入【系统设置】」。

【输出要求 — 必须严格遵守】
- 只输出一个 JSON 数组，不要输出 Markdown 表格、标题或解释性文字
- 不要使用 ``` 代码块包裹
- 字段名必须使用英文

输出格式：
[
  {
    "fp_name": "对应测试项名称",
    "title": "用例标题（简洁，含页面/操作意图）",
    "module": "所属业务模块",
    "priority": "P0 | P1 | P2",
    "precondition": "前置条件（页面/登录态）",
    "steps": ["打开【登录】页面", "在【用户名】输入 admin", "点击【登录】"],
    "expected": ["登录页加载完成", "用户名输入框显示 admin", "跳转到首页且右上角显示用户名"],
    "scenario_type": "正常流程 | 异常流程 | 边界场景"
  }
]
"""

FP_EXTRACT_FLOW_PROMPT = """你是资深的 UI 自动化测试工程师。请仔细阅读**操作手册 / 流程文档**（文字与截图按文档顺序交错出现），提取文档中描述的**操作流程**（SOP），而不是细粒度测试项。

【图文结合 — 必须遵守】
- 每一段说明文字必须与其前后相邻截图对照阅读，禁止只看文字忽略图片。
- 截图中的**红框 / 色框 / 圆圈 / 箭头 / 高亮**标出的区域 = 当前步骤的操作目标或关键信息；必须读出框内或紧邻的按钮文案、菜单项、输入框标签、提示语。
- 若文字写「点击此处」但未写控件名，以截图框选区域的可见文案为准写入 desc。

【流程定义】
- 一个「流程」= 文档中一段完整的业务操作路径（如：新增客户、审批通过、导出报表）。
- 以文档章节、编号步骤或连续截图序列为单位；同一主路径不要拆成正常/异常/边界多项。
- **禁止**为了覆盖率虚构文档未写明的异常、校验、边界场景。

【提取规则】
1. module 格式固定为「一级」或「一级——二级」（中文破折号 ——），取自文档章节标题。
2. name = 流程名称（简洁，与文档用语一致）。
3. desc = 按文档顺序的**逐步摘要**（可用分号或「→」连接），每步尽量含【控件可见文案】；可多句，须覆盖主路径关键点击/输入，勿只写一句空泛概述。
4. category 可用「操作流程」或「其他」。
5. priority：核心主路径 P0，次要 P1/P2。
6. **禁止**把上传文件名、图片文件名、分隔标记当作 module。

【输出要求 — 必须严格遵守】
- 只输出一个 JSON 对象，不要 Markdown、解释或 ``` 包裹
- 字段：module / name / category / desc / priority

输出格式：
{"functional_points":[{"module":"一级——二级","name":"流程名","category":"操作流程","desc":"打开【…】→ 点击【…】→ 在【…】输入 …","priority":"P0|P1|P2"}]}
"""

TC_GENERATE_FLOW_PROMPT = """你是资深的 UI 自动化测试工程师。请根据以下**操作流程摘要**，并结合用户消息中附带的**手册原文与截图**（若有），生成**浏览器可执行的 UI 自动化用例**。

流程详情：
{fp_descriptions}

【图文结合 — 必须遵守】
- 用户消息若含图片：必须逐张查看；红框/色框/圆圈/箭头标出的控件与文案优先写入 steps。
- 文字步骤与相邻截图不一致时，以截图框选区域的可见文案为准，并用【】原样标注。
- 禁止只根据流程摘要臆造控件名；摘要缺细节时必须从截图与原文补全。

【数量硬指标 — 必须严格遵守】
- 本批有 N 个流程时，JSON 数组**通常恰好 N 条**（每个流程 1 条主路径用例）。
- **禁止**为同一流程再造异常/边界用例，除非文档正文明确写出该分支。
- 每条必须带 fp_name（与流程 name 一致），scenario_type 填「文档流程」。

【忠实还原 — 必须遵守】
- 步骤顺序、控件文案、输入示例必须来自文档/截图；禁止臆造文档未出现的页面或按钮。
- 截图可见的按钮、输入框、提示文案优先用【】原样标注。
- 文档未写清的细节可写「按页面实际」类保守表述，但不要发明新步骤。

【预期结果 / 断言 — 必须来自文档】
- expected 只能写手册正文或截图中**已经写明/展示**的结果（成功提示、跳转说明、界面状态、校验文案等）。
- **禁止**自行编写通用断言（如「页加载完成」「输入框显示 xxx」「跳转成功」），也**禁止**填写「文档未写明预期」之类占位语。
- 若某步文档没有给出可观察结果：该步 expected 填空字符串 `""`，不要编造、不要写占位说明。
- steps 与 expected **长度须相等**；有文档断言的步骤原样摘录（可用【】标关键文案），无则对应位置为 `""`。

【设计要求 — UI 自动化】
- module 与流程详情中的模块路径一致
- 每一步单一浏览器操作；steps 与 expected **长度必须相等**
- 【】内只写页面可见文案；禁止「单位下拉框选择【…】」「点击【单位下拉框】」「在用户名输入框输入…」等把「下拉框/输入框/按钮」当页面文案的写法
- 下拉：写「在【单位】中选择【汉东省院】」，或拆成「点击【单位】」+「选择【汉东省院】」
- 输入：写「在【用户名】输入 admin」；点击：写「点击【登录】」
- precondition 写清起始页面或登录态（仅当文档有写；否则写「按文档起始页」）
- 允许语义：打开页面、点击、输入、选择、勾选、等待出现、断言文案/元素/URL

【输出要求】
- 只输出 JSON 数组，无 Markdown / ``` / 解释
- 字段名英文

输出格式：
[
  {
    "fp_name": "对应流程名称",
    "title": "用例标题（与文档流程名一致或加「-文档流程」）",
    "module": "所属业务模块",
    "priority": "P0 | P1 | P2",
    "precondition": "文档写明的前置/起始（无则「按文档起始页」）",
    "steps": ["打开【…】页面", "在【…】输入 …", "点击【…】"],
    "expected": ["", "", "出现文档所述【提交成功】提示"],
    "scenario_type": "文档流程"
  }
]
"""

# Number of test items to bundle into a single Phase-2 batch.
# Smaller batches reduce truncation and improve per-item coverage.
FP_BATCH_SIZE = 2
# Minimum test cases expected per test item (normal + exception + boundary).
MIN_TCS_PER_ITEM = 3
# Flow-manual mode: one main-path case per extracted flow.
MIN_TCS_PER_FLOW = 1


def get_default_prompts() -> dict:
    """返回默认提示词模板字典（key → {label, content}）。"""
    return {
        "fp_extract": {
            "label": "测试项提取",
            "content": FP_EXTRACT_PROMPT.strip(),
        },
        "fp_extract_flow": {
            "label": "流程手册提取",
            "content": FP_EXTRACT_FLOW_PROMPT.strip(),
        },
        "tc_generate": {
            "label": "功能用例生成",
            "content": TC_GENERATE_PROMPT.strip(),
        },
        "tc_generate_ui": {
            "label": "UI自动化用例生成",
            "content": TC_GENERATE_UI_PROMPT.strip(),
        },
        "tc_generate_flow": {
            "label": "流程手册UI用例生成",
            "content": TC_GENERATE_FLOW_PROMPT.strip(),
        },
    }


def pick_fp_prompt_key(skills: list | None) -> str:
    """Choose FP extract prompt key from Agent skills."""
    skills = skills or []
    if "fp_extract_flow" in skills:
        return "fp_extract_flow"
    return "fp_extract"


def pick_tc_prompt_key(skills: list | None) -> str:
    """Choose TC prompt key from Agent skills (flow > UI > functional)."""
    skills = skills or []
    if "tc_generate_flow" in skills:
        return "tc_generate_flow"
    if "tc_generate_ui" in skills:
        return "tc_generate_ui"
    return "tc_generate"


def min_tcs_per_item(skills: list | None = None, *, tc_prompt_key: str | None = None) -> int:
    """Minimum TCs expected per FP/flow for supplemental generation."""
    if tc_prompt_key == "tc_generate_flow":
        return MIN_TCS_PER_FLOW
    skills = skills or []
    if "tc_generate_flow" in skills or "fp_extract_flow" in skills:
        return MIN_TCS_PER_FLOW
    return MIN_TCS_PER_ITEM


__all__ = [
    "FP_EXTRACT_PROMPT",
    "FP_EXTRACT_FLOW_PROMPT",
    "TC_GENERATE_PROMPT",
    "TC_GENERATE_UI_PROMPT",
    "TC_GENERATE_FLOW_PROMPT",
    "FP_BATCH_SIZE",
    "MIN_TCS_PER_ITEM",
    "MIN_TCS_PER_FLOW",
    "get_default_prompts",
    "pick_fp_prompt_key",
    "pick_tc_prompt_key",
    "min_tcs_per_item",
]
