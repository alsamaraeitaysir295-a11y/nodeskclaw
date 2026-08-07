"""验证 Gene 审核中心的三处权限点放宽为 operator 及以上可见/可审核。"""
import uuid

import pytest

from app.core.exceptions import ForbiddenError
from app.models.gene import Gene, GeneReviewStatus
from app.models.org_membership import OrgMembership, OrgRole
from app.models.organization import Organization
from app.models.user import User
from app.services import gene_service
from tests.conftest import TestSessionLocal


async def _make_org_user_pending_gene(role: str):
    suffix = uuid.uuid4().hex[:8]
    async with TestSessionLocal() as db:
        org = Organization(name=f"genereview-org-{suffix}", slug=f"genereview-org-{suffix}")
        db.add(org)
        await db.flush()
        user = User(
            email=f"genereview-{suffix}@example.com", name=f"genereview-{suffix}",
            password_hash="x", current_org_id=org.id,
        )
        db.add(user)
        await db.flush()
        db.add(OrgMembership(org_id=org.id, user_id=user.id, role=role))
        gene_id = f"gene-{suffix}"
        gene = Gene(
            id=gene_id,
            name=f"gene-{suffix}", slug=f"gene-{suffix}", org_id=org.id,
            visibility="org", review_status=GeneReviewStatus.pending_owner,
            # created_by 有外键约束（指向 users 表），不能用随机 uuid，
            # 这里用同一个用户的 id（谁提交跟谁审核在这个测试里无关紧要）。
            created_by=user.id,
            # lineage_group_id 是 NOT NULL 且无 Python 级默认值的血缘分组标识，
            # 直接走 ORM 构造必须显式给值（正常创建路径由 gene_service 生成）。
            lineage_group_id=gene_id,
        )
        db.add(gene)
        await db.commit()
        await db.refresh(user)
        await db.refresh(gene)
        return user, gene


@pytest.mark.asyncio
async def test_operator_can_review_gene():
    user, gene = await _make_org_user_pending_gene(OrgRole.operator)
    async with TestSessionLocal() as db:
        result = await gene_service.review_gene(db, gene.id, "approve", current_user=user)
        assert result["review_status"] == GeneReviewStatus.approved


@pytest.mark.asyncio
async def test_member_cannot_review_gene():
    user, gene = await _make_org_user_pending_gene(OrgRole.member)
    async with TestSessionLocal() as db:
        with pytest.raises(Exception):  # HTTPException 403
            await gene_service.review_gene(db, gene.id, "approve", current_user=user)


@pytest.mark.asyncio
async def test_operator_sees_pending_review_in_own_org():
    user, gene = await _make_org_user_pending_gene(OrgRole.operator)
    async with TestSessionLocal() as db:
        items = await gene_service.get_pending_review_genes(db, current_user=user)
        assert any(item["id"] == gene.id for item in items)


@pytest.mark.asyncio
async def test_member_sees_no_pending_review():
    user, gene = await _make_org_user_pending_gene(OrgRole.member)
    async with TestSessionLocal() as db:
        items = await gene_service.get_pending_review_genes(db, current_user=user)
        assert items == []
