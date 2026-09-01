# Mission（任务空间）P1 详细设计 v3.1

> 本文档是自包含的实施规格，面向在 NoDeskClaw 代码库上工作的 AI 执行者。所有路径均为仓库相对路径。实施前先通读本文档与 `AGENTS.md`，两者冲突时以 `AGENTS.md` 的工程规范（软删除、迁移、i18n、命名）为准，以本文档的功能规格为准。
> v2 变更：吸收设计审核报告全部修订——补 MissionOrgConfig 表、seq 方案定案、Enum 降级 String、调度器 CAS 抢占（K8s 双副本兼容）、匹配器数据冷启动说明、reject 节点级化、token 保险丝进 P1、双轨测试定位、锚点路径修正、token 字段名对齐。
> v3 变更（复核 N1-N3）：实例串行多副本保障改为 instance 行锁 + 占用检查（NOT EXISTS 单用有并发窗口，见 §6）；token 保险丝加确认豁免（`fuse_acknowledged_at`，防确认-再触发死循环）；reject 点名节点直接置 pending（attempt 重置，不混入 failed 语义）；MissionOrgConfig 读写 upsert；表计数标题修正。
>
> **实施状态（2026-08-31）**：T1-T9 已全部实施并通过单元/集成验收（后端 51 测试、插件 6 测试、portal vue-tsc 构建通过）。已知 P1 简化：SSE 用 2s 轮询实现（行为等价 pg_notify 推送，代码注释已标注）；编排器传输仅支持 openai-completions 型组织 Key；legacy 实例回复由调度器合成 ack/done（设计 §7.4）。
> v3.1 变更：T3a 前置验证已由设计方完成并关闭（§4 结论：默认路径透传 response_format，Gemini 路径丢弃；编排器以 prompt+提取+校验为主保证）；jsonschema 依赖动作并入 T3。T4a 保留（数据运营缺口，代码验证无法替代）。

## 1. 背景与定位（一段话）

NoDeskClaw 已有"聊天驱动"的协作空间（Workspace + 群聊/私聊 + 黑板）。本次新增**任务驱动**的协作形态：用户在一个协作空间内提交需求，系统将其拆解为任务 DAG，自动匹配空间内的 AI 员工（OpenClaw 实例）逐节点执行，全过程以事件流形式实时呈现并持久沉淀。

**双轨并存定位**：现有协作空间功能**一行不动**（数据层零改动、API 零改动、现有页面零改动）。新功能以**测试功能**姿态独立入口运行（页面挂 beta 徽标），唯一允许的现有页面改动是在空间详情页加一个纯导航跳转按钮。成熟后评估替代，数据层共享（Mission 挂现有 workspace_id），替代时无迁移。

P1 交付判据：在 Docker Compose 环境下，通过最小验证页面完成一次完整闭环——**提交需求 → 自动拆解 DAG → 人工确认 → 自动派发到 AI 员工 → 实时看到执行播报 → 节点完成 → 验收 → 归档**。

## 2. 已定架构决策（直接执行，不再讨论）

| # | 决策 | 内容 |
|---|------|------|
| D1 | 任务空间是一等公民 | 每个需求生成一个 Mission 对象，聊天降级为事件类型之一 |
| D2 | 编排器（协调者）为无状态后端服务 | 后端直调 llm-proxy 完成拆解，输出受 JSON Schema 约束的 DAG；不引入专职协调者 Agent 实例 |
| D3 | 编排器 roster-blind | 拆解时不感知具体员工，子任务只带能力标签（capability tags）；由独立匹配器在派发时读 Gene 系统解析"谁能做" |
| D4 | 确认模式 | 开工前人工确认 DAG（轻任务自动跳过）；执行中三级上报 L0（自决）/ L1（异步提问不阻塞，带假设继续）/ L2（阻塞等待人类，如改 DAG、危险操作、预算超限） |
| D5 | 三路分流 | 执行者疑问的升级规则由编排器在拆解时写进任务包；运行时调度器机械执行，编排器不做运行时中转 |
| D6 | 轻任务模式 | 单 Agent、拆解输出 ≤1 节点时自动进入 lightweight 模式：无 DAG 确认、直接执行；产物进 TTL 隔离区（默认 14 天，org 可配 0-365，0 即不留），用户可一键"保留"转正式产物 |
| D7 | 隔离层次 | org（硬边界）> workspace > Mission（参与者可见）> 私聊；所有新表带 org_id；实例内会话 session_key = `{org_id}:{mission_id}:{node_id}` |
| D8 | 通信链路 | 任务包经现有 WebSocket 隧道（TunnelAdapter）下发；实例侧由 openclaw-channel-nodeskclaw 插件翻译；旧镜像（无新协议）自动降级为"只回最终结果"模式 |
| D9 | 调度约束 | 每实例同一时刻只处理一个节点（实例级串行队列）；多 Mission 竞争同一实例时按 Mission 创建时间 FIFO（队头阻塞已知问题，P2 改抢占式） |
| D10 | 派发可靠性 | 任务包带幂等 task_id；ack 判定"收到"、task.done 判定"完成"；ack 超时 60s 重试 2 次后节点置 failed 并产生 L2 事件；派发动作走 DB 级 CAS 抢占（多副本部署安全，见 §6） |
| D11 | 双轨测试定位 | 现有协作空间零改动；新功能独立入口 + beta 徽标；现有空间详情页仅加纯导航按钮 |
| D12 | token 保险丝 | 单 Mission token 消耗超 org 阈值时自动挂起 Mission + L2 确认（复用 L2 机制），防止 DAG 失控自循环烧钱 |

## 3. 数据模型（6 张业务表 + 1 张 seq 计数表）

所有表继承现有 `BaseModel`（见 `nodeskclaw-backend/app/models/base.py`，自带 id/created_at/updated_at/deleted_at，时间字段一律 `DateTime(timezone=True)`）。全部查询必须过滤 `deleted_at IS NULL`。唯一约束一律用 Partial Unique Index。

**状态/类型字段一律 String + 模块级常量词表（Python 侧用 `Literal` 约束），不使用 PG Enum**——仓库 models 无 Enum 先例，且原生 enum 的 ALTER TYPE 不能事务内回滚，与容器启动自动迁移相性差。

### 3.1 Mission（`app/models/mission.py` 新建）

| 字段 | 类型 | 说明 |
|------|------|------|
| org_id | String(36) FK organizations.id, not null, index | 租户边界 |
| workspace_id | String(36) FK workspaces.id, not null, index | 所属协作空间（复用现有空间，不新建空间实体） |
| title | String(200), not null | 任务空间标题（编排器从需求提炼） |
| requirement_text | Text, not null | 用户原始需求原文，永不修改 |
| brief | JSONB, not null | 任务简报：`{goal, constraints[], acceptance_criteria[], key_decisions[]}`；编排器初生成，人工可编辑；每次编辑产生 `brief_updated` 事件 |
| mission_type | String(16), not null, default "standard" | 常量：`standard` / `lightweight` |
| status | String(24), not null, default "draft" | 状态机见 3.7 |
| created_by | String(36) FK users.id, not null | 发起人（拥有验收权） |
| escalation_policy | JSONB, nullable | L0/L1/L2 规则覆盖，空=用 org 默认（见 MissionOrgConfig） |
| artifact_ttl_days | Integer, nullable | 空=取 org 配置，org 空=默认 14 |
| token_cost / prompt_token_cost / completion_token_cost | Integer, default 0 | 口径与字段名对齐现有 WorkspaceTask（无 cached 维度，cached 不采集） |
| fuse_acknowledged_at | DateTime(timezone=True), nullable | token 保险丝确认时间戳；人工确认后本次 Mission 豁免保险丝（触发条件见 §6 第 5 步，防确认-再触发死循环） |
| coordinator_meta | JSONB, nullable | `{engine:"builtin", prompt_version, schema_version, decomposition_reason}` |

### 3.2 MissionNode（`app/models/mission_node.py` 新建）

| 字段 | 类型 | 说明 |
|------|------|------|
| mission_id | FK missions.id, not null, index | |
| org_id | String(36), not null, index | 冗余，查询隔离用 |
| seq | Integer, not null | 节点创建序号（编排器输出顺序） |
| title | String(200), not null | 子任务标题 |
| description | Text | 子任务描述（做什么、边界、产出形式） |
| acceptance_criteria | Text | 该节点级验收标准 |
| capability_tags | JSONB（string 数组）, not null | 能力标签，取自能力词表（见 3.6），编排器只允许从词表取值 |
| depends_on | JSONB（node seq 数组） | 依赖的上游节点 seq 列表 |
| status | String(24), not null, default "pending" | 常量：`pending → matched → dispatched → acked → running → done / failed / blocked_question / blocked_dependency / skipped` |
| assigned_instance_id | FK instances.id, nullable | 匹配器写入；人工改派可更新 |
| match_reason | JSONB, nullable | `{candidates:[{instance_id, score, basis}], chosen, at, capability_gap:[tags]}`，审计用 |
| attempt_count / max_attempts | Integer, default 0 / 3 | 含 ack 超时重试 |
| session_key | String(120), not null | `{org_id}:{mission_id}:{node_id}` |
| last_task_id | String(64), nullable | 最近一次派发的幂等 ID（uuid4） |
| dispatched_at / acked_at / started_at / finished_at | DateTime(timezone=True), nullable | 簿记 |
| token_cost / prompt_token_cost / completion_token_cost | Integer, default 0 | |

### 3.3 MissionEvent（统一事件流）

| 字段 | 类型 | 说明 |
|------|------|------|
| mission_id | FK, not null, index | |
| node_id | FK, nullable | 系统级事件为空 |
| org_id | String(36), not null, index | |
| seq | BigInteger, not null | Mission 内单调递增，唯一索引 `(mission_id, seq) WHERE deleted_at IS NULL` |
| event_type | String(32), not null | 常量词表见 3.8 |
| actor_type | String(16), not null | 常量：`user` / `agent` / `scheduler` / `system` |
| actor_id / actor_name | String(36) / String(100) | actor_name 冗余显示名 |
| visibility | String(16), not null, default "both" | 常量：`timeline`（仅前端展示，不注入上下文）/ `both`。心跳与工具播报=timeline |
| content | Text | 人类可读内容 |
| payload | JSONB | 结构化数据 |

**seq 生成唯一方案（不得用 SELECT max()+1，聚合加 FOR UPDATE 是无效锁，空表并发首写必撞唯一索引）**：per-mission 计数行。建 `mission_event_counters` 表（mission_id 主键, next_seq BigInteger），写事件时 `UPDATE mission_event_counters SET next_seq = next_seq + 1 WHERE mission_id = ? RETURNING next_seq`——同一 Mission 的事件写入天然串行化，多 Mission 互不阻塞。Mission 创建时同步插入计数行。

### 3.4 MissionArtifact

| 字段 | 类型 | 说明 |
|------|------|------|
| mission_id / node_id / org_id | FK / FK / String(36) | |
| name | String(255), not null | 产物名（含扩展名） |
| kind | String(16), not null | 常量：`file` / `report` / `code` / `config` / `other` |
| storage_key | String(500), not null | `missions/{org_id}/{workspace_id}/{mission_id}/{artifact_id}/{name}`（复用现有 storage_service） |
| size_bytes / checksum | BigInteger / String(64) | |
| version | Integer, default 1 | 同名产物再次提交 version+1，prev_artifact_id 记在 payload |
| retention | String(16), not null, default "quarantine" | 常量：`quarantine` / `promoted` |
| expires_at | DateTime(timezone=True), nullable | quarantine 必填 = now + TTL；promoted 置空 |
| produced_by_instance_id | FK instances.id | |
| promoted_by_user_id / promoted_at | FK users.id / DateTime | 点"保留"时写入 |

清理协程（每小时）：`retention='quarantine' AND expires_at < now()` → 物理删除存储对象 + 行级软删除 + `artifact_expired` 事件。**注：物理清理存储对象是对"删除一律软删除"规则的有意例外**（隔离区 TTL 是履约清理而非数据删除），表记录与事件留痕均在。

### 3.5 MissionOrgConfig（org 级配置，每 org 一行）

| 字段 | 类型 | 说明 |
|------|------|------|
| org_id | String(36) FK organizations.id, not null, unique | 唯一索引 `WHERE deleted_at IS NULL` |
| artifact_ttl_days | Integer, not null, default 14 | 轻任务产物隔离区天数，0-365 |
| mission_token_fuse | Integer, nullable | 单 Mission 三项 token 成本总和阈值，超限挂起 + L2；空=不启用 |
| escalation_defaults | JSONB, nullable | L0/L1/L2 默认规则（危险操作清单等），空=平台内置默认 |

**读写时机**：不做启动预建；首次读取时 upsert（`INSERT ... ON CONFLICT DO NOTHING` 后 SELECT），更新走 API 时同样 upsert。

### 3.6 CapabilityTag（能力词表）

| 字段 | 类型 | 说明 |
|------|------|------|
| org_id | String(36) FK, nullable | null = 平台预置词表 |
| tag | String(64), not null | 英文 kebab-case：`backend`、`web-frontend`、`payment`、`copywriting`、`data-analysis` 等 |
| label_zh / label_en | String(100) | 显示名 |
| description | Text | 语义说明，供编排器 prompt |
| status | String(16), not null, default "active" | 常量：`active` / `deprecated` |

唯一索引 `(org_id, tag) WHERE deleted_at IS NULL`。启动迁移 seed 平台预置词表（约 20 个）。

**数据冷启动注意（重要）**：当前 Gene 体系的 manifest **没有结构化 capabilities 字段**（模板里仅 description 文案出现该词，gene_service 零读取）。T4 前必须完成种子基因 capabilities 标注（见 §11 T4a），否则匹配器退化为名称相似度。

### 3.7 Mission 状态机

```
draft → decomposing → awaiting_confirm → executing ⇄ blocked_question
                                              │
executing →(全部节点 done)→ acceptance → archived
executing/acceptance → cancelled（未完成节点置 skipped）
decomposing 失败 → draft（保留需求原文，允许重试拆解）
executing --token 保险丝超限--> 挂起（blocked_question + L2 事件，人工确认后恢复或取消）
```

轻任务路径：`draft → decomposing → executing → acceptance → archived`（跳过 awaiting_confirm）。

### 3.8 事件类型常量词表

`mission_created` / `decomposition_started` / `decomposition_done` / `decomposition_failed` / `dag_confirmed` / `dag_edited` / `brief_updated` / `dispatched` / `task_ack` / `progress`（timeline）/ `tool_trace`（timeline）/ `narrative`（timeline）/ `heartbeat`（timeline）/ `l1_question` / `l2_question` / `question_answered` / `blocked` / `unblocked` / `node_done` / `node_failed` / `artifact_produced` / `artifact_promoted` / `artifact_expired` / `mission_accepted` / `mission_rejected` / `mission_cancelled` / `token_fuse_tripped` / `system_note`

## 4. 编排器 v1（`app/services/mission/orchestrator.py` 新建）

- 输入：`requirement_text` + 空间知识库占位（P1 为空）+ 用户偏好卡占位（P1 为空）+ 该 org 可用能力词表（active 标签列表）。
- 调用：经现有 llm-proxy 内部 URL 发起 chat completion（认证方式参考 `app/api/llm_keys.py` 的 `_test_via_proxy` 先例）。
- **response_format 透传结论（2026-08-29 代码级验证完成，原 T3a 验证任务取消，验证记录即本条）**：llm-proxy `app/proxy.py` 有两条转发路径——① 默认路径（openai/openrouter/minimax/anthropic 等）：`_handle_non_stream`/`_handle_stream` **原样转发请求体**（仅对 OpenAI 系流式注入 stream_options），`response_format` 原样透传、由上游执行；② Gemini 路径：`_openai_chat_to_gemini_request` 仅映射 messages/systemInstruction/tools/采样参数（temperature/top_p/max_tokens/stop），**`response_format` 被静默丢弃**（无报错也无强制；已有的 `_sanitize_gemini_schema` 只服务于 tools 参数转换，未覆盖 response_format）。**设计裁定**：编排器以"prompt 强约束 JSON + 服务端提取首个 JSON 块 + jsonschema 校验"为主保证机制（provider 无关、永远启用）；当解析到的 provider 非 gemini 时附带 `response_format={"type":"json_object"}` 作为增强（无害 best-effort）。正确性不依赖 response_format。
- 输出强制 JSON Schema（服务端校验，`jsonschema` 需加入 pyproject 依赖；校验失败重试 1 次，再失败则 decomposition_failed）：

```json
{
  "mission_title": "string, <= 30 字",
  "brief": {
    "goal": "string",
    "constraints": ["string"],
    "acceptance_criteria": ["string"],
    "key_decisions": ["string"]
  },
  "is_lightweight_candidate": "boolean",
  "nodes": [
    {
      "title": "string",
      "description": "string",
      "acceptance_criteria": "string",
      "capability_tags": ["必须来自输入词表的 tag"],
      "depends_on": ["上游节点在数组中的下标，从 0 开始"]
    }
  ],
  "escalation": {
    "l2_rules": ["触发阻塞提问的硬边界描述，如：需要修改 DAG 结构 / 删除不可恢复数据 / 预算超限"]
  }
}
```

- prompt 要点（常量 `ORCHESTRATOR_PROMPT_V1`，**演示前需用典型需求调优，不要拿默认 prompt 上场**）：拆解为可独立验收的最小单元；标签只能从给定词表选择；词表表达不了的子任务用最接近标签并在 description 说明缺口；depends_on 不允许成环。
- 服务端做 DAG 环检测（拓扑排序），成环直接 decomposition_failed。
- `is_lightweight_candidate=true` 且 nodes ≤ 1 → mission_type=lightweight，跳过确认。

## 5. 匹配器 v1（`app/services/mission/matcher.py` 新建）

- 输入：节点 capability_tags + 该 workspace 内全部 agent 实例（经 `app/models/workspace_agent.py` 的 WorkspaceAgent 联表取 instances）。
- 能力来源：`InstanceGene → Gene.manifest`（**manifest 是 Text 存 JSON 字符串，读取必须 `json.loads` + try/except 容错**，参考 llm-proxy `app/skill_usage.py` 的 `_load_instance_gene_map` 同款处理）中的 `capabilities` 数组；无该字段时回退 `Gene.name`。
- 算法（纯确定性，不调 LLM）：标签与能力串规范化比较（小写、去连字符），完全命中 1 分、包含关系 0.6 分、编辑距离相似度 ≥ 0.75 得 0.4 分；节点得分 = 标签均分；按分数降序记候选，chosen 取最高（并列取历史完成数多者）。
- 无候选 → 节点保持 `pending`，`capability_gap` 记入 match_reason，产生 `system_note` 事件。
- **覆盖检查**：awaiting_confirm 时对全部节点 dry-run，结果（建议人选/缺口）随 mission 详情返回，确认页展示；派发时（依赖就绪那一刻）重新匹配——花名册可能已变化。

## 6. 调度器 v1（`app/services/mission/scheduler.py` 新建）

- 形态：backend lifespan 启动的常驻 asyncio 协程，每 5 秒一轮；启停/善后模式复用 `app/main.py` 的 heartbeat_scanner（注意其注释记录的"后端重启会杀死 asyncio.create_task"的真实坑）。
- **多副本安全（K8s 生产 replicas: 2，不得假设单 worker）**：所有状态推进走 DB 级 CAS——节点状态迁移即 `UPDATE mission_nodes SET ... WHERE id=? AND status='<期望前态>'`，抢不到（rowcount=0）的副本自然跳过；事件写入靠计数行天然串行（§3.3）。
- **实例串行的多副本保障（D9 的硬保证）**：实例队列是各副本的内存结构，副本间互相不可见，内存队列不能作为串行依据。派发决策必须走 DB：先 `SELECT * FROM instances WHERE id=? FOR UPDATE`（同实例的派发决策被行锁串行化），锁内检查 `NOT EXISTS (SELECT 1 FROM mission_nodes WHERE assigned_instance_id=? AND status IN ('dispatched','acked','running'))`，通过后 CAS 置 `dispatched`。**注意：NOT EXISTS 单独使用不够**——两个副本并发 UPDATE 不同节点行时，READ COMMITTED 快照下互相看不到未提交变更，存在双派发窗口；instance 行锁是堵死该窗口的关键，不可省略。
- 每轮逻辑：
  1. 取所有 `executing` Mission；
  2. `pending` 且依赖全 `done` 的节点 → 匹配 → 有 chosen 则 CAS 抢占为 `matched` 入本副本实例队列；
  3. 队首且隧道在线（复用 `tunnel_adapter.connected_instances`）→ 按"实例行锁 + 占用检查"流程组装任务包下发 → CAS 置 `dispatched`；
  4. 超时看护：`dispatched` 超 60s 无 ack 重发（attempt+1）；attempt ≥ 3 → `failed` + L2；`running` 超 30 分钟无事件 → L2（不自动 fail）；**后端重启后**，`dispatched` 超过 90s 未 ack 的节点由看护逻辑接管重发（幂等 task_id 保护）。
  5. token 保险丝：每轮检查各 Mission 三项 token 总和，**触发条件 = 超过 `MissionOrgConfig.mission_token_fuse` 且 `fuse_acknowledged_at IS NULL`** → Mission 置挂起 + `token_fuse_tripped` L2 事件；人工确认恢复时写入 `fuse_acknowledged_at`，本次 Mission 豁免到底（不再重复触发；如需梯度告警，阈值翻倍的指数退避属 P2）。
- 下游唤醒：节点 done 后重算下游依赖。
- 幂等：实例上报 task_id 与节点 last_task_id 不符时丢弃并回 `mission.task.stale`。

## 7. Channel 插件协议（`openclaw-channel-nodeskclaw/` 扩展）

复用现有 WebSocket 隧道，新增 `mission.*` 消息命名空间。信封：

```json
{ "type": "mission.task.dispatch", "task_id": "uuid", "protocol_version": 1, "data": { ... } }
```

### 7.1 下行 mission.task.dispatch

```json
{
  "session_key": "org:mission:node",
  "mission_title": "string",
  "brief": { "goal": "", "constraints": [], "acceptance_criteria": [] },
  "subtask": { "title": "", "description": "", "acceptance_criteria": "" },
  "upstream_artifacts": [ { "name": "", "kind": "", "storage_url": "带签名临时 URL" } ],
  "escalation_rules": { "l2_rules": ["..."], "l1_hint": "可带假设继续的歧义，先说明假设再继续" },
  "report_guidance": "执行中请定期调用 report 工具播报进展（人类可见）"
}
```

### 7.2 上行

| type | data 要点 | 语义 |
|------|-----------|------|
| mission.task.ack | `{task_id}` | 收到任务包（插件自动回） |
| mission.task.progress | `{task_id, message, phase}` | 阶段进展，插件节流 10s |
| mission.task.report | `{task_id, message}` | Agent 主动叙事播报 |
| mission.task.artifact | `{task_id, name, kind, content_base64, size}` | 提交产物；单产物超 20MB 拒收（20MB base64 解码峰值约 27MB 内存 + WS 背压丢弃风险，P1 接受，插件需做分块或提示 Agent 改产出路径说明） |
| mission.task.blocked | `{task_id, reason, question: {level: "L1"|"L2", message, assumption?}, request_agent?}` | request_agent 填目标员工名走协商通道（现有 collaboration 链路），否则 L1/L2 进事件流 |
| mission.task.done | `{task_id, summary, token_usage: {input, output}}` | 节点完成（token 两项，无 cached） |

### 7.3 插件侧工具注册

向 OpenClaw 注册 4 个工具：`mission_report(message)`、`mission_submit_artifact(name, kind, content_b64)`、`mission_block(reason, question_level, message, assumption)`、`mission_complete(summary)`。progress/ack 由插件在消息处理与工具调用拦截点自动产生。

### 7.4 版本协商与降级

dispatch 信封带 `protocol_version: 1`。插件在隧道握手（`src/tunnel-client.ts` 现有 auth 消息）上报 `supported_protocols: ["mission.v1"]`；未上报视为 legacy：调度器改走现有 chat.request 链路下发任务文本，仅收最终回复文本（记为 node_done summary），无 ack/播报/产物。前端对该类节点显示"精简模式"标记（i18n key：`missions.node.legacy_mode`）。

## 8. 后端接入层（`app/services/mission/ingest_service.py` 新建）

扩展隧道消息处理（`collaboration_service` 入口处增加 mission.* 分发）：
- ack → 节点 `acked` + `task_ack` 事件；
- progress/report → `progress`/`narrative` 事件（timeline）；
- artifact → 存储 → MissionArtifact（quarantine + TTL）→ `artifact_produced` 事件；
- blocked → L1 记 `l1_question`（节点继续 running）；L2 记 `l2_question`、节点 `blocked_question`、Mission 置 `blocked_question`；request_agent 转协商链路，回复后 `unblocked`；
- done → 节点 `done` + token 汇总 + `node_done`；全部 done → Mission `acceptance`。

## 9. API 设计（`app/api/missions.py` 新建，挂到现有 router）

前缀 `/api/v1`，响应遵循 `{code, error_code, message_key, message, data}` 契约，文案全走 i18n。

| 方法与路径 | 说明 |
|-----------|------|
| POST `/workspaces/{wid}/missions` | body `{requirement_text, mission_type?}`；创建 draft → 异步拆解；返回 mission id |
| GET `/workspaces/{wid}/missions` | 列表，`status` 过滤，成员可见性复用现有 workspace 成员逻辑 |
| GET `/missions/{mid}` | 详情：brief、nodes（建议人选/缺口）、token 汇总；awaiting_confirm 附覆盖检查 |
| POST `/missions/{mid}/confirm` | body `{node_edits?}`；应用人工编辑后 → executing |
| POST `/missions/{mid}/replan` | 仅未开工节点重新拆解（冻结 done/running） |
| POST `/missions/{mid}/cancel` | 任意时刻取消 |
| POST `/missions/{mid}/nodes/{nid}/retry` / `reassign` | 重试 / 改派（`{instance_id}`） |
| POST `/missions/{mid}/questions/{event_id}/answer` | `{answer}`；解除 L2 阻塞 |
| GET `/missions/{mid}/events` | `?after_seq=&kinds=` |
| GET `/missions/{mid}/events/stream` | SSE（鉴权同现有 workspace events SSE，推送复用 `app/services/runtime/pg_notify.py`） |
| GET `/missions/{mid}/artifacts` | 列表（含 expires_at） |
| POST `/missions/{mid}/artifacts/{aid}/promote` | 保留转正 |
| POST `/missions/{mid}/accept` | 验收通过（发起人或空间管理员） |
| POST `/missions/{mid}/reject` | **节点级打回**：body `{reason, rejected_nodes: [node_id]}`；被点名节点回 `pending`（attempt 重置；打回是验收失败语义，不混入运行失败的 failed 状态，也不计入 attempt 失败统计）重新入队、其下游节点依赖重算；未点名节点保持 `done`；Mission 回 `executing`；`mission_rejected` 事件含打回明细 |

## 10. 最小验证页面（`nodeskclaw-portal`）

独立路由 `/mission-lab`（"任务实验室"），页面显著位置挂 beta/测试徽标（i18n：`missions.beta_badge`）。功能：
1. 选 workspace → 提交需求；
2. 拆解结果：节点列表（标题、标签、建议人选/缺口）、确认/编辑；轻任务跳过此页；
3. 执行视图：节点状态徽标 + SSE 实时时间线（progress/narrative/question 混排，L2 有回答入口，legacy 节点显示"精简模式"）；
4. 产物列表 + "保留"按钮；
5. 验收 / 节点级打回。
**现有空间详情页唯一允许的改动**：加一个纯导航按钮跳转至 `/mission-lab`（不调用任何新接口、不动现有组件逻辑）。其余现有页面零改动。

## 11. 实施顺序与任务拆解（每项可独立提交、独立验收）

| 任务 | 内容 | 验收判据 |
|------|------|---------|
| T1 | 6 张表模型 + mission_event_counters + alembic autogenerate 迁移 + 平台词表 seed | `uv run alembic upgrade head` 通过；建表 pytest 通过 |
| T2 | MissionEventService（计数行 seq，并发安全） | 并发 100 次写事件无 seq 冲突 |
| ~~T3a~~ | **已完成（2026-08-29，验证结论已写入 §4，不再需要执行）**：response_format 默认路径原样透传、Gemini 路径丢弃 → 编排器以"prompt 约束 + 首个 JSON 块提取 + jsonschema 校验"为主，非 gemini 时附带 response_format 作增强。剩余动作：pyproject 加 jsonschema（并入 T3） | ~~验证结论写入本文档附录~~ 已写入 §4 正文 |
| T3 | 编排器 v1（prompt + schema 校验 + 环检测 + 轻任务判定） | 3 条测试需求产出合法 DAG；环输入被拒 |
| T4a | **数据冷启动**：种子基因补结构化 capabilities 标注并与词表对齐（含 `scripts/upload_seeds_to_genehub.py` 推送同步，遵循模板与推送同步规则） | 存量种子基因 capabilities 覆盖率 100%；匹配器单测从名称匹配升级为标签匹配 |
| T4 | 匹配器 v1 + 覆盖检查 | 命中/缺口/改派三场景单测通过 |
| T5 | 调度器协程（CAS 抢占 + 实例行锁串行保障 + 超时看护 + 重启接管 + token 保险丝 + legacy 降级） | mock 隧道单测：双副本并发派发仅一成功；**双副本并发向同一实例派不同节点仅一成功（D9 串行验证）**；ack 超时重试；done 唤醒下游；保险丝触发→确认→不再重复触发 |
| T6 | API 全部路由 + SSE | httpx 接口测试覆盖主链路 |
| T7 | channel 插件协议（TS + 镜像构建脚本 bump 版本） | 插件单测：信封解析、4 工具注册、ack 自动回 |
| T8 | ingest_service 隧道接入 | 集成测试：mock 实例走完 dispatch→ack→progress→artifact→done |
| T9 | 最小验证页面 + i18n + 空间详情页导航按钮 | 手工验收 §1 闭环判据 |

提交规范遵循 `AGENTS.md`（单元 commit，中文 message）。

## 12. P1 明确不做

空间知识库及蒸馏（P3）、用户偏好卡编辑（P2）、硬预算联动 quota 体系（P2，P1 有 token 保险丝兜底）、完整 DAG 可视化（P2）、旧消息三表归并（P2 视图统一）、30 天词表治理（P2）、实例队列抢占式调度（P2，队头阻塞已知）、节点间结论文交接（P2，P1 仅 upstream_artifacts 文件）、专职协调者 Agent（远期可选）、六边形拓扑集成。

## 13. 现有代码锚点（执行者从这里读）

| 关注点 | 位置 |
|--------|------|
| Model 基类与软删除 | `nodeskclaw-backend/app/models/base.py`、`app/models/instance.py` |
| Workspace 模型 | `app/models/workspace.py`（Workspace 本体） |
| WorkspaceAgent 联表 | `app/models/workspace_agent.py`（注意：不在 workspace.py 里） |
| 基因与能力 | `app/models/gene.py`（**manifest 是 Text 存 JSON 字符串，读取需 json.loads 容错**） |
| 隧道适配器（下发） | `app/services/tunnel/adapter.py`、`app/services/tunnel/protocol.py` |
| collaboration 消息入口（上行） | `app/services/collaboration_service.py` |
| SSE 与 pg_notify | `app/api/workspaces.py` 的 events 路由、`app/services/runtime/pg_notify.py`（注意：在 runtime/ 子目录） |
| 存储 | `app/services/storage_service.py`（get_presigned_url 有 S3/本地双实现） |
| llm-proxy 调用先例 | `app/api/llm_keys.py` 的 `_test_via_proxy` |
| manifest 解析先例 | llm-proxy `app/skill_usage.py` 的 `_load_instance_gene_map` |
| lifespan 常驻协程先例 | `app/main.py` 的 heartbeat_scanner / telemetry |
| channel 插件 | `openclaw-channel-nodeskclaw/src/`（tunnel-client.ts 握手、tools.ts 工具注册） |
| 种子基因模板与推送 | `app/data/gene_templates/`、`scripts/upload_seeds_to_genehub.py` |
| 部署 | `docker-compose.yml`（Compose 单副本）；`deploy/k8s/backend.yaml`（K8s 生产 replicas: 2——调度器 CAS 抢占因此必做） |

## 附录 A：T3a 前置验证结论（2026-08-29，代码级核实）

llm-proxy 对 `response_format` 的透传行为（`nodeskclaw-llm-proxy/app/proxy.py`）：

- **OpenAI 兼容路径：透传。** 通用代理路径把请求体字节原样转发上游（`_maybe_inject_stream_options` 只注入 stream_options，随后 `client.request(..., content=body)`），请求体里的 `response_format` 原样到达上游
- **Gemini 原生路径：不透传。** `_openai_chat_to_gemini_request` 是显式白名单转换（仅映射 messages/temperature/top_p/max_tokens/stop/tools/tool_choice），`response_format` 被静默丢弃（Gemini 的对应能力是 generationConfig.responseMimeType，转换器未映射）

**编排器实现决策（据此定案）**：不依赖单一机制——请求始终携带 `response_format=json_object`（OpenAI 兼容上游生效，Gemini 路径丢弃无害），服务端始终执行"提取响应文本中首个 JSON 块 + jsonschema 校验"的兜底解析（即设计 §4 降级方案升级为常规路径），校验失败重试 1 次。`jsonschema>=4.23.0` 已加入 backend 依赖。
