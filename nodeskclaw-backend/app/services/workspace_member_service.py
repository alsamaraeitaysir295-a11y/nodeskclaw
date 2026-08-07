"""Workspace member service: permission checks, member search."""

import logging

from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ForbiddenError, NotFoundError
from app.models.admin_membership import AdminMembership
from app.models.base import not_deleted
from app.models.org_membership import ADMIN_ROLE_LEVEL, OrgMembership, OrgRole
from app.models.user import User
from app.models.workspace import Workspace
from app.models.workspace_member import WorkspaceMember

# 每个权限对应的最低组织角色等级；三档均满足即放行，不再区分"使用/配置"两档
WORKSPACE_PERMISSION_MIN_ROLE: dict[str, str] = {
    "send_chat": OrgRole.member,
    "edit_blackboard": OrgRole.operator,
    "manage_agents": OrgRole.operator,
    "edit_topology": OrgRole.operator,
    "manage_settings": OrgRole.admin,
    "manage_members": OrgRole.admin,
    "delete_workspace": OrgRole.admin,
}

logger = logging.getLogger(__name__)


async def _get_org_role(user_id: str, org_id: str, db: AsyncSession) -> str | None:
    result = await db.execute(
        select(OrgMembership.role).where(
            OrgMembership.user_id == user_id,
            OrgMembership.org_id == org_id,
            not_deleted(OrgMembership),
        )
    )
    return result.scalar_one_or_none()


async def _get_workspace_org_id(workspace_id: str, db: AsyncSession) -> str | None:
    result = await db.execute(
        select(Workspace.org_id).where(
            Workspace.id == workspace_id,
            not_deleted(Workspace),
        )
    )
    return result.scalar_one_or_none()


async def _get_workspace(workspace_id: str, db: AsyncSession) -> Workspace | None:
    result = await db.execute(
        select(Workspace).where(
            Workspace.id == workspace_id,
            not_deleted(Workspace),
        )
    )
    return result.scalar_one_or_none()


async def check_workspace_access(
    workspace_id: str,
    user: User,
    required_permission: str,
    db: AsyncSession,
) -> None:
    """Check that *user* has *required_permission* on the workspace.

    纯组织三级角色驱动：不再看创建者身份，也不再查 WorkspaceMember 角色字段。
    返回 None 表示通过，抛出 ForbiddenError / NotFoundError 表示拒绝。
    """
    workspace = await _get_workspace(workspace_id, db)
    if workspace is None:
        raise NotFoundError("办公室不存在", "errors.workspace.not_found")

    org_role = await _get_org_role(user.id, workspace.org_id, db)
    if org_role is None:
        raise ForbiddenError("您不是该组织的成员", "errors.workspace.no_access")

    min_role = WORKSPACE_PERMISSION_MIN_ROLE.get(required_permission, OrgRole.admin)
    user_level = ADMIN_ROLE_LEVEL.get(org_role, 0)
    min_level = ADMIN_ROLE_LEVEL[min_role]
    if user_level < min_level:
        raise ForbiddenError(f"需要 {min_role.value} 及以上角色", "errors.workspace.insufficient_permission")
    return None


async def check_workspace_member(
    workspace_id: str,
    user: User,
    db: AsyncSession,
) -> None:
    """Check that *user* is a member of the workspace (read-only access).

    同 org 任意成员均可通过基础访问检查。
    返回 None 表示通过，抛出 ForbiddenError 表示拒绝。
    """
    org_id = await _get_workspace_org_id(workspace_id, db)
    if org_id is None:
        raise NotFoundError("办公室不存在", "errors.workspace.not_found")

    org_role = await _get_org_role(user.id, org_id, db)
    if org_role is not None:
        return None

    raise ForbiddenError("您不是该组织的成员", "errors.workspace.no_access")


async def get_my_permissions(
    workspace_id: str,
    user: User,
    db: AsyncSession,
) -> dict:
    """Return the current user's permissions and admin status for the workspace."""
    workspace = await _get_workspace(workspace_id, db)
    if workspace is None:
        raise NotFoundError("办公室不存在", "errors.workspace.not_found")

    org_role = await _get_org_role(user.id, workspace.org_id, db)
    if org_role is None:
        raise ForbiddenError("您不是该组织的成员", "errors.workspace.no_access")

    user_level = ADMIN_ROLE_LEVEL.get(org_role, 0)
    permissions = [
        perm for perm, min_role in WORKSPACE_PERMISSION_MIN_ROLE.items()
        if user_level >= ADMIN_ROLE_LEVEL[min_role]
    ]
    return {
        "is_admin": org_role == OrgRole.admin,
        "is_org_admin": org_role == OrgRole.admin,
        "permissions": permissions,
    }


async def search_org_users(
    workspace_id: str,
    org_id: str,
    query_str: str,
    db: AsyncSession,
) -> list[dict]:
    """Search org members who are NOT already workspace members (excluding Admin users)."""
    existing_member_ids = (
        select(WorkspaceMember.user_id)
        .where(
            WorkspaceMember.workspace_id == workspace_id,
            not_deleted(WorkspaceMember),
        )
    )
    admin_user_ids = (
        select(AdminMembership.user_id)
        .where(
            AdminMembership.org_id == org_id,
            AdminMembership.deleted_at.is_(None),
        )
    )

    stmt = (
        select(User)
        .join(OrgMembership, OrgMembership.user_id == User.id)
        .where(
            OrgMembership.org_id == org_id,
            not_deleted(OrgMembership),
            not_deleted(User),
            User.id.notin_(existing_member_ids),
            User.id.notin_(admin_user_ids),
        )
    )

    if query_str and query_str.strip():
        pattern = f"%{query_str.strip()}%"
        stmt = stmt.where(or_(User.name.ilike(pattern), User.email.ilike(pattern)))

    stmt = stmt.limit(20)
    result = await db.execute(stmt)
    return [
        {
            "user_id": u.id,
            "name": u.name,
            "email": u.email,
            "avatar_url": u.avatar_url,
        }
        for u in result.scalars().all()
    ]
