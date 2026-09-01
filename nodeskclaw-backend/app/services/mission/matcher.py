"""任务空间匹配器 v1（设计 §5 / T4）。

职责：把节点的 capability_tags 与实例已装基因的能力清单做确定性比较（不调 LLM），
给出候选/人选/缺口。能力来源 = InstanceGene(status='installed') → Gene.manifest 的
capabilities 数组（manifest 是 Text 存 JSON 字符串，读取必须 json.loads 容错）；
manifest 缺 capabilities 键时回退 Gene.name；**capabilities 为显式空数组则不回退**
（行为型基因不提供工作能力，设计 §3.6/T4a 标注语义）。

评分（设计 §5）：规范化（小写、去连字符/下划线/空格）后
完全命中 1.0 / 包含关系 0.6 / 编辑距离相似度 ≥0.75 得 0.4；节点得分 = 标签均分。
并列取历史完成节点数多者（再并列取 instance_id 保证确定性）。
"""
import difflib
import json
import logging
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import not_deleted
from app.models.gene import Gene, InstanceGene
from app.models.instance import Instance
from app.models.mission import Mission
from app.models.mission_node import MissionNode
from app.models.workspace_agent import WorkspaceAgent

logger = logging.getLogger(__name__)

SCORE_EXACT = 1.0
SCORE_CONTAINS = 0.6
SCORE_SIMILAR = 0.4
SIMILARITY_THRESHOLD = 0.75
# 包含判定最短长度（避免 "api" 命中 "rapid" 之类的短串误报）
_CONTAIN_MIN_LEN = 4


@dataclass
class MatchCandidate:
    instance_id: str
    instance_name: str
    score: float
    done_count: int = 0
    # 每标签命中说明，如 "backend=1.0(backend)"，审计展示用
    basis: list[str] = field(default_factory=list)


@dataclass
class MatchResult:
    candidates: list[MatchCandidate] = field(default_factory=list)
    chosen_instance_id: str | None = None
    chosen_instance_name: str | None = None
    # 全体实例都未命中的标签（capability_gap）
    capability_gap: list[str] = field(default_factory=list)


def _normalize(s: str) -> str:
    return s.lower().replace("-", "").replace("_", "").replace(" ", "")


def _tag_score(tag_norm: str, cap_norm: str) -> float:
    if not tag_norm or not cap_norm:
        return 0.0
    if tag_norm == cap_norm:
        return SCORE_EXACT
    if len(tag_norm) >= _CONTAIN_MIN_LEN and len(cap_norm) >= _CONTAIN_MIN_LEN and (
        tag_norm in cap_norm or cap_norm in tag_norm
    ):
        return SCORE_CONTAINS
    ratio = difflib.SequenceMatcher(None, tag_norm, cap_norm).ratio()
    if ratio >= SIMILARITY_THRESHOLD:
        return SCORE_SIMILAR
    return 0.0


def _capabilities_from_manifest(manifest_raw: str | None, gene_name: str) -> list[str]:
    """manifest JSON → capabilities 数组；键缺失回退基因名；空数组不回退。"""
    try:
        manifest = json.loads(manifest_raw) if manifest_raw else {}
    except (json.JSONDecodeError, TypeError):
        manifest = {}
    if not isinstance(manifest, dict) or "capabilities" not in manifest:
        return [gene_name]
    caps = manifest["capabilities"]
    return [c for c in caps if isinstance(c, str) and c.strip()] if isinstance(caps, list) else []


async def _load_roster(
    db: AsyncSession, workspace_id: str
) -> tuple[list[tuple[str, str]], dict[str, list[str]], dict[str, int]]:
    """花名册：[(instance_id, name)] + 每实例能力串清单 + 每实例历史完成节点数。"""
    instances = (await db.execute(
        select(Instance.id, Instance.name)
        .join(WorkspaceAgent, WorkspaceAgent.instance_id == Instance.id)
        .where(
            WorkspaceAgent.workspace_id == workspace_id,
            not_deleted(WorkspaceAgent),
            not_deleted(Instance),
        )
        .distinct()
    )).all()

    gene_rows = []
    if instances:
        inst_ids = [i.id for i in instances]
        gene_rows = (await db.execute(
            select(InstanceGene.instance_id, Gene.name, Gene.manifest)
            .join(Gene, Gene.id == InstanceGene.gene_id)
            .where(
                InstanceGene.instance_id.in_(inst_ids),
                InstanceGene.status == "installed",
                not_deleted(InstanceGene),
                not_deleted(Gene),
            )
        )).all()

    capabilities: dict[str, list[str]] = {i.id: [] for i in instances}
    for row in gene_rows:
        capabilities[row.instance_id].extend(
            _capabilities_from_manifest(row.manifest, row.name)
        )

    done_counts: dict[str, int] = {i.id: 0 for i in instances}
    if instances:
        inst_ids = [i.id for i in instances]
        for inst_id, cnt in (await db.execute(
            select(MissionNode.assigned_instance_id, func.count())
            .where(
                MissionNode.assigned_instance_id.in_(inst_ids),
                MissionNode.status == "done",
                not_deleted(MissionNode),
            )
            .group_by(MissionNode.assigned_instance_id)
        )).all():
            done_counts[inst_id] = cnt

    return [(i.id, i.name) for i in instances], capabilities, done_counts


async def match_node(db: AsyncSession, node: MissionNode) -> MatchResult:
    """对单个节点跑匹配：候选（全部得分>0 降序）、chosen、capability_gap。"""
    mission = await db.get(Mission, node.mission_id)
    if mission is None:
        return MatchResult(capability_gap=list(node.capability_tags))

    roster, capabilities, done_counts = await _load_roster(db, mission.workspace_id)
    tags = [t for t in node.capability_tags if isinstance(t, str) and t.strip()]
    tag_norms = [(_normalize(t), t) for t in tags]

    candidates: list[MatchCandidate] = []
    tag_best: dict[str, float] = {t: 0.0 for _, t in tag_norms}
    for inst_id, inst_name in roster:
        cap_norms = [_normalize(c) for c in capabilities.get(inst_id, [])]
        basis: list[str] = []
        total = 0.0
        for tag_norm, tag_raw in tag_norms:
            score = max((_tag_score(tag_norm, cn) for cn in cap_norms), default=0.0)
            tag_best[tag_raw] = max(tag_best[tag_raw], score)
            total += score
            if score > 0:
                basis.append(f"{tag_raw}={score}")
        if not tag_norms:
            continue
        avg = total / len(tag_norms)
        if avg > 0:
            candidates.append(MatchCandidate(
                instance_id=inst_id,
                instance_name=inst_name,
                score=round(avg, 4),
                done_count=done_counts.get(inst_id, 0),
                basis=basis,
            ))

    candidates.sort(key=lambda c: (-c.score, -c.done_count, c.instance_id))
    gap = [t for _, t in tag_norms if tag_best[t] == 0.0]
    return MatchResult(
        candidates=candidates,
        chosen_instance_id=candidates[0].instance_id if candidates else None,
        chosen_instance_name=candidates[0].instance_name if candidates else None,
        capability_gap=gap,
    )


async def coverage_check(db: AsyncSession, mission_id: str) -> list[dict]:
    """覆盖检查（设计 §5）：awaiting_confirm 时对全部节点 dry-run，供确认页展示。"""
    nodes = (await db.execute(
        select(MissionNode)
        .where(MissionNode.mission_id == mission_id, not_deleted(MissionNode))
        .order_by(MissionNode.seq)
    )).scalars().all()

    results: list[dict] = []
    for node in nodes:
        m = await match_node(db, node)
        results.append({
            "node_id": node.id,
            "seq": node.seq,
            "title": node.title,
            "capability_tags": node.capability_tags,
            "suggested_instance_id": m.chosen_instance_id,
            "suggested_instance_name": m.chosen_instance_name,
            "candidates": [
                {"instance_id": c.instance_id, "instance_name": c.instance_name,
                 "score": c.score, "done_count": c.done_count, "basis": c.basis}
                for c in m.candidates
            ],
            "capability_gap": m.capability_gap,
        })
    return results
