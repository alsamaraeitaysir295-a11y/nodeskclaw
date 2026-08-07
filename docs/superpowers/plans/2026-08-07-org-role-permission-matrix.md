# 组织三级角色跨模块权限矩阵 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把工作区、AI员工实例、外部Agent、知识库、组织成员管理、Gene 审核中心六个模块的鉴权，从现有的"admin/非-admin 二元判断 + 创建者豁免"统一收敛为纯 `OrgMembership.role`（member/operator/admin）三级驱动，前后端同步落实，防止绕过前端直接调用后端 API 越权。

**Architecture:** 后端复用已存在但目前仅有两处调用方（`llm_keys.py`、`org_settings.py`，见 commit `072990f`）的 `require_org_member_role(min_role)`（`app/core/deps.py:246`），按等级比较模型逐个模块接入；`workspace_member_service.check_workspace_access` 与 `instance_member_service.check_instance_access` 保持公开函数签名不变（各有约 10~40 个调用方），只重写内部实现，去掉 `InstanceMember`/创建者豁免逻辑。前端新增一个角色等级比较工具，把散落各处的 `portal_org_role === 'admin'` 字符串判断改为等级比较，并按模块调整按钮可见性与导航项。

**Tech Stack:** Python 3.12 + FastAPI + SQLAlchemy(asyncpg)，pytest + httpx.AsyncClient 做集成测试；Vue 3 + TypeScript + Pinia，vitest（本地环境无 node/npm，前端测试步骤会如实标注"未在本机执行，需用户在有 node 的环境验证"）。

## Global Constraints

- 角色等级：`member=10 < operator=20 < admin=30`（`ADMIN_ROLE_LEVEL`，`app/models/org_membership.py`），全程复用这一常量，不新建平行常量。
- `require_org_member_role(min_role)` 是唯一新增集成点，函数本身已存在且已有生产验证（`072990f`），本计划**不修改**该函数实现。
- `InstanceMember`/`WorkspaceMember` 数据表**不删除**，只是角色字段不再被鉴权逻辑读取；`InstanceMembers.vue` 管理入口**隐藏但不删代码**。
- 创建者豁免逻辑（`created_by == user.id` 放行）在 workspace/instance 两处**全部去掉**，因为创建门槛已提到 operator+，不会再出现"member 创建了东西却没权限管理"的场景。
- 技能市场（Gene 库其余功能，ownership 逻辑）、超管后台（`is_super_admin`/`AdminMembership`）、`llm_keys.py`/`org_settings.py`（`OrgSettingsGenes`/`OrgSettingsLlmKeys` 已有的 member/operator 读写权限，commit `072990f`）、实例文件访问（PodFS/DockerFS）、邀请流程（`add_member`，仍 admin-only、仍只能选 member/admin）**均不在本次改动范围**，不要顺手"顺便"改。
- 每个后端任务都要写集成测试（用 `tests/conftest.py` 的 `client`/`TestSessionLocal` fixture，覆盖 member/operator/admin 三档角色，"应放行"与"应拒绝"两种场景各至少一例）。
- 前端每个任务改完都要如实说明：本机无 `node`/`npm`，无法执行 `npm run dev`/`vue-tsc -b`/`npm run test` 验证，需要用户在有 node 的环境里跑一遍。
- 每完成一个独立任务立即 `git commit`，不攒批；只 `git add` 本次改动文件，禁止 `git add -A/.`。

---

## Task 1: 工作区权限三级化（`workspace_member_service.py`）

**Files:**
- Modify: `nodeskclaw-backend/app/services/workspace_member_service.py`
- Test: `nodeskclaw-backend/tests/test_workspace_permission_tiers.py`（新建）

**Interfaces:**
- Consumes：`OrgMembership`/`ADMIN_ROLE_LEVEL`（`app/models/org_membership.py`，已存在）。
- Produces：`check_workspace_access(workspace_id, user, required_permission, db)` 与 `get_my_permissions(workspace_id, user, db)` 签名和返回值 shape（`{"is_admin": bool, "is_org_admin": bool, "permissions": list[str]}`）**保持不变**，供 `app/api/workspaces.py`、`stores/workspace.ts` 等既有调用方直接复用，无需改动它们。

- [ ] **Step 1: 写失败测试——三档角色对 `send_chat`/`edit_blackboard`（operator 权限）/`manage_settings`（admin 权限）的访问结果**

```python
"""验证 workspace_member_service 的三级权限收敛（member/operator/admin）。"""
import uuid

import pytest
from httpx import AsyncClient

from app.core.exceptions import ForbiddenError
from app.models.org_membership import OrgMembership, OrgRole
from app.models.organization import Organization
from app.models.user import User
from app.models.workspace import Workspace
from app.services import workspace_member_service
from tests.conftest import TestSessionLocal


async def _make_org_user_workspace(role: str):
    suffix = uuid.uuid4().hex[:8]
    async with TestSessionLocal() as db:
        org = Organization(name=f"wstest-org-{suffix}", slug=f"wstest-org-{suffix}")
        db.add(org)
        await db.flush()
        user = User(
            email=f"wstest-{suffix}@example.com",
            name=f"wstest-{suffix}",
            password_hash="x",
            current_org_id=org.id,
        )
        db.add(user)
        await db.flush()
        db.add(OrgMembership(org_id=org.id, user_id=user.id, role=role))
        # 创建者故意设为另一个不存在的 id，验证"创建者豁免"已被去掉
        workspace = Workspace(org_id=org.id, name=f"ws-{suffix}", created_by=str(uuid.uuid4()))
        db.add(workspace)
        await db.commit()
        await db.refresh(user)
        await db.refresh(workspace)
        return user, workspace


@pytest.mark.asyncio
async def test_member_can_send_chat():
    user, workspace = await _make_org_user_workspace(OrgRole.member)
    async with TestSessionLocal() as db:
        result = await workspace_member_service.check_workspace_access(
            workspace.id, user, "send_chat", db
        )
        assert result is None  # None 表示放行


@pytest.mark.asyncio
async def test_member_cannot_edit_blackboard():
    user, workspace = await _make_org_user_workspace(OrgRole.member)
    async with TestSessionLocal() as db:
        with pytest.raises(ForbiddenError):
            await workspace_member_service.check_workspace_access(
                workspace.id, user, "edit_blackboard", db
            )


@pytest.mark.asyncio
async def test_operator_can_edit_blackboard_and_manage_agents():
    user, workspace = await _make_org_user_workspace(OrgRole.operator)
    async with TestSessionLocal() as db:
        assert await workspace_member_service.check_workspace_access(
            workspace.id, user, "edit_blackboard", db
        ) is None
        assert await workspace_member_service.check_workspace_access(
            workspace.id, user, "manage_agents", db
        ) is None


@pytest.mark.asyncio
async def test_operator_cannot_manage_settings():
    user, workspace = await _make_org_user_workspace(OrgRole.operator)
    async with TestSessionLocal() as db:
        with pytest.raises(ForbiddenError):
            await workspace_member_service.check_workspace_access(
                workspace.id, user, "manage_settings", db
            )


@pytest.mark.asyncio
async def test_admin_can_manage_settings_without_being_creator():
    user, workspace = await _make_org_user_workspace(OrgRole.admin)
    async with TestSessionLocal() as db:
        assert await workspace_member_service.check_workspace_access(
            workspace.id, user, "manage_settings", db
        ) is None


@pytest.mark.asyncio
async def test_get_my_permissions_operator_tier():
    user, workspace = await _make_org_user_workspace(OrgRole.operator)
    async with TestSessionLocal() as db:
        perms = await workspace_member_service.get_my_permissions(workspace.id, user, db)
        assert perms["is_admin"] is False
        assert perms["is_org_admin"] is False
        assert set(perms["permissions"]) == {"send_chat", "edit_blackboard", "manage_agents", "edit_topology"}
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd nodeskclaw-backend && uv run pytest tests/test_workspace_permission_tiers.py -v`
Expected: FAIL —— `test_operator_can_edit_blackboard_and_manage_agents`、`test_get_my_permissions_operator_tier` 会失败（现有实现里 operator 和 member 权限相同，`edit_blackboard` 属于 `WORKSPACE_USE_PERMISSIONS`，`manage_agents` 现在还不存在于任何权限集合判断分支里会走"未知权限兜底"）；`test_admin_can_manage_settings_without_being_creator` 应已通过（因为 `org_role == OrgRole.admin` 分支本来就存在）。

- [ ] **Step 3: 重写 `workspace_member_service.py`**

```python
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
from app.models.workspace_member import (
    WORKSPACE_PERMISSIONS,
    WorkspaceMember,
)

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
        raise ForbiddenError(f"需要 {min_role} 及以上角色", "errors.workspace.insufficient_permission")
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
```

注意：`check_workspace_access` 返回类型从 `WorkspaceMember | None` 改成了 `None`（原实现其实也从未在任何分支返回非 None 的 `WorkspaceMember`，纯粹是类型标注更新，不影响调用方——所有调用方都只是 `await check_workspace_access(...)` 不使用返回值，已用 Grep 确认）。

- [ ] **Step 4: 运行测试确认通过**

Run: `cd nodeskclaw-backend && uv run pytest tests/test_workspace_permission_tiers.py -v`
Expected: PASS（6 项全部通过）

- [ ] **Step 5: 回归测试——确认没有破坏依赖 `check_workspace_access`/`get_my_permissions` 的既有测试**

Run: `cd nodeskclaw-backend && uv run pytest -k "workspace" -v`
Expected: PASS（如有既有测试假设"创建者豁免"行为，需要按新的纯角色模型更新断言——若发现此类测试，直接修正断言而非跳过，因为豁免逻辑已被设计文档明确要求去掉）

- [ ] **Step 6: Commit**

```bash
git add nodeskclaw-backend/app/services/workspace_member_service.py nodeskclaw-backend/tests/test_workspace_permission_tiers.py
git commit -m "feat(backend): 工作区权限收敛为纯组织三级角色驱动"
```

---

## Task 2: 工作区创建门槛提到 operator+

**Files:**
- Modify: `nodeskclaw-backend/app/api/workspaces.py`
- Test: `nodeskclaw-backend/tests/test_workspace_permission_tiers.py`（追加）

**Interfaces:**
- Consumes：`require_org_member_role`（`app/core/deps.py:246`）。

- [ ] **Step 1: 读取 `workspaces.py` 当前 `create_workspace` 实现确认签名**

Run: `cd nodeskclaw-backend && grep -n "async def create_workspace" -A 15 app/api/workspaces.py`

- [ ] **Step 2: 写失败测试（走真实 HTTP 端点）**

```python
@pytest.mark.asyncio
async def test_member_cannot_create_workspace(client: AsyncClient):
    from tests.conftest import _override_user, _clear_override
    user, _ = await _make_org_user_workspace(OrgRole.member)
    _override_user(user)
    try:
        resp = await client.post("/api/workspaces", json={"name": "member-created-ws"})
        assert resp.status_code == 403
        assert resp.json()["detail"]["error_code"] == 40315
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_operator_can_create_workspace(client: AsyncClient):
    from tests.conftest import _override_user, _clear_override
    user, _ = await _make_org_user_workspace(OrgRole.operator)
    _override_user(user)
    try:
        resp = await client.post("/api/workspaces", json={"name": "operator-created-ws"})
        assert resp.status_code == 200
    finally:
        _clear_override()
```

（先用 `grep -n "workspaces" nodeskclaw-backend/app/main.py` 确认真实路由前缀，若不是 `/api/workspaces` 按实际前缀调整两处 URL）

- [ ] **Step 3: 运行测试确认失败**

Run: `cd nodeskclaw-backend && uv run pytest tests/test_workspace_permission_tiers.py::test_member_cannot_create_workspace -v`
Expected: FAIL（当前 member 也能创建成功，返回 200 而非 403）

- [ ] **Step 4: 修改 `create_workspace` 依赖**

把 `create_workspace`（约第102行）的 `org_ctx: tuple = Depends(get_current_org)` 参数改为：

```python
org_ctx: tuple = Depends(require_org_member_role("operator")),
```

同时在文件顶部 import 区加入 `require_org_member_role`（保留原有 `get_current_org` 若文件内其他端点仍在用）。

- [ ] **Step 5: 运行测试确认通过**

Run: `cd nodeskclaw-backend && uv run pytest tests/test_workspace_permission_tiers.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add nodeskclaw-backend/app/api/workspaces.py nodeskclaw-backend/tests/test_workspace_permission_tiers.py
git commit -m "feat(backend): 创建工作区门槛提升到 operator 及以上"
```

---

## Task 3: AI员工实例权限三级化（`instance_member_service.py`）

**Files:**
- Modify: `nodeskclaw-backend/app/services/instance_member_service.py`
- Modify: `nodeskclaw-backend/app/api/portal/instances.py`（`list_instances`/`_is_org_admin` 内联逻辑改用统一的角色映射）
- Test: `nodeskclaw-backend/tests/test_instance_permission_tiers.py`（新建）

**Interfaces:**
- Consumes：`ADMIN_ROLE_LEVEL`/`OrgRole`。
- Produces：`check_instance_access(instance_id, user, min_role: InstanceRole, db)` 签名不变（约 40 个调用方，全部传固定 `InstanceRole` 字面量，不受影响）；`get_user_instance_role(instance_id, user, db) -> str | None` 现在会返回三档（`viewer`/`editor`/`admin`）而不再只有 `viewer`/`admin` 两档，供 `InstanceDetail.vue` 等前端已实现的 `ROLE_LEVEL >= editor` 判断真正生效；新增 `map_org_role_to_instance_role(org_role: str | None) -> str | None`，供 `instance_member_service.get_user_instance_role` 和 `app/api/portal/instances.py: list_instances` 共用同一份映射，避免两处重复实现。

- [ ] **Step 1: 写失败测试**

```python
"""验证 instance_member_service 的三级权限收敛（member/operator/admin）。"""
import uuid

import pytest

from app.core.exceptions import ForbiddenError
from app.models.instance import Instance
from app.models.instance_member import InstanceRole
from app.models.org_membership import OrgMembership, OrgRole
from app.models.organization import Organization
from app.models.user import User
from app.services import instance_member_service
from tests.conftest import TestSessionLocal


async def _make_org_user_instance(role: str):
    suffix = uuid.uuid4().hex[:8]
    async with TestSessionLocal() as db:
        org = Organization(name=f"insttest-org-{suffix}", slug=f"insttest-org-{suffix}")
        db.add(org)
        await db.flush()
        user = User(
            email=f"insttest-{suffix}@example.com",
            name=f"insttest-{suffix}",
            password_hash="x",
            current_org_id=org.id,
        )
        db.add(user)
        await db.flush()
        db.add(OrgMembership(org_id=org.id, user_id=user.id, role=role))
        # 创建者故意设为另一个不存在的 id，验证"创建者豁免"已被去掉
        instance = Instance(
            org_id=org.id, name=f"inst-{suffix}", created_by=str(uuid.uuid4()),
            runtime="openclaw", status="ready",
        )
        db.add(instance)
        await db.commit()
        await db.refresh(user)
        await db.refresh(instance)
        return user, instance


@pytest.mark.asyncio
async def test_member_can_view_but_not_edit():
    user, instance = await _make_org_user_instance(OrgRole.member)
    async with TestSessionLocal() as db:
        assert await instance_member_service.check_instance_access(
            instance.id, user, InstanceRole.viewer, db
        ) is None
        with pytest.raises(ForbiddenError):
            await instance_member_service.check_instance_access(
                instance.id, user, InstanceRole.editor, db
            )


@pytest.mark.asyncio
async def test_operator_can_edit_without_being_creator():
    user, instance = await _make_org_user_instance(OrgRole.operator)
    async with TestSessionLocal() as db:
        assert await instance_member_service.check_instance_access(
            instance.id, user, InstanceRole.editor, db
        ) is None
        with pytest.raises(ForbiddenError):
            await instance_member_service.check_instance_access(
                instance.id, user, InstanceRole.admin, db
            )


@pytest.mark.asyncio
async def test_admin_passes_all_levels():
    user, instance = await _make_org_user_instance(OrgRole.admin)
    async with TestSessionLocal() as db:
        assert await instance_member_service.check_instance_access(
            instance.id, user, InstanceRole.admin, db
        ) is None


@pytest.mark.asyncio
async def test_get_user_instance_role_returns_editor_for_operator():
    user, instance = await _make_org_user_instance(OrgRole.operator)
    async with TestSessionLocal() as db:
        role = await instance_member_service.get_user_instance_role(instance.id, user, db)
        assert role == InstanceRole.editor


@pytest.mark.asyncio
async def test_get_user_instance_role_returns_viewer_for_member():
    user, instance = await _make_org_user_instance(OrgRole.member)
    async with TestSessionLocal() as db:
        role = await instance_member_service.get_user_instance_role(instance.id, user, db)
        assert role == InstanceRole.viewer
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd nodeskclaw-backend && uv run pytest tests/test_instance_permission_tiers.py -v`
Expected: FAIL —— `test_operator_can_edit_without_being_creator`、`test_get_user_instance_role_returns_editor_for_operator` 失败（现有实现 operator 不满足 `org_role == OrgRole.admin` 也不是创建者，会被拒绝；`get_user_instance_role` 现在对非 admin/创建者只返回 `viewer`）。

- [ ] **Step 3: 重写 `instance_member_service.py` 的 `check_instance_access`/`get_user_instance_role`，新增 `map_org_role_to_instance_role`**

```python
"""Instance member service: permission checks, list filtering, member CRUD."""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from app.models.admin_membership import AdminMembership
from app.models.base import not_deleted
from app.models.instance import Instance
from app.models.instance_member import INSTANCE_ROLE_LEVEL, InstanceMember, InstanceRole
from app.models.org_membership import ADMIN_ROLE_LEVEL, OrgMembership, OrgRole
from app.models.user import User

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


def map_org_role_to_instance_role(org_role: str | None) -> str | None:
    """组织角色 -> 实例角色档位映射：member->viewer, operator->editor, admin->admin。

    非组织成员（org_role is None）返回 None。
    """
    if org_role == OrgRole.admin:
        return InstanceRole.admin
    if org_role == OrgRole.operator:
        return InstanceRole.editor
    if org_role is not None:
        return InstanceRole.viewer
    return None


async def check_instance_access(
    instance_id: str,
    user: User,
    min_role: InstanceRole,
    db: AsyncSession,
) -> None:
    """Check that *user* has at least *min_role* on the instance.

    纯组织三级角色驱动：member->viewer、operator->editor、admin->admin，
    不再看创建者身份，也不再查 InstanceMember 角色字段。
    返回 None 表示通过，抛出 ForbiddenError / NotFoundError 表示拒绝。
    """
    instance = (await db.execute(
        select(Instance).where(Instance.id == instance_id, not_deleted(Instance))
    )).scalar_one_or_none()
    if not instance:
        raise NotFoundError("实例不存在", "errors.instance.not_found")

    if not instance.org_id:
        raise ForbiddenError("您没有该实例的访问权限", "errors.instance.no_access")

    org_role = await _get_org_role(user.id, instance.org_id, db)
    if org_role is None:
        raise ForbiddenError("您没有该实例的访问权限", "errors.instance.no_access")

    effective_role = map_org_role_to_instance_role(org_role)
    if INSTANCE_ROLE_LEVEL[effective_role] < INSTANCE_ROLE_LEVEL[min_role]:
        raise ForbiddenError("权限不足", "errors.instance.insufficient_permission")
    return None


async def get_user_instance_role(
    instance_id: str, user: User, db: AsyncSession
) -> str | None:
    """Return the effective role string for user on instance, or None.

    组织 member -> viewer；组织 operator -> editor；组织 admin -> admin；非成员 -> None。
    """
    instance = (await db.execute(
        select(Instance).where(Instance.id == instance_id, not_deleted(Instance))
    )).scalar_one_or_none()
    if not instance or not instance.org_id:
        return None

    org_role = await _get_org_role(user.id, instance.org_id, db)
    return map_org_role_to_instance_role(org_role)
```

（`apply_accessible_filter` 及其余函数保持不变，不重复粘贴——只替换从 `_get_org_role` 定义结束到 `get_user_instance_role` 结束的这段。）

- [ ] **Step 4: 修正 `errors.instance.creator_required` 遗留使用**

Run: `cd nodeskclaw-backend && grep -rn "creator_required" app/ nodeskclaw-portal/src/i18n/` 确认该 message_key 是否被前端专门捕获显示特定文案；若只是通用兜底提示（大概率如此），无需新增 `insufficient_permission` 的 i18n key 也能正常显示后端通用 `message` 字段——但为了准确性仍在 `nodeskclaw-portal/src/i18n/locales/zh-CN.ts`/`en-US.ts` 的 `errors.instance` 块补一条：

```typescript
// zh-CN.ts, errors.instance 块内追加
insufficient_permission: "权限不足",
```

```typescript
// en-US.ts, errors.instance 块内追加
insufficient_permission: "Insufficient permission",
```

- [ ] **Step 5: 修改 `app/api/portal/instances.py: list_instances`，去掉 `InstanceMember` outerjoin 和 `_is_org_admin` 内联逻辑，改用统一映射**

把第48-113行区间替换为：

```python
@router.get("", response_model=ApiResponse[list[InstanceInfo]])
async def list_instances(
    cluster_id: str | None = Query(None),
    org_id: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    effective_org_id = current_user.current_org_id

    query = select(Instance).where(not_deleted(Instance)).order_by(Instance.created_at.desc())
    if cluster_id:
        query = query.where(Instance.cluster_id == cluster_id)
    if effective_org_id:
        query = query.where(Instance.org_id == effective_org_id)

    query = instance_member_service.apply_accessible_filter(
        query, current_user.id, effective_org_id, db
    )

    result = await db.execute(query)

    from app.services.tunnel import tunnel_adapter
    connected = tunnel_adapter.connected_instances
    health_corrected = False

    items = []
    for (inst,) in result.all():
        if inst.status == "running" and inst.health_status != "healthy" and inst.id in connected:
            inst.health_status = "healthy"
            health_corrected = True
        info = InstanceInfo.model_validate(inst)
        org_role = await instance_member_service._get_org_role(current_user.id, inst.org_id, db) if inst.org_id else None
        info.my_role = instance_member_service.map_org_role_to_instance_role(org_role)
        items.append(info)

    if health_corrected:
        try:
            await db.commit()
        except Exception:
            logger.debug("列表 tunnel 健康修正持久化失败（非致命）")

    return ApiResponse(data=items)
```

删除原来的 `_is_org_admin` 辅助函数（不再有调用方）；`InstanceMember` import 若本文件其余端点已不再使用，一并删除，用 `grep -n "InstanceMember" app/api/portal/instances.py` 确认后处理。

- [ ] **Step 6: 运行测试确认通过**

Run: `cd nodeskclaw-backend && uv run pytest tests/test_instance_permission_tiers.py -v`
Expected: PASS（5 项全部通过）

- [ ] **Step 7: 回归测试**

Run: `cd nodeskclaw-backend && uv run pytest -k "instance" -v`
Expected: PASS（如有测试假设"创建者豁免"或依赖 `InstanceMember.role`，按新纯角色模型更新断言）

- [ ] **Step 8: Commit**

```bash
git add nodeskclaw-backend/app/services/instance_member_service.py nodeskclaw-backend/app/api/portal/instances.py nodeskclaw-backend/tests/test_instance_permission_tiers.py nodeskclaw-portal/src/i18n/locales/zh-CN.ts nodeskclaw-portal/src/i18n/locales/en-US.ts
git commit -m "feat(backend): 实例权限收敛为纯组织三级角色驱动，operator 获得编辑级权限"
```

---

## Task 4: 实例创建门槛提到 operator+，自动化任务补跨组织越权校验

**Files:**
- Modify: `nodeskclaw-backend/app/api/deploy.py`
- Modify: `nodeskclaw-backend/app/api/portal/automation_tasks.py`
- Test: `nodeskclaw-backend/tests/test_instance_permission_tiers.py`（追加）
- Test: `nodeskclaw-backend/tests/test_automation_task_org_check.py`（新建）

**Interfaces:**
- Consumes：`require_org_member_role`（Task 2 已引入同一工厂函数，这里是第二处使用）。

- [ ] **Step 1: 写失败测试——deploy 端点门槛**

```python
@pytest.mark.asyncio
async def test_member_cannot_deploy_instance(client: AsyncClient):
    from tests.conftest import _override_user, _clear_override
    user, _ = await _make_org_user_instance(OrgRole.member)
    _override_user(user)
    try:
        resp = await client.post("/api/deploy", json={"name": "member-deploy", "template_slug": "openclaw-basic"})
        assert resp.status_code == 403
        assert resp.json()["detail"]["error_code"] == 40315
    finally:
        _clear_override()
```

（先 `grep -n "deploy" nodeskclaw-backend/app/main.py` 确认真实前缀与最小可行请求体字段，按实际 `DeployRequest` schema 必填字段调整 json body）

- [ ] **Step 2: 运行测试确认失败**

Run: `cd nodeskclaw-backend && uv run pytest tests/test_instance_permission_tiers.py::test_member_cannot_deploy_instance -v`
Expected: FAIL（目前 member 也能触发部署流程）

- [ ] **Step 3: 修改 `deploy.py: deploy` 加门槛**

在 `deploy()` 函数参数列表新增一个不使用返回值的依赖：

```python
async def deploy(
    body: DeployRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _role_check: tuple = Depends(require_org_member_role("operator")),
):
```

并在文件顶部 import 补上 `require_org_member_role`：

```python
from app.core.deps import get_db, require_org_member_role
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd nodeskclaw-backend && uv run pytest tests/test_instance_permission_tiers.py -v`
Expected: PASS

- [ ] **Step 5: 写失败测试——自动化任务跨组织越权**

```python
"""验证 create_automation_task 补充的组织归属校验（堵跨组织越权漏洞，非角色提权）。"""
import uuid

import pytest
from httpx import AsyncClient

from app.models.instance import Instance
from app.models.org_membership import OrgMembership, OrgRole
from app.models.organization import Organization
from app.models.user import User
from tests.conftest import TestSessionLocal, _override_user, _clear_override


@pytest.mark.asyncio
async def test_cannot_create_automation_task_for_other_org_instance(client: AsyncClient):
    suffix = uuid.uuid4().hex[:8]
    async with TestSessionLocal() as db:
        org_a = Organization(name=f"org-a-{suffix}", slug=f"org-a-{suffix}")
        org_b = Organization(name=f"org-b-{suffix}", slug=f"org-b-{suffix}")
        db.add_all([org_a, org_b])
        await db.flush()
        user_a = User(
            email=f"user-a-{suffix}@example.com", name="user-a",
            password_hash="x", current_org_id=org_a.id,
        )
        db.add(user_a)
        await db.flush()
        db.add(OrgMembership(org_id=org_a.id, user_id=user_a.id, role=OrgRole.member))
        # 实例属于 org_b，与发起用户所在的 org_a 不同
        instance_b = Instance(
            org_id=org_b.id, name=f"inst-b-{suffix}", created_by=str(uuid.uuid4()),
            runtime="openclaw", status="ready",
        )
        db.add(instance_b)
        await db.commit()
        await db.refresh(user_a)
        await db.refresh(instance_b)

    _override_user(user_a)
    try:
        resp = await client.post(
            "/api/automation-tasks",
            json={"instance_id": instance_b.id, "name": "跨组织任务", "prompt": "test"},
        )
        assert resp.status_code == 403
    finally:
        _clear_override()
```

（先 `grep -n "automation" nodeskclaw-backend/app/main.py` 确认真实路由前缀）

- [ ] **Step 6: 运行测试确认失败**

Run: `cd nodeskclaw-backend && uv run pytest tests/test_automation_task_org_check.py -v`
Expected: FAIL（当前端点只检查实例是否存在，不检查组织归属，会返回 200）

- [ ] **Step 7: 补组织归属校验（不提升角色门槛，任何组织成员仍可用）**

修改 `app/api/portal/automation_tasks.py: create_automation_task`：

```python
async def create_automation_task(
    body: AutomationTaskCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """新建自动化任务。"""
    inst_result = await db.execute(
        select(Instance).where(Instance.id == body.instance_id, not_deleted(Instance))
    )
    instance = inst_result.scalar_one_or_none()
    if not instance:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="AI 员工不存在")

    # 补充组织归属校验：堵住"实例存在即可用"的跨组织越权漏洞
    if instance.org_id != current_user.current_org_id:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=403,
            detail={
                "error_code": 40312,
                "message_key": "errors.org.org_member_required",
                "message": "您不是该实例所属组织的成员",
            },
        )

    task = AutomationTask(
        user_id=current_user.id,
        instance_id=body.instance_id,
        name=body.name.strip(),
        prompt=body.prompt.strip(),
        frequency=body.frequency,
        exec_time=body.exec_time,
        interval_minutes=body.interval_minutes,
        week_days=json.dumps(body.week_days) if body.week_days is not None else None,
        start_date=body.start_date,
        end_date=body.end_date,
        push_notification=body.push_notification,
        status="active",
    )
    db.add(task)
    await db.commit()
```

- [ ] **Step 8: 运行测试确认通过**

Run: `cd nodeskclaw-backend && uv run pytest tests/test_automation_task_org_check.py -v`
Expected: PASS

- [ ] **Step 9: Commit**

```bash
git add nodeskclaw-backend/app/api/deploy.py nodeskclaw-backend/app/api/portal/automation_tasks.py nodeskclaw-backend/tests/test_instance_permission_tiers.py nodeskclaw-backend/tests/test_automation_task_org_check.py
git commit -m "feat(backend): 创建实例门槛提升到 operator，修复自动化任务跨组织越权漏洞"
```

---

## Task 5: 外部Agent权限三级化

**Files:**
- Modify: `nodeskclaw-backend/app/api/external_agents.py`
- Test: `nodeskclaw-backend/tests/test_external_agent_permission_tiers.py`（新建）

- [ ] **Step 1: 写失败测试**

```python
"""验证外部 Agent CRUD 端点的三级权限门槛。"""
import uuid

import pytest
from httpx import AsyncClient

from app.models.org_membership import OrgMembership, OrgRole
from app.models.organization import Organization
from app.models.user import User
from tests.conftest import TestSessionLocal, _override_user, _clear_override


async def _make_org_user(role: str):
    suffix = uuid.uuid4().hex[:8]
    async with TestSessionLocal() as db:
        org = Organization(name=f"agenttest-org-{suffix}", slug=f"agenttest-org-{suffix}")
        db.add(org)
        await db.flush()
        user = User(
            email=f"agenttest-{suffix}@example.com", name=f"agenttest-{suffix}",
            password_hash="x", current_org_id=org.id,
        )
        db.add(user)
        await db.flush()
        db.add(OrgMembership(org_id=org.id, user_id=user.id, role=role))
        await db.commit()
        await db.refresh(user)
        return user


CREATE_BODY = {
    "name": "test-agent", "endpoint": "https://example.com", "protocol": "openai_compatible",
}


@pytest.mark.asyncio
async def test_member_cannot_create_agent(client: AsyncClient):
    user = await _make_org_user(OrgRole.member)
    _override_user(user)
    try:
        resp = await client.post("/api/external-agents", json=CREATE_BODY)
        assert resp.status_code == 403
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_operator_can_create_agent(client: AsyncClient):
    user = await _make_org_user(OrgRole.operator)
    _override_user(user)
    try:
        resp = await client.post("/api/external-agents", json=CREATE_BODY)
        assert resp.status_code == 200
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_operator_cannot_delete_agent(client: AsyncClient):
    creator = await _make_org_user(OrgRole.admin)
    _override_user(creator)
    try:
        create_resp = await client.post("/api/external-agents", json=CREATE_BODY)
        agent_id = create_resp.json()["data"]["id"]
    finally:
        _clear_override()

    operator = await _make_org_user(OrgRole.operator)
    _override_user(operator)
    try:
        resp = await client.delete(f"/api/external-agents/{agent_id}")
        assert resp.status_code == 403
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_member_can_list_agents(client: AsyncClient):
    user = await _make_org_user(OrgRole.member)
    _override_user(user)
    try:
        resp = await client.get("/api/external-agents")
        assert resp.status_code == 200
    finally:
        _clear_override()
```

（先 `grep -n "external_agents" nodeskclaw-backend/app/main.py` 确认真实路由前缀；`test_operator_cannot_delete_agent` 里 creator 用 admin 身份创建是为了避免依赖 create 门槛尚未验证通过就失败，属于测试隔离的合理选择，不是在验证 admin 行为）

- [ ] **Step 2: 运行测试确认失败**

Run: `cd nodeskclaw-backend && uv run pytest tests/test_external_agent_permission_tiers.py -v`
Expected: FAIL（`test_operator_can_create_agent` 失败，因为当前 create 要求 admin）

- [ ] **Step 3: 修改 `external_agents.py` 决策依赖**

```python
from app.core.deps import async_session_factory, get_current_org, get_db, require_org_admin, require_org_member_role
```

`create_agent`（第69-73行）、`update_agent`（第103-107行）、`sync_agent`（第135-138行）的 `auth=Depends(require_org_admin)` 全部改为 `auth=Depends(require_org_member_role("operator"))`；`delete_agent`（第119-122行）保持 `require_org_admin` 不变；`list_agents`（第92-94行）保持 `get_current_org` 不变。

- [ ] **Step 4: 运行测试确认通过**

Run: `cd nodeskclaw-backend && uv run pytest tests/test_external_agent_permission_tiers.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add nodeskclaw-backend/app/api/external_agents.py nodeskclaw-backend/tests/test_external_agent_permission_tiers.py
git commit -m "feat(backend): 外部Agent创建/编辑/同步门槛降为 operator，删除保持 admin"
```

---

## Task 6: 知识库权限三级化

**Files:**
- Modify: `nodeskclaw-backend/app/api/knowledge_bases.py`
- Test: `nodeskclaw-backend/tests/test_knowledge_base_permission_tiers.py`（新建）

- [ ] **Step 1: 写失败测试**

```python
"""验证知识库端点的三级权限门槛（放宽 list 为 member 可读）。"""
import uuid

import pytest
from httpx import AsyncClient

from app.models.org_membership import OrgMembership, OrgRole
from app.models.organization import Organization
from app.models.user import User
from tests.conftest import TestSessionLocal, _override_user, _clear_override


async def _make_org_user(role: str):
    suffix = uuid.uuid4().hex[:8]
    async with TestSessionLocal() as db:
        org = Organization(name=f"kbtest-org-{suffix}", slug=f"kbtest-org-{suffix}")
        db.add(org)
        await db.flush()
        user = User(
            email=f"kbtest-{suffix}@example.com", name=f"kbtest-{suffix}",
            password_hash="x", current_org_id=org.id,
        )
        db.add(user)
        await db.flush()
        db.add(OrgMembership(org_id=org.id, user_id=user.id, role=role))
        await db.commit()
        await db.refresh(user)
        return user


@pytest.mark.asyncio
async def test_member_can_list_kbs(client: AsyncClient):
    user = await _make_org_user(OrgRole.member)
    _override_user(user)
    try:
        resp = await client.get("/api/knowledge-bases")
        assert resp.status_code == 200
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_member_cannot_create_kb(client: AsyncClient):
    user = await _make_org_user(OrgRole.member)
    _override_user(user)
    try:
        resp = await client.post("/api/knowledge-bases", json={
            "name": "kb1", "ragflow_endpoint": "https://example.com", "ragflow_kb_id": "x", "api_key": "x",
        })
        assert resp.status_code == 403
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_operator_can_create_kb(client: AsyncClient):
    user = await _make_org_user(OrgRole.operator)
    _override_user(user)
    try:
        resp = await client.post("/api/knowledge-bases", json={
            "name": "kb2", "ragflow_endpoint": "https://example.com", "ragflow_kb_id": "x", "api_key": "x",
        })
        assert resp.status_code == 200
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_operator_cannot_delete_kb(client: AsyncClient):
    admin_user = await _make_org_user(OrgRole.admin)
    _override_user(admin_user)
    try:
        create_resp = await client.post("/api/knowledge-bases", json={
            "name": "kb3", "ragflow_endpoint": "https://example.com", "ragflow_kb_id": "x", "api_key": "x",
        })
        kb_id = create_resp.json()["data"]["id"]
    finally:
        _clear_override()

    operator = await _make_org_user(OrgRole.operator)
    _override_user(operator)
    try:
        resp = await client.delete(f"/api/knowledge-bases/{kb_id}")
        assert resp.status_code == 403
    finally:
        _clear_override()
```

（先 `grep -n "knowledge" nodeskclaw-backend/app/main.py` 确认真实路由前缀）

- [ ] **Step 2: 运行测试确认失败**

Run: `cd nodeskclaw-backend && uv run pytest tests/test_knowledge_base_permission_tiers.py -v`
Expected: FAIL（`test_member_can_list_kbs`、`test_operator_can_create_kb` 失败——当前全部端点都要求 admin）

- [ ] **Step 3: 修改 `knowledge_bases.py` 决策依赖**

```python
from app.core.deps import get_db, require_org_admin, require_org_member_role
```

`list_kbs`（第35-38行）、`list_kb_documents`（第90-96行）的 `auth=Depends(require_org_admin)` 改为 `auth=Depends(require_org_member_role("member"))`；`create_kb`（第16-20行）、`update_kb`（第45-50行）、`sync_kb`（第71-75行）改为 `auth=Depends(require_org_member_role("operator"))`；`delete_kb`（第60-64行）保持 `require_org_admin` 不变。

- [ ] **Step 4: 运行测试确认通过**

Run: `cd nodeskclaw-backend && uv run pytest tests/test_knowledge_base_permission_tiers.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add nodeskclaw-backend/app/api/knowledge_bases.py nodeskclaw-backend/tests/test_knowledge_base_permission_tiers.py
git commit -m "feat(backend): 知识库列表放宽为 member 可读，创建/编辑/同步降为 operator，删除保持 admin"
```

---

## Task 7: 组织成员管理门槛降为 operator + 晋升 admin 能力上限校验

**Files:**
- Modify: `nodeskclaw-backend/app/api/organizations.py`
- Modify: `nodeskclaw-backend/app/services/org_service.py`
- Test: `nodeskclaw-backend/tests/test_org_member_management_tiers.py`（新建）

**Interfaces:**
- Produces：`org_service.update_member_role(org_id, membership_id, role, db, *, actor: User)` —— 新增关键字参数 `actor`，调用方（`organizations.py`）必须传入当前请求发起者。

- [ ] **Step 1: 写失败测试**

```python
"""验证组织成员管理三端点门槛降为 operator，以及 operator 不能把人设为 admin 的能力上限。"""
import uuid

import pytest
from httpx import AsyncClient

from app.models.org_membership import OrgMembership, OrgRole
from app.models.organization import Organization
from app.models.user import User
from tests.conftest import TestSessionLocal, _override_user, _clear_override


async def _make_org_with_two_members(actor_role: str, target_role: str = OrgRole.member):
    suffix = uuid.uuid4().hex[:8]
    async with TestSessionLocal() as db:
        org = Organization(name=f"memtest-org-{suffix}", slug=f"memtest-org-{suffix}")
        db.add(org)
        await db.flush()
        actor = User(
            email=f"actor-{suffix}@example.com", name="actor",
            password_hash="x", current_org_id=org.id,
        )
        target = User(
            email=f"target-{suffix}@example.com", name="target",
            password_hash="x", current_org_id=org.id,
        )
        db.add_all([actor, target])
        await db.flush()
        db.add(OrgMembership(org_id=org.id, user_id=actor.id, role=actor_role))
        target_membership = OrgMembership(org_id=org.id, user_id=target.id, role=target_role)
        db.add(target_membership)
        await db.commit()
        await db.refresh(actor)
        await db.refresh(target)
        await db.refresh(target_membership)
        return org, actor, target, target_membership


@pytest.mark.asyncio
async def test_operator_can_promote_member_to_operator(client: AsyncClient):
    org, actor, target, membership = await _make_org_with_two_members(OrgRole.operator)
    _override_user(actor)
    try:
        resp = await client.put(
            f"/api/orgs/{org.id}/members/{membership.id}", json={"role": "operator"}
        )
        assert resp.status_code == 200
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_operator_cannot_promote_to_admin(client: AsyncClient):
    org, actor, target, membership = await _make_org_with_two_members(OrgRole.operator)
    _override_user(actor)
    try:
        resp = await client.put(
            f"/api/orgs/{org.id}/members/{membership.id}", json={"role": "admin"}
        )
        assert resp.status_code == 403
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_admin_can_promote_to_admin(client: AsyncClient):
    org, actor, target, membership = await _make_org_with_two_members(OrgRole.admin)
    _override_user(actor)
    try:
        resp = await client.put(
            f"/api/orgs/{org.id}/members/{membership.id}", json={"role": "admin"}
        )
        assert resp.status_code == 200
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_member_cannot_update_role(client: AsyncClient):
    org, actor, target, membership = await _make_org_with_two_members(OrgRole.member)
    _override_user(actor)
    try:
        resp = await client.put(
            f"/api/orgs/{org.id}/members/{membership.id}", json={"role": "operator"}
        )
        assert resp.status_code == 403
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_operator_can_remove_member(client: AsyncClient):
    org, actor, target, membership = await _make_org_with_two_members(OrgRole.operator)
    _override_user(actor)
    try:
        resp = await client.delete(f"/api/orgs/{org.id}/members/{membership.id}")
        assert resp.status_code == 200
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_operator_can_reset_member_password(client: AsyncClient):
    org, actor, target, membership = await _make_org_with_two_members(OrgRole.operator)
    _override_user(actor)
    try:
        resp = await client.post(f"/api/orgs/{org.id}/members/{target.id}/reset-password")
        assert resp.status_code == 200
    finally:
        _clear_override()
```

（先 `grep -n "organizations" nodeskclaw-backend/app/main.py` 确认真实路由前缀）

- [ ] **Step 2: 运行测试确认失败**

Run: `cd nodeskclaw-backend && uv run pytest tests/test_org_member_management_tiers.py -v`
Expected: FAIL —— `test_operator_can_promote_member_to_operator`/`test_operator_cannot_promote_to_admin`/`test_operator_can_remove_member`/`test_operator_can_reset_member_password` 均失败（当前三端点都要求 admin，operator 一律 403；且当前没有"不能晋升 admin"的能力上限校验）

- [ ] **Step 3: `organizations.py` 三端点门槛降级**

import 区（第10-17行）新增 `require_org_member_role`：

```python
from app.core.deps import (
    get_current_org,
    get_db,
    require_feature,
    require_org_admin,
    require_org_member,
    require_org_member_role,
    require_super_admin_dep,
)
```

`update_member_role`（第249-260行）：

```python
@router.put("/{org_id}/members/{membership_id}", response_model=ApiResponse[MemberInfo])
async def update_member_role(
    org_id: str,
    membership_id: str,
    body: UpdateMemberRoleRequest,
    db: AsyncSession = Depends(get_db),
    _org_ctx: tuple = Depends(require_org_member_role("operator")),
):
    """修改成员角色（组织操作者+；operator 不能把任何人设为 admin，见 org_service.update_member_role）。"""
    data = await org_service.update_member_role(org_id, membership_id, body.role, db, actor=_org_ctx[0])
    await hooks.emit("operation_audit", action="org.member_role_updated", target_type="org_membership", target_id=membership_id, actor_id=_org_ctx[0].id, org_id=org_id)
    return ApiResponse(data=data)
```

`remove_member`（第263-285行）与 `reset_member_password`（第288-341行）的 `_org_ctx: tuple = Depends(require_org_admin)` 均改为 `Depends(require_org_member_role("operator"))`，函数体其余逻辑不变（`reset_member_password` 内部已有的"不能重置 admin 密码"守卫天然限制了 operator 的能力上限，无需额外改动）。

- [ ] **Step 4: `org_service.update_member_role` 新增能力上限校验**

```python
async def update_member_role(
    org_id: str, membership_id: str, role: str, db: AsyncSession, *, actor: User,
) -> MemberInfo:
    """修改成员角色。membership_id 可以是 OrgMembership.id 或 user_id，两者均可匹配。

    能力上限：非超管且自身角色不是 admin 的 actor（即 operator），不能把任何人
    （包括自己）的角色设为 admin。
    """
    if role == OrgRole.admin and not getattr(actor, "is_super_admin", False):
        actor_membership = (await db.execute(
            select(OrgMembership).where(
                OrgMembership.user_id == actor.id,
                OrgMembership.org_id == org_id,
                not_deleted(OrgMembership),
            )
        )).scalar_one_or_none()
        if actor_membership is None or actor_membership.role != OrgRole.admin:
            raise ForbiddenError("操作者无权将成员设为管理员", "errors.org.cannot_promote_to_admin")

    result = await db.execute(
        select(OrgMembership, User)
        .join(User, OrgMembership.user_id == User.id)
        .where(
            or_(OrgMembership.id == membership_id, OrgMembership.user_id == membership_id),
            OrgMembership.org_id == org_id,
            not_deleted(OrgMembership),
            not_deleted(User),
        )
    )
    row = result.first()
    if row is None:
        raise NotFoundError("成员记录不存在")
    membership, user = row
    membership.role = role
    await db.commit()
    return MemberInfo(id=membership.id, user_id=membership.user_id, org_id=membership.org_id, role=membership.role, is_super_admin=user.is_super_admin, user_name=user.name, user_email=user.email, user_avatar_url=user.avatar_url, created_at=membership.created_at)
```

（`ForbiddenError`/`OrgRole`/`User` 均已在文件顶部 import，无需新增；`errors.org.cannot_promote_to_admin` 需要在 `zh-CN.ts`/`en-US.ts` 补充，见 Step 5）

- [ ] **Step 5: 补充 i18n message_key**

`nodeskclaw-backend` 端错误走 `ForbiddenError(message, message_key)` 通用响应结构（`message` 字段兜底），前端如无对应 key 会回退显示 `message`——但为准确起见仍补上：

```typescript
// nodeskclaw-portal/src/i18n/locales/zh-CN.ts，errors.org 块内追加
cannot_promote_to_admin: "操作者无权将成员设为管理员",
```

```typescript
// nodeskclaw-portal/src/i18n/locales/en-US.ts，errors.org 块内追加
cannot_promote_to_admin: "Operators cannot promote members to admin",
```

- [ ] **Step 6: 运行测试确认通过**

Run: `cd nodeskclaw-backend && uv run pytest tests/test_org_member_management_tiers.py -v`
Expected: PASS（7 项全部通过）

- [ ] **Step 7: 回归测试**

Run: `cd nodeskclaw-backend && uv run pytest -k "org_member or org_role" -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add nodeskclaw-backend/app/api/organizations.py nodeskclaw-backend/app/services/org_service.py nodeskclaw-backend/tests/test_org_member_management_tiers.py nodeskclaw-portal/src/i18n/locales/zh-CN.ts nodeskclaw-portal/src/i18n/locales/en-US.ts
git commit -m "feat(backend): 成员角色/移除/重置密码门槛降为 operator，operator 不能晋升任何人为 admin"
```

---

## Task 8: Gene 审核中心放宽为 operator 可见可审核

**Files:**
- Modify: `nodeskclaw-backend/app/services/gene_service.py`
- Test: `nodeskclaw-backend/tests/test_gene_review_operator_access.py`（新建）

- [ ] **Step 1: 写失败测试**

```python
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
        gene = Gene(
            name=f"gene-{suffix}", slug=f"gene-{suffix}", org_id=org.id,
            visibility="org", review_status=GeneReviewStatus.pending_owner,
            created_by=str(uuid.uuid4()),
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd nodeskclaw-backend && uv run pytest tests/test_gene_review_operator_access.py -v`
Expected: FAIL —— `test_operator_can_review_gene`、`test_operator_sees_pending_review_in_own_org` 失败（当前只认 `OrgRole.admin`）

- [ ] **Step 3: 修改 `gene_service.py` 三处权限点**

`review_gene()` 权限校验块（约第2692-2702行）里的：

```python
            membership = (await db.execute(
                select(OrgMembership).where(
                    OrgMembership.user_id == current_user.id,
                    OrgMembership.org_id == gene.org_id,
                    OrgMembership.role == OrgRole.admin,
                    OrgMembership.deleted_at.is_(None),
                )
            )).scalar_one_or_none()
```

改为：

```python
            membership = (await db.execute(
                select(OrgMembership).where(
                    OrgMembership.user_id == current_user.id,
                    OrgMembership.org_id == gene.org_id,
                    OrgMembership.role.in_([OrgRole.operator, OrgRole.admin]),
                    OrgMembership.deleted_at.is_(None),
                )
            )).scalar_one_or_none()
```

`review_gene_overwrite_submission()` 权限校验块（约第2771-2778行）同样把 `OrgMembership.role == OrgRole.admin` 改为 `OrgMembership.role.in_([OrgRole.operator, OrgRole.admin])`。

`get_pending_review_genes()`（约第3564-3571行）的"查其作为 admin 的所有 org_id"逻辑：

```python
    admin_orgs_result = await db.execute(
        select(OrgMembership.org_id).where(
            OrgMembership.user_id == current_user.id,
            OrgMembership.role == OrgRole.admin,
            OrgMembership.deleted_at.is_(None),
        )
    )
    admin_org_ids = [row[0] for row in admin_orgs_result.all()]
```

改为：

```python
    admin_orgs_result = await db.execute(
        select(OrgMembership.org_id).where(
            OrgMembership.user_id == current_user.id,
            OrgMembership.role.in_([OrgRole.operator, OrgRole.admin]),
            OrgMembership.deleted_at.is_(None),
        )
    )
    admin_org_ids = [row[0] for row in admin_orgs_result.all()]
```

（变量名 `admin_org_ids`/`admin_orgs_result` 语义上已不完全准确，但为了保持 diff 最小、避免误伤同名变量的其他引用，本次不重命名——如果后续独立重构这个函数可以顺带改名）

- [ ] **Step 4: 运行测试确认通过**

Run: `cd nodeskclaw-backend && uv run pytest tests/test_gene_review_operator_access.py -v`
Expected: PASS（4 项全部通过）

- [ ] **Step 5: 回归测试——确认 admin 审核流程、bypass_review 逻辑不受影响**

Run: `cd nodeskclaw-backend && uv run pytest -k "gene_overwrite_submission_review or gene_target_fork_review or gene_upload" -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add nodeskclaw-backend/app/services/gene_service.py nodeskclaw-backend/tests/test_gene_review_operator_access.py
git commit -m "feat(backend): Gene 审核中心放宽为组织 operator 及以上可见可审核"
```

---

## Task 9: 前端角色等级比较工具

**Files:**
- Create: `nodeskclaw-portal/src/utils/orgRole.ts`
- Create: `nodeskclaw-portal/src/utils/orgRole.test.ts`

**Interfaces:**
- Produces：`ORG_ROLE_LEVEL: Record<string, number>`（镜像后端 `ADMIN_ROLE_LEVEL`）、`hasOrgRoleLevel(role: string | null | undefined, minRole: 'member' | 'operator' | 'admin'): boolean`，供 Task 10~14 的前端文件统一引用替换散落的 `portal_org_role === 'admin'` 字符串判断。

- [ ] **Step 1: 写失败测试**

```typescript
import { describe, expect, it } from 'vitest'

import { hasOrgRoleLevel, ORG_ROLE_LEVEL } from './orgRole'

describe('ORG_ROLE_LEVEL', () => {
  it('member < operator < admin', () => {
    expect(ORG_ROLE_LEVEL.member).toBeLessThan(ORG_ROLE_LEVEL.operator)
    expect(ORG_ROLE_LEVEL.operator).toBeLessThan(ORG_ROLE_LEVEL.admin)
  })
})

describe('hasOrgRoleLevel', () => {
  it('member 满足 member 门槛，不满足 operator 门槛', () => {
    expect(hasOrgRoleLevel('member', 'member')).toBe(true)
    expect(hasOrgRoleLevel('member', 'operator')).toBe(false)
  })

  it('operator 满足 member/operator 门槛，不满足 admin 门槛', () => {
    expect(hasOrgRoleLevel('operator', 'member')).toBe(true)
    expect(hasOrgRoleLevel('operator', 'operator')).toBe(true)
    expect(hasOrgRoleLevel('operator', 'admin')).toBe(false)
  })

  it('admin 满足所有门槛', () => {
    expect(hasOrgRoleLevel('admin', 'member')).toBe(true)
    expect(hasOrgRoleLevel('admin', 'operator')).toBe(true)
    expect(hasOrgRoleLevel('admin', 'admin')).toBe(true)
  })

  it('null/undefined/未知角色一律不满足任何门槛', () => {
    expect(hasOrgRoleLevel(null, 'member')).toBe(false)
    expect(hasOrgRoleLevel(undefined, 'member')).toBe(false)
    expect(hasOrgRoleLevel('bogus', 'member')).toBe(false)
  })
})
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd nodeskclaw-portal && npm run test -- orgRole`
Expected: FAIL（`orgRole.ts` 尚不存在，import 报错）——**注：本机 bash 环境无 node/npm，无法实际执行此命令，需要用户在本地有 node 的环境跑一遍确认。**

- [ ] **Step 3: 实现 `orgRole.ts`**

```typescript
// 组织角色等级比较工具：镜像后端 ADMIN_ROLE_LEVEL（app/models/org_membership.py）
export const ORG_ROLE_LEVEL: Record<string, number> = {
  member: 10,
  operator: 20,
  admin: 30,
}

export type OrgRoleName = 'member' | 'operator' | 'admin'

/** 判断 role 是否达到 minRole 及以上等级；role 为空/未知角色一律返回 false。 */
export function hasOrgRoleLevel(
  role: string | null | undefined,
  minRole: OrgRoleName,
): boolean {
  const roleLevel = role ? ORG_ROLE_LEVEL[role] ?? 0 : 0
  return roleLevel >= ORG_ROLE_LEVEL[minRole]
}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd nodeskclaw-portal && npm run test -- orgRole`
Expected: PASS —— **同上，本机无法执行，需用户验证。**

- [ ] **Step 5: Commit**

```bash
git add nodeskclaw-portal/src/utils/orgRole.ts nodeskclaw-portal/src/utils/orgRole.test.ts
git commit -m "feat(portal): 新增组织角色等级比较工具"
```

---

## Task 10: `router/index.ts` 审核中心路由分级 + `App.vue`/`Approvals.vue` 联动

**Files:**
- Modify: `nodeskclaw-portal/src/router/index.ts`
- Modify: `nodeskclaw-portal/src/App.vue`
- Modify: `nodeskclaw-portal/src/views/Approvals.vue`

**Interfaces:**
- Consumes：`hasOrgRoleLevel`（Task 9）。

- [ ] **Step 1: `router/index.ts` 审核中心路由 meta 从 `requireAdminOrSuper` 改为分级**

第213行：

```typescript
meta: { requiresAuth: true, requiredOrgRole: 'operator' },
```

守卫逻辑（第283-289行）：

```typescript
    // 申请审核中心守卫：超管或达到 requiredOrgRole 等级的组织成员可进
    const requiredOrgRole = to.meta.requiredOrgRole as 'member' | 'operator' | 'admin' | undefined
    if (requiredOrgRole && !authStore.user?.is_super_admin) {
      const { hasOrgRoleLevel } = await import('@/utils/orgRole')
      if (!hasOrgRoleLevel(authStore.user?.portal_org_role, requiredOrgRole)) {
        return next('/')
      }
    }
```

（`requireSuperAdmin` 守卫块，第277-280行，保持不动，不受影响）

- [ ] **Step 2: `App.vue` 审核中心导航入口条件放宽**

第152-153行：

```html
<button
  v-if="authStore.user?.is_super_admin || hasOrgRoleLevel(authStore.user?.portal_org_role, 'operator')"
```

需要在 `<script setup>` 里 import：

```typescript
import { hasOrgRoleLevel } from '@/utils/orgRole'
```

- [ ] **Step 3: `Approvals.vue` 加入组织/退出组织两个 Tab 收窄为仅 admin 可见**

在 `<script setup>` 顶部新增 import 与 store：

```typescript
import { useAuthStore } from '@/stores/auth'
import { hasOrgRoleLevel } from '@/utils/orgRole'

const authStore = useAuthStore()
```

`TabDef` 接口与 `allTabs`/`visibleTabs`（第307-324行）：

```typescript
interface TabDef {
  key: TabKey
  labelKey: string
  disabled: boolean
  requireFeature?: string
  requireOrgAdmin?: boolean
}
const allTabs: TabDef[] = [
  { key: 'skills', labelKey: 'approvals.tabSkills', disabled: false },
  { key: 'joinRequests', labelKey: 'approvals.tabJoinRequests', disabled: false, requireFeature: 'multi_org', requireOrgAdmin: true },
  { key: 'leaveRequests', labelKey: 'approvals.tabLeaveRequests', disabled: false, requireFeature: 'multi_org', requireOrgAdmin: true },
  { key: 'account', labelKey: 'approvals.tabAccount', disabled: true },
  { key: 'feature', labelKey: 'approvals.tabFeature', disabled: true },
]
const isOrgAdminOrSuper = computed(
  () => authStore.user?.is_super_admin || hasOrgRoleLevel(authStore.user?.portal_org_role, 'admin'),
)
const visibleTabs = computed(() =>
  allTabs.filter(t =>
    (!t.requireFeature || (t.requireFeature === 'multi_org' && hasMultiOrg.value)) &&
    (!t.requireOrgAdmin || isOrgAdminOrSuper.value)
  ),
)
```

（`computed` 需确认已从 `vue` import，Grep 该文件顶部 import 区确认后按需补充）

- [ ] **Step 4: 手动验证清单（本机无 node，无法自动化测试，需用户执行）**

1. `cd nodeskclaw-portal && npm run dev`
2. 用一个 operator 角色账号登录：确认顶部导航能看到"审核中心"入口，进入后只看到"技能审核" Tab，看不到"加入组织"/"退出组织" Tab。
3. 用同组织的 admin 账号登录：确认能看到全部 Tab（受 `multi_org` feature 影响的两个 Tab 除外）。
4. 用 member 角色账号登录：确认看不到"审核中心"导航入口，直接访问 `/approvals` URL 会被重定向到首页。

- [ ] **Step 5: Commit**

```bash
git add nodeskclaw-portal/src/router/index.ts nodeskclaw-portal/src/App.vue nodeskclaw-portal/src/views/Approvals.vue
git commit -m "feat(portal): 审核中心放宽为 operator 可见，加入/退出组织审核 Tab 收窄为 admin 专属"
```

---

## Task 11: `OrgSettings.vue` 导航按角色分级

**Files:**
- Modify: `nodeskclaw-portal/src/views/OrgSettings.vue`

**Interfaces:**
- Consumes：`hasOrgRoleLevel`（Task 9）、`useAuthStore`（已有）。

- [ ] **Step 1: 加 `minRole` 字段与过滤逻辑**

```typescript
import { useAuthStore } from '@/stores/auth'
import { hasOrgRoleLevel, type OrgRoleName } from '@/utils/orgRole'

const authStore = useAuthStore()

interface NavItem {
  name: string
  label: () => string
  icon: typeof Settings
  matchPrefix?: string
  feature?: string  // 关联的 feature_id；未启用时该 Tab 不展示
  minRole: OrgRoleName  // 最低组织角色门槛，驱动侧边栏是否展示该子页面
}

// 组织设置侧边栏导航项；EE/CE 现已共享同一份菜单（集群/Registry/SMTP 等不再 CE 独占）
// minRole：member 可见的仅组织信息/人类成员/LLM用量分析三页，其余均需 operator+
const allNavItems: NavItem[] = [
  { name: 'OrgInfo', label: () => t('orgSettings.orgInfo'), icon: Building2, minRole: 'member' },
  { name: 'OrgSettingsClusters', label: () => t('orgSettings.clusters'), icon: Server, minRole: 'operator' },
  { name: 'OrgSettingsRegistry', label: () => t('orgSettings.registryTitle'), icon: Container, minRole: 'operator' },
  { name: 'OrgSettingsEngineVersions', label: () => t('orgSettings.engineVersionsTab'), icon: Layers, minRole: 'operator' },
  { name: 'OrgSettingsSpecs', label: () => t('orgSettings.specsTab'), icon: Cpu, minRole: 'operator' },
  { name: 'OrgMembers', label: () => t('orgSettings.humanMembers'), icon: Users, minRole: 'member' },
  { name: 'OrgSettingsLlmKeys', label: () => t('orgSettings.llmKeysTab'), icon: KeyRound, minRole: 'operator' },
  { name: 'OrgSettingsLlmAnalytics', label: () => t('orgSettings.llmAnalyticsTab'), icon: BarChart3, feature: 'llm_analytics', minRole: 'member' },
  { name: 'OrgSettingsGenes', label: () => t('orgSettings.requiredGenesTab'), icon: Dna, minRole: 'operator' },
  { name: 'OrgSettingsSmtp', label: () => t('orgSettings.smtpTitle'), icon: Mail, minRole: 'operator' },
  { name: 'OrgSettingsNetwork', label: () => t('orgSettings.networkTab'), icon: Globe, minRole: 'operator' },
  { name: 'OrgEnterpriseFiles', label: () => t('enterpriseFiles.title'), icon: FolderOpen, matchPrefix: '/org-settings/files', minRole: 'operator' },
  { name: 'OrgSettingsAudit', label: () => t('auditLogs.title'), icon: ScrollText, minRole: 'operator' },
]

// 先按"路由是否真实存在"过滤，再按 feature 开关过滤，最后按组织角色等级过滤
const navItems = computed(() =>
  allNavItems.filter(item =>
    router.hasRoute(item.name) &&
    (!item.feature || useFeature(item.feature).isEnabled.value) &&
    (authStore.user?.is_super_admin || hasOrgRoleLevel(authStore.user?.portal_org_role, item.minRole))
  )
)
```

- [ ] **Step 2: 手动验证清单（本机无 node，需用户执行）**

1. member 账号访问 `/org-settings`：侧边栏只显示"组织信息""人类成员""LLM用量分析"三项。
2. operator 账号访问：侧边栏显示全部已注册的子页面。
3. member 账号直接在地址栏输入 `/org-settings/clusters`：确认页面本身是否有内容保护——**如果发现子页面组件内部本身没有角色校验，仅靠这里的导航隐藏无法防止直接改 URL 访问到页面内容**，需要视察 `OrgSettingsClusters.vue` 等子页面是否已经因为后端 API 本身要求 admin 而在页面内容层面自然拒绝（大概率如此，因为这些子页面的写操作从未被本次计划下放权限），如果发现某个子页面在 member 直接访问时能看到完整可交互内容而非空白/403 提示，记录下来但不在本任务展开修复（超出本次设计文档范围，仅追加到跟进列表）。

- [ ] **Step 3: Commit**

```bash
git add nodeskclaw-portal/src/views/OrgSettings.vue
git commit -m "feat(portal): 组织设置侧边栏按三级角色过滤子页面"
```

---

## Task 12: `OrgMembers.vue` 恢复 operator 自助管理

**Files:**
- Modify: `nodeskclaw-portal/src/views/OrgMembers.vue`

**Interfaces:**
- Consumes：`hasOrgRoleLevel`（Task 9）。
- 明确不改：`roleOptions`（第76-78行，供邀请对话框用）、`fetchRoles()`/`DefaultRoleProvider.get_roles()`、邀请按钮、待处理邀请列表——邀请流程维持只能选 member/admin 不变。

- [ ] **Step 1: 新增 `isOrgOperatorOrAbove` 与 `memberRoleOptions` 两个 computed**

在 `isOrgAdmin` 定义（第74行）之后追加：

```typescript
import { hasOrgRoleLevel } from '@/utils/orgRole'

// ...

const isOrgAdmin = computed(() => authStore.user?.portal_org_role === 'admin')
// 用于"人类成员"页操作区（角色下拉/移除/重置密码）的可见性门槛，operator 及以上可管理成员
const isOrgOperatorOrAbove = computed(() => hasOrgRoleLevel(authStore.user?.portal_org_role, 'operator'))

const roleOptions = computed(() =>
  roles.value.map(r => ({ value: r.id, label: t(r.name_key) }))
)
// 仅供"已有成员"角色下拉使用，与邀请对话框的 roleOptions 完全独立：
// - 恒定包含 member/operator
// - 仅当操作者自己是 admin 时才额外包含 admin（呼应后端 operator 不能晋升任何人为 admin 的能力上限）
const memberRoleOptions = computed(() => {
  const options = [
    { value: 'member', label: t('orgMembers.roleMember') },
    { value: 'operator', label: t('orgMembers.roleOperator') },
  ]
  if (isOrgAdmin.value) {
    options.push({ value: 'admin', label: t('orgMembers.roleAdmin') })
  }
  return options
})
```

- [ ] **Step 2: 操作区门槛从 `isOrgAdmin` 改为 `isOrgOperatorOrAbove`，去掉 operator 下拉禁用分支**

第476-489行替换为：

```html
<!-- Actions (operator+ only, not self) -->
<div v-if="isOrgOperatorOrAbove && member.user_id !== authStore.user?.id" class="flex items-center gap-2">
  <CustomSelect
    :model-value="member.role"
    :options="memberRoleOptions"
    size="xs"
    :disabled="actionLoading === member.id"
    @update:model-value="(v: string | null) => handleRoleChange(member, v!)"
  />
```

（第490-507行的重置密码/移除按钮保持不变，仍在同一个 `v-if` 容器内，天然继承新的 `isOrgOperatorOrAbove` 门槛）

- [ ] **Step 3: 邀请按钮/待处理邀请区域确认未受影响**

Run: `cd nodeskclaw-portal && grep -n "isOrgAdmin" src/views/OrgMembers.vue`
Expected: 仅剩邀请按钮（原第384行附近）与 `fetchPendingInvitations`（第127行）、待处理邀请区域（第511行）三处引用 `isOrgAdmin`，操作区那处已经改成了 `isOrgOperatorOrAbove`——确认无遗漏。

- [ ] **Step 4: 手动验证清单（本机无 node，需用户执行）**

1. operator 账号访问"设置 > 成员"：能看到角色下拉（可选 member/operator，选不到 admin），能重置密码/移除非 admin 成员；对 admin 成员该行整体隐藏操作区（因为 `member.user_id !== authStore.user?.id` 加上后端会拒绝，前端行为上 operator 对 admin 成员仍会显示下拉但选 admin 或降级 admin 会被后端 403——**注：前端目前没有单独隐藏"对 admin 成员的下拉"，这与"不能把任何人设为 admin"的能力上限是两回事（后者管的是能设成什么，不是能对谁操作）；若测试中发现 operator 尝试修改 admin 成员角色被 403 但前端没有对应报错提示，检查 `handleRoleChange` 的 `toast.error` 分支是否正常触发**，不因为发现这个而修改代码，只需确认现有错误提示流程能覆盖住。
2. admin 账号：确认下拉能选到 admin，且所有原有行为不受影响（回归）。
3. member 账号：确认操作区整体不显示。
4. 邀请对话框：确认角色选项仍然只有 member/admin 两项（未被本任务影响）。

- [ ] **Step 5: Commit**

```bash
git add nodeskclaw-portal/src/views/OrgMembers.vue
git commit -m "feat(portal): 组织成员管理向 operator 开放，恢复角色下拉三档选项"
```

---

## Task 13: `ExternalAgentList.vue` 按钮门槛调整

**Files:**
- Modify: `nodeskclaw-portal/src/views/external-agents/ExternalAgentList.vue`

- [ ] **Step 1: 拆分 `isAdmin` 为 `canManage`（operator+）与 `canDelete`（admin-only）**

```typescript
import { hasOrgRoleLevel } from '@/utils/orgRole'

// org operator+ 可创建/编辑/同步；delete 保持 org admin 专属
const canManage = computed(
  () => hasOrgRoleLevel(authStore.user?.portal_org_role, 'operator') || authStore.user?.is_super_admin,
)
const canDelete = computed(
  () => authStore.user?.portal_org_role === 'admin' || authStore.user?.is_super_admin,
)
```

- [ ] **Step 2: 模板里替换 `v-if` 目标**

第72行（页头"添加 Agent"按钮）、第97行（空状态"添加 Agent"按钮）、第179行（编辑按钮）、第187行（同步按钮）的 `v-if="isAdmin"` 全部改为 `v-if="canManage"`；第196行（删除按钮）的 `v-if="isAdmin"` 改为 `v-if="canDelete"`。

- [ ] **Step 3: 手动验证清单（本机无 node，需用户执行）**

1. operator 账号访问 `/agents`：能看到"添加 Agent"按钮、每张卡片的编辑/同步按钮，看不到删除按钮。
2. admin 账号：四个操作按钮均可见（回归）。
3. member 账号：只看到"发起对话"按钮，其余按钮不可见（回归，`canManage`/`canDelete` 对 member 都是 false）。

- [ ] **Step 4: Commit**

```bash
git add nodeskclaw-portal/src/views/external-agents/ExternalAgentList.vue
git commit -m "feat(portal): 外部Agent创建/编辑/同步按钮向 operator 开放，删除保持 admin 专属"
```

---

## Task 14: 知识库管理页新增角色门槛

**Files:**
- Modify: `nodeskclaw-portal/src/views/skills/admin/KnowledgeBaseListView.vue`

**Interfaces:**
- Consumes：`hasOrgRoleLevel`（Task 9）、`useAuthStore`（新增 import，此文件此前完全没有权限判断——已用 Grep 确认 `isAdmin|portal_org_role|is_super_admin|authStore` 零匹配，纯粹依赖后端 403，所以列表页此前对 member 是"能看到导航入口但列表接口 403"的糟糕体验；Task 6 已把 `list_kbs` 放宽为 member 可读，从今往后 member 打开这个页面能看到列表本身，只是不该看到写操作按钮）。

- [ ] **Step 1: 新增权限判断**

```typescript
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { BookOpen, Plus, Trash2, Pencil, RefreshCw, CheckCircle2, XCircle, Circle } from 'lucide-vue-next'
import { useSkillStore } from '@/stores/skills'
import { kbApi } from '@/services/skills'
import type { KnowledgeBase } from '@/services/skills'
import KbSyncStatus from '@/components/skills/KbSyncStatus.vue'
import KnowledgeBasePreviewDrawer from '@/components/skills/KnowledgeBasePreviewDrawer.vue'
import { useAuthStore } from '@/stores/auth'
import { hasOrgRoleLevel } from '@/utils/orgRole'

const router = useRouter()
const skillStore = useSkillStore()
const authStore = useAuthStore()
const deleting = ref<string | null>(null)
const syncing = ref<string | null>(null)

// org operator+ 可新建/编辑/同步；delete 保持 org admin 专属
const canManage = computed(
  () => hasOrgRoleLevel(authStore.user?.portal_org_role, 'operator') || authStore.user?.is_super_admin,
)
const canDelete = computed(
  () => authStore.user?.portal_org_role === 'admin' || authStore.user?.is_super_admin,
)
```

- [ ] **Step 2: 模板按钮门槛**

第57-63行（页头"新建知识库"按钮）加 `v-if="canManage"`；第116-123行（同步按钮）加 `v-if="canManage"`；第124-129行（编辑按钮）加 `v-if="canManage"`；第130-136行（删除按钮）加 `v-if="canDelete"`：

```html
<button
  v-if="canManage"
  class="inline-flex items-center gap-1.5 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:opacity-90"
  @click="router.push('/admin/knowledge-bases/new')"
>
  <Plus class="w-4 h-4" />
  新建知识库
</button>
```

```html
<button
  v-if="canManage"
  class="p-2 rounded-lg text-muted-foreground hover:text-primary hover:bg-primary/10 disabled:opacity-40"
  :disabled="syncing === kb.id"
  :title="'验证连接'"
  @click.stop="sync(kb.id)"
>
  <RefreshCw class="w-4 h-4" :class="{ 'animate-spin': syncing === kb.id }" />
</button>
<button
  v-if="canManage"
  class="p-2 rounded-lg text-muted-foreground hover:text-primary hover:bg-primary/10"
  @click.stop="router.push(`/admin/knowledge-bases/${kb.id}/edit`)"
>
  <Pencil class="w-4 h-4" />
</button>
<button
  v-if="canDelete"
  class="p-2 rounded-lg text-muted-foreground hover:text-destructive hover:bg-destructive/10 disabled:opacity-40"
  :disabled="deleting === kb.id"
  @click.stop="remove(kb.id)"
>
  <Trash2 class="w-4 h-4" />
</button>
```

- [ ] **Step 3: `KnowledgeBaseFormView.vue` 路由层兜底**

因为 `/admin/knowledge-bases/new`、`/admin/knowledge-bases/:id/edit` 目前路由 meta 只有 `requiresAuth: true`（`router/index.ts` 第148-158行），member 如果直接改 URL 访问，虽然按钮被隐藏了但路由本身不拦截——加 `requiredOrgRole` meta 堵住：

```typescript
{
  path: '/admin/knowledge-bases/new',
  name: 'AdminKnowledgeBaseNew',
  component: () => import('@/views/skills/admin/KnowledgeBaseFormView.vue'),
  meta: { requiresAuth: true, requiredOrgRole: 'operator' },
},
{
  path: '/admin/knowledge-bases/:id/edit',
  name: 'AdminKnowledgeBaseEdit',
  component: () => import('@/views/skills/admin/KnowledgeBaseFormView.vue'),
  meta: { requiresAuth: true, requiredOrgRole: 'operator' },
},
```

（Task 10 Step 1 已经在路由守卫里实现了 `requiredOrgRole` meta 的通用处理逻辑，这里直接复用，不需要再写一遍守卫代码；`/admin/knowledge-bases` 列表路由本身不加限制，因为 member 现在允许查看列表）

- [ ] **Step 4: 手动验证清单（本机无 node，需用户执行）**

1. member 账号访问 `/admin/knowledge-bases`：能看到知识库列表（因为 Task 6 已放宽 list 为 member 可读），看不到"新建知识库"按钮、每行的编辑/同步/删除按钮。
2. member 账号直接在地址栏输入 `/admin/knowledge-bases/new`：应被路由守卫重定向到首页。
3. operator 账号：能看到新建/编辑/同步按钮，看不到删除按钮。
4. admin 账号：全部按钮可见（回归）。

- [ ] **Step 5: Commit**

```bash
git add nodeskclaw-portal/src/views/skills/admin/KnowledgeBaseListView.vue nodeskclaw-portal/src/router/index.ts
git commit -m "feat(portal): 知识库管理页按钮/表单路由向 operator 开放，删除保持 admin 专属"
```

---

## Task 15: `InstanceLayout.vue` 移除 InstanceMembers 导航入口

**Files:**
- Modify: `nodeskclaw-portal/src/views/InstanceLayout.vue`

**Interfaces:**
- 不改：`myInstanceRole`/`InstanceDetail.vue`/`InstanceSettings.vue`/`InstanceKnowledgeBase.vue`/`AgentDetailDialog.vue` 里已有的 `ROLE_LEVEL >= editor` 门槛判断——这些原本就是为三档角色设计的，Task 3 把后端 `get_user_instance_role` 改成真正返回三档后，这些页面的 operator 编辑权限会自动生效，无需改动。

- [ ] **Step 1: 拆分 Files/Backups 与 Members 两组 nav-item push**

第56-72行的 `navItems` computed：

```typescript
const navItems = computed(() => {
  const items = [
    { name: 'InstanceDetail', label: t('common.overview'), icon: LayoutDashboard },
  ]
  items.push({ name: 'InstanceRuntime', label: t('common.runtimeStatus'), icon: Activity })
  if (caps.value.genes) items.push({ name: 'InstanceGenes', label: t('common.genes'), icon: Dna })
  if (caps.value.evolutionLog) items.push({ name: 'EvolutionLog', label: t('common.evolutionLog'), icon: History })
  items.push({ name: 'InstanceChannels', label: t('common.channels'), icon: Radio })
  if (caps.value.llmConfig) items.push({ name: 'InstanceSettings', label: t('common.modelConfig'), icon: Brain })
  items.push({ name: 'InstanceKnowledgeBase', label: t('common.knowledgeBases'), icon: Database })
  // 实例文件/备份维持现状：仍要求最高权限（admin），不做读写分级
  if (myInstanceRole.value === 'admin') {
    items.push({ name: 'InstanceFiles', label: t('common.files'), icon: FolderOpen })
    items.push({ name: 'InstanceBackups', label: t('backup.title'), icon: Archive })
  }
  // InstanceMembers 管理入口无条件隐藏：InstanceMember 角色字段已不再被任何鉴权
  // 逻辑读取（纯组织角色驱动后成为摆设），继续展示会让管理员误以为设置了角色会生效。
  // 组件本身不删除，数据表也不删除，仅隐藏导航入口，见设计文档"关键澄清"。
  return items
})
```

- [ ] **Step 2: 确认 `Users` 图标 import 是否还有其他用途**

Run: `cd nodeskclaw-portal && grep -n "Users" src/views/InstanceLayout.vue`
Expected: 若只剩下第5行的 import 语句没有任何使用点，从 import 列表里删掉 `Users`（避免死 import 触发 lint 警告）；若 vue-tsc/eslint 在本机无法运行验证，如实记录待用户在有 node 的环境跑 `npm run dev` 时查看控制台是否有未使用变量的警告。

- [ ] **Step 3: 手动验证清单（本机无 node，需用户执行）**

1. 任意角色打开某个实例详情页的左侧导航：确认"成员"（Members）Tab 已经不再出现，无论角色是什么。
2. admin 账号：确认"文件""备份"Tab 仍然可见（回归，未受影响）。
3. operator/member 账号：确认"文件""备份"Tab 仍然不可见（回归，未受影响——这两个维持"全部要求最高权限"不变）。
4. 直接在地址栏输入某实例的 `/instances/:id/members` URL：页面组件本身没删，是否仍可访问不是本任务关心的重点（设计文档明确"隐藏入口不删代码"），但如果该子页面内部逻辑会因为读不到任何有效角色而报错崩溃，记录下来但本任务不修复（组件本就应被视为死代码）。

- [ ] **Step 4: Commit**

```bash
git add nodeskclaw-portal/src/views/InstanceLayout.vue
git commit -m "feat(portal): 移除实例详情页的成员管理导航入口（InstanceMember 角色已成为摆设）"
```

---

## 自查清单（写完计划后的自我审查，已完成，仅记录结论）

1. **规格覆盖**：设计文档（`docs/superpowers/specs/2026-08-07-org-role-permission-matrix-design.md`）权限矩阵表格 8 行——工作区(Task 1-2)、实例(Task 3-4)、外部Agent(Task 5)、知识库(Task 6)、Gene审核中心(Task 8)、技能市场(不变，未建任务)、组织设置(Task 7 后端 + Task 11-12 前端)、超管后台(不变，未建任务)——全部有对应任务覆盖；"关键澄清"五条逐条对应：实例读档含聊天/自动化任务(Task 4)、创建门槛operator+(Task 2/4)、创建者豁免废除(Task 1/3)、邀请流程不变(Task 12 明确不改)、InstanceMembers.vue隐藏(Task 15)、组织自助设置页恢复operator下拉(Task 12)、Gene审核中心鉴权定位(Task 8，已修正设计文档的推测性描述)。
2. **占位符扫描**：全文无 TBD/TODO/"酌情处理"，所有 URL 前缀不确定处均给出了具体的 `grep` 验证命令而非直接假设。
3. **类型一致性**：`check_workspace_access`/`check_instance_access` 在 Task 1/3 中的新签名与 Task 2/4/5/6/7 各调用方引用的参数名、`require_org_member_role` 返回的 `(user, org)` 元组解构方式前后一致；`map_org_role_to_instance_role` 在 Task 3 定义后被 Task 3 Step 5（`list_instances`）复用，未在其他任务重复定义。
4. **超出设计文档范围但确有必要的追加项**：`remove_member`/`reset_member_password` 门槛下调（设计文档只提到 `update_member_role`，但同属"组织设置全部页面操作者可管理成员"的应有之义）、`get_user_instance_role`/`list_instances` 的三档角色映射（设计文档未细化到这一层，但前端 `ROLE_LEVEL >= editor` 判断早已存在，不修复后端会导致 operator 权限矩阵条目名存实亡）——均已在对应任务的 Interfaces/说明中注明理由。

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-08-07-org-role-permission-matrix.md`。共 15 个任务（Task 1-8 后端，Task 9-15 前端），两种执行方式可选：

1. **Subagent-Driven（推荐）**——每个任务派一个全新 subagent 执行，两阶段 review，快速迭代，适合这种任务边界清晰、可独立测试/独立 commit 的场景。
2. **Inline Execution**——在当前会话内按顺序批量执行，每个任务后设检查点。

选哪种？
