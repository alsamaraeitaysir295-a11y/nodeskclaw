# 组织级功能开关补全 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让超管后台的功能开关（`OrganizationFeatureOverride`）对组织级 override 真正生效（目前是摆设），并把协作空间/AI 员工实例/技能市场/自动化/外部 Agent/知识库这 6 个核心模块纳入功能开关管控。

**Architecture:** 后端 `require_feature()` 从只查 edition 改为查 `is_enabled_for_org()`（org override 优先，无 override 退回 edition 默认）；`GET /system/info` 增加可选认证，登录且已选组织时按组织合并 override。`features.yaml` 新增 6 个 feature_id，对应的 7 个后端路由文件在 `APIRouter()` 上挂 `require_feature(...)`。前端修复 `useFeature()` 的 "EE 恒为 true" 短路（这是本次排查中发现的必要配套修复，不修的话后端修好了前端导航栏也不会跟着变化），并给 6 个路由 + 6 个导航按钮补 `requireFeature`/`v-if` 门控。

**Tech Stack:** FastAPI + SQLAlchemy AsyncSession（后端），Vue 3 + Pinia + vue-router（前端），pytest-asyncio + httpx.AsyncClient（后端测试），vitest（前端测试）。

## Global Constraints

- 当前部署场景不区分 CE/EE（用户已确认为公司内部自用），新 feature 直接加进 `features.yaml` 的 `edition_features.ee` 列表，不引入新分类。
- Model/表结构不变（`OrganizationFeatureOverride` 已存在），本次不需要 Alembic 迁移。
- 每个改动文件按项目规范单独 `git add` + commit（禁止 `git add -A/.`），Windows 环境下 Edit/Write 工具改完文件后必须 `sed -i 's/\r$//'` 修正 CRLF 并用 `file <path>` 核对，再提交。
- 后端测试通过 WSL 执行：`wsl -e bash -c "cd /mnt/d/project/nodeskclaw/nodeskclaw-backend && .venv/bin/python -m pytest <path> -v"`。
- 前端类型检查/构建通过 WSL 执行：`wsl -e bash -c "cd /mnt/d/project/nodeskclaw/nodeskclaw-portal && npm run build"`；单测：`npm run test`。
- 禁止 emoji；图标统一 `lucide-vue-next`（本次改动不涉及新图标）。
- 参考设计文档：`ee/docs/2026-08-10-org-feature-toggle-coverage-design.md`（已获用户批准）。

---

## Task 1: `require_feature()` 组织级改造

**Files:**
- Modify: `nodeskclaw-backend/app/core/deps.py:10`（import 行）、`nodeskclaw-backend/app/core/deps.py:432-448`（`require_feature` 函数体）
- Test: `nodeskclaw-backend/tests/test_require_feature_org_override.py`（新建）

**Interfaces:**
- Consumes：`app.core.feature_gate.is_enabled_for_org(feature_id: str, org_id: str | None, db) -> bool`（已存在，`nodeskclaw-backend/app/core/feature_gate.py:103`，本任务不改它）；`app.core.deps._get_current_user_dep()`（已存在，延迟导入 `get_current_user`）。
- Produces：`require_feature(feature_id: str)` 返回的依赖函数签名不变（仍是 `Depends(require_feature("xxx"))` 的用法），但现在会额外解析 `request`/`db`/`user` 三个子依赖——所有已经在用 `require_feature` 的路由（现有 13 个 feature：`multi_org`/`billing`/`admin_members`/`platform_admin`/`enterprise_files`/`org_smtp_config`/`topology_audit`/`performance_analytics`/`llm_analytics`/`akr_management` 等）都会因此隐式要求已登录用户——这些路由本来就都在认证之后才会被访问到，不是行为回归。

- [ ] **Step 1: 写失败测试（fallback 到 current_org_id 场景 + override 生效场景）**

在 `nodeskclaw-backend/tests/test_require_feature_org_override.py` 写入：

```python
"""验证 require_feature() 按组织 override 生效：无 override 退回 edition 默认，
有 override 时优先生效；org_id 分别来自 URL path 参数和 user.current_org_id 两种取值路径。"""
import uuid

import pytest
from httpx import AsyncClient

from app.core.security import get_current_user
from app.main import app
from app.models.org_membership import OrgMembership, OrgRole
from app.models.organization import Organization
from app.models.organization_feature_override import OrganizationFeatureOverride
from app.models.user import User
from tests.conftest import TestSessionLocal


def _override_user(user: User):
    app.dependency_overrides[get_current_user] = lambda: user


def _clear_override():
    app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_no_override_falls_back_to_edition_default(client: AsyncClient):
    """org_id 取自 user.current_org_id（该路由无 org_id path 参数）；
    无 override 时，multi_org 在 EE edition 下默认启用，应正常返回 200。"""
    suffix = uuid.uuid4().hex[:8]
    async with TestSessionLocal() as db:
        org = Organization(name=f"org-rf-{suffix}", slug=f"org-rf-{suffix}")
        db.add(org)
        await db.flush()
        user = User(
            email=f"user-rf-{suffix}@example.com", name="user-rf",
            password_hash="x", current_org_id=org.id,
        )
        db.add(user)
        await db.flush()
        db.add(OrgMembership(org_id=org.id, user_id=user.id, role=OrgRole.member))
        await db.commit()
        await db.refresh(user)

    _override_user(user)
    try:
        resp = await client.get("/api/v1/org-join-requests/my")
        assert resp.status_code == 200, resp.text
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_org_override_disables_feature_via_current_org_id(client: AsyncClient):
    """同一路由（org_id 取自 current_org_id），给该组织加一条 multi_org=False 的
    override 后，应该从 200 变成 403。"""
    suffix = uuid.uuid4().hex[:8]
    async with TestSessionLocal() as db:
        org = Organization(name=f"org-rf2-{suffix}", slug=f"org-rf2-{suffix}")
        db.add(org)
        await db.flush()
        user = User(
            email=f"user-rf2-{suffix}@example.com", name="user-rf2",
            password_hash="x", current_org_id=org.id,
        )
        db.add(user)
        await db.flush()
        db.add(OrgMembership(org_id=org.id, user_id=user.id, role=OrgRole.member))
        db.add(OrganizationFeatureOverride(
            org_id=org.id, feature_id="multi_org", enabled=False,
            set_by_user_id=user.id,
        ))
        await db.commit()
        await db.refresh(user)

    _override_user(user)
    try:
        resp = await client.get("/api/v1/org-join-requests/my")
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["message_key"] == "errors.feature.disabled"
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_org_id_resolved_from_path_param_when_present(client: AsyncClient):
    """org_smtp_config 挂在带 {org_id} path 参数的路由上，用超管身份验证
    org_id 优先从 URL path 取（而不是 current_org_id）：给目标组织加 override=False
    后，超管访问该组织的 smtp-config 应该 403，即便超管自己的 current_org_id 是别的组织。"""
    suffix = uuid.uuid4().hex[:8]
    async with TestSessionLocal() as db:
        org_other = Organization(name=f"org-rf3-other-{suffix}", slug=f"org-rf3-other-{suffix}")
        org_target = Organization(name=f"org-rf3-target-{suffix}", slug=f"org-rf3-target-{suffix}")
        db.add_all([org_other, org_target])
        await db.flush()
        admin_user = User(
            email=f"admin-rf3-{suffix}@example.com", name="admin-rf3",
            password_hash="x", current_org_id=org_other.id, is_super_admin=True,
        )
        db.add(admin_user)
        await db.flush()
        db.add(OrganizationFeatureOverride(
            org_id=org_target.id, feature_id="org_smtp_config", enabled=False,
            set_by_user_id=admin_user.id,
        ))
        await db.commit()
        await db.refresh(admin_user)
        await db.refresh(org_target)

    _override_user(admin_user)
    try:
        resp = await client.get(f"/api/v1/orgs/{org_target.id}/smtp-config")
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["message_key"] == "errors.feature.disabled"
    finally:
        _clear_override()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `wsl -e bash -c "cd /mnt/d/project/nodeskclaw/nodeskclaw-backend && .venv/bin/python -m pytest tests/test_require_feature_org_override.py -v"`
Expected: `test_org_override_disables_feature_via_current_org_id` 和 `test_org_id_resolved_from_path_param_when_present` 两个测试因为当前 `require_feature()` 完全不查 override 表而返回 200（不是预期的 403），断言失败；`test_no_override_falls_back_to_edition_default` 应该已经通过（这条本来就该过，用来确认改造前后行为不回归）。

- [ ] **Step 3: 改造 `require_feature()`**

在 `nodeskclaw-backend/app/core/deps.py` 第 10 行，把：

```python
from app.core.feature_gate import feature_gate
```

改成：

```python
from app.core.feature_gate import feature_gate, is_enabled_for_org
```

第 432-448 行，把：

```python
def require_feature(feature_id: str):
    """工厂函数：生成要求指定 EE feature 已启用的依赖。

    用法：router = APIRouter(dependencies=[Depends(require_feature("billing"))])
    或在单个端点上：@router.get("/...", dependencies=[Depends(require_feature("billing"))])
    """
    async def _check_feature():
        if not feature_gate.is_enabled(feature_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error_code": 40320,
                    "message_key": "errors.feature.ee_required",
                    "message": f"Feature '{feature_id}' requires Enterprise Edition",
                },
            )
    return _check_feature
```

改成：

```python
def require_feature(feature_id: str):
    """工厂函数：生成要求指定 feature（按组织 override 合并后）已启用的依赖。

    用法：router = APIRouter(dependencies=[Depends(require_feature("billing"))])
    或在单个端点上：@router.get("/...", dependencies=[Depends(require_feature("billing"))])

    org_id 解析优先级：URL path 参数 org_id > 当前用户 current_org_id
    （与 require_org_admin / require_org_member_role 的取值逻辑一致）。
    """
    async def _check_feature(
        request: Request,
        db: AsyncSession = Depends(get_db),
        user=Depends(_get_current_user_dep()),
    ):
        org_id = request.path_params.get("org_id") or user.current_org_id
        if not await is_enabled_for_org(feature_id, org_id, db):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error_code": 40320,
                    "message_key": "errors.feature.disabled",
                    "message": f"Feature '{feature_id}' is disabled",
                },
            )
    return _check_feature
```

- [ ] **Step 4: 修正 CRLF 并跑测试确认通过**

Run:
```bash
sed -i 's/\r$//' nodeskclaw-backend/app/core/deps.py nodeskclaw-backend/tests/test_require_feature_org_override.py
file nodeskclaw-backend/app/core/deps.py nodeskclaw-backend/tests/test_require_feature_org_override.py
wsl -e bash -c "cd /mnt/d/project/nodeskclaw/nodeskclaw-backend && .venv/bin/python -m pytest tests/test_require_feature_org_override.py -v"
```
Expected: `file` 输出不含 `CRLF`；三个测试全部 PASS。

- [ ] **Step 5: 跑一遍受影响的既有测试，确认没有因为 require_feature 现在要求登录而回归**

Run: `wsl -e bash -c "cd /mnt/d/project/nodeskclaw/nodeskclaw-backend && .venv/bin/python -m pytest tests/test_org_join_request.py tests/test_org_leave_request.py tests/test_llm_keys_platform_managed_guard.py -v"`
Expected: 全部 PASS（这几个文件覆盖了 `multi_org`/`llm_analytics` 两个既有 require_feature 用户）。

- [ ] **Step 6: Commit**

```bash
git add nodeskclaw-backend/app/core/deps.py nodeskclaw-backend/tests/test_require_feature_org_override.py
git commit -m "$(cat <<'EOF'
fix(backend): require_feature 打通组织级 override 生效链路

之前 require_feature() 只查 edition 默认值，OrganizationFeatureOverride
表里的组织级强制开/关完全不生效。改为调用 is_enabled_for_org()，org_id
优先取 URL path 参数，其次取当前用户 current_org_id。
EOF
)"
```

---

## Task 2: `GET /system/info` 按组织合并 override

**Files:**
- Modify: `nodeskclaw-backend/app/core/security.py`（新增 `get_current_user_optional`，紧跟在 `get_current_user_unchecked` 之后，约第 205 行后）
- Modify: `nodeskclaw-backend/app/api/router.py:1-84`（import 区 + `system_info()` 函数体）
- Test: `nodeskclaw-backend/tests/test_system_info_org_features.py`（新建）

**Interfaces:**
- Consumes：`app.core.security.bearer_scheme`（已存在，`HTTPBearer(auto_error=False)`）、`app.core.security._get_user_by_token(token, db, *, allowed_scopes=None) -> User`（已存在的内部函数）、`app.core.feature_gate.is_enabled_for_org`（Task 1 已引入到 `deps.py`，这里从 `app.core.feature_gate` 直接导入）。
- Produces：`get_current_user_optional(credentials, db) -> User | None`，供 `system_info()` 使用；不设置 `_auth_actor` contextvar（这个端点不产生审计事件，不需要 actor 身份）。`system_info()` 返回结构不变（`{edition, version, features}`），`features[].enabled` 语义从"edition 默认值"变为"当前组织的有效值"（未登录或未选组织时退回 edition 默认值，不变）。

- [ ] **Step 1: 写失败测试**

在 `nodeskclaw-backend/tests/test_system_info_org_features.py` 写入：

```python
"""验证 GET /system/info 在已登录且已选组织时按组织 override 合并 feature 列表；
未登录时保持原有 edition 级默认值不变。"""
import uuid

import pytest
from httpx import AsyncClient

from app.core.security import get_current_user_optional
from app.main import app
from app.models.org_membership import OrgMembership, OrgRole
from app.models.organization import Organization
from app.models.organization_feature_override import OrganizationFeatureOverride
from app.models.user import User
from tests.conftest import TestSessionLocal


def _override_user(user: User | None):
    app.dependency_overrides[get_current_user_optional] = lambda: user


def _clear_override():
    app.dependency_overrides.pop(get_current_user_optional, None)


def _find_feature(features: list[dict], feature_id: str) -> dict:
    return next(f for f in features if f["id"] == feature_id)


@pytest.mark.asyncio
async def test_unauthenticated_returns_edition_default(client: AsyncClient):
    resp = await client.get("/api/v1/system/info")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "edition" in data
    assert "features" in data


@pytest.mark.asyncio
async def test_authenticated_no_override_keeps_edition_default(client: AsyncClient):
    suffix = uuid.uuid4().hex[:8]
    async with TestSessionLocal() as db:
        org = Organization(name=f"org-si-{suffix}", slug=f"org-si-{suffix}")
        db.add(org)
        await db.flush()
        user = User(
            email=f"user-si-{suffix}@example.com", name="user-si",
            password_hash="x", current_org_id=org.id,
        )
        db.add(user)
        await db.flush()
        db.add(OrgMembership(org_id=org.id, user_id=user.id, role=OrgRole.member))
        await db.commit()
        await db.refresh(user)

    _override_user(user)
    try:
        resp = await client.get("/api/v1/system/info")
        assert resp.status_code == 200, resp.text
        feat = _find_feature(resp.json()["features"], "multi_org")
        assert feat["enabled"] is True
    finally:
        _clear_override()


@pytest.mark.asyncio
async def test_authenticated_with_override_merges_org_value(client: AsyncClient):
    suffix = uuid.uuid4().hex[:8]
    async with TestSessionLocal() as db:
        org = Organization(name=f"org-si2-{suffix}", slug=f"org-si2-{suffix}")
        db.add(org)
        await db.flush()
        user = User(
            email=f"user-si2-{suffix}@example.com", name="user-si2",
            password_hash="x", current_org_id=org.id,
        )
        db.add(user)
        await db.flush()
        db.add(OrgMembership(org_id=org.id, user_id=user.id, role=OrgRole.member))
        db.add(OrganizationFeatureOverride(
            org_id=org.id, feature_id="multi_org", enabled=False,
            set_by_user_id=user.id,
        ))
        await db.commit()
        await db.refresh(user)

    _override_user(user)
    try:
        resp = await client.get("/api/v1/system/info")
        assert resp.status_code == 200, resp.text
        feat = _find_feature(resp.json()["features"], "multi_org")
        assert feat["enabled"] is False
    finally:
        _clear_override()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `wsl -e bash -c "cd /mnt/d/project/nodeskclaw/nodeskclaw-backend && .venv/bin/python -m pytest tests/test_system_info_org_features.py -v"`
Expected: `test_authenticated_with_override_merges_org_value` 因为当前 `system_info()` 不查 override 表，`enabled` 仍是 `True` 而不是预期 `False`，断言失败；`get_current_user_optional` 尚不存在，导入即报 `ImportError`，其余两个测试也失败。

- [ ] **Step 3: 在 `security.py` 新增 `get_current_user_optional`**

在 `nodeskclaw-backend/app/core/security.py` 的 `get_current_user_unchecked` 函数之后（第 204 行后，`get_current_user_from_query` 之前）插入：

```python
async def get_current_user_optional(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User | None:
    """可选认证：有合法 token 返回 User，没有 token 或 token 失效都返回 None（不抛异常）。

    用于 /system/info 这类无需强制登录、但登录后要按用户上下文返回差异化结果的场景。
    不设置 _auth_actor（这个场景不产生审计事件）。
    """
    if credentials is None:
        return None
    try:
        return await _get_user_by_token(credentials.credentials, db)
    except HTTPException:
        return None
```

- [ ] **Step 4: 改造 `router.py` 的 `system_info()`**

在 `nodeskclaw-backend/app/api/router.py` 第 1-4 行的 import 区加入：

```python
from app.core.feature_gate import feature_gate, is_enabled_for_org
from app.core.security import get_current_user_optional
from app.models.user import User
```

（`feature_gate` 已经在原第 39 行 `from app.core.feature_gate import feature_gate` 导入，直接把这行改成 `from app.core.feature_gate import feature_gate, is_enabled_for_org` 即可，不用新增一行；`get_current_user_optional`/`User` 是两行新增 import，加在文件顶部 import 区任意位置，紧邻已有的 `from app.core.deps import ...` 即可。）

第 77-84 行，把：

```python
@api_router.get("/system/info", tags=["系统"])
async def system_info():
    """暴露 edition 和启用的 feature 列表，供前端初始化使用。"""
    return {
        "edition": feature_gate.edition,
        "version": settings.APP_VERSION,
        "features": feature_gate.all_features(),
    }
```

改成：

```python
@api_router.get("/system/info", tags=["系统"])
async def system_info(
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
):
    """暴露 edition 和启用的 feature 列表，供前端初始化使用。

    已登录且已选组织时，每个 feature 的 enabled 按组织级 override 合并；
    未登录或未选组织时保持 edition 默认值。
    """
    features = feature_gate.all_features()
    if user is not None and user.current_org_id:
        features = [
            {**f, "enabled": await is_enabled_for_org(f["id"], user.current_org_id, db)}
            for f in features
        ]
    return {
        "edition": feature_gate.edition,
        "version": settings.APP_VERSION,
        "features": features,
    }
```

需要额外确认 `router.py` 顶部已有 `from sqlalchemy.ext.asyncio import AsyncSession` 和 `from app.core.deps import get_db`（用于 `db: AsyncSession = Depends(get_db)` 参数）——当前文件第 3 行是 `from fastapi import APIRouter, Depends`，没有单独导入 `AsyncSession`/`get_db`，需要一并加上：

```python
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.deps import get_db
```

- [ ] **Step 5: 修正 CRLF 并跑测试确认通过**

Run:
```bash
sed -i 's/\r$//' nodeskclaw-backend/app/core/security.py nodeskclaw-backend/app/api/router.py nodeskclaw-backend/tests/test_system_info_org_features.py
file nodeskclaw-backend/app/core/security.py nodeskclaw-backend/app/api/router.py nodeskclaw-backend/tests/test_system_info_org_features.py
wsl -e bash -c "cd /mnt/d/project/nodeskclaw/nodeskclaw-backend && .venv/bin/python -m pytest tests/test_system_info_org_features.py -v"
```
Expected: 无 CRLF；三个测试全部 PASS。

- [ ] **Step 6: 跑一遍全量 import 健全性检查**

Run: `wsl -e bash -c "cd /mnt/d/project/nodeskclaw/nodeskclaw-backend && .venv/bin/python -c 'import app.main'"`
Expected: 无异常（确认 router.py 新增的 import 没有循环依赖问题）。

- [ ] **Step 7: Commit**

```bash
git add nodeskclaw-backend/app/core/security.py nodeskclaw-backend/app/api/router.py nodeskclaw-backend/tests/test_system_info_org_features.py
git commit -m "$(cat <<'EOF'
fix(backend): system/info 按登录用户所在组织合并 feature override

新增可选认证依赖 get_current_user_optional，system_info() 在能解析出
登录用户且已选组织时，用 is_enabled_for_org 合并组织级 override；
未登录/未选组织时行为不变（退回 edition 默认值）。
EOF
)"
```

---

## Task 3: `errors.feature.disabled` 前端 i18n 补充

**Files:**
- Modify: `nodeskclaw-portal/src/i18n/locales/zh-CN.ts:1816`（`errors` 对象内新增 `feature` 分组）
- Modify: `nodeskclaw-portal/src/i18n/locales/en-US.ts:1812`（同上，英文版）

**Interfaces:**
- Consumes：无新代码接口，纯文案。Task 1 把 `message_key` 从 `errors.feature.ee_required` 改成了 `errors.feature.disabled`，这里补上对应翻译，避免前端错误提示直接裸显 message_key 或退化成兜底英文 message。

- [ ] **Step 1: zh-CN.ts 新增 key**

在 `nodeskclaw-portal/src/i18n/locales/zh-CN.ts` 第 1816 行 `errors: {` 之后插入一个新分组（放在 `agent: {...}` 之前或之后均可，这里放在 `common` 之后、`system` 之前，紧邻着补一个 `feature` 分组）：

```typescript
    feature: {
      disabled: "该功能未对当前组织开放，请联系管理员",
    },
```

- [ ] **Step 2: en-US.ts 新增对应 key**

在 `nodeskclaw-portal/src/i18n/locales/en-US.ts` 第 1812 行 `errors: {` 之后同样位置插入：

```typescript
    feature: {
      disabled: "This feature is not enabled for your organization. Please contact an administrator.",
    },
```

- [ ] **Step 3: 修正 CRLF 并跑前端构建确认无语法错误**

Run:
```bash
sed -i 's/\r$//' nodeskclaw-portal/src/i18n/locales/zh-CN.ts nodeskclaw-portal/src/i18n/locales/en-US.ts
file nodeskclaw-portal/src/i18n/locales/zh-CN.ts nodeskclaw-portal/src/i18n/locales/en-US.ts
wsl -e bash -c "cd /mnt/d/project/nodeskclaw/nodeskclaw-portal && npm run build"
```
Expected: 无 CRLF；build 成功无报错。

- [ ] **Step 4: Commit**

```bash
git add nodeskclaw-portal/src/i18n/locales/zh-CN.ts nodeskclaw-portal/src/i18n/locales/en-US.ts
git commit -m "$(cat <<'EOF'
fix(portal): 补充 errors.feature.disabled 的中英文翻译

require_feature() 的 message_key 从 errors.feature.ee_required 改成了
errors.feature.disabled，补上对应文案，避免用户看到裸 key 或英文兜底。
EOF
)"
```

---

## Task 4: `features.yaml` 新增 6 个模块注册

**Files:**
- Modify: `features.yaml:63`（`edition_features.ee` 列表末尾）

**Interfaces:**
- Consumes：无。
- Produces：6 个新 `feature_id`（`workspace`/`instance`/`gene_market`/`automation`/`external_agent`/`knowledge_base`），供 Task 5 的 `require_feature(...)` 调用和 Task 7/8 的前端 `useFeature(...)` 调用引用——**这两个任务里用到的字符串必须跟这里的 id 完全一致**。

- [ ] **Step 1: 在 `features.yaml` 末尾追加 6 个 feature**

在 `features.yaml` 第 63 行（`advanced_rbac` 这一项）之后追加：

```yaml

    # ── 核心模块（组织级功能开关管控） ──
    - id: workspace
      name: 协作空间
      description: 赛博办公室 / 协作空间，AI 员工与人类协作的核心工作区

    - id: instance
      name: AI 员工实例
      description: AI 员工实例的创建、管理、部署

    - id: gene_market
      name: 技能市场
      description: 基因（技能）浏览、发布、fork，以及给 AI 员工实例装卸基因

    - id: automation
      name: 自动化任务
      description: AI 员工的定时/周期自动化任务

    - id: external_agent
      name: 外部专用 Agent
      description: 外部专用 Agent 的接入与对话

    - id: knowledge_base
      name: 知识库
      description: 组织级知识库的创建与管理
```

- [ ] **Step 2: 验证 YAML 语法 + FeatureGate 能正确加载新条目**

Run: `wsl -e bash -c "cd /mnt/d/project/nodeskclaw/nodeskclaw-backend && .venv/bin/python -c \"from app.core.feature_gate import feature_gate; ids = {f['id'] for f in feature_gate.all_features()}; assert {'workspace','instance','gene_market','automation','external_agent','knowledge_base'} <= ids, ids; print('OK', len(ids))\""`
Expected: 输出 `OK 19`（原有 13 个 + 新增 6 个），无异常。

- [ ] **Step 3: 修正 CRLF**

Run:
```bash
sed -i 's/\r$//' features.yaml
file features.yaml
```
Expected: 无 CRLF。

- [ ] **Step 4: Commit**

```bash
git add features.yaml
git commit -m "$(cat <<'EOF'
feat(backend): features.yaml 新增协作空间/实例/技能市场/自动化/外部Agent/知识库

之前功能开关只覆盖零散的 EE 能力，公司实际在用的 6 个核心模块完全没有
注册进这套开关体系，无法按组织管控。
EOF
)"
```

---

## Task 5: 6 个模块的后端路由挂载 `require_feature`

**Files:**
- Modify: `nodeskclaw-backend/app/api/workspaces.py:18,48`
- Modify: `nodeskclaw-backend/app/api/instances.py:13,27-28`
- Modify: `nodeskclaw-backend/app/api/portal/instances.py:13,31`
- Modify: `nodeskclaw-backend/app/api/genes.py:18,47`
- Modify: `nodeskclaw-backend/app/api/portal/automation_tasks.py:12,23`
- Modify: `nodeskclaw-backend/app/api/external_agents.py:19-25,43`
- Modify: `nodeskclaw-backend/app/api/knowledge_bases.py:8,13`
- Test: `nodeskclaw-backend/tests/test_org_feature_toggle_new_modules.py`（新建）

**Interfaces:**
- Consumes：Task 1 改造后的 `app.core.deps.require_feature(feature_id: str)`；Task 4 注册的 6 个 feature_id 字符串（`workspace`/`instance`/`gene_market`/`automation`/`external_agent`/`knowledge_base`，必须跟 `features.yaml` 里的 id 逐字一致）。
- Produces：7 个路由对象（`workspaces.py` 的 `router`、`instances.py` 的 `instance_read_router`+`instance_write_router`、`portal/instances.py` 的 `router`、`genes.py` 的 `router`、`portal/automation_tasks.py` 的 `router`、`external_agents.py` 的 `router`、`knowledge_bases.py` 的 `router`）从此各自要求对应 feature 已启用，供后续任何新增端点自动继承（新端点不用重复声明）。

**范围边界（与设计文档一致，本任务不做的事）**：`blackboard.py`/`corridors.py`/`conversations.py`/`trust.py`/`templates.py`/`instance_templates.py`/`instance_members.py`/`instance_files.py`/`mcp.py`/`channel_configs.py` 这些工作区/实例的周边子路由**不**级联挂 `workspace`/`instance` 门控，即使它们跟 `workspace_router`/`instance_read_router` 挂在同一个 URL 前缀下。

- [ ] **Step 1: 写失败测试（6 个模块各测一遍"关了 403、默认开着 200"）**

在 `nodeskclaw-backend/tests/test_org_feature_toggle_new_modules.py` 写入：

```python
"""验证 6 个核心模块（协作空间/实例/技能市场/自动化/外部Agent/知识库）的
主入口路由都已经挂上对应的 require_feature 门控：关闭 override 后 403，
默认（无 override）时正常放行。"""
import uuid

import pytest
from httpx import AsyncClient

from app.core.security import get_current_user
from app.main import app
from app.models.org_membership import OrgMembership, OrgRole
from app.models.organization import Organization
from app.models.organization_feature_override import OrganizationFeatureOverride
from app.models.user import User
from tests.conftest import TestSessionLocal


def _override_user(user: User):
    app.dependency_overrides[get_current_user] = lambda: user


def _clear_override():
    app.dependency_overrides.pop(get_current_user, None)


async def _make_org_admin(suffix: str) -> tuple[User, Organization]:
    async with TestSessionLocal() as db:
        org = Organization(name=f"org-ft-{suffix}", slug=f"org-ft-{suffix}")
        db.add(org)
        await db.flush()
        user = User(
            email=f"user-ft-{suffix}@example.com", name="user-ft",
            password_hash="x", current_org_id=org.id,
        )
        db.add(user)
        await db.flush()
        db.add(OrgMembership(org_id=org.id, user_id=user.id, role=OrgRole.admin))
        await db.commit()
        await db.refresh(user)
        await db.refresh(org)
        return user, org


async def _disable_feature(org_id: str, feature_id: str, set_by_user_id: str):
    async with TestSessionLocal() as db:
        db.add(OrganizationFeatureOverride(
            org_id=org_id, feature_id=feature_id, enabled=False,
            set_by_user_id=set_by_user_id,
        ))
        await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "feature_id,method,path",
    [
        ("workspace", "GET", "/api/v1/workspaces"),
        ("instance", "GET", "/api/v1/instances"),
        ("gene_market", "GET", "/api/v1/genes"),
        ("automation", "GET", "/api/v1/automation-tasks"),
        ("external_agent", "GET", "/api/v1/external-agents"),
        ("knowledge_base", "GET", "/api/v1/knowledge-bases"),
    ],
)
async def test_module_enabled_by_default(client: AsyncClient, feature_id, method, path):
    suffix = uuid.uuid4().hex[:8] + feature_id[:4]
    user, _org = await _make_org_admin(suffix)

    _override_user(user)
    try:
        resp = await client.request(method, path)
        assert resp.status_code != 403, resp.text
    finally:
        _clear_override()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "feature_id,method,path",
    [
        ("workspace", "GET", "/api/v1/workspaces"),
        ("instance", "GET", "/api/v1/instances"),
        ("gene_market", "GET", "/api/v1/genes"),
        ("automation", "GET", "/api/v1/automation-tasks"),
        ("external_agent", "GET", "/api/v1/external-agents"),
        ("knowledge_base", "GET", "/api/v1/knowledge-bases"),
    ],
)
async def test_module_disabled_via_org_override(client: AsyncClient, feature_id, method, path):
    suffix = uuid.uuid4().hex[:8] + feature_id[:4]
    user, org = await _make_org_admin(suffix)
    await _disable_feature(org.id, feature_id, user.id)

    _override_user(user)
    try:
        resp = await client.request(method, path)
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["message_key"] == "errors.feature.disabled"
    finally:
        _clear_override()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `wsl -e bash -c "cd /mnt/d/project/nodeskclaw/nodeskclaw-backend && .venv/bin/python -m pytest tests/test_org_feature_toggle_new_modules.py -v"`
Expected: `test_module_disabled_via_org_override` 的 6 个参数化用例全部失败（路由还没挂 `require_feature`，即使 override 关闭也不会返回 403）；`test_module_enabled_by_default` 的 6 个用例应该已经通过（默认放行本来就成立）。

- [ ] **Step 3: `workspaces.py` 挂 `workspace`**

`nodeskclaw-backend/app/api/workspaces.py` 第 18 行，把：

```python
from app.core.deps import async_session_factory, get_current_org, get_db, require_org_member_role
```

改成：

```python
from app.core.deps import async_session_factory, get_current_org, get_db, require_feature, require_org_member_role
```

第 48 行，把：

```python
router = APIRouter()
```

改成：

```python
router = APIRouter(dependencies=[Depends(require_feature("workspace"))])
```

- [ ] **Step 4: `instances.py` 挂 `instance`（读+写两个 router）**

`nodeskclaw-backend/app/api/instances.py` 第 13 行，把：

```python
from app.core.deps import get_current_org, get_db
```

改成：

```python
from app.core.deps import get_current_org, get_db, require_feature
```

第 27-28 行，把：

```python
instance_read_router = APIRouter()
instance_write_router = APIRouter()
```

改成：

```python
instance_read_router = APIRouter(dependencies=[Depends(require_feature("instance"))])
instance_write_router = APIRouter(dependencies=[Depends(require_feature("instance"))])
```

- [ ] **Step 5: `portal/instances.py` 挂 `instance`**

`nodeskclaw-backend/app/api/portal/instances.py` 第 13 行，把：

```python
from app.core.deps import get_db
```

改成：

```python
from app.core.deps import get_db, require_feature
```

第 31 行，把：

```python
router = APIRouter()
```

改成：

```python
router = APIRouter(dependencies=[Depends(require_feature("instance"))])
```

- [ ] **Step 6: `genes.py` 挂 `gene_market`**

`nodeskclaw-backend/app/api/genes.py` 第 18 行，把：

```python
from app.core.deps import get_current_org, get_db, require_org_role
```

改成：

```python
from app.core.deps import get_current_org, get_db, require_feature, require_org_role
```

第 47 行，把：

```python
router = APIRouter()
```

改成：

```python
router = APIRouter(dependencies=[Depends(require_feature("gene_market"))])
```

- [ ] **Step 7: `portal/automation_tasks.py` 挂 `automation`**

`nodeskclaw-backend/app/api/portal/automation_tasks.py` 第 12 行，把：

```python
from app.core.deps import get_db
```

改成：

```python
from app.core.deps import get_db, require_feature
```

第 23 行，把：

```python
router = APIRouter()
```

改成：

```python
router = APIRouter(dependencies=[Depends(require_feature("automation"))])
```

- [ ] **Step 8: `external_agents.py` 挂 `external_agent`**

`nodeskclaw-backend/app/api/external_agents.py` 第 19-25 行，把：

```python
from app.core.deps import (
    async_session_factory,
    get_current_org,
    get_db,
    require_org_admin,
    require_org_member_role,
)
```

改成：

```python
from app.core.deps import (
    async_session_factory,
    get_current_org,
    get_db,
    require_feature,
    require_org_admin,
    require_org_member_role,
)
```

第 43 行，把：

```python
router = APIRouter()
```

改成：

```python
router = APIRouter(dependencies=[Depends(require_feature("external_agent"))])
```

- [ ] **Step 9: `knowledge_bases.py` 挂 `knowledge_base`**

`nodeskclaw-backend/app/api/knowledge_bases.py` 第 8 行，把：

```python
from app.core.deps import get_db, require_org_admin, require_org_member_role
```

改成：

```python
from app.core.deps import get_db, require_feature, require_org_admin, require_org_member_role
```

第 13 行，把：

```python
router = APIRouter()
```

改成：

```python
router = APIRouter(dependencies=[Depends(require_feature("knowledge_base"))])
```

- [ ] **Step 10: 修正 CRLF 并跑测试确认通过**

Run:
```bash
sed -i 's/\r$//' nodeskclaw-backend/app/api/workspaces.py nodeskclaw-backend/app/api/instances.py nodeskclaw-backend/app/api/portal/instances.py nodeskclaw-backend/app/api/genes.py nodeskclaw-backend/app/api/portal/automation_tasks.py nodeskclaw-backend/app/api/external_agents.py nodeskclaw-backend/app/api/knowledge_bases.py nodeskclaw-backend/tests/test_org_feature_toggle_new_modules.py
file nodeskclaw-backend/app/api/workspaces.py nodeskclaw-backend/app/api/instances.py nodeskclaw-backend/app/api/portal/instances.py nodeskclaw-backend/app/api/genes.py nodeskclaw-backend/app/api/portal/automation_tasks.py nodeskclaw-backend/app/api/external_agents.py nodeskclaw-backend/app/api/knowledge_bases.py nodeskclaw-backend/tests/test_org_feature_toggle_new_modules.py
wsl -e bash -c "cd /mnt/d/project/nodeskclaw/nodeskclaw-backend && .venv/bin/python -m pytest tests/test_org_feature_toggle_new_modules.py -v"
```
Expected: 无 CRLF；12 个参数化用例全部 PASS。

- [ ] **Step 11: 跑一遍这 6 个模块原有的测试，确认门控没有误伤合法请求**

Run: `wsl -e bash -c "cd /mnt/d/project/nodeskclaw/nodeskclaw-backend && .venv/bin/python -m pytest tests/test_workspace_permission_tiers.py tests/test_instance_permission_tiers.py tests/test_external_agent_permission_tiers.py tests/test_knowledge_base_permission_tiers.py tests/test_automation_task_org_check.py tests/test_gene_name_dedup.py -v"`
Expected: 全部 PASS（这些是已知覆盖了这 6 个模块权限行为的既有测试文件；如果本机 DB 有第 `test_gene_name_dedup.py` 相关的共享测试库脏数据导致个别无关用例失败，需要用 root cause 分析确认是不是本任务改动导致，而不是直接忽略）。

- [ ] **Step 12: Commit**

```bash
git add nodeskclaw-backend/app/api/workspaces.py nodeskclaw-backend/app/api/instances.py nodeskclaw-backend/app/api/portal/instances.py nodeskclaw-backend/app/api/genes.py nodeskclaw-backend/app/api/portal/automation_tasks.py nodeskclaw-backend/app/api/external_agents.py nodeskclaw-backend/app/api/knowledge_bases.py nodeskclaw-backend/tests/test_org_feature_toggle_new_modules.py
git commit -m "$(cat <<'EOF'
feat(backend): 6 个核心模块路由挂载组织级功能开关

workspaces.py/instances.py/portal/instances.py/genes.py/
portal/automation_tasks.py/external_agents.py/knowledge_bases.py
分别挂上 require_feature("workspace"/"instance"/"gene_market"/
"automation"/"external_agent"/"knowledge_base")，超管现在可以按组织
关闭这些模块。genes.py 按整个文件统一门控，不拆分市场/实例装卸基因；
工作区/实例的周边子路由（blackboard/corridors/conversations/trust/
templates 等）本轮不级联门控。
EOF
)"
```

---

## Task 6: 前端 `useFeature()` 修复 "EE 恒为 true" 短路

**Files:**
- Modify: `nodeskclaw-portal/src/composables/useFeature.ts`
- Test: `nodeskclaw-portal/src/composables/useFeature.spec.ts`（新建）

**Interfaces:**
- Consumes：`useAuthStore().systemInfo`（Task 2 改造后，登录且已选组织时其中的 `features[].enabled` 已经是组织合并后的有效值）。
- Produces：`useFeature(featureId: string) -> { isEnabled: ComputedRef<boolean> }`，函数签名不变，但 `isEnabled` 的取值不再对 EE edition 特殊短路——这是 Task 7/8 里 `App.vue` 导航按钮门控能生效的前提，**必须先做这个任务，否则后端修好了、导航栏也不会跟着变化**（EE 恒真短路会覆盖掉后端返回的真实 `enabled` 值）。

- [ ] **Step 1: 写失败测试**

在 `nodeskclaw-portal/src/composables/useFeature.spec.ts` 写入：

```typescript
import { describe, it, expect, beforeEach } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useAuthStore } from '@/stores/auth'
import { useFeature, useEdition } from './useFeature'

describe('useFeature', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('returns false when systemInfo is not loaded yet', () => {
    const { isEnabled } = useFeature('knowledge_base')
    expect(isEnabled.value).toBe(false)
  })

  it('EE edition + org override disabled -> isEnabled is false (not short-circuited to true)', () => {
    const authStore = useAuthStore()
    authStore.systemInfo = {
      edition: 'ee',
      version: '1.0.0',
      features: [{ id: 'knowledge_base', name: '知识库', enabled: false }],
    }
    const { isEnabled } = useFeature('knowledge_base')
    expect(isEnabled.value).toBe(false)
  })

  it('EE edition + feature enabled -> isEnabled is true', () => {
    const authStore = useAuthStore()
    authStore.systemInfo = {
      edition: 'ee',
      version: '1.0.0',
      features: [{ id: 'knowledge_base', name: '知识库', enabled: true }],
    }
    const { isEnabled } = useFeature('knowledge_base')
    expect(isEnabled.value).toBe(true)
  })

  it('feature not present in the list falls back to false', () => {
    const authStore = useAuthStore()
    authStore.systemInfo = { edition: 'ee', version: '1.0.0', features: [] }
    const { isEnabled } = useFeature('unregistered_feature')
    expect(isEnabled.value).toBe(false)
  })
})

describe('useEdition', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('exposes the edition and isEE flag from systemInfo', () => {
    const authStore = useAuthStore()
    authStore.systemInfo = { edition: 'ee', version: '1.0.0', features: [] }
    const { edition, isEE } = useEdition()
    expect(edition.value).toBe('ee')
    expect(isEE.value).toBe(true)
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `wsl -e bash -c "cd /mnt/d/project/nodeskclaw/nodeskclaw-portal && npx vitest run src/composables/useFeature.spec.ts"`
Expected: `EE edition + org override disabled -> isEnabled is false` 这一条失败（当前实现里 `info.edition === 'ee'` 短路直接返回 `true`，断言 `false` 不成立）；其余用例应该已经通过。

- [ ] **Step 3: 修复 `useFeature.ts`**

把 `nodeskclaw-portal/src/composables/useFeature.ts` 全文：

```typescript
import { computed } from 'vue'
import { useAuthStore } from '@/stores/auth'

export function useFeature(featureId: string) {
  const authStore = useAuthStore()

  const isEnabled = computed(() => {
    const info = authStore.systemInfo
    if (!info) return false
    if (info.edition === 'ee') return true
    const feature = info.features.find(f => f.id === featureId)
    return feature?.enabled ?? false
  })

  return { isEnabled }
}

export function useEdition() {
  const authStore = useAuthStore()

  const edition = computed(() => authStore.systemInfo?.edition ?? 'ce')
  const isEE = computed(() => edition.value === 'ee')

  return { edition, isEE }
}
```

改成：

```typescript
import { computed } from 'vue'
import { useAuthStore } from '@/stores/auth'

export function useFeature(featureId: string) {
  const authStore = useAuthStore()

  const isEnabled = computed(() => {
    const info = authStore.systemInfo
    if (!info) return false
    const feature = info.features.find(f => f.id === featureId)
    // features 数组只登记了受控 feature；不在列表里的 id 视为不受控，默认放行
    if (!feature) return true
    return feature.enabled
  })

  return { isEnabled }
}

export function useEdition() {
  const authStore = useAuthStore()

  const edition = computed(() => authStore.systemInfo?.edition ?? 'ce')
  const isEE = computed(() => edition.value === 'ee')

  return { edition, isEE }
}
```

（这里的关键变化：不再对 `edition === 'ee'` 整体短路。`enabled` 字段现在由后端 `/system/info` 在已登录场景下按组织 override 算好，前端只管读值；`feature` 在列表里找不到时视为"未注册/不受控"，维持原来"未知 feature 默认放行"的兜底语义，而不是像原来那样只在 CE 下才生效。）

- [ ] **Step 4: 调整测试里"feature not present"用例的预期**

回看 Step 1 写的 `feature not present in the list falls back to false` 用例——按 Step 3 的新语义，未注册 feature 现在应该默认放行（`true`）而不是 `false`，把 `useFeature.spec.ts` 里这一条改成：

```typescript
  it('feature not present in the list defaults to enabled (unregistered = uncontrolled)', () => {
    const authStore = useAuthStore()
    authStore.systemInfo = { edition: 'ee', version: '1.0.0', features: [] }
    const { isEnabled } = useFeature('unregistered_feature')
    expect(isEnabled.value).toBe(true)
  })
```

- [ ] **Step 5: 修正 CRLF 并跑测试确认全部通过**

Run:
```bash
sed -i 's/\r$//' nodeskclaw-portal/src/composables/useFeature.ts nodeskclaw-portal/src/composables/useFeature.spec.ts
file nodeskclaw-portal/src/composables/useFeature.ts nodeskclaw-portal/src/composables/useFeature.spec.ts
wsl -e bash -c "cd /mnt/d/project/nodeskclaw/nodeskclaw-portal && npx vitest run src/composables/useFeature.spec.ts"
```
Expected: 无 CRLF；5 个用例全部 PASS。

- [ ] **Step 6: 跑一遍全量前端单测，确认没有依赖旧短路行为的用例回归**

Run: `wsl -e bash -c "cd /mnt/d/project/nodeskclaw/nodeskclaw-portal && npm run test -- --run"`
Expected: 全部 PASS。如果有测试断言了"EE 下未启用的 feature 仍显示为 enabled"这种旧短路行为，需要判断是不是本来就该修——按当前任务目标（组织级开关要真正生效）这类断言是过时预期，应该跟着改掉，而不是保留短路逻辑迁就它。

- [ ] **Step 7: Commit**

```bash
git add nodeskclaw-portal/src/composables/useFeature.ts nodeskclaw-portal/src/composables/useFeature.spec.ts
git commit -m "$(cat <<'EOF'
fix(portal): useFeature 去掉 EE 恒为 true 的短路

之前 EE edition 下 useFeature() 无视 systemInfo.features 里的实际
enabled 值一律返回 true，导致后端 system_info 修好组织级 override 合并
后，前端导航栏依然不会跟着变化。改为直接读 enabled 字段，未注册的
feature id 维持"默认放行"语义。
EOF
)"
```

---

## Task 7: 前端路由级门控（6 个主入口路由）

**Files:**
- Modify: `nodeskclaw-portal/src/router/index.ts:26,47,119,125,145,164`

**Interfaces:**
- Consumes：`router/index.ts:271-277` 已有的 `beforeEach` 守卫逻辑（读 `to.meta.requireFeature`，查 `authStore.systemInfo.features`，不满足则 `next('/')`）——本任务不改这段守卫代码，只给 6 个路由定义补 `meta.requireFeature`。
- Produces：无（路由 meta 是叶子配置，不被其他任务消费）。

**范围边界**：只给 6 个模块的主入口路由加，子路由/详情页（如 `/instances/:id`、`/gene-market/gene/:slug`、`/agents/:id/chat`）本轮不加，与设计文档 §5 一致。

- [ ] **Step 1: `WorkspaceList`（路径 `/`）加 `workspace` 门控**

`nodeskclaw-portal/src/router/index.ts` 第 24-28 行，把：

```typescript
  {
    path: '/',
    name: 'WorkspaceList',
    component: () => import('@/views/WorkspaceList.vue'),
  },
```

改成：

```typescript
  {
    path: '/',
    name: 'WorkspaceList',
    component: () => import('@/views/WorkspaceList.vue'),
    meta: { requireFeature: 'workspace' },
  },
```

- [ ] **Step 2: `InstanceList`（路径 `/instances`）加 `instance` 门控**

第 45-49 行，把：

```typescript
  {
    path: '/instances',
    name: 'InstanceList',
    component: () => import('@/views/InstanceList.vue'),
  },
```

改成：

```typescript
  {
    path: '/instances',
    name: 'InstanceList',
    component: () => import('@/views/InstanceList.vue'),
    meta: { requireFeature: 'instance' },
  },
```

- [ ] **Step 3: `Automation`（路径 `/automation`）加 `automation` 门控**

第 117-122 行，把：

```typescript
  {
    path: '/automation',
    name: 'Automation',
    component: () => import('@/views/AutomationView.vue'),
    meta: { requiresAuth: true },
  },
```

改成：

```typescript
  {
    path: '/automation',
    name: 'Automation',
    component: () => import('@/views/AutomationView.vue'),
    meta: { requiresAuth: true, requireFeature: 'automation' },
  },
```

- [ ] **Step 4: `GeneMarket`（路径 `/gene-market`）加 `gene_market` 门控**

第 123-127 行，把：

```typescript
  {
    path: '/gene-market',
    name: 'GeneMarket',
    component: () => import('@/views/GeneMarket.vue'),
  },
```

改成：

```typescript
  {
    path: '/gene-market',
    name: 'GeneMarket',
    component: () => import('@/views/GeneMarket.vue'),
    meta: { requireFeature: 'gene_market' },
  },
```

- [ ] **Step 5: `AdminKnowledgeBaseList`（路径 `/admin/knowledge-bases`）加 `knowledge_base` 门控**

第 143-148 行，把：

```typescript
  {
    path: '/admin/knowledge-bases',
    name: 'AdminKnowledgeBaseList',
    component: () => import('@/views/skills/admin/KnowledgeBaseListView.vue'),
    meta: { requiresAuth: true },
  },
```

改成：

```typescript
  {
    path: '/admin/knowledge-bases',
    name: 'AdminKnowledgeBaseList',
    component: () => import('@/views/skills/admin/KnowledgeBaseListView.vue'),
    meta: { requiresAuth: true, requireFeature: 'knowledge_base' },
  },
```

- [ ] **Step 6: `ExternalAgentList`（路径 `/agents`）加 `external_agent` 门控**

第 162-167 行，把：

```typescript
  {
    path: '/agents',
    name: 'ExternalAgentList',
    component: () => import('@/views/external-agents/ExternalAgentList.vue'),
    meta: { requiresAuth: true },
  },
```

改成：

```typescript
  {
    path: '/agents',
    name: 'ExternalAgentList',
    component: () => import('@/views/external-agents/ExternalAgentList.vue'),
    meta: { requiresAuth: true, requireFeature: 'external_agent' },
  },
```

- [ ] **Step 7: 修正 CRLF 并跑类型检查/构建**

Run:
```bash
sed -i 's/\r$//' nodeskclaw-portal/src/router/index.ts
file nodeskclaw-portal/src/router/index.ts
wsl -e bash -c "cd /mnt/d/project/nodeskclaw/nodeskclaw-portal && npm run build"
```
Expected: 无 CRLF；build 成功无报错。

- [ ] **Step 8: Commit**

```bash
git add nodeskclaw-portal/src/router/index.ts
git commit -m "$(cat <<'EOF'
feat(portal): 6 个核心模块主入口路由补 requireFeature 门控

WorkspaceList/InstanceList/Automation/GeneMarket/
AdminKnowledgeBaseList/ExternalAgentList 六个主入口路由加上
meta.requireFeature，复用已有的 beforeEach 路由守卫；子路由/详情页
本轮不加。
EOF
)"
```

---

## Task 8: 前端导航按钮门控（`App.vue`）+ 人工验证

**Files:**
- Modify: `nodeskclaw-portal/src/App.vue:9,26,84-150`

**Interfaces:**
- Consumes：Task 6 修好的 `useFeature(featureId)`。
- Produces：无。

- [ ] **Step 1: 在 `<script setup>` 里补 6 个 `useFeature` 调用**

`nodeskclaw-portal/src/App.vue` 第 26 行，把：

```typescript
const { isEnabled: isPlatformAdminEnabled } = useFeature('platform_admin')
```

改成：

```typescript
const { isEnabled: isPlatformAdminEnabled } = useFeature('platform_admin')
const { isEnabled: isWorkspaceEnabled } = useFeature('workspace')
const { isEnabled: isInstanceEnabled } = useFeature('instance')
const { isEnabled: isGeneMarketEnabled } = useFeature('gene_market')
const { isEnabled: isAutomationEnabled } = useFeature('automation')
const { isEnabled: isExternalAgentEnabled } = useFeature('external_agent')
const { isEnabled: isKnowledgeBaseEnabled } = useFeature('knowledge_base')
```

（`isPlatformAdminEnabled` 是已有变量，本步骤只是在它下面追加 6 行，不改动它本身。）

- [ ] **Step 2: 协作空间按钮加 `v-if`**

第 85-95 行，把：

```html
            <button
              :class="[
                'shrink-0 whitespace-nowrap px-3 py-1.5 rounded-md text-sm transition-colors',
                (route.path === '/' || route.path.startsWith('/workspace')) && !route.path.startsWith('/instances') ? 'bg-primary/10 text-primary font-medium' : 'text-muted-foreground hover:text-foreground',
              ]"
              @click="router.push('/')"
            >
              <Boxes class="w-4 h-4 inline mr-1.5" />
              <span class="hidden lg:inline">{{ t('common.workspace') }}</span>
              <span class="lg:hidden">{{ t('nav.workspace') }}</span>
            </button>
```

改成：

```html
            <button
              v-if="isWorkspaceEnabled"
              :class="[
                'shrink-0 whitespace-nowrap px-3 py-1.5 rounded-md text-sm transition-colors',
                (route.path === '/' || route.path.startsWith('/workspace')) && !route.path.startsWith('/instances') ? 'bg-primary/10 text-primary font-medium' : 'text-muted-foreground hover:text-foreground',
              ]"
              @click="router.push('/')"
            >
              <Boxes class="w-4 h-4 inline mr-1.5" />
              <span class="hidden lg:inline">{{ t('common.workspace') }}</span>
              <span class="lg:hidden">{{ t('nav.workspace') }}</span>
            </button>
```

- [ ] **Step 3: 实例按钮加 `v-if`**

第 96-105 行，把：

```html
            <button
              :class="[
                'shrink-0 whitespace-nowrap px-3 py-1.5 rounded-md text-sm transition-colors',
                route.path.startsWith('/instances') ? 'bg-primary/10 text-primary font-medium' : 'text-muted-foreground hover:text-foreground',
              ]"
              @click="router.push('/instances')"
            >
              <Server class="w-4 h-4 inline mr-1.5" />
              {{ t('common.instance') }}
            </button>
```

改成：

```html
            <button
              v-if="isInstanceEnabled"
              :class="[
                'shrink-0 whitespace-nowrap px-3 py-1.5 rounded-md text-sm transition-colors',
                route.path.startsWith('/instances') ? 'bg-primary/10 text-primary font-medium' : 'text-muted-foreground hover:text-foreground',
              ]"
              @click="router.push('/instances')"
            >
              <Server class="w-4 h-4 inline mr-1.5" />
              {{ t('common.instance') }}
            </button>
```

- [ ] **Step 4: 技能市场按钮加 `v-if`**

第 106-116 行，把：

```html
            <button
              :class="[
                'shrink-0 whitespace-nowrap px-3 py-1.5 rounded-md text-sm transition-colors',
                route.path.startsWith('/gene-market') ? 'bg-primary/10 text-primary font-medium' : 'text-muted-foreground hover:text-foreground',
              ]"
              @click="router.push('/gene-market')"
            >
              <FlaskConical class="w-4 h-4 inline mr-1.5" />
              <span class="hidden lg:inline">{{ t('common.geneMarket') }}</span>
              <span class="lg:hidden">{{ t('nav.geneMarket') }}</span>
            </button>
```

改成：

```html
            <button
              v-if="isGeneMarketEnabled"
              :class="[
                'shrink-0 whitespace-nowrap px-3 py-1.5 rounded-md text-sm transition-colors',
                route.path.startsWith('/gene-market') ? 'bg-primary/10 text-primary font-medium' : 'text-muted-foreground hover:text-foreground',
              ]"
              @click="router.push('/gene-market')"
            >
              <FlaskConical class="w-4 h-4 inline mr-1.5" />
              <span class="hidden lg:inline">{{ t('common.geneMarket') }}</span>
              <span class="lg:hidden">{{ t('nav.geneMarket') }}</span>
            </button>
```

- [ ] **Step 5: 自动化按钮加 `v-if`**

第 117-128 行，把：

```html
            <!-- 自动化任务入口 -->
            <button
              :class="[
                'shrink-0 whitespace-nowrap px-3 py-1.5 rounded-md text-sm transition-colors',
                route.path.startsWith('/automation') ? 'bg-primary/10 text-primary font-medium' : 'text-muted-foreground hover:text-foreground',
              ]"
              @click="router.push('/automation')"
            >
              <Zap class="w-4 h-4 inline mr-1.5" />
              <span class="hidden lg:inline">自动化</span>
              <span class="lg:hidden">自动化</span>
            </button>
```

改成：

```html
            <!-- 自动化任务入口 -->
            <button
              v-if="isAutomationEnabled"
              :class="[
                'shrink-0 whitespace-nowrap px-3 py-1.5 rounded-md text-sm transition-colors',
                route.path.startsWith('/automation') ? 'bg-primary/10 text-primary font-medium' : 'text-muted-foreground hover:text-foreground',
              ]"
              @click="router.push('/automation')"
            >
              <Zap class="w-4 h-4 inline mr-1.5" />
              <span class="hidden lg:inline">自动化</span>
              <span class="lg:hidden">自动化</span>
            </button>
```

- [ ] **Step 6: Agent 按钮加 `v-if`**

第 130-140 行，把：

```html
            <!-- 外部专用 Agent 入口（所有成员可见） -->
            <button
              :class="[
                'shrink-0 whitespace-nowrap px-3 py-1.5 rounded-md text-sm transition-colors',
                route.path.startsWith('/agents') ? 'bg-primary/10 text-primary font-medium' : 'text-muted-foreground hover:text-foreground',
              ]"
              @click="router.push('/agents')"
            >
              <Bot class="w-4 h-4 inline mr-1.5" />
              Agent
            </button>
```

改成：

```html
            <!-- 外部专用 Agent 入口（所有成员可见） -->
            <button
              v-if="isExternalAgentEnabled"
              :class="[
                'shrink-0 whitespace-nowrap px-3 py-1.5 rounded-md text-sm transition-colors',
                route.path.startsWith('/agents') ? 'bg-primary/10 text-primary font-medium' : 'text-muted-foreground hover:text-foreground',
              ]"
              @click="router.push('/agents')"
            >
              <Bot class="w-4 h-4 inline mr-1.5" />
              Agent
            </button>
```

- [ ] **Step 7: 知识库按钮加 `v-if`**

第 141-150 行，把：

```html
            <button
              :class="[
                'shrink-0 whitespace-nowrap px-3 py-1.5 rounded-md text-sm transition-colors',
                route.path.startsWith('/admin/knowledge-bases') ? 'bg-primary/10 text-primary font-medium' : 'text-muted-foreground hover:text-foreground',
              ]"
              @click="router.push('/admin/knowledge-bases')"
            >
              <BookOpen class="w-4 h-4 inline mr-1.5" />
              知识库
            </button>
```

改成：

```html
            <button
              v-if="isKnowledgeBaseEnabled"
              :class="[
                'shrink-0 whitespace-nowrap px-3 py-1.5 rounded-md text-sm transition-colors',
                route.path.startsWith('/admin/knowledge-bases') ? 'bg-primary/10 text-primary font-medium' : 'text-muted-foreground hover:text-foreground',
              ]"
              @click="router.push('/admin/knowledge-bases')"
            >
              <BookOpen class="w-4 h-4 inline mr-1.5" />
              知识库
            </button>
```

- [ ] **Step 8: 修正 CRLF 并跑构建**

Run:
```bash
sed -i 's/\r$//' nodeskclaw-portal/src/App.vue
file nodeskclaw-portal/src/App.vue
wsl -e bash -c "cd /mnt/d/project/nodeskclaw/nodeskclaw-portal && npm run build"
```
Expected: 无 CRLF；build 成功无报错。

- [ ] **Step 9: Commit**

```bash
git add nodeskclaw-portal/src/App.vue
git commit -m "$(cat <<'EOF'
feat(portal): 6 个核心模块导航按钮补 v-if 功能开关门控

协作空间/实例/技能市场/自动化/Agent/知识库六个顶部导航按钮加上
useFeature(...).isEnabled 的 v-if 判断，组织级功能开关关闭后按钮直接
不渲染，跟路由级门控（Task 7）配套。
EOF
)"
```

- [ ] **Step 10: 人工验证端到端链路（需要用户在能跑通服务的环境里做，AI 不代跑）**

请用户按以下步骤手动验证一次完整链路（对应设计文档"数据流"章节）：

1. 启动服务：`./dev.sh ee`（或用户日常启动方式）。
2. 用超管账号登录，进入超管后台 → 选中一个测试组织 → "功能开关" tab，确认能看到新的 6 个 feature（`workspace`/`instance`/`gene_market`/`automation`/`external_agent`/`knowledge_base`），把其中一个（比如"知识库"）强制关闭。
3. 用该组织的普通成员账号登录（或刷新已登录会话），确认：
   - 顶部导航栏"知识库"按钮消失；
   - 直接在地址栏访问 `/admin/knowledge-bases`，被重定向回首页；
   - 用浏览器 devtools 或 API 工具直接调 `GET /api/v1/knowledge-bases`，返回 403 且 `message_key` 是 `errors.feature.disabled`。
4. 把该 feature 重新打开（清除 override 或设为强制开启），确认导航栏按钮恢复、页面可访问、API 恢复 200。

Expected: 4 步全部符合预期。如果有偏差，记录具体现象反馈，不要凭猜测下结论——按项目规范需要端到端分层排查（前端→后端→DB）。
