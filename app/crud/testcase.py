# app/crud/testcase.py - 测试步骤 + 测试用例 CRUD
import logging

from sqlalchemy.orm import Session

from app import db_models, models

logger = logging.getLogger(__name__)


# ----------------------------
# 测试步骤CRUD
# ----------------------------

def create_test_step(db: Session, step: models.TestStepCreate) -> db_models.TestStep:
    """创建测试步骤（不 commit，由调用者负责）"""
    db_step = db_models.TestStep(
        case_id=step.case_id,
        step_order=step.step_order,
        description=step.description,
        parsed_result=step.parsed_result
    )
    db.add(db_step)
    db.flush()
    db.refresh(db_step)
    return db_step

def get_steps_for_case(db: Session, case_id: int) -> list[db_models.TestStep]:
    """获取测试用例的所有步骤"""
    return db.query(db_models.TestStep).filter(db_models.TestStep.case_id == case_id).order_by(db_models.TestStep.step_order.asc()).all()

def delete_steps_for_case(db: Session, case_id: int) -> None:
    """删除测试用例的所有步骤（不 commit，由调用者负责）"""
    # 先将 RunLog 中对这些步骤的引用置空，否则 FK 约束会阻止删除
    db.query(db_models.RunLog).filter(
        db_models.RunLog.step_id.in_(
            db.query(db_models.TestStep.id).filter(db_models.TestStep.case_id == case_id)
        )
    ).update({db_models.RunLog.step_id: None}, synchronize_session=False)
    db.query(db_models.TestStep).filter(db_models.TestStep.case_id == case_id).delete()

# ----------------------------
# 测试用例CRUD
# ----------------------------

def get_next_project_case_number(db: Session, project_id: int) -> int:
    """获取项目内下一个用例编号"""
    max_num = db.query(db_models.TestCase.project_case_number).filter(
        db_models.TestCase.project_id == project_id
    ).order_by(db_models.TestCase.project_case_number.desc()).first()
    return (max_num[0] + 1) if max_num and max_num[0] else 1


def create_test_case(db: Session, case: models.TestCaseCreate) -> db_models.TestCase:
    """创建测试用例及其步骤（单事务）"""
    db_case = db_models.TestCase(
        project_id=case.project_id,
        module_id=case.module_id,
        project_case_number=get_next_project_case_number(db, case.project_id),
        name=case.name,
        description=case.description
    )
    db.add(db_case)
    db.flush()

    # 创建步骤
    created_steps = []
    for step_data in case.steps:
        db_step = db_models.TestStep(
            case_id=db_case.id,
            step_order=step_data.step_order,
            description=step_data.description,
            parsed_result=step_data.parsed_result
        )
        db.add(db_step)
        created_steps.append(db_step)

    db.commit()
    db.refresh(db_case)
    db_case.steps = created_steps
    return db_case

def get_test_case(db: Session, case_id: int) -> db_models.TestCase | None:
    """通过ID获取测试用例"""
    return db.query(db_models.TestCase).filter(db_models.TestCase.id == case_id).first()

def get_all_test_cases_for_project(db: Session, project_id: int) -> list[db_models.TestCase]:
    """获取项目的所有测试用例"""
    return db.query(db_models.TestCase).filter(db_models.TestCase.project_id == project_id).order_by(db_models.TestCase.created_at.desc()).all()

def get_all_test_cases_for_project_paginated(db: Session, project_id: int, page: int = 1, size: int = 20) -> dict[str, any]:
    """分页获取项目的测试用例"""
    total_items = db.query(db_models.TestCase).filter(db_models.TestCase.project_id == project_id).count()
    offset = (page - 1) * size
    items = db.query(db_models.TestCase).filter(db_models.TestCase.project_id == project_id).order_by(db_models.TestCase.id.asc()).offset(offset).limit(size).all()
    return {"total_items": total_items, "items": items}

def get_all_test_cases_for_module(db: Session, module_id: int) -> list[db_models.TestCase]:
    """获取模块的所有测试用例"""
    return db.query(db_models.TestCase).filter(db_models.TestCase.module_id == module_id).order_by(db_models.TestCase.id.asc()).all()

def get_all_test_cases_for_module_paginated(db: Session, module_id: int, page: int = 1, size: int = 20) -> dict[str, any]:
    """获取模块的所有测试用例（分页）"""
    total_items = db.query(db_models.TestCase).filter(db_models.TestCase.module_id == module_id).count()
    offset = (page - 1) * size
    items = db.query(db_models.TestCase).filter(db_models.TestCase.module_id == module_id).order_by(db_models.TestCase.id.asc()).offset(offset).limit(size).all()
    return {"total_items": total_items, "items": items}

def search_test_cases(db: Session, project_id: int, query: str, page: int = 1, size: int = 20) -> dict[str, any]:
    """根据名称和描述搜索测试用例"""
    q = db.query(db_models.TestCase).filter(db_models.TestCase.project_id == project_id)
    if query:
        like = f"%{query}%"
        q = q.filter(
            (db_models.TestCase.name.ilike(like)) | (db_models.TestCase.description.ilike(like))
        )
    total = q.count()
    items = q.order_by(db_models.TestCase.id.asc()).offset((page-1)*size).limit(size).all()
    return {"total_items": total, "items": items}

def update_test_case(db: Session, case_id: int, case: models.TestCaseUpdate) -> db_models.TestCase | None:
    """更新测试用例（单事务）"""
    db_case = get_test_case(db, case_id)
    if not db_case:
        return None

    # 更新基本信息
    db_case.name = case.name
    db_case.description = case.description
    db_case.module_id = case.module_id

    # 删除旧步骤并创建新步骤（单事务）
    delete_steps_for_case(db, case_id)

    for step_data in case.steps:
        db_step = db_models.TestStep(
            case_id=case_id,
            step_order=step_data.step_order,
            description=step_data.description,
            parsed_result=step_data.parsed_result
        )
        db.add(db_step)

    db.commit()
    db.refresh(db_case)
    return db_case

def update_test_case_is_init(db: Session, case_id: int, is_init: bool) -> db_models.TestCase | None:
    """切换测试用例的初始化标记"""
    db_case = get_test_case(db, case_id)
    if not db_case:
        return None
    db_case.is_init = is_init
    db.commit()
    db.refresh(db_case)
    return db_case


def get_init_test_cases(db: Session, project_id: int) -> list[db_models.TestCase]:
    """获取项目下所有标记为初始化的测试用例"""
    return db.query(db_models.TestCase).filter(
        db_models.TestCase.project_id == project_id,
        db_models.TestCase.is_init == True,
    ).order_by(db_models.TestCase.created_at.desc()).all()


def delete_test_case(db: Session, case_id: int) -> dict[str, str] | None:
    """删除测试用例"""
    db_case = get_test_case(db, case_id)
    if not db_case:
        return None
    
    # 删除关联的步骤
    delete_steps_for_case(db, case_id)
    
    db.delete(db_case)
    db.commit()
    return {"message": f"测试用例 {case_id} 及其步骤已删除。"}