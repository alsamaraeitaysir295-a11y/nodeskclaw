# 注册登录功能改造设计文档

**日期**：2026-08-05  
**状态**：已确认  
**范围**：`nodeskclaw-backend` + `nodeskclaw-portal`

---

## 背景与目标

将注册表单由"邮箱为主"改为"工号为主"，适配企业内部用户体系：工号唯一标识用户，邮箱/手机号降为可选信息。同步简化登录页，去除验证码登录 Tab，统一为账号+密码单一入口。注册时强制选择所属部门组织，注册成功后自动以 `member` 角色加入。

---

## 一、数据库变更

### 1.1 User 表新增 employee_id 字段

```python
employee_id: Mapped[str | None] = mapped_column(String(8), nullable=True)
```

新增部分唯一索引，仅约束未软删且非 NULL 的行：

```python
Index(
    "uq_users_employee_id", "employee_id",
    unique=True,
    postgresql_where=text("deleted_at IS NULL AND employee_id IS NOT NULL"),
)
```

### 1.2 修改 email / phone 唯一约束

**现状**：列级 `unique=True`（全表硬约束），多 NULL 会被视为重复值，无法支持可选邮箱场景。

**变更**：去掉列级 `unique=True`，分别改为部分唯一索引：

```python
# email
Index("uq_users_email", "email", unique=True,
      postgresql_where=text("deleted_at IS NULL AND email IS NOT NULL"))

# phone
Index("uq_users_phone", "phone", unique=True,
      postgresql_where=text("deleted_at IS NULL AND phone IS NOT NULL"))
```

### 1.3 Alembic 迁移

执行 `alembic revision --autogenerate -m "add_employee_id_fix_email_phone_unique"` 生成迁移，涵盖以上三处变更，不手写 revision ID。

---

## 二、后端 API 变更

### 2.1 新增公开组织列表接口

```
GET /api/v1/auth/orgs
```

- **认证**：无需
- **返回**：所有 `is_active=True` 且 `deleted_at IS NULL` 的组织列表，每项包含 `id`、`name`
- **用途**：注册页下拉框数据源

### 2.2 注册请求 Schema（`RegisterRequest`）

```python
class RegisterRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)       # 必填：姓名
    employee_id: str = Field(pattern=r"^\d{8}$")          # 必填：8位纯数字工号
    org_id: str                                            # 必填：所属组织 ID
    password: str = Field(min_length=6, max_length=200)   # 必填：密码
    email: EmailStr | None = None                         # 可选：邮箱
    phone: str | None = None                              # 可选：手机号
```

### 2.3 注册 Service 逻辑（`auth_service.register_user()`）

变更点：

1. **工号唯一性校验**：查询 DB 是否已存在相同 `employee_id`（软删过滤），冲突时抛出错误码 `EMPLOYEE_ID_ALREADY_EXISTS`
2. **组织合法性校验**：校验 `org_id` 对应组织存在且 `is_active=True`，失败时抛出 `ORG_NOT_FOUND`
3. **自动加入组织**：注册成功后调用 `org_service.add_member(org_id, user_id, role="member")`，取代原有自动加入 `slug="default"` 逻辑

### 2.4 登录账号识别（`auth_service` 账号类型判断）

在现有识别链（email → phone → username）前插入：

- 输入为**纯 8 位数字** → 按 `employee_id` 字段查询用户

最终识别顺序：`employee_id（8位数字）→ email → phone → username`

### 2.5 启动时默认组织保证（`app/main.py` lifespan）

应用启动时检查 `organizations` 表是否存在任意激活组织，若无则自动创建：

```python
{"name": "默认部门", "slug": "default", "plan": "free"}
```

---

## 三、前端变更

### 3.1 注册页（`nodeskclaw-portal/src/views/Register.vue`）

**表单字段**：

| 字段 | 类型 | 必填 | 变化 |
|------|------|------|------|
| 姓名 | 文本 | 是 | 不变 |
| 工号 | 文本 | 是 | **新增** |
| 部门组织 | 下拉 | 是 | **新增** |
| 密码 | 密码 | 是 | 不变 |
| 邮箱 | 邮箱 | 否 | 改为可选 |
| 手机号 | 电话 | 否 | 不变 |

**工号校验**：输入不满足 `^\d{8}$` 时，字段下方显示"工号必须为 8 位数字"，且 `canSubmit` 返回 false。

**组织下拉**：页面 `onMounted` 时调用 `GET /auth/orgs`，结果填充 `<select>` 选项，加载失败时展示错误提示。

**canSubmit 逻辑**：

```typescript
const canSubmit = computed(() =>
  form.value.name &&
  /^\d{8}$/.test(form.value.employee_id) &&
  form.value.org_id &&
  form.value.password.length >= 6
)
```

**提交调用**：传递 `{ name, employee_id, org_id, password, email?, phone? }`。

### 3.2 登录页（`nodeskclaw-portal/src/views/Login.vue`）

- **去掉**「邮件验证码登录」Tab 及相关 UI，整体改为单一账号+密码表单
- 账号字段 `placeholder` 改为"工号 / 邮箱 / 手机号"
- 表单结构保持 `account + password`，无结构性变动

### 3.3 authStore / api.ts

- 注册方法签名更新，传入新字段
- 删除 `verificationCodeLogin`、`sendVerificationCode` 相关方法

---

## 四、错误码

| 错误码 | 触发场景 |
|--------|---------|
| `EMPLOYEE_ID_ALREADY_EXISTS` | 工号已被其他用户注册 |
| `ORG_NOT_FOUND` | 提交的 org_id 不存在或已停用 |

---

## 五、不在本次范围内

- Admin 后台（`ee/nodeskclaw-frontend`）的用户创建表单暂不改动
- 邀请链接注册（`AcceptInvite.vue`）流程不受影响
- 组织申请加入流程（`JoinOrganization.vue`）不变
- SMS 验证码登录保持现状（后端标注 503，未接入）
