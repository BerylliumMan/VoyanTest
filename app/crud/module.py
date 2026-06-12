# app/crud/module.py - 模块 CRUD
import logging

from sqlalchemy.orm import Session

from app import db_models, models

logger = logging.getLogger(__name__)


# ----------------------------
# 模块CRUD
# ----------------------------

def create_module(db: Session, project_id: int, module: models.ModuleCreate) -> db_models.Module:
    """创建新模块"""
    db_module = db_models.Module(
        project_id=project_id,
        name=module.name,
        description=module.description,
        parent_id=module.parent_id
    )
    db.add(db_module)
    db.commit()
    db.refresh(db_module)
    return db_module

def get_module(db: Session, module_id: int) -> db_models.Module | None:
    """通过ID获取模块"""
    return db.query(db_models.Module).filter(db_models.Module.id == module_id).first()

def get_modules_for_project(db: Session, project_id: int) -> list[db_models.Module]:
    """获取项目的所有模块"""
    return db.query(db_models.Module).filter(db_models.Module.project_id == project_id).order_by(db_models.Module.name.asc()).all()

def get_module_tree(db: Session, project_id: int) -> list[dict]:
    """递归构建模块树形结构"""
    all_modules = db.query(db_models.Module).filter(
        db_models.Module.project_id == project_id
    ).order_by(db_models.Module.name.asc()).all()

    module_map = {}
    for m in all_modules:
        module_map[m.id] = {
            "id": m.id,
            "name": m.name,
            "project_id": m.project_id,
            "parent_id": m.parent_id,
            "description": m.description,
            "created_at": m.created_at.isoformat() if m.created_at else None,
            "children": [],
        }

    tree = []
    for m in all_modules:
        node = module_map[m.id]
        if m.parent_id and m.parent_id in module_map:
            module_map[m.parent_id]["children"].append(node)
        else:
            tree.append(node)

    return tree

def get_module_descendants(db: Session, module_id: int) -> list[int]:
    """递归获取模块及所有下级模块 ID 列表"""
    result_ids = [module_id]
    children = db.query(db_models.Module).filter(
        db_models.Module.parent_id == module_id
    ).all()
    for child in children:
        result_ids.extend(get_module_descendants(db, child.id))
    return result_ids

def validate_module_parent(db: Session, module_id: int, parent_id: int) -> bool:
    """检查 parent_id 不会形成循环引用"""
    if parent_id is None:
        return True
    if parent_id == module_id:
        return False
    current = parent_id
    visited = set()
    while current is not None:
        if current == module_id or current in visited:
            return False
        visited.add(current)
        parent_module = db.query(db_models.Module).filter(db_models.Module.id == current).first()
        if not parent_module:
            break
        current = parent_module.parent_id
    return True

def update_module(db: Session, module_id: int, module: models.ModuleUpdate) -> db_models.Module | None:
    """更新模块"""
    db_module = get_module(db, module_id)
    if not db_module:
        return None

    update_data = module.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(db_module, key, value)

    db.commit()
    db.refresh(db_module)
    return db_module

def delete_module(db: Session, module_id: int) -> bool | None:
    """删除模块（含删除保护：模块或子模块下有测试用例时拒绝删除；否则级联删除子模块）"""
    db_module = get_module(db, module_id)
    if not db_module:
        return None

    # 检查模块及其所有子模块下是否有测试用例
    descendant_ids = get_module_descendants(db, module_id)
    case_count = db.query(db_models.TestCase).filter(
        db_models.TestCase.module_id.in_(descendant_ids)
    ).count()
    if case_count > 0:
        raise ValueError(f"模块或其子模块下有 {case_count} 个测试用例，无法删除")

    # 先删除所有子模块（从叶子到根），再删除自身
    child_ids = [cid for cid in descendant_ids if cid != module_id]
    if child_ids:
        db.query(db_models.Module).filter(
            db_models.Module.id.in_(child_ids)
        ).delete(synchronize_session=False)

    db.delete(db_module)
    db.commit()
    return True