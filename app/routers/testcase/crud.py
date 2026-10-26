from __future__ import annotations

import io
import json
import logging
from typing import Any, Dict, List

from fastapi import APIRouter, File, HTTPException, UploadFile, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import crud, models, db_models
from app.auth import require_admin, require_project_access, get_user_project_filter, get_current_user
from app.database import get_async_db

logger = logging.getLogger(__name__)

router = APIRouter()



def _validate_api_case_spec(case) -> None:
    """031（US9）：api 用例写入前校验 api_spec。

    动机（实测发现）：手工/脚本创建的 api 用例若把提取器写成 ``name`` 而非 ``variable``，
    校验器能识别但创建路径不校验 → 执行时提取器静默失效、下游步骤报「未定义变量」，
    排查成本高。这里在写入前用同一套 validate_api_spec 拦截（422 附问题清单）。
    """
    if (getattr(case, "case_kind", None) or "") != "api":
        return
    spec = getattr(case, "api_spec", None)
    if not spec:
        return
    from core.api_spec import validate_api_spec

    problems = validate_api_spec(spec)
    if problems:
        raise HTTPException(
            status_code=422,
            detail={"message": "api_spec 校验未通过", "problems": problems[:8]},
        )


def _validate_parameters_for_save(parameters, steps) -> None:
    """039：保存时校验未声明参数（有声明才校验，兼容存量用例）。"""
    if parameters is None:
        return
    from core.dataset_variables import validate_parameters

    errors = validate_parameters(steps or [], parameters)
    if errors:
        raise HTTPException(
            status_code=422,
            detail={"message": "参数校验未通过", "problems": errors[:8]},
        )


async def _validate_dataset_binding(db, db_case, dataset_id, steps) -> None:
    """039：绑定校验（同项目、用途=ui、列覆盖步骤参数）。"""
    if dataset_id is None:
        return
    from app.crud.api_dataset import get_dataset
    from core.dataset_variables import missing_columns

    dataset = await get_dataset(db, dataset_id)
    if dataset is None or dataset.project_id != db_case.project_id:
        raise HTTPException(status_code=404, detail="数据集不存在或不属于当前项目")
    if (getattr(dataset, "purpose", None) or "api") != "ui":
        raise HTTPException(status_code=400, detail="该数据集不是 UI 数据集，不能绑定到 UI 用例")
    missing = missing_columns(steps or [], dataset.columns or [])
    if missing:
        raise HTTPException(
            status_code=400,
            detail="数据集缺少列：" + "、".join(missing),
        )


@router.post("/", response_model=models.TestCase)
async def create_test_case(case: models.TestCaseCreate, user=Depends(get_current_user), db: AsyncSession = Depends(get_async_db)) -> models.TestCase:
    allowed_ids = get_user_project_filter(user)
    if allowed_ids is not None and case.project_id not in allowed_ids:
        raise HTTPException(status_code=403, detail="无权访问该项目")
    """
    创建带有步骤的新测试用例。
    """
    db_project = await crud.get_project(db, case.project_id)
    if db_project is None:
        raise HTTPException(status_code=404, detail=f"Project with id {case.project_id} not found")

    _validate_api_case_spec(case)
    _validate_parameters_for_save(getattr(case, "parameters", None), getattr(case, "steps", []))
    await _validate_dataset_binding(
        db, case, getattr(case, "dataset_id", None), getattr(case, "steps", [])
    )

    try:
        return await crud.create_test_case(db, case)
    except Exception as e:
        logger.exception("创建测试用例失败")
        raise HTTPException(status_code=400, detail="创建测试用例失败，请检查输入数据")


@router.get("/search", response_model=models.TestCasePage)
async def search_test_cases(
    project_id: int, q: str = "", page: int = 1, size: int = 20,
    case_kind: str | None = None,
    user=Depends(get_current_user), db: AsyncSession = Depends(get_async_db),
) -> dict:
    """
    搜索测试用例（按名称和描述）。
    """
    allowed_ids = get_user_project_filter(user)
    if allowed_ids is not None and project_id not in allowed_ids:
        raise HTTPException(status_code=404, detail="Project not found")
    result = await crud.search_test_cases(db, project_id, q, page, size, case_kind=case_kind)
    return {
        "items": result["items"],
        "total_items": result["total_items"],
        "page": page,
        "size": size,
    }


@router.get("/init-cases", response_model=List[models.TestCase])
async def list_init_test_cases(
    project_id: int,
    case_kind: str | None = None,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> list[models.TestCase]:
    """获取项目下所有标记为初始化的测试用例"""
    allowed_ids = get_user_project_filter(user)
    if allowed_ids is not None and project_id not in allowed_ids:
        raise HTTPException(status_code=404, detail="Project not found")
    return await crud.get_init_test_cases(db, project_id, case_kind=case_kind)


@router.get("/export")
async def export_test_cases(
    project_id: int,
    case_kind: str | None = None,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
):
    """导出项目测试用例为 xlsx（格式对齐 AI 生成下载格式）。"""
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, PatternFill, Border, Side

    cases = await crud.get_all_test_cases_for_project(db, project_id, case_kind=case_kind)

    wb = Workbook()
    ws = wb.active
    ws.title = "测试用例"

    # 表头样式（对齐 gen/history.py 格式）
    header_font = Font(name="微软雅黑", bold=True, size=11, color="FFFFFF")
    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    thin_border = Border(
        left=Side(style="thin"), right=Side(style="thin"),
        top=Side(style="thin"), bottom=Side(style="thin"),
    )
    cell_align = Alignment(vertical="top", wrap_text=True)

    api_export = (case_kind or "").strip() == "api"
    headers = (
        ["用例ID", "所属模块", "标题", "优先级", "接口定义JSON"]
        if api_export
        else ["用例ID", "所属模块", "标题", "前置条件", "测试步骤", "预期结果", "优先级", "结构化步骤JSON"]
    )
    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_align
        cell.border = thin_border

    for row_idx, tc in enumerate(cases, 2):
        # 步骤合并为文本块（每行 "序号. 描述 → 预期结果"）
        steps_str = ""
        expected_str = ""
        structured_json = ""
        if tc.steps:
            import json as _json

            lines = []
            exp_lines = []
            struct_list = []
            for s in tc.steps:
                lines.append(f"{s.step_order}. {s.description}")
                exp_lines.append(f"{s.step_order}. {s.parsed_result or ''}")
                ss = getattr(s, "structured_step", None)
                if isinstance(ss, dict):
                    struct_list.append(ss)
            steps_str = "\n".join(lines)
            expected_str = "\n".join(exp_lines)
            if struct_list:
                structured_json = _json.dumps(struct_list, ensure_ascii=False)

        module_name = tc.module.name if tc.module else ""

        from app.gen.adapter import format_priority_for_export

        if api_export:
            values = [
                tc.project_case_number,
                module_name,
                tc.name,
                format_priority_for_export(tc.priority),
                json.dumps(tc.api_spec or {}, ensure_ascii=False),
            ]
        else:
            values = [
                tc.project_case_number,
                module_name,
                tc.name,
                tc.description or "",
                steps_str,
                expected_str,
                format_priority_for_export(tc.priority),
                structured_json,
            ]
        for col, val in enumerate(values, 1):
            cell = ws.cell(row=row_idx, column=col, value=val)
            cell.alignment = cell_align
            cell.border = thin_border

    # 列宽（对齐 gen/history.py 格式 + 结构化列）
    from openpyxl.utils import get_column_letter

    widths = [10, 16, 30, 10, 60] if api_export else [10, 16, 30, 24, 40, 40, 10, 36]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=testcases_project_{project_id}.xlsx"},
    )


@router.get("/last-runs")
async def last_runs_by_cases(
    project_id: int,
    case_ids: str = "",
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """040：接口用例列表的“最近执行”列（按用例取最新一条 TestRun）。"""
    allowed = get_user_project_filter(user)
    if allowed is not None and project_id not in allowed:
        raise HTTPException(status_code=403, detail="无权访问该项目")
    ids = [int(x) for x in case_ids.split(",") if x.strip().isdigit()][:500]
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(db_models.TestRun)
            .join(db_models.TestCase, db_models.TestCase.id == db_models.TestRun.case_id)
            .where(
                db_models.TestCase.project_id == project_id,
                db_models.TestRun.case_id.in_(ids),
            )
            .order_by(db_models.TestRun.id.desc())
        )
    ).scalars().all()
    latest: dict[str, dict] = {}
    for run in rows:
        key = str(run.case_id)
        if key in latest:
            continue
        latest[key] = {
            "run_id": run.id,
            "batch_id": run.batch_id,
            "status": run.status,
            "duration": run.duration,
            "start_time": run.start_time.isoformat() if run.start_time else None,
        }
    return latest


@router.get("/{case_id}/revisions")
async def list_revisions(
    case_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """040：用例变更记录（页面/对话双来源）。"""
    from app.crud.chat import list_case_revisions

    db_case = await crud.get_test_case(db, case_id)
    if db_case is None:
        raise HTTPException(status_code=404, detail="Test case not found")
    allowed = get_user_project_filter(user)
    if allowed is not None and db_case.project_id not in allowed:
        raise HTTPException(status_code=403, detail="无权访问该项目")
    rows = await list_case_revisions(db, case_id)
    return {
        "items": [
            {
                "id": row.id,
                "source": row.source,
                "summary": row.summary,
                "user_id": row.user_id,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "before": row.before_snapshot,
                "after": row.after_snapshot,
            }
            for row in rows
        ]
    }


@router.post("/{case_id}/revisions/{revision_id}/restore")
async def restore_revision(
    case_id: int,
    revision_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """040：恢复某个历史版本（接口用例；只恢复 api_spec/名称/优先级）。"""
    db_case = await crud.get_test_case(db, case_id)
    if db_case is None:
        raise HTTPException(status_code=404, detail="Test case not found")
    allowed = get_user_project_filter(user)
    if allowed is not None and db_case.project_id not in allowed:
        raise HTTPException(status_code=403, detail="无权访问该项目")
    revision = await db.get(db_models.CaseRevision, revision_id)
    if revision is None or revision.case_id != case_id:
        raise HTTPException(status_code=404, detail="变更记录不存在")
    snapshot = revision.before_snapshot or {}
    api_spec = snapshot.get("api_spec")
    if not api_spec:
        raise HTTPException(status_code=400, detail="该历史版本未记录 api_spec，无法恢复")
    payload = models.TestCaseUpdate(
        name=snapshot.get("name") or db_case.name,
        description=db_case.description,
        module_id=db_case.module_id,
        priority=snapshot.get("priority") or db_case.priority,
        tags=db_case.tags,
        steps=[],
        api_spec=api_spec,
    )
    updated = await crud.update_test_case(
        db,
        case_id,
        payload,
        revision_user_id=user.id,
        revision_source="page",
        revision_summary=f"恢复历史版本 #{revision_id}",
    )
    if updated is None:
        raise HTTPException(status_code=500, detail="恢复失败")
    return {"id": updated.id, "name": updated.name}


@router.get("/{case_id}", response_model=models.TestCase)
async def get_test_case(case_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_async_db)) -> models.TestCase:
    """
    通过其ID检索单个测试用例，包括其步骤。
    """
    db_case = await crud.get_test_case(db, case_id)
    if db_case is None:
        raise HTTPException(status_code=404, detail="Test case not found")
    # 验证项目访问权限
    allowed_ids = get_user_project_filter(user)
    if allowed_ids is not None and db_case.project_id not in allowed_ids:
        raise HTTPException(status_code=404, detail="Test case not found")
    return db_case


@router.delete("/{case_id}")
async def delete_test_case(case_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_async_db)) -> dict[str, str] | None:
    """
    删除测试用例及其步骤。
    """
    db_case = await crud.get_test_case(db, case_id)
    if db_case is None:
        raise HTTPException(status_code=404, detail="Test case not found")
    allowed_ids = get_user_project_filter(user)
    if allowed_ids is not None and db_case.project_id not in allowed_ids:
        raise HTTPException(status_code=403, detail="无权访问该项目")

    if getattr(db_case, "case_kind", None) == "api":
        from app.crud.api_refs import scenarios_using_case

        names = await scenarios_using_case(db, db_case.project_id, case_id)
        if names:
            raise HTTPException(status_code=400, detail="仍被场景引用：" + "、".join(names))

    try:
        return await crud.delete_test_case(db, case_id)
    except Exception as e:
        logger.exception("删除测试用例失败 case_id=%s", case_id)
        raise HTTPException(status_code=500, detail="删除测试用例时发生内部错误")


@router.delete("/{case_id}/compiled-script")
async def clear_case_compiled_script(
    case_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_async_db),
) -> dict:
    """Manually clear the solidified Playwright script for a case."""
    db_case = await crud.get_test_case(db, case_id)
    if db_case is None:
        raise HTTPException(status_code=404, detail="Test case not found")
    allowed_ids = get_user_project_filter(user)
    if allowed_ids is not None and db_case.project_id not in allowed_ids:
        raise HTTPException(status_code=403, detail="无权访问该项目")
    from core.compiled_script import clear_compiled_script
    changed = clear_compiled_script(db_case)
    if changed:
        await db.commit()
    return {"ok": True, "cleared": changed}


@router.get("/{case_id}/compiled-script")
async def get_case_compiled_script(
    case_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_async_db),
) -> dict:
    """Return solidified Playwright script metadata + source (if any)."""
    db_case = await crud.get_test_case(db, case_id)
    if db_case is None:
        raise HTTPException(status_code=404, detail="Test case not found")
    allowed_ids = get_user_project_filter(user)
    if allowed_ids is not None and db_case.project_id not in allowed_ids:
        raise HTTPException(status_code=403, detail="无权访问该项目")
    return {
        "case_id": case_id,
        "has_script": bool(getattr(db_case, "compiled_script", None)),
        "compiled_script": getattr(db_case, "compiled_script", None),
        "compiled_script_hash": getattr(db_case, "compiled_script_hash", None),
        "compiled_at": getattr(db_case, "compiled_at", None),
    }


@router.get("/{case_id}/parameters")
async def get_case_parameters(
    case_id: int, user=Depends(get_current_user), db: AsyncSession = Depends(get_async_db),
) -> dict:
    """039：返回用例声明的参数、步骤中发现的参数与校验错误。"""
    from core.dataset_variables import discover_parameters, validate_parameters

    db_case = await crud.get_test_case(db, case_id)
    if db_case is None:
        raise HTTPException(status_code=404, detail="Test case not found")
    allowed_ids = get_user_project_filter(user)
    if allowed_ids is not None and db_case.project_id not in allowed_ids:
        raise HTTPException(status_code=403, detail="无权访问该项目")
    declared = getattr(db_case, "parameters", None) or []
    discovered = discover_parameters(db_case.steps or [])
    return {
        "case_id": case_id,
        "declared": declared,
        "discovered": discovered,
        "errors": validate_parameters(db_case.steps or [], declared) if declared else [],
    }


@router.put("/{case_id}/dataset", response_model=models.TestCase)
async def update_case_dataset(
    case_id: int,
    payload: models.TestCaseDatasetUpdate,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> models.TestCase:
    """039：专用数据集绑定端点（绑定/解绑/模式/循环/失败策略/参数声明）。"""
    from app.auth import log_audit

    db_case = await crud.get_test_case(db, case_id)
    if db_case is None:
        raise HTTPException(status_code=404, detail="Test case not found")
    allowed_ids = get_user_project_filter(user)
    if allowed_ids is not None and db_case.project_id not in allowed_ids:
        raise HTTPException(status_code=403, detail="无权访问该项目")

    fields = getattr(payload, "model_fields_set", set()) or set()
    steps = db_case.steps or []
    if "parameters" in fields:
        _validate_parameters_for_save(payload.parameters, steps)
    if "dataset_id" in fields:
        await _validate_dataset_binding(db, db_case, payload.dataset_id, steps)

    kwargs: dict[str, Any] = {}
    for name in ("parameters", "dataset_id", "dataset_mode", "loop_count", "fail_fast"):
        if name in fields:
            kwargs[name] = getattr(payload, name)
    updated, previous_dataset_id = await crud.update_case_dataset(db, case_id, **kwargs)
    if updated is None:
        raise HTTPException(status_code=404, detail="Test case not found")

    action = "case.dataset_unbind" if payload.dataset_id is None and "dataset_id" in fields else "case.dataset_bind"
    await log_audit(
        db,
        user.id,
        action,
        details={
            "case_id": case_id,
            "dataset_id": updated.dataset_id,
            "previous_dataset_id": previous_dataset_id,
            "mode": updated.dataset_mode,
            "loop_count": updated.loop_count,
            "fail_fast": bool(updated.fail_fast),
        },
    )
    return updated


@router.put("/{case_id}", response_model=models.TestCase)
async def update_test_case(case_id: int, case: models.TestCaseUpdate, user=Depends(get_current_user), db: AsyncSession = Depends(get_async_db)) -> models.TestCase:
    """
    更新测试用例，包括其步骤。
    """
    db_case = await crud.get_test_case(db, case_id)
    if db_case is None:
        raise HTTPException(status_code=404, detail="Test case not found")
    allowed_ids = get_user_project_filter(user)
    if allowed_ids is not None and db_case.project_id not in allowed_ids:
        raise HTTPException(status_code=403, detail="无权访问该项目")

    try:
        _validate_api_case_spec(case)
        _fields = getattr(case, "model_fields_set", set()) or set()
        _params = case.parameters if "parameters" in _fields else db_case.parameters
        _steps = case.steps or db_case.steps
        _validate_parameters_for_save(_params, _steps)
        if "dataset_id" in _fields:
            await _validate_dataset_binding(db, db_case, case.dataset_id, _steps)
        return await crud.update_test_case(
            db, case_id, case,
            # 034：页面编辑同样留痕（FR-015；source=page）
            revision_user_id=user.id,
            revision_source="page",
        )
    except HTTPException:
        raise  # 031（US9）：api_spec 校验 422 直接透传，不被 500 吞掉
    except Exception as e:
        logger.exception("更新测试用例失败 case_id=%s", case_id)
        raise HTTPException(status_code=500, detail="更新测试用例时发生内部错误")


@router.get("/module/{module_id}/testcases", response_model=models.TestCasePage)
async def get_module_test_cases(
    module_id: int, page: int = 1, size: int = 20,
    case_kind: str | None = None,
    user=Depends(get_current_user), db: AsyncSession = Depends(get_async_db),
) -> dict:
    """
    检索特定模块及其下级模块的所有测试用例，并分页。
    """
    db_module = await crud.get_module(db, module_id)
    if db_module is None:
        raise HTTPException(status_code=404, detail="Module not found")
    # 验证项目访问权限
    allowed_ids = get_user_project_filter(user)
    if allowed_ids is not None and db_module.project_id not in allowed_ids:
        raise HTTPException(status_code=404, detail="Module not found")

    paginated_data = await crud.get_all_test_cases_for_module_paginated(
        db, module_id, page, size, case_kind=case_kind,
    )
    return {
        "items": paginated_data["items"],
        "total_items": paginated_data["total_items"],
        "page": page,
        "size": size,
    }


@router.put("/{case_id}/toggle-init", response_model=models.TestCase)
async def toggle_test_case_init(case_id: int, body: Dict[str, Any], user=Depends(get_current_user), db: AsyncSession = Depends(get_async_db)) -> models.TestCase:
    """切换测试用例的初始化标记"""
    db_case = await crud.get_test_case(db, case_id)
    if db_case is None:
        raise HTTPException(status_code=404, detail="Test case not found")
    allowed_ids = get_user_project_filter(user)
    if allowed_ids is not None and db_case.project_id not in allowed_ids:
        raise HTTPException(status_code=403, detail="无权访问该项目")
    is_init = body.get("is_init", False)
    db_case = await crud.update_test_case_is_init(db, case_id, is_init)
    if db_case is None:
        raise HTTPException(status_code=404, detail="Test case not found")
    return db_case


@router.get("/project/{project_id}/testcases", response_model=models.TestCasePage)
async def get_project_test_cases(
    project_id: int, page: int = 1, size: int = 20,
    case_kind: str | None = None,
    user=Depends(get_current_user), db: AsyncSession = Depends(get_async_db),
) -> dict:
    """
    检索特定项目的所有测试用例，并分页。
    """
    allowed_ids = get_user_project_filter(user)
    if allowed_ids is not None and project_id not in allowed_ids:
        raise HTTPException(status_code=404, detail="Project not found")
    db_project = await crud.get_project(db, project_id)
    if db_project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    paginated_data = await crud.get_all_test_cases_for_project_paginated(
        db, project_id, page, size, case_kind=case_kind,
    )
    return {
        "items": paginated_data["items"],
        "total_items": paginated_data["total_items"],
        "page": page,
        "size": size,
    }


@router.post("/import")
async def import_test_cases(
    project_id: int,
    file: UploadFile = File(...),
    case_kind: str = "ui",
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """从 xlsx 文件导入测试用例。"""
    allowed_ids = get_user_project_filter(user)
    if allowed_ids is not None and project_id not in allowed_ids:
        raise HTTPException(status_code=403, detail="无权访问该项目")
    if case_kind not in ("functional", "ui", "api"):
        case_kind = "ui"
    from openpyxl import load_workbook

    if not file.filename or not file.filename.endswith((".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="仅支持 .xlsx 文件")

    contents = await file.read()
    wb = load_workbook(io.BytesIO(contents))
    ws = wb.active
    if ws is None:
        raise HTTPException(status_code=400, detail="工作表为空")

    rows = list(ws.iter_rows(min_row=2, values_only=True))
    created = 0
    errors = []

    if case_kind == "api":
        from app.gen.adapter import normalize_priority_to_storage
        from core.api_spec import validate_api_spec

        for row in rows:
            if not any(str(c or "").strip() for c in row):
                continue
            cells = [str(c or "").strip() for c in row]
            _case_id, module_name, title, priority, spec_text = (cells + [""] * 5)[:5]
            if not title:
                continue
            try:
                spec = json.loads(spec_text) if spec_text else None
                problems = validate_api_spec(spec)
                if problems:
                    errors.append(f"「{title}」接口定义非法: {'；'.join(problems[:3])}")
                    continue
                await crud.create_test_case(db, models.TestCaseCreate(
                    project_id=project_id,
                    name=title,
                    module_id=None,
                    steps=[],
                    priority=normalize_priority_to_storage(priority),
                    case_kind="api",
                    api_spec=spec,
                ))
                created += 1
            except Exception as e:
                errors.append(f"创建接口用例「{title}」失败: {e}")
        return {"created": created, "errors": errors}

    # 从表头判断格式（新格式含"测试步骤"/"前置条件"，旧格式含"步骤序号"/"步骤描述"）
    header_cells = list(ws.iter_rows(min_row=1, max_row=1, values_only=True))
    has_header = header_cells and any(str(c or "").strip() in ("前置条件", "测试步骤") for c in header_cells[0])

    for row in rows:
        if not any(str(c or "").strip() for c in row):
            continue

        if has_header:
            # 新格式：用例ID, 所属模块, 标题, 前置条件, 测试步骤, 预期结果, 优先级
            cells = [str(c or "").strip() for c in row]
            case_id, module_name, title, preconditions, steps_text, expected_text, priority = (
                (cells + [""] * 7)[:7]
            )
            if not title:
                continue

            try:
                # 解析合并的步骤文本
                steps_payload = []
                if steps_text:
                    for line in steps_text.split("\n"):
                        line = line.strip()
                        if not line:
                            continue
                        desc = line
                        exp = ""
                        if " → " in line:
                            desc, exp = line.split(" → ", 1)
                        # 去掉序号前缀 "1. "
                        if ". " in desc and desc[0].isdigit():
                            desc = desc.split(". ", 1)[1]
                        steps_payload.append(models.TestStepCreatePayload(
                            step_order=len(steps_payload) + 1,
                            description=desc.strip(),
                            parsed_result=exp.strip() or None,
                        ))

                from app.gen.adapter import normalize_priority_to_storage

                await crud.create_test_case(db, models.TestCaseCreate(
                    project_id=project_id, name=title,
                    description=preconditions or "",
                    steps=steps_payload,
                    priority=normalize_priority_to_storage(priority),
                    case_kind=case_kind,
                ))
                created += 1
            except Exception as e:
                errors.append(f"创建用例「{title}」失败: {e}")
        else:
            # 旧格式（向后兼容）：用例名称, 模块, 步骤序号, 步骤描述, 预期结果, 优先级, 标签
            cells = [str(c or "").strip() for c in row]
            name, _module, _so, desc, expected, priority, tags = (cells + [""] * 7)[:7]
            if not name:
                continue
            try:
                steps_payload = []
                if desc:
                    steps_payload.append(models.TestStepCreatePayload(
                        step_order=1, description=desc, parsed_result=expected or None,
                    ))
                from app.gen.adapter import normalize_priority_to_storage

                await crud.create_test_case(db, models.TestCaseCreate(
                    project_id=project_id, name=name,
                    steps=steps_payload,
                    priority=normalize_priority_to_storage(priority),
                    tags=tags or "",
                    case_kind=case_kind,
                ))
                created += 1
            except Exception as e:
                errors.append(f"创建用例「{name}」失败: {e}")

    return {"created": created, "errors": errors}
