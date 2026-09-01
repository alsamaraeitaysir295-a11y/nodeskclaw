"""任务空间事件流服务（设计 T2）。

设计见 docs/mission-space-p1-design.md §3.3：
- MissionEvent 写入路径唯一：全部经本服务 append()，内部用 mission_event_counters
  计数行生成 seq——INSERT ... ON CONFLICT DO NOTHING 补建 + UPDATE ... RETURNING
  原子自增取号，行级锁天然串行化同一 Mission 的并发写入
  （禁用 SELECT max()+1：聚合查询加 FOR UPDATE 锁不住任何行/间隙，空表并发首写必撞唯一索引）
- 事务由调用方控制：append 只 flush 不 commit，便于与节点状态变更
  （调度器 CAS / ingest 处理）同事务提交，保证"状态变迁 + 事件"原子性
"""
from typing import Any

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.mission_event import (
    EventVisibility,
    MissionEvent,
    MissionEventCounter,
)


class MissionEventService:
    """统一事件流写入：每 Mission 内 seq 单调递增，多 Mission 互不阻塞。"""

    def __init__(self, db: AsyncSession):
        self._db = db

    async def append(
        self,
        mission_id: str,
        *,
        org_id: str,
        event_type: str,
        actor_type: str,
        node_id: str | None = None,
        actor_id: str | None = None,
        actor_name: str | None = None,
        visibility: str = EventVisibility.both,
        content: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> MissionEvent:
        """追加一条事件并分配 seq（flush 不 commit，事务由调用方控制）。

        计数行缺失时自动补建（幂等 upsert），兼容未走标准创建路径的 Mission。
        """
        # 同事务两步：幂等补建计数行 + 原子自增取号（并发 append 在行锁上排队，
        # 各自拿到不同 next_seq；seq 单调但不保证连续——回滚会产生空洞，可接受）
        await self._db.execute(
            pg_insert(MissionEventCounter)
            .values(mission_id=mission_id, next_seq=0)
            .on_conflict_do_nothing(index_elements=[MissionEventCounter.mission_id])
        )
        seq = (await self._db.execute(
            update(MissionEventCounter)
            .where(MissionEventCounter.mission_id == mission_id)
            .values(next_seq=MissionEventCounter.next_seq + 1)
            .returning(MissionEventCounter.next_seq)
        )).scalar_one()

        ev = MissionEvent(
            mission_id=mission_id,
            node_id=node_id,
            org_id=org_id,
            seq=seq,
            event_type=event_type,
            actor_type=actor_type,
            actor_id=actor_id,
            actor_name=actor_name,
            visibility=visibility,
            content=content,
            payload=payload,
        )
        self._db.add(ev)
        await self._db.flush()
        return ev
