"""超管审计查询 endpoint。"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db, require_super_admin_dep
from app.models.admin_action import AdminAction
from app.models.user import User
from app.schemas.common import ApiResponse, PaginatedResponse, Pagination
from app.services.audit_actor_names import batch_resolve_actor_display_names
from ee.backend.services.admin import audit_service
from ee.backend.services.admin.errors import AdminErrorCode, raise_admin_error

router = APIRouter()


async def _resolve_actor_ids(db: AsyncSession, actor: str | None) -> list[str] | None:
    """操作人筛选支持姓名/邮箱/ID：按模糊词解析出用户 id 集合。

    - 无输入返回 None（不过滤）
    - 按姓名/邮箱/精确 ID 匹配用户；匹配不到时回退按原值精确匹配
      （兼容 agent / system 等非用户 actor_id），仍匹配不到则返回空列表 → 结果为空
    """
    if not actor or not actor.strip():
        return None
    keyword = actor.strip()
    like = f"%{keyword}%"
    rows = (await db.execute(
        select(User.id).where(
            User.deleted_at.is_(None),
            or_(User.name.ilike(like), User.email.ilike(like), User.id == keyword),
        )
    )).scalars().all()
    return list(rows) or [keyword]


@router.get("/audit/actions", response_model=ApiResponse[list[str]])
async def list_audit_actions(
    admin: User = Depends(require_super_admin_dep),
):
    """返回所有 AdminAction enum value，供前端筛选下拉使用。"""
    return ApiResponse[list[str]](data=[a.value for a in AdminAction])


@router.get("/audit", response_model=PaginatedResponse[dict])
async def list_audit(
    actor: str | None = Query(None),
    action: str | None = Query(None),
    from_ts: datetime | None = Query(None, alias="from"),
    to_ts: datetime | None = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_super_admin_dep),
):
    """分页查询审计日志，支持按操作人、动作、时间段过滤。"""
    # 时间区间校验：from 不能晚于 to
    if from_ts and to_ts and from_ts > to_ts:
        raise_admin_error(
            AdminErrorCode.AUDIT_TIME_RANGE_INVALID,
            message_key="errors.admin.audit_time_range_invalid",
            message="from must be <= to",
        )

    # action 字符串转 enum，无效值返回 409
    action_enum: AdminAction | None = None
    if action:
        try:
            action_enum = AdminAction(action)
        except ValueError:
            raise_admin_error(
                AdminErrorCode.AUDIT_ACTION_INVALID,
                message_key="errors.admin.audit_action_invalid",
                message=f"Invalid action: {action}",
            )

    rows, total = await audit_service.query_audit_logs(
        db,
        actor_ids=await _resolve_actor_ids(db, actor),
        action=action_enum,
        from_dt=from_ts,
        to_dt=to_ts,
        page=page,
        page_size=page_size,
    )

    user_ids = {r.actor_id for r in rows if r.actor_type == "user" and not r.actor_name}
    name_map = await batch_resolve_actor_display_names(db, user_ids)

    data = [
        {
            "id": r.id,
            "action": r.action,
            "actor_id": r.actor_id,
            "actor_name": r.actor_name or name_map.get(r.actor_id),
            "actor_type": r.actor_type,
            "target_type": r.target_type,
            "target_id": r.target_id,
            "org_id": r.org_id,
            "details": r.details,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]

    return PaginatedResponse[dict](
        data=data,
        pagination=Pagination(page=page, page_size=page_size, total=total),
    )
