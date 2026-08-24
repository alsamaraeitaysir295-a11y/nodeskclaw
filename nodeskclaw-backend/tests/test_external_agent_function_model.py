"""外部 Agent Function 模型层测试。

覆盖 Phase 2 §5 数据模型 + review P1-2 / P2-8：
- 创建 / 字段 round-trip
- (agent_id, name) 同插件内不重复（partial unique index）
- (agent_id, sort_order) 同插件内不重复（partial unique index，仅约束未软删除行）
- ondelete=CASCADE 与软删除的关系（FK 仅在硬删除时级联；BaseModel.delete 软删不影响子表）
- invoke_config / input_schema 经 Manifest* Pydantic 校验可往返（证明 JSONB 列不丢字段）

测试范围：纯 ORM 层，不依赖 HTTP 端点；运行依赖 conftest 提供的 TestSessionLocal。
"""
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.base import not_deleted
from app.models.external_agent import ExternalAgent
from app.models.external_agent_function import ExternalAgentFunction
from app.models.organization import Organization
from app.schemas.external_agent import (
    ManifestInputSchema,
    ManifestInvokeConfig,
)
from tests.conftest import TestSessionLocal


async def _make_org_and_agent() -> tuple[str, str]:
    """建组织 + tool 型插件；返回 (org_id, agent_id)。"""
    suffix = uuid.uuid4().hex[:8]
    async with TestSessionLocal() as db:
        org = Organization(
            name=f"fn-org-{suffix}",
            slug=f"fn-org-{suffix}",
            external_agent_allowed_cidrs=["127.0.0.0/8"],
        )
        db.add(org)
        await db.flush()
        agent = ExternalAgent(
            org_id=org.id,
            name=f"fn-agent-{suffix}",
            endpoint="http://example.com",
            type="tool",
            status="active",
            version=1,
        )
        db.add(agent)
        await db.commit()
        return org.id, agent.id


# ── 创建 + 字段 round-trip ──────────────────────────────────────────────────


async def test_create_function_for_agent():
    """create → query → assert 全部字段 round-trip 一致（含默认值）。"""
    _, agent_id = await _make_org_and_agent()
    async with TestSessionLocal() as db:
        fn = ExternalAgentFunction(
            agent_id=agent_id,
            name="query_orders",
            summary="查询订单列表",
            invoke_config={
                "endpoint": "http://example.com/orders",
                "method": "GET",
            },
            input_schema={
                "order": ["line"],
                "fields": {
                    "line": {"type": "string", "ui": "input", "label": "产线"},
                },
            },
            status="active",
            sort_order=1,
            source="manual",
            version=1,
        )
        db.add(fn)
        await db.commit()
        await db.refresh(fn)

        assert fn.id is not None
        assert fn.agent_id == agent_id
        assert fn.name == "query_orders"
        assert fn.summary == "查询订单列表"
        assert fn.invoke_config["endpoint"] == "http://example.com/orders"
        assert fn.input_schema["order"] == ["line"]
        assert fn.status == "active"
        assert fn.sort_order == 1
        assert fn.source == "manual"
        assert fn.version == 1
        assert fn.origin_meta is None
        assert fn.created_at is not None
        assert fn.updated_at is not None
        assert fn.deleted_at is None


# ── (agent_id, name) 唯一约束 ────────────────────────────────────────────────


async def test_function_unique_per_agent_name():
    """同插件内两条 function 同名 → IntegrityError。"""
    _, agent_id = await _make_org_and_agent()
    async with TestSessionLocal() as db:
        db.add(ExternalAgentFunction(
            agent_id=agent_id, name="dup", sort_order=1, status="draft",
        ))
        await db.commit()

    async with TestSessionLocal() as db:
        db.add(ExternalAgentFunction(
            agent_id=agent_id, name="dup", sort_order=2, status="draft",
        ))
        with pytest.raises(IntegrityError):
            await db.commit()
        await db.rollback()


# ── (agent_id, sort_order) 唯一约束（review P1-2 决策点）────────────────────


async def test_function_unique_per_agent_sort_order():
    """同插件内两条 function 同 sort_order → IntegrityError。"""
    _, agent_id = await _make_org_and_agent()
    async with TestSessionLocal() as db:
        db.add(ExternalAgentFunction(
            agent_id=agent_id, name="a", sort_order=1, status="draft",
        ))
        await db.commit()

    async with TestSessionLocal() as db:
        db.add(ExternalAgentFunction(
            agent_id=agent_id, name="b", sort_order=1, status="draft",
        ))
        with pytest.raises(IntegrityError):
            await db.commit()
        await db.rollback()


async def test_function_unique_sort_order_skips_soft_deleted():
    """被软删除的 function 不参与 partial unique index → 新 function 可复用其 sort_order。"""
    _, agent_id = await _make_org_and_agent()
    async with TestSessionLocal() as db:
        first = ExternalAgentFunction(
            agent_id=agent_id, name="orig", sort_order=5, status="draft",
        )
        db.add(first)
        await db.commit()
        await db.refresh(first)
        first.soft_delete()
        await db.commit()

    async with TestSessionLocal() as db:
        db.add(ExternalAgentFunction(
            agent_id=agent_id, name="replacement", sort_order=5, status="draft",
        ))
        await db.commit()  # 不应抛 IntegrityError

        result = await db.execute(
            select(ExternalAgentFunction).where(
                ExternalAgentFunction.agent_id == agent_id,
                not_deleted(ExternalAgentFunction),
            )
        )
        live = result.scalars().all()
        assert {f.name for f in live} == {"replacement"}


# ── ondelete=CASCADE 与软删除的关系（review P2-8）───────────────────────────


async def test_function_survives_agent_soft_delete():
    """软删除插件（BaseModel.soft_delete）不影响子表——FK CASCADE 仅在硬删除触发。"""
    _, agent_id = await _make_org_and_agent()
    async with TestSessionLocal() as db:
        fn = ExternalAgentFunction(
            agent_id=agent_id, name="keep", sort_order=1, status="draft",
        )
        db.add(fn)
        await db.commit()

        # 软删除插件
        agent = (await db.execute(
            select(ExternalAgent).where(ExternalAgent.id == agent_id)
        )).scalar_one()
        agent.soft_delete()
        await db.commit()

        # 子表 function 仍存在（软删除插件 → 插件行 deleted_at 非 NULL，
        # function 行 deleted_at 仍 NULL；FK CASCADE 未触发）。
        fn_check = (await db.execute(
            select(ExternalAgentFunction).where(
                ExternalAgentFunction.id == fn.id,
            )
        )).scalar_one()
        assert fn_check.deleted_at is None
        assert fn_check.agent_id == agent_id


async def test_function_hard_delete_via_db_engine_cascades():
    """硬删除插件（绕过 BaseModel.soft_delete 直接 DELETE）→ FK CASCADE 删除 function。

    仓库约定：物理删除不会自动发生（BaseModel 仅暴露 soft_delete），但 FK
    `ondelete='CASCADE'` 是防御性的硬删除兜底（drop_all / DBA 手工 DROP 等
    路径）。本测试用 raw SQL 模拟硬删除，验证 CASCADE 实际生效。
    """
    _, agent_id = await _make_org_and_agent()
    async with TestSessionLocal() as db:
        fn = ExternalAgentFunction(
            agent_id=agent_id, name="to_cascade", sort_order=1, status="draft",
        )
        db.add(fn)
        await db.commit()
        await db.refresh(fn)
        fn_id = fn.id

        # 走 raw DELETE 触发 FK CASCADE（不通过 ORM session）
        from sqlalchemy import text
        await db.execute(text("DELETE FROM external_agents WHERE id = :aid"), {"aid": agent_id})
        await db.commit()

        gone = (await db.execute(
            select(ExternalAgentFunction).where(ExternalAgentFunction.id == fn_id)
        )).scalar_one_or_none()
        assert gone is None, "FK ondelete=CASCADE 应该把子行一并删掉"


# ── JSONB 列与 Manifest* Pydantic schema 的 round-trip 一致性 ──────────────


async def test_function_invoke_config_schema_conformance():
    """invoke_config 存 dict → 取出后能通过 ManifestInvokeConfig.model_validate()。"""
    _, agent_id = await _make_org_and_agent()
    cfg = ManifestInvokeConfig(
        endpoint="http://example.com/x",
        method="POST",
        timeout_seconds=30,
        pass_mode="multipart",
    ).model_dump()
    async with TestSessionLocal() as db:
        fn = ExternalAgentFunction(
            agent_id=agent_id, name="t1", sort_order=1,
            invoke_config=cfg, status="draft",
        )
        db.add(fn)
        await db.commit()
        await db.refresh(fn)

    async with TestSessionLocal() as db:
        reloaded = (await db.execute(
            select(ExternalAgentFunction).where(
                ExternalAgentFunction.agent_id == agent_id,
            )
        )).scalar_one()
        # JSONB round-trip 后再次校验 schema 不丢字段
        validated = ManifestInvokeConfig.model_validate(reloaded.invoke_config)
        assert validated.endpoint == "http://example.com/x"
        assert validated.method == "POST"


async def test_function_input_schema_schema_conformance():
    """input_schema 存 dict → 取出后能通过 ManifestInputSchema.model_validate()。"""
    _, agent_id = await _make_org_and_agent()
    schema = ManifestInputSchema.model_validate({
        "order": ["line"],
        "fields": {
            "line": {
                "type": "string", "ui": "select", "label": "产线",
                "required": True, "options": ["热压1线", "热压2线"],
            },
        },
    }).model_dump()
    async with TestSessionLocal() as db:
        fn = ExternalAgentFunction(
            agent_id=agent_id, name="t2", sort_order=1,
            input_schema=schema, status="draft",
        )
        db.add(fn)
        await db.commit()
        await db.refresh(fn)

    async with TestSessionLocal() as db:
        reloaded = (await db.execute(
            select(ExternalAgentFunction).where(
                ExternalAgentFunction.agent_id == agent_id,
            )
        )).scalar_one()
        validated = ManifestInputSchema.model_validate(reloaded.input_schema)
        assert validated.order == ["line"]
        assert validated.fields["line"].required is True
        assert validated.fields["line"].options == ["热压1线", "热压2线"]
