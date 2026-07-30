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

# Shared step-writing contract for UI automation + flow-manual generation.
_UI_STEP_CONTRACT = """
【步骤原子化 — Instant 风格（对齐 Midscene Instant Action）— 必须严格遵守】
- **一步 = 一个浏览器动作**。禁止把「打开→填写→点击」写进同一步。
- 句式固定、控件文案进【】，便于执行端「先抽 Intent 再绑元素」，减少理解偏差。
- 步骤句式只能用下列模板（【】内为页面真实可见文案）：
  1) 打开/进入：`打开【页面或菜单名】` / `进入【模块名】`
  2) 点击：`点击【按钮/链接/菜单项文案】`
  3) 输入：`在【字段标签】输入 具体值`（值不要放进【】，除非值本身是选项文案）
  4) 选择：优先拆成 `点击【字段标签】` → `选择【选项文案】`；或 `在【字段标签】中选择【选项文案】`
  5) 勾选：`勾选【选项文案】` / `取消勾选【选项文案】`
  6) 等待：`等待【文案或区域】出现` / `等待页面加载完成`（跳转、提交、打开弹窗后必须加）
  7) 关闭弹窗：`点击【关闭】` 或 `点击消息框的【确定】/【关闭】`（【】内必须是按钮可见文案）
  8) 断言（仅当需要写在 steps 时）：`断言页面包含【文案】` / `断言【元素】可见`
- 若某步点击会**打开新浏览器标签页**：下一步按新标签页内容写（执行端会自动切到新标签）；不要假设仍停在原页。
- 【】内**只写**按钮名、字段标签、菜单项、选项、提示原文；**禁止**写入「按钮/输入框/下拉框/文本框/选择器/弹窗/对话框」等控件类型词。
- 禁止模糊主语：「点击提交」「填写表单」「选择单位」——必须带【】与完整句式。
- 禁止一步多目标：「在【用户名】输入 a 并在【密码】输入 b」必须拆成两步。
- 相近文案必须写全：提交≠确定≠保存；查询≠搜索；取消≠关闭。

【时序与稳定性 — 必须遵守】
- 打开页面 / 点击菜单 / 提交 / 登录之后：下一步优先写 `等待页面加载完成` 或 `等待【关键文案】出现`。
- 文档或场景会出现消息框/对话框时：先 `等待【提示文案或弹窗标题】出现`，再 `点击【关闭】/【确定】`；不要假设弹窗一定存在却直接点关闭。
- 若文档写「可能弹出提示」：正常路径写成「若出现则关闭」对应的两步（等待→关闭）；不要把不确定的弹窗当成必现硬点。
- 自定义下拉必须先展开再选：优先两步 `点击【单位】` → `选择【汉东省院】`。

【坏例子（禁止）→ 好例子】
- 坏：`单位下拉框选择【汉东省院】` → 好：`点击【单位】` + `选择【汉东省院】`
- 坏：`在用户名输入框输入 admin` → 好：`在【用户名】输入 admin`
- 坏：`点击登录按钮` → 好：`点击【登录】`
- 坏：`关闭弹出的消息框` → 好：`等待【登录成功】出现` + `点击【关闭】`（文案以页面为准）
- 坏：`填写登录信息并提交` → 好：拆成输入用户名、输入密码、点击【登录】、等待…
""".strip()

TC_GENERATE_UI_PROMPT = """你是资深的 UI 自动化测试工程师。请为以下**测试项**生成**浏览器可执行的 UI 自动化用例**。
生成结果将交给 Playwright/AI 执行：步骤含糊 = 定位失败。请宁可步骤多、也要可执行。

测试项详情：
{fp_descriptions}

【数量硬指标 — 必须严格遵守】
- 本批有 N 个测试项时，JSON 数组**至少**包含 N×3 条用例；禁止只生成 1～2 条。
- **每个测试项必须恰好覆盖三类**：1 条主路径成功 + 1 条异常/校验提示 + 1 条边界或状态类。
- 每条用例必须填写 scenario_type，且带上 fp_name（与测试项名称一致）。

【设计要求 — UI 自动化专用】
- module 必须与测试项详情中的模块路径一致（「一级」或「一级——二级」）
- title 含页面/操作意图；precondition 写清起始页面或登录态（如：已打开登录页 / 已登录普通用户）
- **steps 与 expected 数组长度必须完全相等**；中间步骤也要写可观测预期（文案、按钮状态、URL、元素出现/消失）
- 禁止：无法在页面观察的内部状态、纯接口调用、数据库校验、后台批处理
- 优先主路径与关键异常 UI，避免纯后台逻辑

""" + _UI_STEP_CONTRACT + """

【输出要求 — 必须严格遵守】
- 只输出一个 JSON 数组，不要 Markdown / 解释 / ``` 包裹
- 字段名必须使用英文

输出格式：
[
  {
    "fp_name": "对应测试项名称",
    "title": "用例标题（简洁，含页面/操作意图）",
    "module": "所属业务模块",
    "priority": "P0 | P1 | P2",
    "precondition": "前置条件（页面/登录态）",
    "steps": ["打开【登录】页面", "等待页面加载完成", "在【用户名】输入 admin", "在【密码】输入 Admin@123", "点击【登录】", "等待页面加载完成"],
    "expected": ["登录页打开", "登录表单可见", "【用户名】显示已输入", "【密码】已填写", "登录请求已触发", "进入系统首页或出现登录后界面"],
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
3. desc = 按文档顺序的**逐步摘要**（用「→」连接），**每一步必须可单独执行**：含操作动词 +【控件可见文案】；禁止只写「完成登录」「填写信息」这类空泛句。
4. desc 句式优先 Instant：`点击【登录】`、`在【用户名】输入 admin`、`在【单位】中选择【汉东省院】`；禁止「密码输入【x】」「点击登录按钮」「单位下拉框选择…」。
5. 手册中的等待/提示/关闭弹窗也要写进 desc（如：等待【提交成功】→ 点击【关闭】）。
6. category 可用「操作流程」或「其他」。
7. priority：核心主路径 P0，次要 P1/P2。
8. **禁止**把上传文件名、图片文件名、分隔标记当作 module。

【输出要求 — 必须严格遵守】
- 只输出一个 JSON 对象，不要 Markdown、解释或 ``` 包裹
- 字段：module / name / category / desc / priority

输出格式：
{"functional_points":[{"module":"一级——二级","name":"流程名","category":"操作流程","desc":"打开【…】→ 等待页面加载完成 → 点击【…】→ 在【用户名】输入 admin → 在【密码】输入 Admin@123 → 点击【登录】","priority":"P0|P1|P2"}]}
"""

TC_GENERATE_FLOW_PROMPT = """你是资深的 UI 自动化测试工程师。请根据以下**操作流程摘要**，并结合用户消息中附带的**手册原文与截图**（若有），生成**浏览器可执行的 UI 自动化用例**。
生成结果将交给 Playwright/AI 逐步执行：步骤含糊、并步骤、控件名臆造、预期只有编号没有正文 → 定位失败或断言无效。

流程详情：
{fp_descriptions}

【图文结合 — 必须遵守】
- 用户消息若含图片：必须逐张查看；红框/色框/圆圈/箭头标出的控件与文案优先写入 steps。
- 文字步骤与相邻截图不一致时，以截图框选区域的可见文案为准，并用【】原样标注。
- 禁止只根据流程摘要臆造控件名；摘要缺细节时必须从截图与原文补全。
- 手册编号步骤（1.2.3…）必须按顺序展开为 steps，**禁止合并或跳步**。

【数量硬指标 — 必须严格遵守】
- 本批有 N 个流程时，JSON 数组**通常恰好 N 条**（每个流程 1 条主路径用例）。
- **禁止**为同一流程再造异常/边界用例，除非文档正文明确写出该分支。
- 每条必须带 fp_name（与流程 name 一致），scenario_type 填「文档流程」。

【忠实还原 — 必须遵守】
- 步骤顺序、控件文案、输入示例必须来自文档/截图；禁止臆造文档未出现的页面或按钮。
- 截图可见的按钮、输入框、提示文案优先用【】原样标注。
- 文档未写清的细节可省略该细节，但不要发明新业务步骤。

【预期结果 / 断言 — 严禁空编号】
- expected 是与 steps **等长**的字符串数组；每一项要么是**有完整语义的可观察结果**，要么是空字符串 `""`。
- **严禁**输出只有序号没有正文的预期，例如：`"1、"`、`"2."`、`"3、 "`、整段 `"1、2、3、"`、或数组 `["1、","2、","3、"]`。
- **严禁**把手册步骤编号原样抄进 expected 却不写结果正文。
- 有文档/截图写明的结果：写成完整句子或带【】的关键文案，例如 `"出现【提交成功】提示"`、`"进入【数据总览】页面"`。
- 文档未写明该步结果：该项必须是 `""`，不要写「无」「同上」「成功」等敷衍词，也不要只写序号。
- 仅为稳定性插入的「等待页面加载完成」：对应 expected 填 `""`。
- 坏例子（禁止）→ 好例子：
  - 坏：`"expected": ["1、", "2、", "3、"]` → 好：`"expected": ["", "", "出现【保存成功】提示"]`
  - 坏：`"expected": "1、\\n2、\\n3、"` → 好：按步填 `""` 或完整结果句
  - 坏：`"expected": ["页面正常", "1、", "2、登录成功"]` → 好：去掉空编号，有内容的写全句

【设计要求 — 面向 UI 自动化执行】
- module 与流程详情中的模块路径一致
- precondition 写清起始页面或登录态（仅当文档有写；否则写「按文档起始页」）
- 每一步必须是**单一**浏览器动作；打开/点击/输入/选择/等待/关闭拆开写
- 句式优先 Instant 风格（见下方步骤合同）：`点击【登录】`、`在【用户名】输入 admin`、`在【单位】中选择【汉东省院】`
- 禁止：`填写表单`、`选择单位`、`密码输入【xxx】`（值不要放进【】）、`点击登录按钮`（类型词不要进【】）
- 下拉：优先两步 `点击【单位】` → `选择【汉东省院】`，或一步 `在【单位】中选择【汉东省院】`
- 提交/打开新页后：加 `等待页面加载完成` 或 `等待【关键文案】出现`
- 有弹窗：先等待提示出现，再 `点击【确定】/【关闭】`（【】内用按钮可见文案）

""" + _UI_STEP_CONTRACT + """

【输出要求】
- 只输出 JSON 数组，无 Markdown / ``` / 解释
- 字段名英文
- expected 数组元素禁止以单独的数字序号充当内容

输出格式：
[
  {
    "fp_name": "对应流程名称",
    "title": "用例标题（与文档流程名一致或加「-文档流程」）",
    "module": "所属业务模块",
    "priority": "P0 | P1 | P2",
    "precondition": "文档写明的前置/起始（无则「按文档起始页」）",
    "steps": ["打开【…】页面", "等待页面加载完成", "点击【…】", "在【用户名】输入 admin", "在【密码】输入 Admin@123", "点击【登录】", "等待页面加载完成"],
    "expected": ["", "", "", "", "", "", "进入系统首页或出现文档所述登录后界面"],
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
