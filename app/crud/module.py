# app/crud/module.py - 模块 CRUD
import logging

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import db_models, models

logger = logging.getLogger(__name__)


# ----------------------------
# 模块CRUD
# ----------------------------

async def create_module(db: AsyncSession, project_id: int, module: models.ModuleCreate) -> db_models.Module:
    """创建新模块"""
    db_module = db_models.Module(
        project_id=project_id,
        name=module.name,
        description=module.description,
        parent_id=module.parent_id
    )
    db.add(db_module)
    await db.commit()
    await db.refresh(db_module)
    return db_module

async def get_module(db: AsyncSession, module_id: int) -> db_models.Module | None:
    """通过ID获取模块"""
    result = await db.execute(
        select(db_models.Module).where(db_models.Module.id == module_id)
    )
    return result.scalar_one_or_none()

async def get_modules_for_project(db: AsyncSession, project_id: int) -> list[db_models.Module]:
    """获取项目的所有模块"""
    result = await db.execute(
        select(db_models.Module)
        .where(db_models.Module.project_id == project_id)
        .order_by(db_models.Module.name.asc())
    )
    return result.scalars().all()

async def get_module_tree(db: AsyncSession, project_id: int) -> list[dict]:
    """递归构建模块树形结构"""
    result = await db.execute(
        select(db_models.Module)
        .where(db_models.Module.project_id == project_id)
        .order_by(db_models.Module.name.asc())
    )
    all_modules = result.scalars().all()

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

async def get_module_descendants(db: AsyncSession, module_id: int) -> list[int]:
    """递归获取模块及所有下级模块 ID 列表"""
    result_ids = [module_id]
    children_result = await db.execute(
        select(db_models.Module).where(db_models.Module.parent_id == module_id)
    )
    children = children_result.scalars().all()
    for child in children:
        result_ids.extend(await get_module_descendants(db, child.id))
    return result_ids

async def validate_module_parent(db: AsyncSession, module_id: int, parent_id: int) -> bool:
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
        parent_result = await db.execute(
            select(db_models.Module).where(db_models.Module.id == current)
        )
        parent_module = parent_result.scalar_one_or_none()
        if not parent_module:
            break
        current = parent_module.parent_id
    return True

async def update_module(db: AsyncSession, module_id: int, module: models.ModuleUpdate) -> db_models.Module | None:
    """更新模块"""
    db_module = await get_module(db, module_id)
    if not db_module:
        return None

    update_data = module.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(db_module, key, value)

    await db.commit()
    await db.refresh(db_module)
    return db_module

async def delete_module(db: AsyncSession, module_id: int) -> bool | None:
    """删除模块（含删除保护：模块或子模块下有测试用例时拒绝删除；否则级联删除子模块）"""
    db_module = await get_module(db, module_id)
    if not db_module:
        return None

    # 检查模块及其所有子模块下是否有测试用例
    descendant_ids = await get_module_descendants(db, module_id)
    count_result = await db.execute(
        select(func.count())
        .select_from(db_models.TestCase)
        .where(db_models.TestCase.module_id.in_(descendant_ids))
    )
    case_count = count_result.scalar()
    if case_count > 0:
        raise ValueError(f"模块或其子模块下有 {case_count} 个测试用例，无法删除")

    # 先删除所有子模块（从叶子到根），再删除自身
    child_ids = [cid for cid in descendant_ids if cid != module_id]
    if child_ids:
        await db.execute(
            delete(db_models.Module).where(db_models.Module.id.in_(child_ids))
        )

    await db.delete(db_module)
    await db.commit()
    return True
