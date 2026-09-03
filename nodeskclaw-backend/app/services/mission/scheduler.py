"""任务空间调度器 v1（设计 §6 / T5）。

形态：backend lifespan 常驻 asyncio 协程（启停复用 main.py heartbeat_scanner 模式），
每 SCAN_INTERVAL_S 一轮；全部状态在 DB，无内存状态——后端重启后下一轮自然接管
（dispatched 超 ACK_TIMEOUT_S 未 ack 即重发，即设计的"重启接管"，无需独立计时器）。

多副本安全（设计 §6，K8s 生产 replicas: 2）：
- 所有节点/任务状态推进走 DB 级 CAS（UPDATE ... WHERE status=<期望前态>），
  抢不到（rowcount=0）的副本自然跳过
- 实例串行（D9）的硬保证：派发事务内先 `SELECT instances FOR UPDATE` 行锁
  串行化同实例派发决策，锁内 NOT EXISTS 占用检查（dispatched/acked/running），
  再 CAS 置 dispatched。NOT EXISTS 单独使用在 READ COMMITTED 下有双派发窗口，
  行锁是堵死窗口的关键（设计 v3 §6）
- 先 commit 再发隧道消息：发送失败由 ack 超时看护 60s 兜底重发（幂等 task_id）

token 保险丝（D12）：每轮检查，触发条件 = 超阈值且 fuse_acknowledged_at 为空
（确认后本次豁免到底，防确认-再触发死循环）。

时间一律 aware UTC（技能市场统计 P3 教训：naive datetime 绑定 timestamptz 会被
按 UTC 解释导致窗口偏移）。
"""
import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import not_deleted
from app.models.gene import Gene
from app.models.instance import Instance
from app.models.mission import Mission
from app.models.mission_artifact import MissionArtifact
from app.models.mission_event import MissionEvent
from app.models.mission_node import MissionNode
from app.models.mission_org_config import MissionOrgConfig
from app.services.mission.event_service import MissionEventService
from app.services.mission.matcher import match_node

logger = logging.getLogger(__name__)

SCAN_INTERVAL_S = 5
ACK_TIMEOUT_S = 60
RUNNING_STALL_S = 30 * 60
# P1.6 上下文防爆：单节点 tool_trace 事件数上限，超限自动暂停 + L2
MAX_TOOL_TRACES_PER_NODE = 50
# 实例占用态：这些状态下实例被视为"正在处理一个节点"（D9 串行依据）
OCCUPYING_STATUSES = ("dispatched", "acked", "running")

# 派发函数签名：(instance_id, task_package, task_id) -> 是否送达（False/异常由看护兜底）
SenderFn = Callable[[str, dict, str], Any]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MissionScheduler:
    """任务空间调度器：匹配 → 派发 → 超时看护 → token 保险丝。"""

    def __init__(
        self,
        session_factory: Any,
        *,
        tunnel: Any = None,
        sender: SenderFn | None = None,
        scan_interval_s: int = SCAN_INTERVAL_S,
        ack_timeout_s: int = ACK_TIMEOUT_S,
        running_stall_s: int = RUNNING_STALL_S,
        max_tool_traces: int = MAX_TOOL_TRACES_PER_NODE,
    ):
        self._sf = session_factory
        self._tunnel = tunnel
        self._sender = sender or self._smart_sender
        self._scan_interval_s = scan_interval_s
        self._ack_timeout_s = ack_timeout_s
        self._running_stall_s = running_stall_s
        self._max_tool_traces = max_tool_traces

    # ── 生命周期 ──────────────────────────────────────────────

    async def run_forever(self) -> None:
        """常驻循环（lifespan 启动）；单轮异常只记日志不退出。"""
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                logger.info("Mission scheduler 已停止")
                raise
            except Exception:
                logger.warning("Mission scheduler 单轮执行失败", exc_info=True)
            await asyncio.sleep(self._scan_interval_s)

    async def run_once(self) -> dict:
        """单轮：保险丝 → 匹配 → 派发 → 看护。返回统计（测试/观测用）。"""
        stats = {"fused": 0, "matched": 0, "dispatched": 0, "failed": 0, "stalled": 0}
        stats["fused"] = await self._check_token_fuses()
        stats["matched"] = await self._match_ready_nodes()
        stats["dispatched"] = await self._dispatch_due()
        failed, stalled = await self._watchdog()
        stats["failed"] = failed
        stats["stalled"] = stalled
        return stats

    # ── token 保险丝（D12）────────────────────────────────────

    async def _check_token_fuses(self) -> int:
        """P2 预算硬阻断：确认后继续监控——每次确认阈值翻倍（fuse * 2^ack_count），再超再断。"""
        tripped = 0
        async with self._sf() as db:
            missions = (await db.execute(
                select(Mission).where(Mission.status == "executing", not_deleted(Mission))
            )).scalars().all()
            for m in missions:
                total = m.token_cost + m.prompt_token_cost + m.completion_token_cost
                if total <= 0:
                    continue
                cfg = (await db.execute(
                    select(MissionOrgConfig).where(
                        MissionOrgConfig.org_id == m.org_id, not_deleted(MissionOrgConfig),
                    )
                )).scalar_one_or_none()
                fuse = cfg.mission_token_fuse if cfg else None
                if not fuse:
                    continue
                # P2 阶梯阈值：fuse * 2^已确认次数（第 0 次=fuse，确认后=fuse*2，再确认=fuse*4…）
                effective_threshold = fuse * (2 ** (m.fuse_ack_count or 0))
                if total < effective_threshold:
                    continue
                res = await db.execute(
                    update(Mission)
                    .where(
                        Mission.id == m.id,
                        Mission.status == "executing",
                        not_deleted(Mission),
                    )
                    .values(status="blocked_question")
                )
                if res.rowcount:
                    tripped += 1
                    ack_count = m.fuse_ack_count or 0
                    await MissionEventService(db).append(
                        m.id, org_id=m.org_id,
                        event_type="token_fuse_tripped", actor_type="scheduler",
                        content=(
                            f"token 消耗 {total} 已超过阈值 {effective_threshold}"
                            f"（第 {ack_count + 1} 次触发，基础阈值 {fuse}），任务已挂起，"
                            f"请人工确认后继续"
                        ),
                        payload={
                            "total_tokens": total,
                            "fuse": fuse,
                            "effective_threshold": effective_threshold,
                            "ack_count": ack_count,
                        },
                    )
                    logger.warning(
                        "token 保险丝触发: mission=%s total=%d threshold=%d ack=%d",
                        m.id, total, effective_threshold, ack_count,
                    )
            await db.commit()
        return tripped

    # ── 匹配：pending 且依赖全 done → matched ─────────────────

    @staticmethod
    async def _suggest_genes_for_gap(db: AsyncSession, gap_tags: list[str]) -> list[str]:
        """从基因库找覆盖缺口能力的基因（建议安装，产品化"缺失能力建议"）。

        基因量级小（几十条），Python 侧解析 manifest 匹配即可；
        返回展示名列表（最多 3 个）。
        """
        rows = (await db.execute(
            select(Gene.name, Gene.slug, Gene.manifest).where(
                not_deleted(Gene), Gene.manifest.is_not(None),
            )
        )).all()
        wanted = {t.lower().replace("-", "").replace("_", "") for t in gap_tags}
        hits: list[str] = []
        for name, slug, manifest_raw in rows:
            try:
                caps = (json.loads(manifest_raw) or {}).get("capabilities") or []
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(caps, list):
                continue
            if any(
                isinstance(c, str) and c.lower().replace("-", "").replace("_", "") in wanted
                for c in caps
            ):
                hits.append(name or slug)
            if len(hits) >= 3:
                break
        return hits

    async def _match_ready_nodes(self) -> int:
        matched = 0
        async with self._sf() as db:
            missions = (await db.execute(
                select(Mission).where(Mission.status == "executing", not_deleted(Mission))
            )).scalars().all()
            for m in missions:
                nodes = (await db.execute(
                    select(MissionNode)
                    .where(MissionNode.mission_id == m.id, not_deleted(MissionNode))
                    .order_by(MissionNode.seq)
                )).scalars().all()
                done_seqs = {n.seq for n in nodes if n.status == "done"}
                for node in nodes:
                    if node.status != "pending":
                        continue
                    if not all(int(d) in done_seqs for d in (node.depends_on or [])):
                        continue  # 依赖未就绪，本轮不匹配
                    result = await match_node(db, node)
                    # 先记旧匹配状态：下方 UPDATE 经 ORM session 同步会把新值写回
                    # node.match_reason，更新后再读就拿不到"上一轮"的缺口集合了
                    prev_chosen = (node.match_reason or {}).get("chosen")
                    prev_gap = sorted((node.match_reason or {}).get("capability_gap") or [])
                    reason = {
                        "candidates": [
                            {"instance_id": c.instance_id, "instance_name": c.instance_name,
                             "score": c.score, "basis": c.basis}
                            for c in result.candidates
                        ],
                        "chosen": result.chosen_instance_id,
                        "at": _utcnow().isoformat(),
                        "capability_gap": result.capability_gap,
                    }
                    if result.chosen_instance_id:
                        res = await db.execute(
                            update(MissionNode)
                            .where(
                                MissionNode.id == node.id,
                                MissionNode.status == "pending",
                                not_deleted(MissionNode),
                            )
                            .values(
                                status="matched",
                                assigned_instance_id=result.chosen_instance_id,
                                match_reason=reason,
                            )
                        )
                        if res.rowcount:
                            matched += 1
                    else:
                        # 缺口：保持 pending。缺口集合首次出现或变化时，发"缺失能力建议"
                        # （可操作：推荐可安装的基因；无覆盖则提示补充标注），不再静默挂起
                        await db.execute(
                            update(MissionNode)
                            .where(MissionNode.id == node.id, not_deleted(MissionNode))
                            .values(match_reason=reason)
                        )
                        if prev_chosen or prev_gap != sorted(result.capability_gap):
                            suggested = await self._suggest_genes_for_gap(db, result.capability_gap)
                            gap_text = "、".join(result.capability_gap) or "无候选员工"
                            if suggested:
                                content = (f"节点「{node.title}」缺少能力：{gap_text}。"
                                           f"建议为员工安装基因：{'、'.join(suggested)}"
                                           f"（技能市场可获取），安装后自动继续执行")
                            else:
                                content = (f"节点「{node.title}」缺少能力：{gap_text}，"
                                           f"当前基因库暂无覆盖该能力的基因——"
                                           f"可改派员工、取消任务，或联系管理员补充基因能力标注")
                            await MissionEventService(db).append(
                                m.id, org_id=m.org_id, node_id=node.id,
                                event_type="system_note", actor_type="scheduler",
                                content=content,
                                payload={"capability_gap": result.capability_gap,
                                         "suggested_genes": suggested},
                            )
            await db.commit()
        return matched

    # ── 派发：实例行锁 + 占用检查 + CAS，commit 后发送 ─────────

    async def _dispatch_due(self) -> int:
        """新派发（看护重发在 _watchdog 内）。返回本轮派发数。

        依赖门在匹配步（pending→matched 仅当依赖全 done）；此处再防御性复核一次
        ——匹配后上游若被打回/重置，节点停在 matched 不派发，依赖恢复后下一轮自然续派。
        """
        async with self._sf() as db:
            rows = (await db.execute(
                select(MissionNode, Mission.created_at)
                .join(Mission, Mission.id == MissionNode.mission_id)
                .where(
                    MissionNode.status == "matched",
                    MissionNode.assigned_instance_id.is_not(None),
                    Mission.status == "executing",
                    # step_review 门控：暂停审核中的任务不派发下游节点
                    Mission.paused_for_review.is_(False),
                    not_deleted(MissionNode),
                    not_deleted(Mission),
                )
                .order_by(Mission.priority.desc(), Mission.created_at, MissionNode.seq)
            )).all()
            # 各 Mission 的 done seq 集合（派发前复核依赖）
            mission_ids = {node.mission_id for node, _ in rows}
            done_seqs: dict[str, set[int]] = {mid: set() for mid in mission_ids}
            if mission_ids:
                for n in (await db.execute(
                    select(MissionNode).where(
                        MissionNode.mission_id.in_(mission_ids),
                        MissionNode.status == "done",
                        not_deleted(MissionNode),
                    )
                )).scalars().all():
                    done_seqs[n.mission_id].add(n.seq)

        queues: dict[str, list[MissionNode]] = {}
        for node, _ in rows:
            if not all(int(d) in done_seqs[node.mission_id] for d in (node.depends_on or [])):
                continue
            queues.setdefault(node.assigned_instance_id, []).append(node)

        dispatched = 0
        for inst_id, nodes in queues.items():
            if self._tunnel is not None and inst_id not in getattr(self._tunnel, "connected_instances", set()):
                continue
            try:
                ok, package, task_id = await self._dispatch_one(inst_id, nodes[0])
            except Exception:
                logger.warning("mission 派发失败 instance=%s", inst_id, exc_info=True)
                continue
            if ok:
                dispatched += 1
                self._send_fire_and_forget(inst_id, package, task_id)
        return dispatched

    async def _dispatch_one(self, inst_id: str, node: MissionNode) -> tuple[bool, dict, str]:
        """锁实例行 → 占用检查 → CAS matched→dispatched → 组装任务包，commit 后返回待发送。"""
        task_id = str(uuid.uuid4())
        async with self._sf() as db:
            # 实例行锁：串行化不同副本对同一实例的派发决策（NOT EXISTS 单独用有并发窗口）
            locked = (await db.execute(
                select(Instance).where(Instance.id == inst_id).with_for_update().limit(1)
            )).scalar_one_or_none()
            if locked is None:
                return False, {}, task_id
            occupied = (await db.execute(
                select(exists().where(
                    MissionNode.assigned_instance_id == inst_id,
                    MissionNode.status.in_(OCCUPYING_STATUSES),
                    not_deleted(MissionNode),
                ))
            )).scalar()
            if occupied:
                return False, {}, task_id

            res = await db.execute(
                update(MissionNode)
                .where(
                    MissionNode.id == node.id,
                    MissionNode.status == "matched",
                    not_deleted(MissionNode),
                )
                .values(
                    status="dispatched",
                    last_task_id=task_id,
                    dispatched_at=func.now(),
                )
            )
            if not res.rowcount:
                return False, {}, task_id  # 另一副本已抢到该节点

            mission = await db.get(Mission, node.mission_id)
            package = await self._build_task_package(db, mission, node)
            await MissionEventService(db).append(
                mission.id, org_id=mission.org_id, node_id=node.id,
                event_type="dispatched", actor_type="scheduler",
                content=f"节点「{node.title}」已派发给 {locked.name}",
                payload={"task_id": task_id, "instance_id": inst_id},
            )
            await db.commit()
            return True, package, task_id

    async def _build_task_package(self, db: AsyncSession, mission: Mission, node: MissionNode) -> dict:
        """组装 §7.1 任务包（upstream_artifacts 带签名 URL + upstream_conclusions 结论文）。"""
        siblings = (await db.execute(
            select(MissionNode).where(
                MissionNode.mission_id == mission.id, not_deleted(MissionNode),
            )
        )).scalars().all()
        upstream_nodes = [n for n in siblings if n.seq in (node.depends_on or [])]
        upstream_ids = [n.id for n in upstream_nodes]

        upstream_artifacts: list[dict] = []
        if upstream_ids:
            arts = (await db.execute(
                select(MissionArtifact).where(
                    MissionArtifact.node_id.in_(upstream_ids),
                    not_deleted(MissionArtifact),
                )
            )).scalars().all()
            from app.services.storage_service import get_presigned_url
            for a in arts:
                try:
                    url = await get_presigned_url(a.storage_key)
                except Exception:
                    logger.warning("mission 产物签名 URL 生成失败: %s", a.storage_key, exc_info=True)
                    url = ""
                upstream_artifacts.append({"name": a.name, "kind": a.kind, "storage_url": url})

        # P2 结论文交接：取上游节点 node_done 事件的 payload.summary，按 seq 有序注入
        upstream_conclusions: list[dict] = []
        if upstream_ids:
            done_events = (await db.execute(
                select(MissionEvent).where(
                    MissionEvent.node_id.in_(upstream_ids),
                    MissionEvent.event_type == "node_done",
                    not_deleted(MissionEvent),
                )
            )).scalars().all()
            summary_by_node = {}
            for ev in done_events:
                payload = ev.payload if isinstance(ev.payload, dict) else {}
                summary_by_node[ev.node_id] = str(payload.get("summary") or ev.content or "")[:500]
            for up in sorted(upstream_nodes, key=lambda n: n.seq):
                summary = summary_by_node.get(up.id, "")
                if summary:
                    upstream_conclusions.append({
                        "seq": up.seq, "title": up.title, "summary": summary,
                    })

        brief = mission.brief or {}
        policy = mission.escalation_policy if isinstance(mission.escalation_policy, dict) else {}

        # 编排者打回反馈（含最近一次人工答复）注入任务包，驱动子智能体针对性改进
        from app.services.mission.coordinator_review import collect_review_feedback
        review_feedback = await collect_review_feedback(db, node.id, mission.id)

        return {
            "session_key": node.session_key,
            "mission_title": mission.title,
            "brief": {
                "goal": brief.get("goal", ""),
                "constraints": brief.get("constraints", []),
                "acceptance_criteria": brief.get("acceptance_criteria", []),
            },
            "subtask": {
                "title": node.title,
                "description": node.description or "",
                "acceptance_criteria": node.acceptance_criteria or "",
            },
            "upstream_conclusions": upstream_conclusions,
            "upstream_artifacts": upstream_artifacts,
            "review_feedback": review_feedback,
            "escalation_rules": {
                "l2_rules": policy.get("l2_rules", []),
                "l1_hint": policy.get("l1_hint", "可带假设继续的歧义，先说明假设再继续"),
            },
            "report_guidance": "执行中请定期调用 report 工具播报进展（人类可见）",
        }

    def _send_fire_and_forget(self, inst_id: str, package: dict, task_id: str) -> None:
        async def _run() -> None:
            try:
                await self._sender(inst_id, package, task_id)
            except Exception:
                # commit 已完成：由 ack 超时看护 60s 后重发兜底
                logger.warning("mission 任务包发送失败 instance=%s task=%s", inst_id, task_id, exc_info=True)
        asyncio.create_task(_run())

    # ── 超时看护：ack 超时重发/失败 + running 停滞 L2 ──────────

    async def _watchdog(self) -> tuple[int, int]:
        """返回 (failed 数, stalled 数)。DB 驱动，重启接管由本逻辑天然覆盖。"""
        failed = stalled = 0
        now = _utcnow()
        ack_cutoff = now - timedelta(seconds=self._ack_timeout_s)
        stall_cutoff = now - timedelta(seconds=self._running_stall_s)

        # ── ack 超时：重发或失败 ──
        async with self._sf() as db:
            overdue = (await db.execute(
                select(MissionNode).where(
                    MissionNode.status == "dispatched",
                    MissionNode.dispatched_at < ack_cutoff,
                    not_deleted(MissionNode),
                )
            )).scalars().all()
            resend_jobs: list[tuple[MissionNode, dict]] = []
            for node in overdue:
                mission = await db.get(Mission, node.mission_id)
                if mission is None or mission.status != "executing":
                    continue
                if node.attempt_count >= node.max_attempts:
                    res = await db.execute(
                        update(MissionNode)
                        .where(
                            MissionNode.id == node.id,
                            MissionNode.status == "dispatched",
                            not_deleted(MissionNode),
                        )
                        .values(status="failed", finished_at=func.now())
                    )
                    if res.rowcount:
                        failed += 1
                        await MissionEventService(db).append(
                            mission.id, org_id=mission.org_id, node_id=node.id,
                            event_type="l2_question", actor_type="scheduler",
                            content=f"节点「{node.title}」多次派发无响应（{node.max_attempts} 次），"
                                    f"请改派或取消",
                            payload={"reason": "ack_timeout_exhausted"},
                        )
                    continue
                res = await db.execute(
                    update(MissionNode)
                    .where(
                        MissionNode.id == node.id,
                        MissionNode.status == "dispatched",
                        not_deleted(MissionNode),
                    )
                    .values(attempt_count=node.attempt_count + 1, dispatched_at=func.now())
                )
                if res.rowcount:
                    package = await self._build_task_package(db, mission, node)
                    resend_jobs.append((node, package))
            await db.commit()
        for node, package in resend_jobs:
            self._send_fire_and_forget(node.assigned_instance_id, package, node.last_task_id)

        # ── running 停滞：仅 L2 提示，不改状态（设计 §6）──
        async with self._sf() as db:
            running = (await db.execute(
                select(MissionNode).where(
                    MissionNode.status == "running", not_deleted(MissionNode),
                )
            )).scalars().all()
            for node in running:
                # P1.6 上下文防爆：tool_trace 超限 → 自动暂停 + L2（防死循环刷上下文）
                tool_count = (await db.execute(
                    select(func.count()).where(
                        MissionEvent.node_id == node.id,
                        MissionEvent.event_type == "tool_trace",
                        not_deleted(MissionEvent),
                    )
                )).scalar() or 0
                if tool_count > self._max_tool_traces:
                    if await self._has_open_l2(db, node.id):
                        continue
                    mission = await db.get(Mission, node.mission_id)
                    if mission is None or mission.status != "executing":
                        continue
                    await db.execute(
                        update(MissionNode)
                        .where(MissionNode.id == node.id, MissionNode.status == "running")
                        .values(status="blocked_question")
                    )
                    await MissionEventService(db).append(
                        mission.id, org_id=mission.org_id, node_id=node.id,
                        event_type="l2_question", actor_type="scheduler",
                        content=f"节点「{node.title}」工具调用已达 {tool_count} 次"
                                f"（上限 {self._max_tool_traces}），可能存在循环，请人工检查",
                        payload={"reason": "tool_trace_limit", "count": tool_count},
                    )
                    stalled += 1
                    continue

                last_active = (await db.execute(
                    select(func.max(MissionEvent.created_at)).where(MissionEvent.node_id == node.id)
                )).scalar() or node.started_at or node.dispatched_at
                if last_active is None or last_active > stall_cutoff:
                    continue
                if await self._has_open_l2(db, node.id):
                    continue
                mission = await db.get(Mission, node.mission_id)
                if mission is None or mission.status != "executing":
                    continue
                await MissionEventService(db).append(
                    mission.id, org_id=mission.org_id, node_id=node.id,
                    event_type="l2_question", actor_type="scheduler",
                    content=f"节点「{node.title}」已超过 {self._running_stall_s // 60} 分钟无进展，疑似停滞，请关注",
                    payload={"reason": "running_stalled"},
                )
                stalled += 1
            await db.commit()
        return failed, stalled

    @staticmethod
    async def _has_open_l2(db: AsyncSession, node_id: str) -> bool:
        """该节点是否有未回答的 L2（最新的 l2/question_answered 事件是 l2_question）。"""
        row = (await db.execute(
            select(MissionEvent.event_type)
            .where(
                MissionEvent.node_id == node_id,
                MissionEvent.event_type.in_(("l2_question", "question_answered")),
                not_deleted(MissionEvent),
            )
            .order_by(MissionEvent.mission_id, MissionEvent.seq.desc())
            .limit(1)
        )).scalar_one_or_none()
        return row == "l2_question"

    # ── 默认发送：mission.v1 信封优先，legacy chat.request 降级 ──────────────

    async def _smart_sender(self, instance_id: str, package: dict, task_id: str) -> bool:
        """按版本协商选择通道（设计 §7.4）：插件握手上报 mission.v1 的实例走
        mission.task.dispatch 信封（有 ack/播报/产物），旧镜像走 chat.request 文本。"""
        tunnel = self._tunnel
        if tunnel is not None and instance_id in getattr(
            tunnel, "mission_capable_instances", set(),
        ):
            await tunnel.send_mission_dispatch(instance_id, task_id, package)
            return True
        return await self._legacy_sender(instance_id, package, task_id)

    # ── legacy 降级路径：chat.request 文本（旧镜像，只回最终文本）──

    async def _legacy_sender(self, instance_id: str, package: dict, task_id: str) -> bool:
        """旧镜像（无 mission.v1 协议）走现有 chat.request 下发任务文本（设计 §7.4）。

        旧插件不会回 mission.* 消息：发送成功后合成 ack；流结束时把最终回复文本
        合成为 mission.task.done（summary=全文），交 ingest 走常规状态机。
        """
        from app.services.tunnel import tunnel_adapter

        lines = [
            f"[任务包 task_id={task_id}]",
            f"任务: {package['mission_title']} - {package['subtask']['title']}",
            f"目标: {package['brief']['goal']}",
        ]
        if package['subtask']['description']:
            lines.append(f"说明: {package['subtask']['description']}")
        if package['subtask']['acceptance_criteria']:
            lines.append(f"验收标准: {package['subtask']['acceptance_criteria']}")
        if package['upstream_artifacts']:
            names = ", ".join(a['name'] for a in package['upstream_artifacts'])
            lines.append(f"上游产物: {names}")
        if package['escalation_rules']['l2_rules']:
            lines.append("必须阻塞等待人类的情形: " + "; ".join(package['escalation_rules']['l2_rules']))
        lines.append(package['report_guidance'])
        lines.append("完成后请直接给出最终结果全文。")

        stream = await tunnel_adapter.send_chat_request(
            instance_id,
            [{"role": "user", "content": "\n".join(lines)}],
            stream=True,
        )

        # 旧插件无 mission 协议：立即合成 ack（否则 60s 看护会误判超时重发）
        from app.services.mission.ingest_service import handle_mission_message
        await handle_mission_message(instance_id, {
            "type": "mission.task.ack", "task_id": task_id, "data": {},
        })

        async def _drain() -> None:
            # 消费回复流（避免流队列泄漏）；DONE 时把全文合成 mission.task.done
            from app.services.tunnel.protocol import TunnelMessageType
            full_text: list[str] = []
            failed = False
            try:
                async for msg in stream:
                    if msg.type == TunnelMessageType.CHAT_RESPONSE_ERROR:
                        failed = True
                    content = msg.payload.get("content", "")
                    if content:
                        full_text.append(str(content))
            except Exception:
                logger.warning("mission legacy 回复流中断 task=%s", task_id, exc_info=True)
                return
            if failed:
                await handle_mission_message(instance_id, {
                    "type": "mission.task.blocked", "task_id": task_id,
                    "data": {"reason": "legacy 实例执行失败",
                             "question": {"level": "L1", "message": "旧镜像执行出错，可重试或改派"}},
                })
            else:
                await handle_mission_message(instance_id, {
                    "type": "mission.task.done", "task_id": task_id,
                    "data": {"summary": "".join(full_text)[:4000]},
                })

        asyncio.create_task(_drain())
        return True


# ── lifespan 启停（main.py 调用，模式同 heartbeat_scanner）──────────────────

_scheduler_task: asyncio.Task | None = None


def start_mission_scheduler(session_factory: Any, **kwargs: Any) -> asyncio.Task | None:
    """启动调度器常驻协程（重复调用幂等）。"""
    global _scheduler_task
    if _scheduler_task is not None and not _scheduler_task.done():
        return _scheduler_task
    from app.services.tunnel import tunnel_adapter
    scheduler = MissionScheduler(session_factory, tunnel=tunnel_adapter, **kwargs)
    _scheduler_task = asyncio.create_task(scheduler.run_forever())
    logger.info("Mission 调度器已启动（间隔 %ss）", SCAN_INTERVAL_S)
    return _scheduler_task


def stop_mission_scheduler() -> None:
    global _scheduler_task
    if _scheduler_task is not None and not _scheduler_task.done():
        _scheduler_task.cancel()
    _scheduler_task = None
