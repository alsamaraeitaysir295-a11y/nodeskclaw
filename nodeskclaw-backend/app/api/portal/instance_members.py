"""Portal instance member management endpoints."""

import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.core.security import get_current_user
from app.core import hooks
from app.models.instance_member import InstanceRole
from app.models.user import User
from app.schemas.common import ApiResponse
from app.schemas.instance_member import (
    AddMemberRequest,
    InstanceMemberInfo,
    SearchUserResult,
    UpdateMemberRoleRequest,
)
from app.services import instance_member_service

logger = logging.getLogger(__name__)

router = APIRouter()


async def _get_instance_org_id(instance_id: str, db: AsyncSession) -> str | None:
    from app.models.base import not_deleted
    from app.models.instance import Instance
    from sqlalchemy import select
    result = await db.execute(
        select(Instance.org_id).where(Instance.id == instance_id, not_deleted(Instance))
    )
    return result.scalar_one_or_none()


@router.get("/{instance_id}/members", response_model=ApiResponse[list[InstanceMemberInfo]])
async def list_members(
    instance_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await instance_member_service.check_instance_access(
        instance_id, current_user, InstanceRole.viewer, db
    )
    data = await instance_member_service.list_members(instance_id, db)
    return ApiResponse(data=data)


@router.get("/{instance_id}/members/search-users", response_model=ApiResponse[list[SearchUserResult]])
async def search_users_for_member(
    instance_id: str,
    q: str = Query("", description="按名称/邮箱模糊搜索"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await instance_member_service.check_instance_access(
        instance_id, current_user, InstanceRole.admin, db
    )
    role = await instance_member_service.get_user_instance_role(instance_id, current_user, db)
    from app.models.instance import Instance
    from app.models.base import not_deleted
    from sqlalchemy import select
    instance = (await db.execute(
        select(Instance).where(Instance.id == instance_id, not_deleted(Instance))
    )).scalar_one_or_none()
    if not instance or not instance.org_id:
        return ApiResponse(data=[])
    data = await instance_member_service.search_org_users(instance_id, instance.org_id, q, db)
    return ApiResponse(data=data)


@router.post("/{instance_id}/members", response_model=ApiResponse[InstanceMemberInfo])
async def add_member(
    instance_id: str,
    body: AddMemberRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await instance_member_service.check_instance_access(
        instance_id, current_user, InstanceRole.admin, db
    )
    data = await instance_member_service.add_member(instance_id, body.user_id, body.role, db)
    org_id = await _get_instance_org_id(instance_id, db)
    await hooks.emit("operation_audit", action="instance_member.added", target_type="instance_member", target_id=data["id"], actor_id=current_user.id, org_id=org_id, details={"instance_id": instance_id, "user_id": body.user_id, "role": body.role})
    return ApiResponse(data=data)


@router.put("/{instance_id}/members/{member_id}", response_model=ApiResponse[InstanceMemberInfo])
async def update_member_role(
    instance_id: str,
    member_id: str,
    body: UpdateMemberRoleRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await instance_member_service.check_instance_access(
        instance_id, current_user, InstanceRole.admin, db
    )
    data = await instance_member_service.update_member(instance_id, member_id, body.role, db)
    org_id = await _get_instance_org_id(instance_id, db)
    await hooks.emit("operation_audit", action="instance_member.role_updated", target_type="instance_member", target_id=member_id, actor_id=current_user.id, org_id=org_id, details={"instance_id": instance_id, "role": body.role})
    return ApiResponse(data=data)


@router.delete("/{instance_id}/members/{member_id}", response_model=ApiResponse)
async def remove_member(
    instance_id: str,
    member_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await instance_member_service.check_instance_access(
        instance_id, current_user, InstanceRole.admin, db
    )
    org_id = await _get_instance_org_id(instance_id, db)
    await instance_member_service.remove_member(instance_id, member_id, current_user.id, db)
    await hooks.emit("operation_audit", action="instance_member.removed", target_type="instance_member", target_id=member_id, actor_id=current_user.id, org_id=org_id, details={"instance_id": instance_id})
    return ApiResponse(message="成员已移除")
