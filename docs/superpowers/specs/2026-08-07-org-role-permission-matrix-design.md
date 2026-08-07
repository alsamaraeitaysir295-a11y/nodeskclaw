# 组织三级角色跨模块权限矩阵设计文档

**日期**：2026-08-07
**状态**：待用户最终确认

---

## 背景

组织角色体系（`OrgMembership.role`，定义于 `nodeskclaw-backend/app/models/org_membership.py`）本来就是 member/operator/admin 三级，但目前只在极少数地方真正按三级分层校验——绝大多数后端端点用的是二元判断（`require_org_admin`：admin/非-admin），operator 这一级角色在实际鉴权逻辑里几乎完全是摆设，仅作为数据库枚举值存在。

现有鉴权分裂成三套并行体系，语义互不相通：

- **组织级**：`OrgRole` 三级枚举，但校验只做 admin/非-admin 二元判断
- **工作区级**：`WORKSPACE_USE_PERMISSIONS`/`WORKSPACE_CONFIG_PERMISSIONS` 字符串权限位（`workspace_member_service.py`），CONFIG 权限只认 `org_role == admin` 或创建者
- **实例级**：独立的 `InstanceRole` 枚举（viewer/user/editor/admin，`instance_member.py`），`check_instance_access` 同样只认 `org_role == admin` 或创建者，`InstanceMember` 表里设置的角色不影响实际权限判断（模型和管理 UI `InstanceMembers.vue` 都在，但形同虚设）

本次要把这三套体系统一收敛成**纯组织三级角色驱动**：不再有按单个资源逐一授权（`InstanceMember`/`WorkspaceMember` 的角色字段不再作为权限判断依据），只要是组织的 operator，就能管理该组织下所有工作区和实例；admin 拥有全部权限。同时把这套权限矩阵扩展到外部 Agent、知识库、Gene 审核中心、组织设置页几个此前从未做三级区分的模块。

不在本次范围内：技能市场（Gene 库）按 ownership 的现有逻辑不变；超管后台（`is_super_admin`/`AdminMembership`）继续维持独立体系不变，不接入本矩阵。

## 权限矩阵

| 模块 | member | operator | admin |
|---|---|---|---|
| 工作区（AI员工写作空间） | 使用（`send_chat`） | + 编排空间内实例（`manage_agents`/`edit_topology`）、中央黑板编辑（`edit_blackboard`）、**创建工作区** | + `manage_settings`、`manage_members`、`delete_workspace` |
| AI员工实例 | 读（详情/日志/部署历史）、对话聊天、发布自动化任务 | + 编辑实例详情（改名/扩缩容/重启/改配置/回滚）、**创建实例** | + 删除实例 |
| 外部Agent | 读（list/detail）、聊天 | + 创建/编辑/同步/建立链接 | + 删除 |
| 知识库 | 读（list/detail，当前是 admin 专属，本次放宽） | + 创建/编辑/同步/建立链接 | + 删除 |
| Gene 审核中心 | 不可见 | 可见 + 审核操作 | 全部 |
| 技能市场（Gene库其余功能） | 不变（按现有 ownership 逻辑，不接入本矩阵） | 不变 | 不变 |
| 组织设置 | 仅"组织信息""人类成员""LLM用量分析"三个子页，均只读 | 全部子页可访问，可管理成员角色，**但不能把任何人（含自己）设为 admin** | 全部 |
| 超管后台 | 不可见 | 不可见 | 不变（`is_super_admin`/`AdminMembership` 独立体系，不接入本矩阵） |

前后端都要落实这套矩阵（不只是前端隐藏按钮，后端 API 同步加校验，防止绕过前端直接调用）。

## 关键澄清

- **实例"读/使用"档明确包含对话聊天和发布自动化任务**：`automation_tasks.py: create_automation_task`（约149行）保持"任何组织成员可用"，不提升门槛；但会顺带修补一个已发现的真实漏洞——该端点目前只检查实例是否存在，完全没检查发起用户是否属于该实例所在组织，本次会补上组织归属校验（不算权限收紧，是堵一个跨组织越权漏洞）。
- **创建门槛统一为 operator+**：工作区创建（`workspaces.py: create_workspace`，约102行，目前只要求登录+同组织，无角色区分）和实例创建（`deploy.py: deploy`）都要求 operator 及以上，member 只能使用已有的工作区/实例，不能新建。
- **创建者豁免逻辑直接废除**：由于创建工作区/实例本身已要求 operator+，不会再出现"member 创建了东西却没权限管理"的场景，`check_workspace_access`/`check_instance_access` 里现有的"`created_by == user.id` 也放行"分支本次一并去掉，改成纯组织角色判断。
- **邀请流程维持不变**：邀请功能使用率低，用户确认以自助注册为主，不需要为邀请对话框开放 operator 选项，邀请仍只能选 member/admin。
- **`InstanceMember` 表和 `InstanceMembers.vue` 管理入口成为摆设**：角色判断改成纯组织角色驱动后，这张表的角色字段不再被任何鉴权逻辑读取。为避免管理员在这个页面设置了角色却发现不生效造成误解，本次会隐藏/移除 `InstanceMembers.vue` 的角色管理入口（保留数据表本身，不做删表操作）。
- **组织自助设置页要撤销上一轮刚提交的"operator 禁用下拉"逻辑**：`OrgMembers.vue` 此前（commit `8a6f93f`）把 operator 角色成员的角色下拉禁用、仅展示徽章，理由是"当时不开放自助管理"。本次要反转这个决定：重新启用该下拉，让它可选 member/operator/admin 三档，实际写入受后端"操作者不能设置任何人为 admin"的能力上限校验保护（管理员自己不受此限制）。
- **Gene 审核中心的鉴权底座需要切换**：目前 `Approvals.vue` 对接的 `pending-review` 列表走的是"任何登录用户 + service 层按 `org_role == admin` 过滤"（`gene_service.get_pending_review_genes`），要把 operator 也纳入"能看到本组织列表"的条件；而 `genes.py` 里其余 `/admin/genes/*`（stats/activity/matrix/list/create/update/delete 等）挂的是 `require_org_role("admin")`，这个依赖查的是**完全独立的 `AdminMembership` 表**（EE 管理后台专用），不是 `OrgMembership`——如果审核中心的具体审核操作（通过/驳回）落在这批端点里，需要把对应端点从 `AdminMembership` 鉴权切换成 `require_org_member_role`（`OrgMembership`）。实现阶段需要先精确定位 `Approvals.vue` 的"审核通过/驳回"按钮实际调用的是哪个端点，再决定具体切哪几个。

## 技术实现方式

### 后端

- `deps.py` 里已存在但零调用的 `require_org_member_role(min_role)`（约246行）是本次的核心接入点，直接复用，不新增机制。
- `workspace_member_service.py`：`WORKSPACE_USE_PERMISSIONS`/`WORKSPACE_CONFIG_PERMISSIONS` 两档拆成三档（member/operator/admin 各自的权限集合），`check_workspace_access` 改成按等级比较，去掉创建者豁免分支。
- `instance_member_service.py`：`check_instance_access` 改成按 `OrgRole` 等级比较（原来的 viewer/editor/admin 语义映射为 member/operator/admin），去掉 `InstanceMember` 表查询和创建者豁免分支。
- `external_agents.py`/`knowledge_bases.py`：list 类端点从 `require_org_admin` 降到 `require_org_member_role("member")`；create/update/sync 降到 `require_org_member_role("operator")`；delete 保持 `require_org_admin`（等价于 `require_org_member_role("admin")`）。
- `workspaces.py: create_workspace`、`deploy.py: deploy`：加 `require_org_member_role("operator")`。
- `automation_tasks.py: create_automation_task`：补组织归属校验（实例所属 org 必须等于当前用户所在 org），不提升角色门槛。
- `organizations.py: update_member_role`：门槛从 `require_org_admin` 降到 `require_org_member_role("operator")`；`org_service.update_member_role` 内部新增校验——若操作者自身角色是 `operator`（非 `admin`/超管），且目标 `role` 参数为 `admin`，拒绝并返回 403。
- Gene 审核中心相关端点：按上文"关键澄清"定位后切换鉴权依赖。

### 前端（`nodeskclaw-portal`）

- 新建角色等级比较工具（镜像后端 `ADMIN_ROLE_LEVEL`），替换掉散落各处的 `portal_org_role === 'admin'` 字符串判断（`App.vue`、`router/index.ts`、`ExternalAgentList.vue`、`CreateInstance.vue` 等已发现的现存判断点）。
- 路由 meta 从布尔 `requireAdminOrSuper` 扩展为分级字段（如 `requiredOrgRole: 'operator' | 'admin'`），`router/index.ts` 守卫改按等级比较；`requireSuperAdmin`（超管后台）不受影响，维持原样。
- `OrgSettings.vue` 的 `NavItem`（约14-20行）新增 `minRole` 字段，`navItems` 过滤逻辑（约41-46行）加角色判断，驱动侧边栏子页面隐藏；各子页面内部原有的 admin-only 判断同步改成等级判断。
- `WorkspaceView.vue`/`WorkspaceSettings.vue` 已有的 `hasPermission()` 位图模式（`stores/workspace.ts`）保留同样的调用方式，只是后端 `/workspaces/{id}/my-permissions` 返回的权限集合按新的三档拆分逻辑计算，前端不需要改判断模式本身。
- `OrgMembers.vue`：撤销 commit `8a6f93f` 里对 operator 角色的下拉禁用逻辑，重新启用下拉并加入 `operator` 选项（角色列表来源 `DefaultRoleProvider.get_roles()` 需要补上 operator）。
- `InstanceMembers.vue`：隐藏角色管理入口（说明见上文"关键澄清"）。
- `ExternalAgentList.vue`、知识库列表/表单页、`Approvals.vue`：按新等级模型调整按钮/页面可见性。

### 测试

- 后端：针对每个改动端点补充三档角色（member/operator/admin）的鉴权测试用例，覆盖"应放行"和"应拒绝"两种场景；`update_member_role` 单独覆盖"operator 尝试设置 admin 被拒绝"的用例。
- 前端：由于本地环境缺少 node/npm（已在此前会话中确认），无法运行 `npm run dev`/`vue-tsc`/`npm run test` 做实机验证，实现阶段会如实告知这一限制，不代替用户做浏览器验证。

## 不在本次范围内

- 技能市场（Gene 库）的 ownership 逻辑（fork/上传/删除权限）不改动。
- 超管后台（`is_super_admin`/`AdminMembership`）继续维持独立体系，不与本矩阵合并。
- 实例文件访问（PodFS/DockerFS 的 list/read/write/download）维持现状全部要求最高权限，不做读写分级。
- `InstanceMember`/`WorkspaceMember` 数据表本身不删除，只是角色字段不再被鉴权逻辑读取；`InstanceMembers.vue` 管理入口隐藏但不删除代码，便于后续如有需要恢复。
- RBAC phase1 影子体系（`app/models/rbac/*`）不涉及，本次改动完全基于现有 `OrgMembership.role` 加深使用，不迁移到该体系。
