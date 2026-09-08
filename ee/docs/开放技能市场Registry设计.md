# 开放技能市场 Registry 设计

## 背景与目标

DeskClaw 平台的技能市场（Gene 市场）目前只在门户登录态内可见。目标是让平台成为**开放的数据提供方**：外部系统（WorkBuddy、其他智能体平台、其他 DeskClaw 实例）无需账号即可发现、获取公共技能，使市场成为开放公共资源。

### 需求决策记录

| 决策点 | 结论 |
|---|---|
| 外部消费者 | WorkBuddy、其他智能体平台等异构 HTTP 客户端 |
| 认证 | 完全开放匿名：仅 public 技能可读；org_private/personal 仍走门户账号体系（现有端点已覆盖） |
| API 范围 | 纯只读（搜索/详情/manifest/下载），不接收外部回写 |
| 被知晓方式 | llms.txt 自描述 + market-client 元技能；**MCP 面本期不做**（YAGNI，等有明确 MCP 原生消费者再上） |

## 方案选型

- **方案 A（选定）**：独立开放 registry 面，挂 `/registry` 前缀，路径与 GeneHub 协议完全一致。一次实现同时服务两类消费者：任意 HTTP 客户端直接调；其他 DeskClaw 平台把 `GENEHUB_REGISTRY_URL` 配成 `https://<host>/registry` 即可用现成 `GeneHubAdapter` 把本平台当 registry 源（其 URL 拼法为 `{base_url}/api/v1/genes`，响应解析要求 `{code:0, data}` 与现有 `ApiResponse` 天然一致）。
- 方案 B（否决）：门户 `/api/v1/open/*` 免登录子集——路径与 GeneHub 协议对不上，外部 DeskClaw 无法即插即用。
- 方案 C（否决）：仅免登录 ZIP 直链——无发现能力，不构成"数据提供方"。

## 架构

新增 `nodeskclaw-backend/app/api/open_registry.py`（免登录 router），在 `main.py` 挂载：

```python
app.include_router(open_registry_router, prefix="/registry")
```

与门户 `/api/v1/*`（`get_current_user` 登录态）完全隔离，零路径冲突。纯只读、无状态、匿名访问。CORS 由 app 级 `CORSMiddleware` 覆盖（`CORS_ORIGINS` 配置控制浏览器端跨域）。

## API 端点（6 个）

统一响应包装 `ApiResponse`（`{code:0, message:"success", data:...}`）；列表分页形状 `{items: [...], total}`。

| 端点 | 作用 | 参数 |
|---|---|---|
| `GET /registry/api/v1/genes` | 搜索列表 | `q`（关键词）/ `tags` / `category` / `sort`（`popular`\|`rating`\|`newest`）/ `page` / `page_size`（默认 20，**上限 100**，防外部大分页拖库） |
| `GET /registry/api/v1/genes/tags` | 标签聚合 | 无 |
| `GET /registry/api/v1/genes/featured` | 精选列表 | `limit`（默认 10） |
| `GET /registry/api/v1/genes/{slug}` | 详情（含 dependencies/synergies） | 可选 `version`（本期忽略，预留协议位） |
| `GET /registry/api/v1/genes/{slug}/manifest` | 技能本体 manifest JSON | 可选 `version`（同上） |
| `GET /registry/api/v1/genes/{slug}/download` | ZIP 下载（协议扩展） | 无 |

**路由声明顺序**：`tags` / `featured` 必须声明在 `{slug}` 之前（FastAPI 按声明顺序匹配，静态段优先才不被 slug 吞掉）。

**数据分两层**：
- 发现层（列表/详情）：仅市场元数据，不含技能本体，避免列表页负载膨胀。
- 获取层（manifest/download）：返回完整技能包。manifest 结构 `{skill: {content: SKILL.md 全文}, scripts: {文件名: 源码}, assets: {相对路径: 内容或 base64}, references: {相对路径: 内容}}`——所有文件全部内联（二进制 base64），外部 agent 一次请求即可还原完整技能目录。download 为同内容的 ZIP 形式（`{slug}/SKILL.md`、`{slug}/<script>`、`{slug}/assets/...`、`{slug}/references/...`）。

**ZIP 打包逻辑复用**：从现有门户 download 端点（`app/api/genes.py` 的 `download_gene`）抽出共享 helper（含 `_to_bytes` 二进制解码、路径安全处理），门户与开放面两处调用，禁止复制粘贴（同源副本规则）。

## 数据口径（可见性）

唯一口径：`visibility = 'public' AND review_status = 'approved' AND is_published = True AND deleted_at IS NULL`，与门户市场可见性（`gene_service` 列表查询）完全一致。org_private / personal / 待审 / 未发布 / 已删除的一律 404。

服务层新增（放**新文件** `app/services/open_registry_service.py`，避免 gene_service.py 进一步膨胀）：
- `list_public_market_genes(db, ...)`——public-only 搜索查询（关键词/标签/分类/排序/分页）
- `get_public_market_gene_by_slug(db, slug)`——**必须用 `.scalars().first()`**（`(slug, org_id)` 唯一索引下 public gene 的 org_id 为 NULL，Postgres NULL 不参与唯一约束，同 slug 可多行并存；禁用 `scalar_one_or_none()`）

**种子基因口径**（与门户市场列表一致，见 `_list_genes_local`）：列表隐藏平台种子基因（`source=official AND created_by IS NULL`——它们是 DeskClaw 实例内部工具，对外部 agent 无意义），唯一豁免 `market-client`（引导技能必须可被外部发现）；按 slug 直取（详情/manifest/download）不做种子过滤——知道 slug 属刻意访问，且与门户按 slug 取的行为一致。

## 对外字段脱敏

**只暴露**：`slug / name / description / short_description / category / tags / version / icon / install_count / avg_rating / effectiveness_score / is_featured / dependencies / synergies / created_at / updated_at`。

**绝不暴露**：`created_by / org_id / lineage_group_id / parent_gene_id / created_by_instance_id / review_status`（内部标识与真人信息不外泄，符合"审核列表禁裸显 UUID"精神）。列表项不含 manifest（体积考虑）；详情不含 manifest（有专门端点）。

## 限流（匿名防滥用）

新写 `app/core/open_registry_rate_limit.py`，照抄 `auth_rate_limit.py` 的进程内滑动窗口模式（不引 Redis）。阈值按平台日活 ~150 校准：外部接入方初期为个位数，限流目的是挡滥用而非保护高负载（无缓存层、埋点直写 DB 均按此规模设计）。

- 维度：IP（复用 `get_client_ip()`，已处理 X-Forwarded-For）
- 阈值（settings 项，环境变量可调，免重新构建）：
  - 读类接口（列表/详情/manifest/tags/featured/llms.txt）默认 **60 次/分钟/IP**
  - `download` 单独默认 **30 次/分钟/IP**
- 超限抛 429（复用 `errors.common.too_many_attempts` 语义）
- 已知边界：多副本部署下计数不跨 Pod 共享（日活 150 规模后端 1-2 副本，内存计数器足够，与登录限流同款限制）

## 埋点

开放面 download / manifest 拉取复用 `gene_market_stat_service.record_event`：
- 匿名场景 `user_id=None, org_id=None`
- `target_scope="open_registry"` 区分内外来源，市场统计可分辨"外部下载"
- 零 schema 改动（`target_scope` 参数已存在）

## llms.txt 自描述

`GET /registry/llms.txt` 返回模块内常量 markdown（非文件 I/O），面向 LLM/集成者：

- 一句话定位（DeskClaw 开放技能市场）
- 6 个端点 + 参数 + 响应形状
- curl 示例链路：搜索 → 拿 manifest → 下载 ZIP → 解压到 agent 的 skills 目录
- 声明数据范围（仅公共已审技能）、限流值、无鉴权说明
- 排版遵循 llms.txt 约定（H1 + 简述 + 分节）

## market-client 元技能

- 新增 `app/data/gene_templates/market_client.json`，登记进 `main.py` 种子导入的 `_gene_files` 列表——随 `SEED_GENES` 幂等种子到每个部署（官方 source、public、approved）
- SKILL.md 内容教 agent：从 llms.txt 发现 registry 端点、搜索/下载/落盘安装技能、错误处理（404/429 重试）
- 写成**协议通用版**（不绑死本平台域名，装到任何会执行 SKILL.md 的 agent 里都能用）
- 按 CLAUDE.md 规则：官方种子新增后需用 `scripts/upload_seeds_to_genehub.py` 同步推送 GeneHub

## 配置开关

`app/core/config.py` 新增三个 settings 项：

- `OPEN_REGISTRY_ENABLED`（默认 `True`）：关闭时整个 `/registry` 面返回 404（被滥用时运维一键止血）
- `OPEN_REGISTRY_READ_RATE_LIMIT`（默认 60）：读类接口每分钟每 IP 上限
- `OPEN_REGISTRY_DOWNLOAD_RATE_LIMIT`（默认 30）：下载接口每分钟每 IP 上限

## 测试

`nodeskclaw-backend/tests/test_open_registry.py`：
- 匿名可达（无 Authorization 头）
- 口径过滤：org_private / personal / pending / unpublished / 已删除均不可见（404 或不出现在列表）
- 脱敏断言：响应不含 `created_by` / `org_id` / `lineage_group_id` 等字段
- ZIP 完整性：与门户 download 同结构（SKILL.md/scripts/assets/references 齐全）
- 限流：读类/下载分别超阈值返回 429
- `page_size` 上限 100：超限请求被截断或拒绝
- 路由顺序：`tags` / `featured` 不被 `{slug}` 动态路由吞掉
- `OPEN_REGISTRY_ENABLED=False` 时返回 404
- 门户 download 端点抽取 helper 后原有测试不回归

## 文档同步

- `nodeskclaw-backend/README.md`（或 `docs/`）补"开放 Registry API"一节：面向集成者的接入说明 + llms.txt 指引
- 无前端改动、无 i18n、**无 Alembic 迁移**（零 schema 变更）

## 非目标（本期不做）

- MCP server 工具面（等有明确 MCP 原生消费者）
- 外部安装量/效果回写端点
- API Key 发放与管理
- 版本化 manifest（`version` 参数仅预留）
