"""审计日志 actor 展示名批量解析 — name 优先，email 兜底，避免裸显 UUID。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User


async def batch_resolve_actor_display_names(
    db: AsyncSession, actor_ids: set[str],
) -> dict[str, str]:
    """按 actor_id 批量查 User.name/email，返回 {actor_id: 展示名}。

    姓名优先、邮箱兜底；某个 actor_id 查不到用户或 name/email 都是空则不放进返回字典，
    交给调用方按约定兜底显示截短 ID。
    """
    if not actor_ids:
        return {}
    result = await db.execute(
        select(User.id, User.name, User.email).where(User.id.in_(actor_ids))
    )
    return {uid: (name or email) for uid, name, email in result.all() if (name or email)}
