# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

DeskClaw（曾用名 NoDeskClaw）— DeskClaw 实例可视化管理平台，通过 Web 界面管理 K8s 集群上的 DeskClaw 实例。

CE（社区版，本仓库）/ EE（企业版，私有 `ee/` 目录）双版本架构。`FeatureGate` 判断版本：优先读 `NODESKCLAW_EDITION` 环境变量，未设置时检测 `ee/` 是否存在。`./dev.sh ce` 会强制 CE 模式。

## 项目结构

```
nodeskclaw-portal/                 # 用户门户前端（CE+EE，Vue3+Tailwind）
nodeskclaw-backend/                # 后端 API（Python 3.12 + FastAPI）
nodeskclaw-llm-proxy/              # LLM Proxy（Python + FastAPI）
nodeskclaw-artifacts/              # 镜像构建 & 部署制品
openclaw-channel-nodeskclaw/       # DeskClaw channel plugin（tunnel/工作区通信）
openclaw-channel-dingtalk/         # DingTalk channel plugin
openclaw-channel-learning/         # Gene 演化生态 channel plugin
openclaw-security-layer/           # OpenClaw 安全层
nanobot-security-layer/            # Nanobot 安全层（二者均为瘦客户端：拦截工具调用转发给后端 services/security/ 统一评估，自身无安全逻辑）
nodeskclaw-tunnel-bridge/          # Nanobot 接入 tunnel 的 Python 桥接
hermes-nodeskclaw-bridge/          # Hermes runtime 的 tunnel 桥接
deploy/                            # K8s 部署 CLI（cli.sh）+ manifests
scripts/                           # 运维脚本（Gene 推送、文档一致性检查等）
features.yaml                      # EE 功能清单
ee/                                # EE 私有模块；ee/nodeskclaw-frontend 是 Admin 管理后台（EE-only）
openclaw/, vibecraft/              # 依赖的独立源码仓库（本地副本，用于调试）
```

## 常用命令

```bash
./dev.sh [ce|ee]              # 一键启动，自动检测 ee/ 存在与否

# 后端
cd nodeskclaw-backend && uv sync && uv run uvicorn app.main:app --reload --port 4510
uv run pytest [path::test]    # 测试
uv run ruff check [--fix] .   # lint

# 前端
cd nodeskclaw-portal && npm install && npm run dev       # :4517，npm run test 跑 vitest
cd ee/nodeskclaw-frontend && npm install && npm run dev  # :4518（EE-only），vue-tsc -b 类型检查
```

Docker Compose：`docker compose up -d`（CE）/ 加 `-f docker-compose.ee.yml`（EE）。Windows 必须显式设置 `NODESKCLAW_DATA_DIR`。

## i18n

- 覆盖 `nodeskclaw-portal` / `ee/nodeskclaw-frontend` / `nodeskclaw-backend`
- 前端优先用 `message_key` 本地翻译，缺失回退 `message`；后端失败响应含 `code`+`error_code`+`message_key`+`message`+`data`

## 代码架构

- **前端**：双前端，`nodeskclaw-portal`（CE+EE 用户门户）与 `ee/nodeskclaw-frontend`（EE-only Admin）。图标统一 `lucide-vue-next`
- **后端**：FastAPI + SQLAlchemy + asyncpg，Service Layer 模式
- **DeskClaw 源码**：本地副本 `openclaw/src/`，判断 DeskClaw 行为必须以此为依据
- **Runtime/Provider 双重抽象**：Runtime（OpenClaw/Nanobot，端口/数据目录/配置格式差异）查 `RuntimeSpec`（`app/services/runtime/registries/runtime_registry.py`），禁止硬编码；Compute Provider（K8s/Docker/Process）文件访问走 `PodFS`/`DockerFS`（`app/services/nfs_mount.py`），新增能力需同时覆盖三种 provider 实现
- **Admin/Portal 用户体系**：两套独立身份体系（`AdminMembership` vs `OrgMembership`），面向 Portal 的查询必须排除 Admin 用户
- **Gene System**：模块化能力包，模板在 `app/data/gene_templates/`，Agent/Channel/API 行为变更需评估同步模板并用 `scripts/upload_seeds_to_genehub.py` 推送

## 关键规则

- 禁止 emoji，图标用 `lucide-vue-next`；Docker 操作必须 `--platform linux/amd64`
- K8s/DeskClaw 问题必须用 kubectl 实际查看集群状态判断，不凭猜测；kubectl 命令必须显式 `--context <name>`，禁止依赖 current-context
- 数据删除一律软删除（`deleted_at`），唯一约束用 Partial Unique Index
- Model 改动必须同步生成 Alembic 迁移（`alembic revision --autogenerate`），禁止手写 revision ID
- JSONC 解析前剥离行注释；NFS 路径需容器路径 ↔ 本地路径正确转换
- 修改一处逻辑后必须搜索同源副本同步修改（常见于两个前端的 slug 生成/表单校验/`api.ts`）
- 部署脚本（`deploy/cli.sh`）必须用户手动执行，AI 禁止直接跑；破坏性操作（K8s 删除、DB DELETE、force push）必须逐项确认
- 改动 ≥1 个独立功能点先进入 Plan 模式；Plan 中禁止用行号定位代码（并发编辑会失效），改用类/函数/文件
- 每完成一个独立改动立即 commit，不攒批；多 Agent 协作时只 `git add` 本次改动文件，禁止 `git add -A/.`
- 新建目录/子项目必须有 README；改代码要同步受影响文档（设计文档存 `ee/docs/`，CE 仓库不建 `docs/` 目录）
- 任何新功能先判断是否服务于"人和 AI 共同经营"这一产品定位，说不清价值就先质疑
- Grep 搜不到不等于不存在：换更宽泛关键词重试确认后才能下结论
- 排查问题必须端到端验证 + 分层用证据排查（前端→后端→K8s→镜像），不凭猜测/对话上下文下结论
- 代码中禁止真人个人信息，占位统一 `@example.com`

## 易踩点（反复出现的真实 bug，遇到相关代码区先看这里）

- **Gene 按 slug 查询禁用 `scalar_one_or_none()`**：fork 后同 slug 可在多 scope（personal/org/public）并存，唯一约束是 `(slug, org_id)` 非全局。取一条用 `.scalars().first()`；精确定位用 `gene_service.get_gene_by_slug_in_scope()`
- **Windows 异步子进程需同步 fallback**：`asyncio.create_subprocess_exec` 在 Windows SelectorEventLoop 抛 `NotImplementedError`（`str()` 为空串，易被通用 `except` 吞掉），需单独捕获后走 `asyncio.to_thread(subprocess.run, ...)`
- **审核入口需对操作者自身权限做 bypass**：admin/超管自上传不该走 `pending_owner`，用 `is_user_admin_of_org()` 判定后传 `bypass_review`（涉及 `/genes/upload-folder`、`/genes/manual`、`fork_gene_to_library` 三处）
- **审核/审计列表禁裸显 UUID**：`created_by`/`user_id` 等字段服务层批量 join `User` 表填姓名/邮箱（不要 N+1），前端三级回退 `name → email → UUID 截短`
- **文件分发白名单需与源目录同步**：如 `llm_config_service.py` 的 `PLUGIN_FILES`，新增/删除分发类源文件后必须同步白名单，否则文件不生效且报错现象与根因无关联

## Git 规范

- 分支 `<type>/<kebab-case-description>`（`feat/fix/refactor/chore/docs/perf/test/build`），禁止无意义名/纯日期名
- Commit/PR 标题：`<type>(<scope>): <中文描述>`，subject 中文祈使语态，禁止 `Co-authored-by`
- 社区 PR 合并用 `gh pr merge --rebase`（禁止 `--merge`/`--squash`）保留原作者归属；追加修复用 `git cherry-pick`（禁止 `--no-commit`）

---

详见 `.cursor/rules/` 下的规则文件（编码风格、K8s 操作、CE/EE 边界等细则）。
