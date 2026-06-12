import re
import os
import logging

from app.gen.model_client import call_model
from app.gen.docx_parser import extract_text
from app.gen.md_parser import extract_text_from_md
from app.gen.pdf_parser import extract_text_from_pdf, is_pdf_dual_layer, render_pdf_pages_to_images, validate_pdf
from app.gen.image_parser import encode_image
from app.gen.csv_generator import CSV_HEADER
from app.gen.models import AnalysisSession, FunctionalPoint, TestCase
from markupsafe import escape

logger = logging.getLogger(__name__)

MODEL_MAX_TOKENS = 8192

# Two-phase pipeline prompts (all file types use this pipeline)

FP_EXTRACT_PROMPT = """你是资深的软件测试工程师。请仔细阅读需求文档，按文档的**章节/标题结构**提取功能点。

**第一步：识别文档结构**
请先识别文档中的所有模块/章节标题（如"功能模块1"、"用户管理"、"订单管理"等）。这些标题就是【模块名】，必须原样使用，不要自行创建或修改模块名。

**第二步：按模块提取功能点**
对于每个模块标题下的内容，提取其中的功能点归属到对应模块。
- 【模块名】必须与文档中的章节标题完全一致
- 如果文档有"功能模块1"、"功能模块2"等标题，就使用这些作为模块名
- 不要将功能点名称或分类拼接成模块名（例如"数据列表与批量操作(数据操作)"不是模块名）
- 不要提取跨模块的通用UI模式——这些应该归入它们所属的具体业务模块
- 只有确实无法归属的全局功能（如登录、权限）才能使用"通用"

**提取范围：**
- UI交互元素：输入框、按钮、下拉框、复选框、单选框、日期选择器、搜索框、文本域、文件上传、提示信息、加载动画、错误提示
- 数据操作：数据列表、分页、排序、筛选、搜索、导出、导入、批量操作、批量删除
- 状态流转：审批状态、上下架状态、支付状态变更、订单状态流转、审核状态变更
- 权限控制：角色权限、操作权限、数据权限、菜单权限、按钮级权限
- 管理功能：批量操作、导入导出、模板管理、配置管理、定时任务、日志查看
- 业务规则：验证规则、计算公式、条件判断、数据校验规则、长度限制、格式要求
- 通知提醒：消息通知、邮件提醒、短信提醒、站内信、推送通知
- 跨模块交互：模块间数据传递、状态同步、联动效果、级联选择

如果是图片原型，还需关注：
- 页面布局、导航结构、视觉组件
- 状态标签、操作按钮、空占位、错误提示区域

**输出格式：**
## 功能点清单
- **【模块名】功能点名称(分类)**: 详细描述，包含交互规则、约束条件和边界条件
- 示例：- **【功能模块1】数据新增与编辑(数据操作)**: 支持通过表单进行数据录入...
"""

TC_GENERATE_PROMPT = """你是资深的软件测试工程师。请为以下功能点生成测试用例。

功能点详情：
{fp_descriptions}

要求：
- 模块名：必须使用功能点对应的模块名称（功能点详情中"模块："字段），不要自行修改或简化。
- 请为每个功能点生成测试用例，覆盖以下维度：
1. 正常流程：完整的正向操作流程
2. 边界值：空值、最大值、最小值、特殊字符、超长输入等
3. 异常场景：网络异常、权限不足、并发操作、数据冲突等
4. UI交互：按钮状态、加载提示、错误提示、页面跳转等
5. 跨功能联动：与其他功能的交互影响

测试步骤中请用【】标注操作元素的界面名称（如"点击【保存】按钮"、"在【用户名】输入框输入值"）。

输出格式（严格7列markdown表格，每列必须独立）：
| {csv_header} |
| --- | --- | --- | --- | --- | --- | --- |
| TC-001 | 模块名 | 标题 | 前置条件（简短） | 1.步骤1 2.步骤2 | 1.结果1 2.结果2 | 高 |

**格式要求（必须严格遵守）**：
- 每个表格行必须有且仅有7个单元格，用`|`分隔
- 每个单元格内容必须在一行内完成，禁止换行
- 前置条件：简短描述，如"用户已登录"
- 测试步骤：用空格分隔的序号格式，如`1.点击【新增】 2.输入数据 3.点击【保存】`
- 预期结果：用空格分隔的序号格式，且**数量必须与测试步骤一一对应**，如`1.弹窗正常打开 2.输入框可正常填写 3.提示保存成功`
- 禁止在单元格内使用换行符或<br>
- 优先级：高/中/低

**数量对应规则（极端重要）**：
- 每个测试步骤必须有且只有1个预期结果，两者**数量必须完全相等**
- 正确示例（5步→5结果）：`1.点击【新增】 2.输入1000字符 3.输入特殊字符 4.输入1001字符 5.点击【保存】` → `1.新增表单打开 2.输入正常 3.特殊字符可输入 4.提示超长 5.提示保存成功`
- 错误示例（5步→4结果，缺少第5步的结果）：`1.步骤A 2.步骤B 3.步骤C 4.步骤D 5.步骤E` → `1.结果A 2.结果B 3.结果C 4.结果D` ❌
- 错误示例（3步→1结果，将多个结果合并了）：`1.步骤A 2.步骤B 3.步骤C` → `1.结果A结果B结果C` ❌
- 如果某个步骤没有可描述的预期结果，至少写上"操作成功"保持数量一致
- 如果某个步骤有多个可验证的点，请拆分成多个步骤，不要合并写入单个步骤中
- 如果功能点描述中信息不足，请基于描述合理推断测试场景，不要遗漏明显功能
"""

FP_BATCH_SIZE = 8


def get_default_prompts() -> dict:
    """返回默认提示词模板字典（key → {label, content}）。"""
    return {
        "fp_extract": {
            "label": "功能点提取",
            "content": FP_EXTRACT_PROMPT.strip(),
        },
        "tc_generate": {
            "label": "测试用例生成",
            "content": TC_GENERATE_PROMPT.strip(),
        },
    }


def _clean_text(value: str) -> str:
    value = re.sub(r"<\s*br\s*/?\s*>", "\n", value, flags=re.IGNORECASE)
    # Match both "1. step 2. step" and "1.步骤 2.步骤" (Chinese has no space after dot)
    value = re.sub(r"(\s+)(\d+\.)", r"\n\2", value)
    return value


def _to_html(value: str) -> str:
    return value.replace("\n", "<br>")


def _parse_response(session: AnalysisSession, text: str):
    fp_section = text.split("## 功能点清单")[-1].split("## 测试用例")[0] if "## 功能点清单" in text else ""
    if "## 测试用例" in text:
        tc_section = text.split("## 测试用例")[-1]
    else:
        # Fallback: use entire text if no section marker found
        tc_section = text

    fp_lines = fp_section.strip().split("\n")
    fp_id = 0
    for i, line in enumerate(fp_lines):
        stripped = line.strip()
        # Only match top-level FP lines that contain 【模块名】 prefix
        # This avoids parsing sub-items like "- **交互规则**: ..." as separate FPs
        if (stripped.startswith("- **") or stripped.startswith("* **")) and "【" in stripped:
            name = re.sub(r"^[-*]\s*\*\*([^*]+)\*\*.*", r"\1", stripped).strip()
            desc = re.sub(r"^[-*]\s*\*\*[^*]+\*\*\s*[:：]?\s*", "", stripped).strip()
            cat = "通用"
            module = ""
            # Extract module name from 【】prefix and strip it from name
            if "【" in name and "】" in name:
                m = re.search(r"【([^】]*)】", name)
                if m:
                    module = m.group(1).strip()
                    name = re.sub(r"^【[^】]*】\s*", "", name).strip()
            if "(" in name and ")" in name:
                parts = name.split("(")
                name = parts[0].strip()
                cat = parts[1].rstrip(")").strip()
            fp_id += 1
            session.functional_points.append(FunctionalPoint(
                id=fp_id,
                session_id=session.session_id,
                module=module or "通用",
                name=name,
                description=_clean_text(desc),
                category=cat,
            ))

    import re as _re

    tc_lines = tc_section.strip().split("\n")
    tc_index = 0
    for line in tc_lines:
        line = line.strip()
        if line.startswith("|") and line.count("|") >= 7:
            cells = [c.strip() for c in line.split("|")]
            cells = [c for c in cells if c]
            if len(cells) < 7:
                continue
            # Skip header and separator lines
            if cells[0] == "用例ID":
                continue
            # Skip markdown table separator lines like ---, :---, :-:, etc.
            if _re.match(r"^:?-+:?$", cells[0]):
                continue
            tc_index += 1
            session.test_cases.append(TestCase(
                test_case_id=f"TC-{tc_index:03d}",
                session_id=session.session_id,
                module=_clean_text(cells[1]) if len(cells) > 1 else "",
                title=_clean_text(cells[2]) if len(cells) > 2 else "",
                preconditions=_clean_text(cells[3]) if len(cells) > 3 else "",
                test_steps=_clean_text(cells[4]) if len(cells) > 4 else "",
                expected_result=_clean_text(cells[5]) if len(cells) > 5 else "",
                priority=_clean_text(cells[6]) if len(cells) > 6 else "中",
            ))


# Two-phase pipeline helper functions


def _parse_fps_from_text(text: str, session_id: str = "") -> list[FunctionalPoint]:
    """Parse only functional points from model response text."""
    tmp = AnalysisSession()
    tmp.session_id = session_id
    _parse_response(tmp, text)
    return tmp.functional_points


def _parse_tcs_from_text(text: str, session_id: str = "", start_index: int = 0) -> list[TestCase]:
    """Parse only test cases from model response text.

    Args:
        start_index: Starting index for TC numbering (default 0 → TC-001).
    """
    tmp = AnalysisSession()
    tmp.session_id = session_id
    _parse_response(tmp, text)
    for tc in tmp.test_cases:
        start_index += 1
        tc.test_case_id = f"TC-{start_index:03d}"
    return tmp.test_cases


def extract_functional_points(
    text: str = None,
    image_data: tuple = None,
    project_description: str = "",
    progress_callback=None,
    fp_prompt: str = None,
) -> list[FunctionalPoint]:
    """Extract functional points from document text or image.

    Phase 1 of two-phase pipeline.

    Args:
        text: Document text content (for .docx files).
        image_data: Tuple of (file_extension, base64_encoded_image) for image files.
        project_description: User-supplied project background.
        progress_callback: Optional callable(current, total, message) — forwarded by caller.
        fp_prompt: Custom FP extraction prompt (None = use default).

    Exactly one of text or image_data must be provided.
    """
    fp_prompt = fp_prompt or FP_EXTRACT_PROMPT
    desc_prefix = ""
    if project_description:
        desc_prefix = f"[项目背景]: {escape(project_description)}\n\n---\n\n"

    if image_data:
        suffix, b64 = image_data
        prompt = desc_prefix + fp_prompt
        if progress_callback:
            progress_callback(0, 0, "正在分析图片提取功能点")
        content = call_model([
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "请分析此界面原型图中的所有功能点和UI元素："},
                    {"type": "image_url", "image_url": {"url": f"data:image/{suffix};base64,{b64}"}},
                ],
            },
        ])
    else:
        prompt = desc_prefix + fp_prompt
        if progress_callback:
            progress_callback(0, 0, "正在分析文档提取功能点")
        content = call_model([
            {"role": "system", "content": prompt},
            {"role": "user", "content": text},
        ])

    fps = _parse_fps_from_text(content)
    if progress_callback:
        progress_callback(0, 0, f"提取到 {len(fps)} 个功能点")
    return fps


def generate_test_cases_for_fps(
    fps: list[FunctionalPoint],
    project_description: str,
    progress_callback=None,
    phase1_offset=1,
    total_steps=1,
    tc_prompt: str = None,
) -> dict:
    """Generate test cases for functional points in batches of 5-8.

    Phase 2 of two-phase pipeline. Groups FPs into batches, calls
    TC_GENERATE_PROMPT per batch with structured FP descriptions as context
    (no longer sends the full document), and merges all test cases.

    Args:
        phase1_offset: The starting step number for Phase 2 (default 1, meaning
            Phase 1 = step 0).
        total_steps: Total number of steps (Phase 1 + all batches).
        tc_prompt: Custom TC generation prompt (None = use default).

    Returns:
        dict with 'test_cases' (list[TestCase]) and 'warnings' (list[str]).
    """
    tc_prompt = tc_prompt or TC_GENERATE_PROMPT
    all_tcs: list[TestCase] = []
    warnings: list[str] = []
    tc_counter = 0  # running counter for global TC numbering

    # Split FPs into batches of FP_BATCH_SIZE
    batches = []
    for i in range(0, len(fps), FP_BATCH_SIZE):
        batches.append(fps[i : i + FP_BATCH_SIZE])

    import time

    for idx, batch in enumerate(batches):
        # Build display name for logging/warnings
        fp_names = ", ".join(fp.name for fp in batch[:3])
        if len(batch) > 3:
            fp_names += f" +{len(batch) - 3} more"

        if progress_callback:
            step = phase1_offset + idx
            msg = f"正在为 {batch[0].name} 生成用例 ({step + 1}/{total_steps})"
            progress_callback(step, total_steps, msg)

        MAX_RETRIES = 3
        RETRY_DELAY = 3
        tcs = []
        content = ""

        for attempt in range(MAX_RETRIES):
            try:
                fp_descriptions = "\n".join(
                    f"- 模块：{fp.module}\n  功能点：{fp.name} ({fp.category})\n  描述：{fp.description}"
                    for fp in batch
                )

                prompt = tc_prompt.format(
                    fp_descriptions=fp_descriptions,
                    csv_header=' | '.join(CSV_HEADER),
                )

                desc_prefix = ""
                if project_description:
                    desc_prefix = f"[项目背景]: {escape(project_description)}\n\n---\n\n"

                content = call_model([
                    {"role": "system", "content": desc_prefix + prompt},
                    {"role": "user", "content": f"请为以上功能点生成测试用例。"},
                ])

                tcs = _parse_tcs_from_text(content, start_index=tc_counter)
                if tcs:
                    break
                else:
                    logger.warning("Batch %d attempt %d: no TCs parsed, retrying...",
                                  idx + 1, attempt + 1)
                    logger.warning("Batch %d raw model output (first 500 chars): %s", idx + 1, content[:500])
                    if attempt < MAX_RETRIES - 1:
                        time.sleep(RETRY_DELAY * (attempt + 1))
            except Exception as e:
                logger.warning("Batch %d attempt %d failed: %s", idx + 1, attempt + 1, e)
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_DELAY * (attempt + 1))

        if not tcs:
            logger.warning("Batch %d failed after %d attempts. fp_descriptions: %s",
                          idx + 1, MAX_RETRIES, fp_descriptions[:500] if fp_descriptions else "N/A")
            warnings.append(f"Batch {idx + 1} ({fp_names}) returned no test cases after {MAX_RETRIES} retries")
        else:
            tc_counter += len(tcs)
            all_tcs.extend(tcs)
            logger.info("Batch %d generated %d test cases for: %s", idx + 1, len(tcs), fp_names)

        if idx < len(batches) - 1:
            time.sleep(2)

    return {"test_cases": all_tcs, "warnings": warnings}


def two_phase_analyze(
    text: str,
    progress_callback=None,
    project_description: str = "",
    prompts: dict = None,
) -> dict:
    """Orchestrate two-phase analysis: extract FPs then generate TCs per batch.

    Phase 1: Extract all functional points from the full document.
    Phase 2: Generate test cases for FPs in batches using FP descriptions
        as context (no longer sends the full document).

    Args:
        text: the extracted document text.
        progress_callback: optional callable(current, total, message).
        project_description: user-supplied project background context.

    Returns:
        dict with 'functional_points', 'test_cases', 'warnings'.
    """
    prompts = prompts or {}
    fp_prompt = prompts.get("fp_extract", {}).get("content") if isinstance(prompts.get("fp_extract"), dict) else prompts.get("fp_extract")
    tc_prompt = prompts.get("tc_generate", {}).get("content") if isinstance(prompts.get("tc_generate"), dict) else prompts.get("tc_generate")
    warnings: list[str] = []

    # Truncate text if it exceeds model's token budget
    total_tokens = int(len(text) * 1.5)
    max_tokens = 8192
    chunk_token_budget = int(max_tokens * 0.8)
    if total_tokens > chunk_token_budget:
        max_chars = int(chunk_token_budget / 1.5)
        logger.info("Doc too long (%d tokens > %d budget), truncating to %d chars",
                     total_tokens, chunk_token_budget, max_chars)
        text = text[:max_chars]

    if progress_callback:
        # Phase 1 = step 0. We don't know total yet (need FPs to know batch count).
        # Send with total=0 to indicate indeterminate progress for Phase 1.
        progress_callback(0, 0, "正在提取功能点清单")

    try:
        fps = extract_functional_points(text=text, project_description=project_description, fp_prompt=fp_prompt)
        if not fps:
            warnings.append("No functional points extracted from document")
        logger.info("Phase 1: extracted %d functional points", len(fps))
    except Exception as e:
        logger.error("Phase 1 (FP extraction) failed: %s", e)
        return {"functional_points": [], "test_cases": [], "warnings": [f"FP extraction failed: {e}"], "error": True}

    # Phase 2: Generate test cases per FP batch
    if fps:
        num_batches = max(1, (len(fps) + FP_BATCH_SIZE - 1) // FP_BATCH_SIZE)
        total_steps = 1 + num_batches  # 1 for Phase 1 + N batches
        result = generate_test_cases_for_fps(
            fps, project_description, progress_callback,
            phase1_offset=1, total_steps=total_steps, tc_prompt=tc_prompt,
        )
        warnings.extend(result.get("warnings", []))
        all_tcs = result["test_cases"]
    else:
        all_tcs = []

    return {
        "functional_points": fps,
        "test_cases": all_tcs,
        "warnings": warnings,
    }


def _analyze_image_two_phase(file, progress_callback, project_description) -> dict:
    """Two-phase analysis for image files: extract FPs from image then generate TCs."""
    warnings: list[str] = []
    suffix = os.path.splitext(file.filename)[1].lstrip(".")
    b64 = encode_image(file)
    image_data = (suffix, b64)

    if progress_callback:
        progress_callback(0, 0, "正在从图片提取功能点")

    try:
        fps = extract_functional_points(image_data=image_data, project_description=project_description)
        if not fps:
            warnings.append("No functional points extracted from image")
        logger.info("Phase 1 (image): extracted %d functional points", len(fps))
    except Exception as e:
        logger.error("Phase 1 (image FP extraction) failed: %s", e)
        return {"functional_points": [], "test_cases": [], "warnings": [f"Image FP extraction failed: {e}"], "error": True}

    # Phase 2: Generate test cases per FP batch
    if fps:
        num_batches = max(1, (len(fps) + FP_BATCH_SIZE - 1) // FP_BATCH_SIZE)
        total_steps = 1 + num_batches
        result = generate_test_cases_for_fps(
            fps, project_description, progress_callback,
            phase1_offset=1, total_steps=total_steps,
        )
        warnings.extend(result.get("warnings", []))
        all_tcs = result["test_cases"]
    else:
        all_tcs = []

    return {
        "functional_points": fps,
        "test_cases": all_tcs,
        "warnings": warnings,
    }


def _analyze_pdf_two_phase(file, progress_callback, project_description) -> dict:
    """Two-phase analysis for PDF files: auto-detect dual-layer vs scan-only."""
    warnings: list[str] = []

    # Validate PDF
    is_valid, error_msg = validate_pdf(file)
    if not is_valid:
        return {"functional_points": [], "test_cases": [], "warnings": [error_msg], "error": True}

    # Detect dual-layer
    if is_pdf_dual_layer(file):
        # Dual-layer: extract text and use text pipeline
        if progress_callback:
            progress_callback(0, 0, "正在从PDF提取文字")
        text = extract_text_from_pdf(file)
        if not text.strip():
            return {"functional_points": [], "test_cases": [], "warnings": ["PDF文件中无有效文字内容"], "error": True}
        return two_phase_analyze(text, progress_callback, project_description)
    else:
        # Scan-only: render pages to images, extract FPs per page
        if progress_callback:
            progress_callback(0, 0, "正在将PDF页面转为图片")
        page_images = render_pdf_pages_to_images(file)
        if not page_images:
            return {"functional_points": [], "test_cases": [], "warnings": ["PDF文件中无有效页面"], "error": True}

        total_pages = len(page_images)
        all_fps: list[FunctionalPoint] = []

        for idx, (ext, b64) in enumerate(page_images):
            if progress_callback:
                progress_callback(idx, total_pages, f"正在分析第 {idx + 1}/{total_pages} 页")
            try:
                fps = extract_functional_points(
                    image_data=(ext, b64),
                    project_description=project_description,
                    progress_callback=progress_callback,
                )
                if fps:
                    # Re-number FPs to be globally sequential
                    for fp in fps:
                        fp.id = len(all_fps) + 1
                        fp.session_id = ""
                    all_fps.extend(fps)
            except Exception as e:
                logger.warning("PDF page %d FP extraction failed: %s", idx + 1, e)
                warnings.append(f"第 {idx + 1} 页功能点提取失败: {e}")

        logger.info("PDF scan-only analysis: extracted %d functional points from %d pages", len(all_fps), total_pages)

        # Phase 2: Generate test cases from merged FPs
        if all_fps:
            num_batches = max(1, (len(all_fps) + FP_BATCH_SIZE - 1) // FP_BATCH_SIZE)
            total_steps = total_pages + num_batches
            result = generate_test_cases_for_fps(
                all_fps, project_description, progress_callback,
                phase1_offset=total_pages, total_steps=total_steps,
            )
            warnings.extend(result.get("warnings", []))
            all_tcs = result["test_cases"]
        else:
            all_tcs = []

        return {
            "functional_points": all_fps,
            "test_cases": all_tcs,
            "warnings": warnings,
        }


MAX_FILES = 10
MAX_TOTAL_SIZE = 50 * 1024 * 1024  # 50MB
ALLOWED_EXTENSIONS = {".docx", ".md", ".png", ".jpg", ".jpeg", ".pdf"}


def extract_multi_file_content(files, filenames, progress_callback=None) -> tuple[str, list[str], list[str]]:
    """从多个文件中提取内容并拼接为一个大文本。

    Args:
        files: 文件对象列表（SpooledTemporaryFile 等，同步 read/write）
        filenames: 对应文件名列表
        progress_callback: 可选 callable(current, total, message)

    Returns:
        (combined_text, filenames, warnings)
        - combined_text: 拼接后的文本
        - filenames: 所有文件名列表
        - warnings: 警告信息列表
    """
    warnings: list[str] = []
    text_parts: list[str] = []
    image_fp_parts: list[str] = []

    # 校验文件数量
    if len(files) > MAX_FILES:
        raise ValueError(f"最多上传 {MAX_FILES} 个文件，当前选择了 {len(files)} 个")

    # 校验总大小
    total_size = 0
    for f in files:
        f.seek(0, 2)  # SEEK_END
        total_size += f.tell()
        f.seek(0)     # SEEK_SET
    if total_size > MAX_TOTAL_SIZE:
        raise ValueError(f"文件总大小超过 50MB 限制（当前 {total_size / 1024 / 1024:.1f}MB）")

    total_files = len(files)

    for idx, file in enumerate(files):
        filename = filenames[idx] if idx < len(filenames) else f"file_{idx + 1}"
        ext = os.path.splitext(filename)[1].lower()

        if ext not in ALLOWED_EXTENSIONS:
            raise ValueError(f"不支持的文件类型: {ext}，仅支持 .docx、.md、.png、.jpg、.jpeg、.pdf")

        if progress_callback:
            progress_callback(idx, total_files, f"正在提取: {filename} ({idx + 1}/{total_files})")

        try:
            file.seek(0)
            if ext == ".docx":
                text = extract_text(file)
                if not text.strip():
                    warnings.append(f"文件 {filename} 为空，已跳过")
                    continue
                text_parts.append(f"===== 文件{idx + 1}: {filename} =====\n{text}")

            elif ext == ".md":
                text = extract_text_from_md(file)
                if not text.strip():
                    warnings.append(f"文件 {filename} 为空，已跳过")
                    continue
                text_parts.append(f"===== 文件{idx + 1}: {filename} =====\n{text}")

            elif ext == ".pdf":
                # 双层 PDF 提取文本，扫描 PDF 提取图片功能点
                if is_pdf_dual_layer(file):
                    text = extract_text_from_pdf(file)
                    if not text.strip():
                        warnings.append(f"PDF 文件 {filename} 无有效文字，尝试图片模式")
                        file.seek(0)
                        page_images = render_pdf_pages_to_images(file)
                        if page_images:
                            for page_idx, (pext, pb64) in enumerate(page_images):
                                fps = extract_functional_points(
                                    image_data=(pext, pb64),
                                    progress_callback=progress_callback,
                                )
                                fp_text = "\n".join(
                                    f"- 【{fp.module}】{fp.name}({fp.category}): {fp.description}"
                                    for fp in fps
                                ) if fps else "（未提取到功能点）"
                                image_fp_parts.append(
                                    f"===== 图片功能点提取: {filename} 第{page_idx + 1}页 =====\n{fp_text}"
                                )
                        else:
                            warnings.append(f"PDF文件 {filename} 无有效页面，已跳过")
                        continue
                    text_parts.append(f"===== 文件{idx + 1}: {filename} =====\n{text}")
                else:
                    page_images = render_pdf_pages_to_images(file)
                    if not page_images:
                        warnings.append(f"PDF文件 {filename} 无有效页面，已跳过")
                        continue
                    for page_idx, (pext, pb64) in enumerate(page_images):
                        fps = extract_functional_points(
                            image_data=(pext, pb64),
                            progress_callback=progress_callback,
                        )
                        fp_text = "\n".join(
                            f"- 【{fp.module}】{fp.name}({fp.category}): {fp.description}"
                            for fp in fps
                        ) if fps else "（未提取到功能点）"
                        image_fp_parts.append(
                            f"===== 图片功能点提取: {filename} 第{page_idx + 1}页 =====\n{fp_text}"
                        )

            elif ext in (".png", ".jpg", ".jpeg"):
                suffix = ext.lstrip(".")
                b64 = encode_image(file)
                MAX_RETRIES = 3
                RETRY_DELAY = 5
                fps = []
                for attempt in range(MAX_RETRIES):
                    try:
                        fps = extract_functional_points(
                            image_data=(suffix, b64),
                            progress_callback=progress_callback,
                        )
                        break
                    except Exception as e:
                        if "timed out" in str(e).lower() and attempt < MAX_RETRIES - 1:
                            logger.warning("图片 %s 分析超时，第 %d 次重试...", filename, attempt + 1)
                            import time
                            time.sleep(RETRY_DELAY * (attempt + 1))
                        else:
                            raise
                fp_text = "\n".join(
                    f"- 【{fp.module}】{fp.name}({fp.category}): {fp.description}"
                    for fp in fps
                ) if fps else "（未提取到功能点）"
                image_fp_parts.append(
                    f"===== 图片功能点提取: {filename} =====\n{fp_text}"
                )

        except Exception as e:
            logger.warning("文件 %s 提取失败: %s", filename, e)
            warnings.append(f"文件 {filename} 提取失败: {e}")

    # 拼接：文档文本在前，图片功能点描述在后
    combined = "\n\n".join(text_parts + image_fp_parts)

    if not combined.strip():
        raise ValueError("所有文件均未提取到有效内容")

    return combined, filenames, warnings
