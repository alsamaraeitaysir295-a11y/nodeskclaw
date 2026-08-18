"""Portal instance file management endpoints — read & write (instance admin only)."""

import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.core.security import get_current_user
from app.core import hooks
from app.models.base import not_deleted
from app.models.instance import Instance
from app.models.instance_member import InstanceRole
from app.models.user import User
from app.schemas.common import ApiResponse
from app.services import enterprise_file_service, instance_member_service

logger = logging.getLogger(__name__)

router = APIRouter()


class WriteFileRequest(BaseModel):
    path: str
    content: str


@router.get("/{instance_id}/files", response_model=ApiResponse)
async def list_files(
    instance_id: str,
    path: str = Query(default="", alias="path"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await instance_member_service.check_instance_access(
        instance_id, current_user, InstanceRole.admin, db
    )
    data = await enterprise_file_service.list_files_for_instance(instance_id, path, db)
    return ApiResponse(data=data)


@router.get("/{instance_id}/files/content", response_model=ApiResponse)
async def read_file_content(
    instance_id: str,
    path: str = Query(..., alias="path"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await instance_member_service.check_instance_access(
        instance_id, current_user, InstanceRole.admin, db
    )
    data = await enterprise_file_service.read_file_for_instance(instance_id, path, db)
    return ApiResponse(data=data)


@router.put("/{instance_id}/files/content", response_model=ApiResponse)
async def write_file_content(
    instance_id: str,
    body: WriteFileRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await instance_member_service.check_instance_access(
        instance_id, current_user, InstanceRole.admin, db
    )
    data = await enterprise_file_service.write_file_content(
        instance_id, body.path, body.content, db
    )
    inst_q = await db.execute(
        select(Instance.org_id).where(Instance.id == instance_id, not_deleted(Instance))
    )
    org_id = inst_q.scalar_one_or_none()
    await hooks.emit("operation_audit", action="instance_file.written", target_type="instance", target_id=instance_id, actor_id=current_user.id, org_id=org_id, details={"path": body.path, "size": data.get("size")})
    return ApiResponse(data=data)


@router.get("/{instance_id}/files/download")
async def download_file(
    instance_id: str,
    path: str = Query(..., alias="path"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await instance_member_service.check_instance_access(
        instance_id, current_user, InstanceRole.admin, db
    )
    raw_bytes, filename, mime_type = await enterprise_file_service.download_file_for_instance(
        instance_id, path, db
    )
    filename_encoded = quote(filename, safe="")
    return Response(
        content=raw_bytes,
        media_type=mime_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{filename_encoded}",
            "Content-Length": str(len(raw_bytes)),
        },
    )


# ── 窄作用域端点：仅浏览/下载 workspace 子目录，权限降到 viewer ──────────────
# 供聊天页"生成的文件"面板使用，不暴露 .openclaw 根下 agents/canvas/cron/
# extensions/skills 等运行时内部目录；仍然不做编辑（写入接口维持 admin-only）。


@router.get("/{instance_id}/files/workspace", response_model=ApiResponse)
async def list_workspace_files(
    instance_id: str,
    path: str = Query(default="", alias="path"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await instance_member_service.check_instance_access(
        instance_id, current_user, InstanceRole.viewer, db
    )
    data = await enterprise_file_service.list_files_for_instance(
        instance_id, path, db, workspace_only=True,
    )
    return ApiResponse(data=data)


@router.get("/{instance_id}/files/workspace/download")
async def download_workspace_file(
    instance_id: str,
    path: str = Query(..., alias="path"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await instance_member_service.check_instance_access(
        instance_id, current_user, InstanceRole.viewer, db
    )
    raw_bytes, filename, mime_type = await enterprise_file_service.download_file_for_instance(
        instance_id, path, db, workspace_only=True,
    )
    filename_encoded = quote(filename, safe="")
    return Response(
        content=raw_bytes,
        media_type=mime_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{filename_encoded}",
            "Content-Length": str(len(raw_bytes)),
        },
    )


@router.get("/{instance_id}/files/workspace/content", response_model=ApiResponse)
async def read_workspace_file_content(
    instance_id: str,
    path: str = Query(..., alias="path"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await instance_member_service.check_instance_access(
        instance_id, current_user, InstanceRole.viewer, db
    )
    data = await enterprise_file_service.read_file_for_instance(
        instance_id, path, db, workspace_only=True,
    )
    return ApiResponse(data=data)
