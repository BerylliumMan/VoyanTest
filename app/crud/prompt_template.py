# app/crud/prompt_template.py
# CRUD operations for PromptTemplate (versioned prompt templates)
from sqlalchemy import select, func, delete as sa_delete, and_, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db_models import PromptTemplate


async def list_prompt_templates(db: AsyncSession) -> list[PromptTemplate]:
    """List all prompt templates, returning active version for each key."""
    # Subquery: get max version for each active key
    subq = (
        select(
            PromptTemplate.key,
            func.max(PromptTemplate.version).label("max_version"),
        )
        .group_by(PromptTemplate.key)
        .subquery()
    )
    q = select(PromptTemplate).join(
        subq,
        and_(
            PromptTemplate.key == subq.c.key,
            PromptTemplate.version == subq.c.max_version,
        ),
    ).order_by(PromptTemplate.key)
    result = await db.execute(q)
    return list(result.scalars().all())


async def get_prompt_template_by_key(
    db: AsyncSession, key: str, version: int | None = None
) -> PromptTemplate | None:
    """Get a prompt template by key. If version omitted, returns active version."""
    if version:
        q = select(PromptTemplate).where(
            PromptTemplate.key == key,
            PromptTemplate.version == version,
        )
    else:
        q = select(PromptTemplate).where(
            PromptTemplate.key == key,
            PromptTemplate.is_active.is_(True),
        )
        q = q.limit(1)
    result = await db.execute(q)
    return result.scalar_one_or_none()


async def get_prompt_versions(
    db: AsyncSession, key: str
) -> list[PromptTemplate]:
    """List all versions of a prompt template."""
    q = select(PromptTemplate).where(
        PromptTemplate.key == key,
    ).order_by(PromptTemplate.version.desc())
    result = await db.execute(q)
    return list(result.scalars().all())


async def create_prompt_template(
    db: AsyncSession,
    key: str,
    name: str,
    category: str,
    content: str,
    variables: list[str] | None = None,
    description: str | None = None,
) -> PromptTemplate:
    """Create a new version of a prompt template. Version is auto-incremented."""
    # Get current max version
    max_q = select(func.coalesce(func.max(PromptTemplate.version), 0)).where(
        PromptTemplate.key == key,
    )
    result = await db.execute(max_q)
    max_ver = result.scalar() or 0

    pt = PromptTemplate(
        key=key,
        name=name,
        category=category,
        content=content,
        variables=variables or [],
        version=max_ver + 1,
        is_active=False,
        description=description,
    )
    db.add(pt)
    await db.commit()
    await db.refresh(pt)
    return pt


async def activate_prompt_version(
    db: AsyncSession, key: str, version: int
) -> PromptTemplate | None:
    """Activate a specific version of a prompt template."""
    # Deactivate all versions
    await db.execute(
        sa_update(PromptTemplate)
        .where(PromptTemplate.key == key)
        .values(is_active=False)
    )
    # Activate the specified version
    pt = await get_prompt_template_by_key(db, key, version=version)
    if pt is None:
        await db.rollback()
        return None
    pt.is_active = True
    await db.commit()
    await db.refresh(pt)
    return pt


async def delete_prompt_template(
    db: AsyncSession, key: str, version: int | None = None
) -> bool:
    """Delete a prompt template version. If version omitted, deletes all."""
    q = sa_delete(PromptTemplate).where(PromptTemplate.key == key)
    if version:
        q = q.where(PromptTemplate.version == version)
    result = await db.execute(q)
    await db.commit()
    return result.rowcount > 0
