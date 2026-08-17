"""Instance template API routes."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_org, get_db
from app.core.security import get_current_user
from app.core import hooks
from app.models.user import User
from app.schemas.common import ApiResponse, PaginatedResponse, Pagination
from app.schemas.gene import ReviewRequest
from app.schemas.instance_template import (
    InstanceTemplateCreate,
    InstanceTemplateFromInstance,
    InstanceTemplateInfo,
    InstanceTemplateUpdate,
)
from app.services import instance_template_service as svc

router = APIRouter()


@router.get("/instance-templates", response_model=PaginatedResponse)
async def list_templates(
    keyword: str | None = Query(None),
    featured: bool = Query(False),
    visibility: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    org_info=Depends(get_current_org),
):
    user, org = org_info
    items, total = await svc.list_templates(
        db, org_id=org.id, visibility=visibility, keyword=keyword, featured_only=featured,
        page=page, page_size=page_size, requesting_user_id=user.id,
    )
    return PaginatedResponse(
        data=[item.model_dump(mode="json") for item in items],
        pagination=Pagination(page=page, page_size=page_size, total=total),
    )


@router.get("/instance-templates/featured", response_model=ApiResponse)
async def featured_templates(
    db: AsyncSession = Depends(get_db),
    org_info=Depends(get_current_org),
):
    user, org = org_info
    items, _ = await svc.list_templates(db, org_id=org.id, featured_only=True, page=1, page_size=10)
    return ApiResponse(data=[item.model_dump(mode="json") for item in items])


@router.get("/instance-templates/{template_id}", response_model=ApiResponse)
async def get_template(
    template_id: str,
    db: AsyncSession = Depends(get_db),
    org_info=Depends(get_current_org),
):
    _user, org = org_info
    item = await svc.get_template(db, template_id, org.id)
    return ApiResponse(data=item.model_dump(mode="json"))


@router.post("/instance-templates", response_model=ApiResponse)
async def create_template(
    body: InstanceTemplateCreate,
    db: AsyncSession = Depends(get_db),
    org_info=Depends(get_current_org),
):
    user, org = org_info
    item = await svc.create_template(db, body, user_id=user.id, org_id=org.id, is_super_admin=user.is_super_admin)
    await hooks.emit("operation_audit", action="instance_template.created", target_type="instance_template", target_id=item.id, actor_id=user.id, org_id=org.id)
    return ApiResponse(data=item.model_dump(mode="json"))


@router.post("/instance-templates/from-instance/{instance_id}", response_model=ApiResponse)
async def create_from_instance(
    instance_id: str,
    body: InstanceTemplateFromInstance,
    db: AsyncSession = Depends(get_db),
    org_info=Depends(get_current_org),
):
    user, org = org_info
    item = await svc.create_from_instance(db, instance_id, body, user_id=user.id, org_id=org.id, is_super_admin=user.is_super_admin)
    await hooks.emit("operation_audit", action="instance_template.created_from_instance", target_type="instance_template", target_id=item.id, actor_id=user.id, org_id=org.id, details={"source_instance_id": instance_id})
    return ApiResponse(data=item.model_dump(mode="json"))


@router.put("/instance-templates/{template_id}", response_model=ApiResponse)
async def update_template(
    template_id: str,
    body: InstanceTemplateUpdate,
    db: AsyncSession = Depends(get_db),
    org_info=Depends(get_current_org),
):
    user, org = org_info
    item = await svc.update_template(db, template_id, body, org.id)
    await hooks.emit("operation_audit", action="instance_template.updated", target_type="instance_template", target_id=template_id, actor_id=user.id, org_id=org.id)
    return ApiResponse(data=item.model_dump(mode="json"))


@router.delete("/instance-templates/{template_id}", response_model=ApiResponse)
async def delete_template(
    template_id: str,
    db: AsyncSession = Depends(get_db),
    org_info=Depends(get_current_org),
):
    user, org = org_info
    result = await svc.delete_template(db, template_id, org.id)
    await hooks.emit("operation_audit", action="instance_template.deleted", target_type="instance_template", target_id=template_id, actor_id=user.id, org_id=org.id)
    return ApiResponse(data=result)


@router.get("/admin/instance-templates/pending-review")
async def admin_pending_review_templates(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 列表内容按权限过滤：超管全部 / 组织 operator+ 仅本组织 / 其他用户空列表
    templates = await svc.get_pending_review_instance_templates(db, current_user=current_user)
    return ApiResponse(data=templates)


@router.put("/admin/instance-templates/{template_id}/review")
async def admin_review_template(
    template_id: str,
    req: ReviewRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 权限校验下沉到 service：仅模板所属 org 的 operator/admin 或平台超管
    result = await svc.review_instance_template(
        db, template_id, req.action, req.reason, current_user=current_user,
    )
    await hooks.emit("operation_audit", action="instance_template.reviewed", target_type="instance_template", target_id=template_id, actor_id=current_user.id, org_id=current_user.current_org_id, details={"review_action": req.action, "reason": req.reason})
    return ApiResponse(data=result)
