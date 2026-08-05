# 注册登录功能改造 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将注册表单改为工号必填、组织下拉必选、邮箱可选，登录支持工号+密码，去除验证码 Tab。

**Architecture:** 后端新增 `employee_id` 字段（User 表），修正 email/phone 唯一约束为部分索引，新增 `GET /auth/orgs` 公开接口，注册时自动加入所选组织；前端注册页增加工号输入框和组织下拉，登录页移除验证码 Tab。

**Tech Stack:** Python 3.12 + FastAPI + SQLAlchemy (asyncpg) + Alembic；Vue 3 + TypeScript + vue-i18n。

## Global Constraints

- 数据删除一律软删除（`deleted_at`），唯一约束用 Partial Unique Index，禁止全表 unique
- Alembic 迁移必须 `alembic revision --autogenerate`，禁止手写 revision ID
- 工号格式：8 位纯数字，正则 `^\d{8}$`
- 图标用 `lucide-vue-next`，禁止 emoji
- 后端错误码格式：`{ error_code: int, message_key: str, message: str }`
- 每完成一个 Task 立即 commit，不攒批

---

## 文件变更一览

| 文件 | 操作 |
|------|------|
| `nodeskclaw-backend/app/models/user.py` | 修改：新增 employee_id 字段，修正 email/phone 唯一约束 |
| `nodeskclaw-backend/alembic/versions/xxxx_add_employee_id.py` | 新建：Alembic 自动生成 |
| `nodeskclaw-backend/app/schemas/auth.py` | 修改：RegisterRequest 新增 employee_id/org_id，email 改可选 |
| `nodeskclaw-backend/app/api/auth.py` | 修改：新增 GET /auth/orgs，更新 public_register 调用 |
| `nodeskclaw-backend/app/services/auth_service.py` | 修改：register_user 新逻辑，_detect_account_type 加工号识别 |
| `nodeskclaw-backend/tests/test_auth_employee_id.py` | 新建：工号识别 + 注册接口单元测试 |
| `nodeskclaw-portal/src/stores/auth.ts` | 修改：register 方法签名，移除验证码登录方法 |
| `nodeskclaw-portal/src/i18n/locales/zh-CN.ts` | 修改：新增工号/组织相关 i18n key |
| `nodeskclaw-portal/src/i18n/locales/en-US.ts` | 修改：同步英文翻译 |
| `nodeskclaw-portal/src/views/Register.vue` | 修改：新增工号输入框、组织下拉框，邮箱改可选 |
| `nodeskclaw-portal/src/views/Login.vue` | 修改：移除验证码 Tab 及相关代码 |

---

## Task 1: User 模型 + Alembic 迁移

**Files:**
- Modify: `nodeskclaw-backend/app/models/user.py`
- Create: `nodeskclaw-backend/alembic/versions/xxxx_add_employee_id_fix_email_phone_unique.py`（auto-generated）

**Interfaces:**
- Produces: `User.employee_id` 字段（String(8), nullable），供 Task 3 的 `register_user` 和 `_detect_account_type` 使用

- [ ] **Step 1: 修改 User 模型**

打开 `nodeskclaw-backend/app/models/user.py`，做以下三处改动：

**1a. 在 `__table_args__` 中增加三个部分唯一索引，同时移除原有的列级 `unique=True`：**

将 `__table_args__` 从：
```python
__table_args__ = (
    Index(
        "uq_users_username", "username",
        unique=True, postgresql_where=text("deleted_at IS NULL"),
    ),
)
```
改为：
```python
__table_args__ = (
    Index(
        "uq_users_username", "username",
        unique=True, postgresql_where=text("deleted_at IS NULL"),
    ),
    Index(
        "uq_users_employee_id", "employee_id",
        unique=True,
        postgresql_where=text("deleted_at IS NULL AND employee_id IS NOT NULL"),
    ),
    Index(
        "uq_users_email", "email",
        unique=True,
        postgresql_where=text("deleted_at IS NULL AND email IS NOT NULL"),
    ),
    Index(
        "uq_users_phone", "phone",
        unique=True,
        postgresql_where=text("deleted_at IS NULL AND phone IS NOT NULL"),
    ),
)
```

**1b. 修改 email 列：去掉 `unique=True`：**
```python
# 改前
email: Mapped[str | None] = mapped_column(String(256), unique=True, nullable=True)
# 改后
email: Mapped[str | None] = mapped_column(String(256), nullable=True)
```

**1c. 修改 phone 列：去掉 `unique=True`：**
```python
# 改前
phone: Mapped[str | None] = mapped_column(String(32), unique=True, nullable=True)
# 改后
phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
```

**1d. 在 `name` 字段之后新增 `employee_id` 字段（在 username 之前）：**
```python
name: Mapped[str] = mapped_column(String(128), nullable=False)
employee_id: Mapped[str | None] = mapped_column(String(8), nullable=True)
email: Mapped[str | None] = mapped_column(String(256), nullable=True)
```

- [ ] **Step 2: 生成 Alembic 迁移**

```bash
cd nodeskclaw-backend
uv run alembic revision --autogenerate -m "add_employee_id_fix_email_phone_unique"
```

检查生成的迁移文件（`alembic/versions/xxxx_add_employee_id_fix_email_phone_unique.py`）：
- `upgrade()` 中应包含 `op.add_column('users', sa.Column('employee_id', sa.String(8), nullable=True))`
- 应包含三条 `op.create_index(...)` 调用（employee_id、email、phone 各一条）
- 应包含 `op.drop_constraint(...)` 移除原来 email/phone 上的全表 unique 约束（PostgreSQL 中列级 unique 会生成 uq 约束）

如果 `drop_constraint` 未自动生成（Alembic 有时检测不到隐式约束），需手动在 `upgrade()` 补写：
```python
op.drop_constraint("users_email_key", "users", type_="unique")
op.drop_constraint("users_phone_key", "users", type_="unique")
```
并在 `downgrade()` 中补写对应的 `op.create_unique_constraint`。

- [ ] **Step 3: 应用迁移，验证表结构**

```bash
cd nodeskclaw-backend
uv run alembic upgrade head
```

```bash
# 确认 employee_id 列存在，email/phone 上已无全表 unique 约束
uv run python -c "
import asyncio
from sqlalchemy import inspect, text
from app.core.deps import engine

async def check():
    async with engine.connect() as conn:
        result = await conn.execute(text(
            \"SELECT indexname, indexdef FROM pg_indexes WHERE tablename='users' ORDER BY indexname\"
        ))
        for row in result:
            print(row)

asyncio.run(check())
"
```

预期看到 `uq_users_employee_id`、`uq_users_email`、`uq_users_phone` 三条部分唯一索引，但不再看到 `users_email_key`、`users_phone_key`。

- [ ] **Step 4: Commit**

```bash
git add nodeskclaw-backend/app/models/user.py nodeskclaw-backend/alembic/versions/
git commit -m "feat(backend): 新增 employee_id 字段，修正 email/phone 唯一约束为部分索引"
```

---

## Task 2: 后端 Schema + API（注册接口 + 公开组织列表）

**Files:**
- Modify: `nodeskclaw-backend/app/schemas/auth.py`
- Modify: `nodeskclaw-backend/app/api/auth.py`

**Interfaces:**
- Consumes: `User.employee_id`（Task 1）
- Produces:
  - `RegisterRequest(name, employee_id, org_id, password, email?, phone?)` — Task 3 的 `register_user` 依赖此签名
  - `GET /api/v1/auth/orgs` — 返回 `[{id: str, name: str}]`，前端 Task 6 调用

- [ ] **Step 1: 更新 RegisterRequest Schema**

打开 `nodeskclaw-backend/app/schemas/auth.py`，将 `RegisterRequest` 替换为：

```python
class RegisterRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)         # 必填：姓名
    employee_id: str = Field(pattern=r"^\d{8}$")            # 必填：8 位纯数字工号
    org_id: str = Field(min_length=1)                       # 必填：所属组织 ID
    password: str = Field(min_length=6, max_length=200)     # 必填：密码
    email: EmailStr | None = None                           # 可选：邮箱
    phone: str | None = None                                # 可选：手机号
```

同时在文件顶部 `from pydantic import BaseModel, EmailStr, Field, field_validator` 已有 `EmailStr`，无需添加。

- [ ] **Step 2: 新增公开组织列表响应 Schema**

在 `app/schemas/auth.py` 末尾新增：

```python
class PublicOrgItem(BaseModel):
    id: str
    name: str

    model_config = {"from_attributes": True}
```

- [ ] **Step 3: 新增 GET /auth/orgs 端点**

打开 `nodeskclaw-backend/app/api/auth.py`，在文件顶部 imports 中补充：
```python
from app.schemas.auth import (
    ...
    PublicOrgItem,
)
```

在 `# ── 公共注册` 代码块之前插入新路由：

```python
# ── 公开组织列表（注册时下拉框数据源，无需认证）─────────────
@router.get("/orgs", response_model=ApiResponse[list[PublicOrgItem]])
async def list_public_orgs(db: AsyncSession = Depends(get_db)):
    """返回所有激活组织列表，供注册页下拉框使用，无需认证。"""
    from app.models.organization import Organization
    from sqlalchemy import select
    result = await db.execute(
        select(Organization)
        .where(Organization.is_active.is_(True), Organization.deleted_at.is_(None))
        .order_by(Organization.created_at.asc())
    )
    orgs = [PublicOrgItem.model_validate(o) for o in result.scalars().all()]
    return ApiResponse(data=orgs)
```

- [ ] **Step 4: 更新 public_register 端点调用**

找到 `public_register` 函数（约第 122 行），将 body 传递改为：

```python
@router.post("/register", response_model=ApiResponse[RegisterResponse])
async def public_register(body: RegisterRequest, db: AsyncSession = Depends(get_db)):
    """公共注册（无需邀请）。"""
    result = await auth_service.register_user(
        name=body.name,
        employee_id=body.employee_id,
        org_id=body.org_id,
        password=body.password,
        email=body.email,
        phone=body.phone,
        db=db,
    )
    await hooks.emit(
        "operation_audit",
        action="auth.registered", target_type="user",
        target_id=result.user.id, actor_id=result.user.id,
        org_id=result.user.current_org_id,
        details={"method": "employee_id"},
    )
    return ApiResponse(data=result)
```

- [ ] **Step 5: 验证新端点可访问**

启动后端（或在测试环境），访问：
```bash
curl http://localhost:4510/api/v1/auth/orgs
```

预期返回 `{"code": 200, "data": [...]}` 列表（即使为空也应是 `"data": []`，不报错）。

- [ ] **Step 6: Commit**

```bash
git add nodeskclaw-backend/app/schemas/auth.py nodeskclaw-backend/app/api/auth.py
git commit -m "feat(backend): 新增 GET /auth/orgs 公开接口，更新 RegisterRequest 添加工号/组织字段"
```

---

## Task 3: Auth Service — 注册逻辑 + 工号登录识别

**Files:**
- Modify: `nodeskclaw-backend/app/services/auth_service.py`
- Create: `nodeskclaw-backend/tests/test_auth_employee_id.py`

**Interfaces:**
- Consumes: `User.employee_id`（Task 1），`RegisterRequest` 签名（Task 2）
- Produces:
  - `register_user(name, employee_id, org_id, password, email, phone, db)` — 新签名
  - `_detect_account_type("12345678")` → `"employee_id"`

- [ ] **Step 1: 写 _detect_account_type 的失败测试**

新建 `nodeskclaw-backend/tests/test_auth_employee_id.py`：

```python
"""工号登录识别 + 注册校验逻辑单测。"""
import pytest
from app.services.auth_service import _detect_account_type


def test_detect_employee_id_8_digits():
    assert _detect_account_type("12345678") == "employee_id"


def test_detect_employee_id_leading_zeros():
    assert _detect_account_type("00000001") == "employee_id"


def test_detect_email():
    assert _detect_account_type("user@example.com") == "email"


def test_detect_phone_intl():
    assert _detect_account_type("+8613812345678") == "phone"


def test_detect_phone_digits_9():
    # 9位数字：不是工号（8位），也不像邮箱，按 phone 识别（满足 7-15 位数字）
    assert _detect_account_type("123456789") == "phone"


def test_detect_username():
    assert _detect_account_type("alice_bob") == "username"


def test_detect_7_digits_is_phone():
    # 7位纯数字：满足 phone 正则（7-15位），不是工号（需精确8位）
    assert _detect_account_type("1234567") == "phone"
```

- [ ] **Step 2: 运行测试，验证失败**

```bash
cd nodeskclaw-backend
uv run pytest tests/test_auth_employee_id.py::test_detect_employee_id_8_digits -v
```

预期：`FAILED`（`_detect_account_type` 未识别 employee_id）。

- [ ] **Step 3: 修改 _detect_account_type 加入工号识别**

打开 `nodeskclaw-backend/app/services/auth_service.py`，找到 `_detect_account_type` 函数（约第 261 行），修改为：

```python
def _detect_account_type(account: str) -> Literal["employee_id", "email", "phone", "username"]:
    if re.match(r"^\d{8}$", account):     # 精确 8 位纯数字 → 工号
        return "employee_id"
    if "@" in account:
        return "email"
    if re.match(r"^\+?\d{7,15}$", account):
        return "phone"
    return "username"
```

同时更新返回类型注解（`Literal` 新增 `"employee_id"`）。

- [ ] **Step 4: 更新 login_with_account 加入工号分支**

找到 `login_with_account` 函数（约第 312 行），在 `account_type == "email"` 分支之前插入：

```python
async def login_with_account(
    account: str, password: str, db: AsyncSession
) -> LoginResponse:
    """Unified account+password login. Detects employee_id / email / phone / username."""
    account_type = _detect_account_type(account)

    if account_type == "employee_id":
        return await _login_by_field(
            account, password, db,
            where_clause=User.employee_id == account,
        )
    if account_type == "email":
        return await login_with_email(account, password, db)
    if account_type == "phone":
        return await _login_by_field(account, password, db, where_clause=User.phone == account)
    return await _login_by_field(account, password, db, where_clause=User.username == account)
```

- [ ] **Step 5: 运行所有检测类型测试，验证通过**

```bash
cd nodeskclaw-backend
uv run pytest tests/test_auth_employee_id.py -v
```

预期：所有 detect 测试 `PASSED`。

- [ ] **Step 6: 修改 register_user 函数签名及逻辑**

找到 `register_user` 函数（约第 509 行），完整替换为：

```python
async def register_user(
    name: str,
    employee_id: str,
    org_id: str,
    password: str,
    email: str | None,
    phone: str | None,
    db: AsyncSession,
) -> LoginResponse:
    """公共注册：校验工号唯一性 + 组织合法性，创建用户并加入所选组织。"""
    from app.models.organization import Organization
    from app.models.org_membership import OrgMembership, OrgRole

    # 校验工号唯一性
    existing_emp = (await db.execute(
        select(User).where(User.employee_id == employee_id, User.deleted_at.is_(None))
    )).scalar_one_or_none()
    if existing_emp:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": 40032,
                "message_key": "errors.auth.employee_id_already_registered",
                "message": "该工号已被注册",
            },
        )

    # 校验邮箱唯一性（如提供）
    if email:
        existing_email = (await db.execute(
            select(User).where(User.email == email, User.deleted_at.is_(None))
        )).scalar_one_or_none()
        if existing_email:
            raise HTTPException(
                status_code=400,
                detail={
                    "error_code": 40030,
                    "message_key": "errors.auth.email_already_registered",
                    "message": "该邮箱已被注册",
                },
            )

    # 校验手机号唯一性（如提供）
    if phone:
        existing_phone = (await db.execute(
            select(User).where(User.phone == phone, User.deleted_at.is_(None))
        )).scalar_one_or_none()
        if existing_phone:
            raise HTTPException(
                status_code=400,
                detail={
                    "error_code": 40031,
                    "message_key": "errors.auth.phone_already_registered",
                    "message": "该手机号已被注册",
                },
            )

    # 校验组织合法性
    org = (await db.execute(
        select(Organization).where(
            Organization.id == org_id,
            Organization.is_active.is_(True),
            Organization.deleted_at.is_(None),
        )
    )).scalar_one_or_none()
    if org is None:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": 40033,
                "message_key": "errors.auth.org_not_found",
                "message": "所选组织不存在或已停用",
            },
        )

    # 创建用户
    user = User(
        name=name,
        employee_id=employee_id,
        email=email or None,
        phone=phone or None,
        password_hash=hash_password(password),
        current_org_id=org.id,
    )
    db.add(user)
    await db.flush()

    # 加入所选组织（member 角色）
    db.add(OrgMembership(
        user_id=user.id,
        org_id=org.id,
        role=OrgRole.member,
    ))
    from app.services.rbac_sync import grant_role
    await grant_role(
        db, subject_type="user", subject_id=user.id,
        role_key="org_member",
        scope_type="org", scope_id=org.id,
        granted_reason="register_auto_join",
    )

    await db.commit()
    await db.refresh(user)

    access_token = create_access_token(user.id)
    refresh_token = create_refresh_token(user.id)

    logger.info("新用户注册: id=%s employee_id=%s org_id=%s", user.id, employee_id, org.id)

    return LoginResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        user=UserInfo(
            id=user.id,
            name=user.name,
            email=user.email,
            phone=user.phone,
            username=user.username,
            avatar_url=user.avatar_url,
            role=user.role,
            is_active=user.is_active,
            is_super_admin=user.is_super_admin,
            has_password=True,
            must_change_password=False,
            current_org_id=user.current_org_id,
            org_role=None,
            portal_org_role=None,
            last_login_at=None,
            oauth_connections=[],
        ),
        needs_org_setup=False,
    )
```

- [ ] **Step 7: 运行全量测试**

```bash
cd nodeskclaw-backend
uv run pytest tests/test_auth_employee_id.py -v
```

预期：全部 `PASSED`。

- [ ] **Step 8: Lint 检查**

```bash
cd nodeskclaw-backend
uv run ruff check app/services/auth_service.py app/api/auth.py
```

如有报错，按提示修复。

- [ ] **Step 9: Commit**

```bash
git add nodeskclaw-backend/app/services/auth_service.py \
        nodeskclaw-backend/tests/test_auth_employee_id.py
git commit -m "feat(backend): 注册改为工号+组织必填，登录新增工号识别"
```

---

## Task 4: 前端 i18n 翻译 Key

**Files:**
- Modify: `nodeskclaw-portal/src/i18n/locales/zh-CN.ts`
- Modify: `nodeskclaw-portal/src/i18n/locales/en-US.ts`

**Interfaces:**
- Produces: `auth.employeeIdLabel`、`auth.employeeIdPlaceholder`、`auth.employeeIdError`、`auth.orgLabel`、`auth.orgPlaceholder`、`auth.orgLoadError`、`auth.accountLoginPlaceholder`（更新）、`auth.namePlaceholder`（更新） — Task 5/6 使用

- [ ] **Step 1: 修改 zh-CN.ts**

打开 `nodeskclaw-portal/src/i18n/locales/zh-CN.ts`，在 `auth` 对象中（`nameLabel`/`emailLabel` 附近）新增以下 key，并修改两个现有 key：

**新增（插入到 `emailLabel` 之后即可）：**
```typescript
employeeIdLabel: "工号",
employeeIdPlaceholder: "请输入 8 位数字工号",
employeeIdError: "工号必须为 8 位数字",
orgLabel: "部门组织",
orgPlaceholder: "请选择所属部门组织",
orgLoadError: "组织列表加载失败，请刷新重试",
```

**修改现有 key：**
```typescript
// 改前
accountLoginPlaceholder: "邮箱、手机号或用户名",
accountPlaceholder: "邮箱、手机号或用户名",
namePlaceholder: "你的名字（可选）",
// 改后
accountLoginPlaceholder: "工号 / 邮箱 / 手机号",
accountPlaceholder: "工号 / 邮箱 / 手机号",
namePlaceholder: "你的姓名",
```

- [ ] **Step 2: 修改 en-US.ts**

同上逻辑，找到相同位置，新增：
```typescript
employeeIdLabel: "Employee ID",
employeeIdPlaceholder: "Enter 8-digit employee ID",
employeeIdError: "Employee ID must be exactly 8 digits",
orgLabel: "Department",
orgPlaceholder: "Select your department",
orgLoadError: "Failed to load organization list, please refresh",
```

修改现有 key：
```typescript
accountLoginPlaceholder: "Employee ID / Email / Phone",
accountPlaceholder: "Employee ID / Email / Phone",
namePlaceholder: "Your full name",
```

- [ ] **Step 3: Commit**

```bash
git add nodeskclaw-portal/src/i18n/locales/zh-CN.ts \
        nodeskclaw-portal/src/i18n/locales/en-US.ts
git commit -m "feat(portal): 新增工号/组织选择相关 i18n key，更新账号登录占位文字"
```

---

## Task 5: 前端 Auth Store 更新

**Files:**
- Modify: `nodeskclaw-portal/src/stores/auth.ts`

**Interfaces:**
- Consumes: 后端 `POST /auth/register`（Task 2/3 新签名），`POST /auth/account-login`
- Produces:
  - `register(name, employeeId, orgId, password, email?, phone?)` — Task 6 调用
  - 移除 `sendVerificationCode`、`verificationCodeLogin`

- [ ] **Step 1: 更新 register 方法签名**

打开 `nodeskclaw-portal/src/stores/auth.ts`，找到 `register` 函数（约第 129 行），替换为：

```typescript
async function register(
  name: string,
  employeeId: string,
  orgId: string,
  password: string,
  email?: string,
  phone?: string,
) {
  const res = await api.post('/auth/register', {
    name,
    employee_id: employeeId,
    org_id: orgId,
    password,
    email: email || undefined,
    phone: phone || undefined,
  })
  const data = res.data.data
  setTokens(data.access_token, data.refresh_token)
  user.value = data.user
  return data
}
```

- [ ] **Step 2: 移除验证码登录相关方法**

删除以下两个函数：
- `sendVerificationCode`（约第 116-119 行）
- `verificationCodeLogin`（约第 121-127 行）

- [ ] **Step 3: 更新 return 导出对象**

找到 `return { ... }` 块，移除 `sendVerificationCode, verificationCodeLogin`，确保 `register` 仍在导出列表中。

改前：
```typescript
return {
  token, refreshToken, user, systemInfo, isLoggedIn,
  setTokens, clearAuth,
  emailLogin, sendSmsCode, smsLogin,
  accountLogin, sendVerificationCode, verificationCodeLogin,
  register,
  fetchSystemInfo, fetchUser, logout,
}
```

改后：
```typescript
return {
  token, refreshToken, user, systemInfo, isLoggedIn,
  setTokens, clearAuth,
  emailLogin, sendSmsCode, smsLogin,
  accountLogin,
  register,
  fetchSystemInfo, fetchUser, logout,
}
```

- [ ] **Step 4: Commit**

```bash
git add nodeskclaw-portal/src/stores/auth.ts
git commit -m "feat(portal): 更新 register store 方法签名，移除验证码登录方法"
```

---

## Task 6: 前端注册页 Register.vue

**Files:**
- Modify: `nodeskclaw-portal/src/views/Register.vue`

**Interfaces:**
- Consumes:
  - `GET /api/v1/auth/orgs` → `[{id, name}]`（Task 2）
  - `authStore.register(name, employeeId, orgId, password, email?, phone?)`（Task 5）
  - i18n key：`auth.employeeIdLabel/Placeholder/Error`、`auth.orgLabel/Placeholder/orgLoadError`（Task 4）

- [ ] **Step 1: 替换 `<script setup>` 块**

打开 `nodeskclaw-portal/src/views/Register.vue`，将整个 `<script setup lang="ts">` 块替换为：

```typescript
<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useAuthStore } from '@/stores/auth'
import { getCurrentLocale, setCurrentLocale } from '@/i18n'
import { resolveApiErrorMessage } from '@/i18n/error'
import { Loader2, Eye, EyeOff, ArrowLeft } from 'lucide-vue-next'
import LocaleSelect from '@/components/shared/LocaleSelect.vue'
import api from '@/services/api'

const router = useRouter()
const route = useRoute()
const authStore = useAuthStore()
const { t } = useI18n()

const loading = ref(false)
const error = ref('')

const form = ref({
  name: '',
  employee_id: '',
  org_id: '',
  email: '',
  phone: '',
  password: '',
})
const showPassword = ref(false)
const locale = ref(getCurrentLocale())

// 组织列表（下拉框数据源）
interface OrgItem { id: string; name: string }
const orgs = ref<OrgItem[]>([])
const orgsLoading = ref(false)
const orgsError = ref('')

// 工号格式校验
const employeeIdValid = computed(() => /^\d{8}$/.test(form.value.employee_id))
// 工号输入过且格式不对时才显示错误提示，避免初始状态误报
const showEmployeeIdError = computed(() =>
  form.value.employee_id.length > 0 && !employeeIdValid.value
)

const canSubmit = computed(() =>
  form.value.name.trim().length > 0 &&
  employeeIdValid.value &&
  form.value.org_id.length > 0 &&
  form.value.password.length >= 6
)

async function loadOrgs() {
  orgsLoading.value = true
  orgsError.value = ''
  try {
    const res = await api.get('/auth/orgs')
    orgs.value = res.data.data ?? []
  } catch {
    orgsError.value = t('auth.orgLoadError')
  } finally {
    orgsLoading.value = false
  }
}

onMounted(loadOrgs)

async function handleSubmit() {
  if (!canSubmit.value || loading.value) return
  loading.value = true
  try {
    await authStore.register(
      form.value.name,
      form.value.employee_id,
      form.value.org_id,
      form.value.password,
      form.value.email || undefined,
      form.value.phone || undefined,
    )
    error.value = ''
    router.replace('/')
  } catch (e: any) {
    error.value = resolveApiErrorMessage(e, t('auth.registerFailed'))
  } finally {
    loading.value = false
  }
}

function onLocaleChange(value: string) {
  locale.value = setCurrentLocale(value)
}
</script>
```

- [ ] **Step 2: 替换表单 HTML 部分**

在 `<template>` 中找到 `<form class="space-y-4" @submit.prevent="handleSubmit">` 块，替换其内部全部字段为：

```html
<form class="space-y-4" @submit.prevent="handleSubmit">
  <!-- 姓名 -->
  <div class="space-y-1.5">
    <label class="text-sm font-medium text-foreground">{{ t('auth.nameLabel') }}</label>
    <input
      v-model="form.name"
      type="text"
      :placeholder="t('auth.namePlaceholder')"
      required
      class="w-full h-10 px-3 rounded-lg border border-input bg-background text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-1 transition-shadow"
    />
  </div>

  <!-- 工号 -->
  <div class="space-y-1.5">
    <label class="text-sm font-medium text-foreground">{{ t('auth.employeeIdLabel') }}</label>
    <input
      v-model="form.employee_id"
      type="text"
      inputmode="numeric"
      maxlength="8"
      :placeholder="t('auth.employeeIdPlaceholder')"
      required
      class="w-full h-10 px-3 rounded-lg border border-input bg-background text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-1 transition-shadow"
      :class="showEmployeeIdError ? 'border-destructive focus:ring-destructive' : ''"
    />
    <p v-if="showEmployeeIdError" class="text-xs text-destructive">{{ t('auth.employeeIdError') }}</p>
  </div>

  <!-- 部门组织 -->
  <div class="space-y-1.5">
    <label class="text-sm font-medium text-foreground">{{ t('auth.orgLabel') }}</label>
    <select
      v-model="form.org_id"
      required
      class="w-full h-10 px-3 rounded-lg border border-input bg-background text-sm focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-1 transition-shadow"
      :class="orgsError ? 'border-destructive' : ''"
    >
      <option value="" disabled>
        {{ orgsLoading ? '加载中...' : t('auth.orgPlaceholder') }}
      </option>
      <option v-for="org in orgs" :key="org.id" :value="org.id">{{ org.name }}</option>
    </select>
    <p v-if="orgsError" class="text-xs text-destructive">{{ orgsError }}</p>
  </div>

  <!-- 密码 -->
  <div class="space-y-1.5">
    <label class="text-sm font-medium text-foreground">{{ t('auth.passwordLabel') }}</label>
    <div class="relative">
      <input
        v-model="form.password"
        :type="showPassword ? 'text' : 'password'"
        :placeholder="t('auth.passwordPlaceholder')"
        required
        minlength="6"
        class="w-full h-10 px-3 pr-10 rounded-lg border border-input bg-background text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-1 transition-shadow"
      />
      <button
        type="button"
        tabindex="-1"
        class="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground transition-colors"
        @click="showPassword = !showPassword"
      >
        <EyeOff v-if="showPassword" class="w-4 h-4" />
        <Eye v-else class="w-4 h-4" />
      </button>
    </div>
    <p class="text-xs text-muted-foreground">{{ t('auth.passwordMinLength') }}</p>
  </div>

  <!-- 邮箱（可选） -->
  <div class="space-y-1.5">
    <label class="text-sm font-medium text-foreground">{{ t('auth.emailLabel') }}<span class="text-muted-foreground text-xs ml-1">（可选）</span></label>
    <input
      v-model="form.email"
      type="email"
      inputmode="email"
      :placeholder="t('auth.emailPlaceholder')"
      class="w-full h-10 px-3 rounded-lg border border-input bg-background text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-1 transition-shadow"
    />
  </div>

  <!-- 手机号（可选） -->
  <div class="space-y-1.5">
    <label class="text-sm font-medium text-foreground">{{ t('auth.phoneLabel') }}</label>
    <input
      v-model="form.phone"
      type="tel"
      inputmode="tel"
      :placeholder="t('auth.phonePlaceholder')"
      class="w-full h-10 px-3 rounded-lg border border-input bg-background text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-1 transition-shadow"
    />
  </div>

  <button
    type="submit"
    :disabled="!canSubmit || loading"
    class="w-full h-10 rounded-lg bg-primary text-primary-foreground font-medium text-sm hover:bg-primary/90 transition-all hover:shadow-lg hover:shadow-primary/20 disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2"
  >
    <Loader2 v-if="loading" class="w-4 h-4 animate-spin" />
    {{ t('auth.register') }}
  </button>
</form>
```

- [ ] **Step 3: 验证前端编译无报错**

```bash
cd nodeskclaw-portal
npm run build 2>&1 | tail -20
```

预期：无 TypeScript 编译错误。如有报错，按提示修复。

- [ ] **Step 4: Commit**

```bash
git add nodeskclaw-portal/src/views/Register.vue
git commit -m "feat(portal): 注册页新增工号输入框和组织下拉选择，邮箱改为可选"
```

---

## Task 7: 前端登录页 Login.vue — 移除验证码 Tab

**Files:**
- Modify: `nodeskclaw-portal/src/views/Login.vue`

**Interfaces:**
- Consumes: `authStore.accountLogin`（不变），i18n `auth.accountLoginPlaceholder`（Task 4 已更新）

- [ ] **Step 1: 删除验证码相关 script 代码**

打开 `nodeskclaw-portal/src/views/Login.vue`，在 `<script setup>` 中删除以下内容：

1. 删除 `import { ..., MessageSquareCode, ... }` 中的 `MessageSquareCode`（保留其他图标）
2. 删除 `const activeTab = ref<'account' | 'code'>('account')`
3. 删除 `const codeForm = ref({ account: '', code: '' })`
4. 删除 `const codeSending = ref(false)`、`const codeCountdown = ref(0)`、`let codeTimer` 三行
5. 删除 `const canSubmitCode = computed(...)` 函数
6. 删除 `isEmailInput` 函数
7. 删除 `handleSendCode` 函数
8. 删除 `handleCodeSubmit` 函数
9. 删除 `watch(activeTab, ...)` 行

- [ ] **Step 2: 删除验证码相关 template 代码**

在 `<template>` 中：

1. 删除整个 Tab 切换 `<div class="flex rounded-lg bg-muted p-1 gap-1">` 块（含两个 button）
2. 删除条件判断 `v-if="activeTab === 'account'"` — 保留表单本身，只去掉这个条件属性
3. 删除整个 `<form v-if="activeTab === 'code'" ...>` 块（验证码表单，从 `<!-- 验证码表单 -->` 注释到对应 `</form>`）

- [ ] **Step 3: 更新账号字段 placeholder**

找到账号输入框（`v-model="accountForm.account"`），确认 placeholder 绑定是 `t('auth.accountLoginPlaceholder')`。i18n 在 Task 4 已更新为"工号 / 邮箱 / 手机号"，此处无需改代码。

- [ ] **Step 4: 验证前端编译无报错**

```bash
cd nodeskclaw-portal
npm run build 2>&1 | tail -20
```

预期：无错误。

- [ ] **Step 5: 启动前端，手动测试登录页**

```bash
cd nodeskclaw-portal
npm run dev
```

打开浏览器访问登录页：
- 确认只有一个登录表单（无 Tab 切换）
- 账号字段 placeholder 显示"工号 / 邮箱 / 手机号"
- 能用工号+密码正常登录（需已注册工号用户）

- [ ] **Step 6: 手动测试注册页**

访问注册页：
- 确认字段顺序：姓名 → 工号 → 部门组织（下拉）→ 密码 → 邮箱（可选）→ 手机号（可选）
- 工号输入不满 8 位时，下方显示"工号必须为 8 位数字"
- 组织下拉从后端加载（需后端运行中）
- 邮箱不填也能提交

- [ ] **Step 7: Commit**

```bash
git add nodeskclaw-portal/src/views/Login.vue
git commit -m "feat(portal): 登录页移除验证码 Tab，账号框提示改为工号/邮箱/手机号"
```

---

## 自审检查清单

- [x] **Spec 覆盖**：工号必填（Task 1/2/3/6）✓，工号 8 位校验（Task 3/6）✓，工号唯一性（Task 1/3）✓，部门组织必选（Task 2/3/6）✓，注册加入组织（Task 3）✓，邮箱可选（Task 2/6）✓，公开组织列表接口（Task 2）✓，登录工号识别（Task 3）✓，去掉验证码 Tab（Task 7）✓，启动默认组织（已由 seed.py 覆盖，无需额外任务）✓
- [x] **类型一致性**：`register_user(name, employee_id, org_id, password, email, phone, db)` 在 Task 3 定义，Task 2 的端点调用参数一致；`register(name, employeeId, orgId, password, email?, phone?)` 在 Task 5 定义，Task 6 调用一致
- [x] **无占位符**：所有步骤含实际代码，无 TBD/TODO
