# 组织级功能开关补全设计

## Context

超管后台"功能开关"目前只有 13 个 feature，覆盖的多是零散的 EE 能力（超管后台本身、多组织管理、SSO 等），完全没有覆盖公司实际在用的核心模块：协作空间（Workspace）、AI 员工实例（Instance）、技能市场（Gene Market）、自动化任务、外部 Agent、知识库。用户希望能通过这套功能开关按组织管控这 6 个模块的可用性。

排查代码后发现一个比"缺注册"更底层的问题：**组织级功能覆盖（`OrganizationFeatureOverride` 表 + 超管后台"强制开/强制关"）目前对系统运行完全不起作用**：
- 后端路由门禁 `require_feature(feature_id)`（`app/core/deps.py:432`）只调用 `feature_gate.is_enabled(feature_id)`，这个函数只看 edition（CE/EE），不看 org_id，也不查 override 表。
- 前端 `useFeature()` 的数据源 `GET /system/info` 同样是纯 edition 级返回，跟当前登录用户/组织无关。
- 唯一会查 override 表的函数 `is_enabled_for_org()`（`feature_gate.py:103`）在全代码库里**从未被调用**，是个孤儿函数。

也就是说，现有 13 个 feature 的组织级覆盖同样是摆设，这次要一并修好。

用户已确认：当前用途是公司内部自用，**不需要区分 CE/EE 版本**，新模块直接按现有 13 个的方式加进 `features.yaml` 同一份列表即可，不用引入"EE 专属 vs 核心功能"的分类概念。

## 架构方案

### 1. 打通组织级 override 的运行时生效

**`require_feature()` 改造**（`app/core/deps.py`）：

```python
def require_feature(feature_id: str):
    async def _check_feature(
        request: Request,
        db: AsyncSession = Depends(get_db),
        user=Depends(_get_current_user_dep()),
    ):
        org_id = request.path_params.get("org_id") or getattr(user, "current_org_id", None)
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

直接复用现成的 `is_enabled_for_org()`（`app/core/feature_gate.py:103`：先查 org override，没有覆盖则退回 `feature_gate.is_enabled()` 的 edition 默认值）。org_id 优先取 URL path 参数（跟 `require_org_member_role` 现有取法一致），没有的话退回当前用户的 `current_org_id`。

这个改动影响**所有**已经在用 `require_feature()` 的路由（现有 13 个 feature），不只是新加的 6 个——这是有意为之，一并修好同样的问题。

### 2. `GET /system/info` 按组织合并覆盖

`app/api/router.py:77` 的 `system_info()` 目前无需认证、返回纯 edition 级 feature 列表。前端只在已登录（有 token）时才调用它（`router/index.ts:247`），所以改造成"能解析出登录用户就按其 `current_org_id` 合并 override，解析不出就退回原来的 edition 默认值"，不引入新接口、不影响任何未登录场景：

```python
@api_router.get("/system/info", tags=["系统"])
async def system_info(
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),  # 新增：可选认证
):
    features = feature_gate.all_features()
    if user and user.current_org_id:
        for f in features:
            f["enabled"] = await is_enabled_for_org(f["id"], user.current_org_id, db)
    return {
        "edition": feature_gate.edition,
        "version": settings.APP_VERSION,
        "features": features,
    }
```

需要新增一个"可选认证"依赖 `get_current_user_optional`（Authorization header 存在且合法就返回 User，否则返回 None，不抛异常）——`app/core/security.py` 里现有的 `get_current_user`/`get_current_user_unchecked` 都是"必须登录，没有就 401"，这里需要一个不强制的版本。

返回结构（`{edition, version, features}`）不变，`features` 数组每项的 `enabled` 字段语义从"edition 默认值"变成"当前组织的有效值"，前端 `useFeature.ts` 不需要改代码。

### 3. 新增 6 个 feature 注册（`features.yaml`）

和现有 13 个一样直接加进 `edition_features.ee` 列表（复用同一套结构，不引入新分类）：

| feature_id | name | 门控的后端路由 | 门控的前端入口 |
|---|---|---|---|
| `workspace` | 协作空间 | `app/api/workspaces.py` | `App.vue` 顶部导航"协作空间"按钮 + `/` 路由 |
| `instance` | AI 员工实例 | `app/api/instances.py` + `app/api/portal/instances.py` | `App.vue` 顶部导航"实例"按钮 + `/instances` 路由 |
| `gene_market` | 技能市场 | `app/api/genes.py` | `App.vue` 顶部导航"技能市场"按钮 + `/gene-market` 路由 |
| `automation` | 自动化任务 | `app/api/portal/automation_tasks.py` | `App.vue` 顶部导航"自动化"按钮 + `/automation` 路由 |
| `external_agent` | 外部 Agent | `app/api/external_agents.py` | `App.vue` 顶部导航"Agent"按钮 + `/agents` 路由 |
| `knowledge_base` | 知识库 | `app/api/knowledge_bases.py` | `App.vue` 顶部导航"知识库"按钮 + `/admin/knowledge-bases` 路由 |

**范围边界（有意不做的部分）**：`genes.py` 同时承载"技能市场"（浏览/发布/fork 公共基因）和"给实例装卸基因"的机制——本次按整个 `genes.py` 路由统一门控，不拆分成"市场"和"实例基因管理"两个更细的 feature，接受"关闭技能市场=连实例装卸基因也关闭"这个耦合，避免过度设计。同理，`blackboard.py`/`corridors.py`/`conversations.py`/`trust.py`/`instance_members.py`/`instance_files.py`/`mcp.py`/`channel_configs.py`/`templates.py` 等工作区/实例相关的周边子路由本次**不**级联挂 `workspace`/`instance` 门控，只挂各自模块的主入口路由文件，避免一次改动的爆炸半径过大。

### 4. 后端路由挂载

6 个文件的 `router = APIRouter()` 改成 `router = APIRouter(dependencies=[Depends(require_feature("xxx"))])`，与 `org_join_requests.py`/`org_leave_requests.py` 现有写法一致。`instances.py`/`portal/instances.py` 两个文件都要挂（分别对应管理端和 Portal 端两套路由）。

### 5. 前端门控

- **路由级**：给 6 个路由（`WorkspaceList`/根路由、`Instances`、`GeneMarket`、`Automation`、`Agents`、`AdminKnowledgeBaseList` 等，具体路由 name 以 `router/index.ts` 实际定义为准）的 `meta` 加 `requireFeature: 'xxx'`，复用现有 `beforeEach` 里已经在跑的 `requireFeature` 判断逻辑（`router/index.ts:268-274`），未开通直接重定向首页。
- **导航级**：`App.vue` 的 6 个顶部导航按钮各自加 `useFeature('xxx').isEnabled` 判断，不满足则不渲染该按钮（`v-if`），参照 `OrgSettings.vue` 里 `navItems` 按 feature 过滤的现有写法。

## 数据流

1. 超管在 `AdminOrgDetail.vue` 的"功能开关"tab 给某组织"强制关"`knowledge_base`（现有 UI，不用改）→ 写入 `OrganizationFeatureOverride(org_id, feature_id="knowledge_base", enabled=False)`。
2. 该组织用户下次登录/刷新 → `GET /system/info` 检测到 token → 解析 `current_org_id` → 对 `knowledge_base` 调用 `is_enabled_for_org` → 查到 override → 返回 `enabled: false`。
3. 前端 `useFeature('knowledge_base').isEnabled` 变为 `false` → `App.vue` 的"知识库"导航按钮消失，直接访问 `/admin/knowledge-bases` 会被路由守卫重定向首页。
4. 即使绕过前端直接调后端 `GET/POST .../knowledge-bases`，`require_feature("knowledge_base")` 依赖会用同一个 `is_enabled_for_org` 查出 `False`，返回 403。

## 测试

- 后端：新增/扩展 `feature_gate.py`/`require_feature` 的单测，覆盖"无 override 退回 edition 默认""有 override 覆盖 edition 默认""org_id 从 path 参数取""org_id 从 current_org_id 取"四种场景；`system_info` 的可选认证合并逻辑单测。
- 前端：`npm run build` 验证路由/nav 改动无类型错误；如有条件用 `/run` 手动验证一次"超管关闭某组织的知识库 feature → 该组织用户导航栏消失 + 直接访问 URL 被重定向 + 直接调 API 返回 403"的完整链路。
