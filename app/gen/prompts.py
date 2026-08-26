"""Prompt templates and default prompt registry for AI generation.

All file types flow through the two-phase pipeline (test-item extraction → TC
generation), so prompt content lives here in one place to keep wording
consistent and easy to tweak.
"""

# ---------------------------------------------------------------------------
# Shared grounding contract — highest priority for every generation prompt.
# ---------------------------------------------------------------------------
_GROUNDING_CONTRACT = """
【素材锚定 — 最高优先级，压过数量/覆盖率】
你只能依据用户消息里的**需求正文 + 截图可见内容**出题。截图里没有的文案 = 不存在。

硬禁令：
1. **禁止臆造**页面上不存在的按钮、菜单、Tab、输入框、下拉、筛选项、选项值、提示语。
2. **禁止**把提示词里的教学示例（登录/客户列表/姓名/状态/单位…）抄进本批输出；此禁令只针对示例本身——需求文档中**真实写明**的同类功能（如文档明确描述了登录页）必须正常提取。
3. **禁止**凭行业常识补控件：例如看板页只有检查项列表与表格列「单位名称」，不得发明「单位」筛选下拉或「汉东省院」选项。
4. **表格列名 ≠ 操作控件**：列头「单位名称 / 公司名称 / 姓名」只说明表结构，不是可点的筛选器。
5. **看不清 / 截断 / 被遮挡**的文案：不要猜；可跳过该细节，或用 icon_hint 描述位置外观。
6. 文档未写、截图未见的异常/权限/组合查询：**直接跳过**，不要为凑覆盖率编造。
7. 数量指标让位于真实性：素材支撑几条就写几条；宁可少，不可假。
8. 若用户消息含「【页面实况控件】」清单：步骤中的【控件名】必须能在清单中指认；
   清单与截图冲突时，以清单与截图同时可见者为准；清单没有且截图也看不清的控件禁止编造。

【所属模块 module — 必须与截图导航一致】
1. **优先读左侧导航**：一级 = 侧栏分组/父菜单原文；二级 = **当前高亮/选中**的子菜单原文；格式固定 `一级——二级`。
2. **页眉产品名 / LOGO 旁名称不是模块**（例如顶栏「检查助手」），禁止单独当一级，也禁止写成与侧栏不一致的树（如把产品名当一级再拼二级）。
3. 同一批截图侧栏结构必须统一：禁止混用 `合规检查助手——每日预警` / `每日预警` / `检查助手——每日预警` 等多种写法。
4. 当前高亮在「每日预警」时，相关项 module 必须是 `侧栏父级原文——每日预警`；高亮在「合规检查」同理。
5. **禁止**用上传文件名、`文件N`、分隔线当 module；文件名主题仅作读图对照，最终以侧栏为准。
""".strip()


# Two-phase pipeline prompts (all file types use this pipeline)

FP_EXTRACT_PROMPT = """你是资深的软件测试工程师。请仔细阅读需求文档（含文字与图片），提取**可测测试项**。

""" + _GROUNDING_CONTRACT + """

【测试项定义】
- 测试项 = 一条可独立设计用例的能力、交互或校验（以素材为依据）。
- 截图中的可见控件、菜单、检查项名称、状态卡片、表头、提示语，都可以成为测试项来源。
- 禁止把多个无关场景揉成一个粗项；也禁止为「拆细」而发明素材没有的交互。

【密度 — 服从素材】
- **不要**为了「每屏至少 3 条」「必须覆盖增删改查/筛选」而硬凑。
- 分类清单仅作检查提醒（有则写、无则跳过）：可见的增删改、可见提示、空态文案、权限入口、真实筛选区。
- 同一截图已写清的能力不要重复拆成多条同义项。

【查询 / 筛选】
- **仅当**截图或正文出现真实筛选区/搜索框/查询按钮时，才按**可见字段标签**拆「按××查询」。
- 同一真实筛选区可见字段 ≥2 时，可额外 1 条「组合查询」（两两组合）；字段不足则不要写组合查询。

【提取规则】
1. module 必须按上方「所属模块」规则从侧栏读取，禁止自造树或用页眉产品名。
2. name / desc 使用素材用语；desc 一句内（约 20 字），勿展开成完整操作脚本。
3. 控件名、选项、提示必须能在素材中指认；不能指认则删除该项。
4. **禁止**把上传文件名、`文件N`、分隔线当作 module。

【优先级】P0 核心可见主路径；P1 重要可见能力；P2 辅助。

【输出】只输出一个 JSON 对象，无 Markdown / ```。
{"functional_points":[{"module":"一级——二级","name":"测试项名","category":"增删改查|校验规则|交互反馈|权限控制|数据展示|其他","desc":"一句说明","priority":"P0|P1|P2"}]}

【原文锚定 — 最高优先级】
每个测试项的名称必须能在需求原文中找到连续出现的文字依据。
原文没有写的功能（哪怕行业里常见）一律不得输出。
"""

TC_GENERATE_PROMPT = """你是资深的软件测试工程师。请为以下**测试项**生成**功能测试用例**（业务场景验证）。

【可执行数据 — 硬规则】
输入/选择类步骤必须在步骤文本中用「」给出具体值（如「输入 SO-20260824-001」）。
禁止出现"一个/某个/任意/已存在的"等泛指表述——执行引擎会拒绝执行无具体值的步骤。

【target_name 规范 — 硬规则】
structured_steps 的 target_name 必须是该控件的可见文案或 aria-label（如「Add to cart」「Username」「Continue」「查询」），
禁止写成描述短语——禁止包含"该卡片/该按钮/该图标/检查/观察/页面上的/商品…"等叙述性字样。
点击类动作的 target_name 就是被点击控件本身的文字。

""" + _GROUNDING_CONTRACT + """

测试项详情：
{fp_descriptions}

【数量】
- 每个测试项通常 1～2 条；仅当素材明确写出多种可见结果时再多写。
- **禁止**机械套「正常/异常/边界」凑数；文档/截图没写的场景不要编。

【场景】
- scenario_type 用具体标签（主路径、校验失败、空结果…）；无依据则跳过该类型。
- 查询类：仅当素材有真实查询控件时写有效查询/空结果；组合查询仅当有 ≥2 个可见条件。

【设计】
- module / fp_name 与测试项一致；title 自然语言，禁止「-正常/-异常」后缀。
- 步骤里【】标注的界面元素必须来自素材；expected 可观察。

【输出】只输出 JSON 数组，无 Markdown / ```。
[
  {
    "fp_name": "对应测试项名称",
    "title": "用例标题",
    "module": "所属业务模块",
    "priority": "P0 | P1 | P2",
    "precondition": "前置条件（须与素材一致）",
    "steps": ["步骤1", "步骤2"],
    "expected": ["预期1", "预期2"],
    "scenario_type": "主路径 | 校验失败 | 空结果 | …"
  }
]
"""

# Shared step-writing contract for UI automation + flow-manual generation.
_UI_STEP_CONTRACT = """
【步骤结构化 — StructuredStep — 必须严格遵守】
- **一步 = 一个浏览器动作**。禁止把「打开→填写→点击」写进同一步。
- **steps 必须是对象数组**，禁止纯字符串步骤。字段：
  - `action`（必填）：goto|click|fill|select|check|uncheck|wait|assert_text|assert_visible|hover|press_key|click_blank|icon_click
  - `target_name`：**必须是素材中出现过的**可见文案 / accessible name / aria-label / title / tooltip
  - `target_role`（推荐）：button|link|textbox|combobox|option|menuitem|checkbox|radio|img|…
  - `value`：fill/select/wait/assert 的文案；也必须能在素材中找到依据（或为文档给出的示例输入）
  - `disambiguation` / `icon_hint` / `frame_hint` / `note`（可选）
- action 用法（格式示例，**文案勿照抄到本批**）：
  1) goto：`{"action":"goto","target_name":"<素材中的页面/菜单名>"}`
  2) click：`{"action":"click","target_name":"<素材按钮文案>","target_role":"button"}`
  3) fill：`{"action":"fill","target_name":"<素材输入标签>","target_role":"textbox","value":"<素材或文档给出的值>"}`
  4) select：先 click 展开再 select；字段名与选项均须来自素材
  5) wait / assert：等待或断言的文案必须在素材中出现过
  6) 纯图标无字：icon_click + icon_hint（位置+外观+用途），禁止 `target_name":"图标"`
- **禁止**控件类型词进 target_name（按钮、下拉框、输入框…）
- **禁止**省略号；看不清就跳过或改用 icon_hint
- **禁止**一步多目标

【忠实于素材 — 步骤级】
- 写每一步前自检：若把 target_name/value 拿到截图上找，找不到 → **删除该步或整条用例**。
- 测试项摘要若与截图冲突：以截图可见控件为准；摘要臆造的筛选项一律丢弃。
- 不要用「单位 / 汉东省院 / 客户列表 / 张三」等常见示例词，除非它们真的印在本批截图或正文里。
""".strip()

TC_GENERATE_UI_PROMPT = """你是资深的 UI 自动化测试工程师。请为以下**测试项**生成**浏览器可执行的 UI 自动化用例**。
执行器只能点击页面上真实存在的文案；臆造控件 = 必然失败。

""" + _GROUNDING_CONTRACT + """

测试项详情：
{fp_descriptions}

【数量 — 服从截图】
- 每个测试项通常 **0～2** 条可执行用例；截图不足以支撑操作则输出 0 条（不要硬写）。
- **禁止**「至少 N×2」式凑数；禁止为覆盖率发明筛选、组合查询、不存在的按钮。

【场景选型】
- 只写截图上能点、能看见结果的路径：点侧栏/检查项、看状态卡片、看列表行、看细则文案、看已有提示。
- 查询/筛选/登录表单：仅当截图里**真有**对应控件。
- 跳过：接口、库表、弱网、素材未见的权限分支。

【设计】
- module / fp_name 与测试项详情中的「模块 / 测试项」**逐字一致**，禁止改写成别的树或简称。
- title 禁止「-正常/-异常」后缀。
- precondition 只能写素材支持的起始态（如已打开某菜单——该菜单名须在截图出现）。
- steps 与 expected **等长**；中间步 expected 用 `""`；断言文案必须截图可见。
- 测试项名称若含「按单位查询」等但截图无该控件：**跳过该测试项**，不要改写成假筛选。

""" + _UI_STEP_CONTRACT + """

【输出】只输出 JSON 数组，无 Markdown / ```。可为空数组 []。
[
  {
    "fp_name": "<与测试项 name 一致>",
    "title": "<场景标题>",
    "module": "<模块路径>",
    "priority": "P0 | P1 | P2",
    "precondition": "<素材支持的前置>",
    "steps": [
      {"action": "click", "target_name": "<截图可见文案>", "target_role": "button"}
    ],
    "expected": [""],
    "scenario_type": "主路径 | 空态 | …"
  }
]
"""

FP_EXTRACT_FLOW_PROMPT = """你是资深的 UI 自动化测试工程师。请阅读**操作手册 / 流程文档**（文字与截图按序交错），提取文档中的**操作流程**（SOP）。

""" + _GROUNDING_CONTRACT + """

【图文结合】
- 每段文字必须与相邻截图对照；红框/色框/箭头标出的区域优先。
- 文字写「点击此处」但未给名：用框内可见文案或 tooltip；都没有则用
  `点击{位置}的{外观}图标（用途：{功能}）`，禁止 `点击【图标】`。

【流程定义】
- 一个流程 = 文档中一段完整操作路径；按章节/编号/连续截图切分。
- **禁止**虚构文档未写的异常/边界分支。

【提取规则】
1. module：「一级」或「一级——二级」，取自文档标题。
2. name 与文档用语一致。
3. desc 用「→」连接逐步摘要；每步含动词 +【素材可见文案】。
4. 句式：`点击【…】`、`在【…】输入 …`、`在【…】中选择【…】`——【】内必须来自手册/截图。
5. 等待写 `等待【可见文案】出现`，禁止 `等待页面加载完成`。
6. category 可用「操作流程」；priority：主路径 P0，次要 P1/P2。

【输出】只输出 JSON 对象，无 Markdown / ```。
{"functional_points":[{"module":"一级——二级","name":"流程名","category":"操作流程","desc":"打开【…】→ 点击【…】→ …","priority":"P0|P1|P2"}]}
"""

TC_GENERATE_FLOW_PROMPT = """你是资深的 UI 自动化测试工程师。根据**操作流程摘要**与用户消息中的**手册原文/截图**，生成可执行 UI 用例。

""" + _GROUNDING_CONTRACT + """

流程详情：
{fp_descriptions}

【图文结合】
- 必须逐张看图；框选控件文案优先写入 steps。
- 摘要与截图冲突时以截图为准；禁止只按摘要臆造控件。

【数量】通常每流程 1 条主路径；禁止额外编异常/边界（除非文档写明）。

【忠实还原】
- 步骤顺序、控件文案、输入示例必须来自文档/截图。
- 文档未写清可省略，不可发明新业务步。

【预期】
- expected 与 steps 等长；无结果用 `""`；禁止空编号 `1、`/`2、`。

【设计】
- steps 为 StructuredStep 对象数组；一步一动作。
- 下拉须先展开再选；提交后 wait 可见文案须来自素材。

""" + _UI_STEP_CONTRACT + """

【输出】只输出 JSON 数组，无 Markdown / ```。
[
  {
    "fp_name": "<流程 name>",
    "title": "<与文档一致的标题>",
    "module": "<模块>",
    "priority": "P0 | P1 | P2",
    "precondition": "按文档起始页",
    "steps": [
      {"action": "click", "target_name": "<手册/截图可见文案>", "target_role": "button"}
    ],
    "expected": [""],
    "scenario_type": "文档流程"
  }
]
"""


# Number of test items to bundle into a single Phase-2 batch.
FP_BATCH_SIZE = 2
# Soft floor for supplemental TC generation (UI path no longer forces this).
MIN_TCS_PER_ITEM = 2
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


def case_kind_from_tc_prompt_key(tc_prompt_key: str | None) -> str:
    """Map generation prompt key to persisted case_kind (functional | ui)."""
    if tc_prompt_key in ("tc_generate_ui", "tc_generate_flow"):
        return "ui"
    return "functional"


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
    "case_kind_from_tc_prompt_key",
    "min_tcs_per_item",
]
