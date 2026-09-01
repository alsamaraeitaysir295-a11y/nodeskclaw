"""T3：编排器 v1 —— 拆解/校验/环检测/轻任务判定（纯逻辑，LLM 经 fake 注入）。

验收（设计 §11 T3）：给定测试需求产出合法 DAG；构造环输入被拒。
不依赖数据库与真实 LLM。
"""
import json

import pytest

from app.services.mission.orchestrator import (
    DecompositionError,
    _extract_first_json_block,
    decompose,
)

VOCAB = [
    {"tag": "backend", "label_zh": "后端开发", "description": "服务端与 API 开发"},
    {"tag": "frontend", "label_zh": "前端开发", "description": "Web 页面开发"},
    {"tag": "document-writing", "label_zh": "文档撰写", "description": "报告与文档撰写"},
]


def _fake_chat(responses: list[str]):
    """按序返回预设响应的 chat 函数；记录收到的 prompt。"""
    prompts: list[str] = []

    async def chat(prompt: str) -> str:
        prompts.append(prompt)
        return responses[len(prompts) - 1]

    return chat, prompts


def _valid_payload(*, nodes: list[dict], lightweight: bool = False) -> str:
    return json.dumps({
        "mission_title": "竞品分析报告",
        "brief": {
            "goal": "输出三竞品对比报告",
            "constraints": ["使用公开资料"],
            "acceptance_criteria": ["包含功能/价格/优劣势对比"],
            "key_decisions": ["以表格呈现"],
        },
        "is_lightweight_candidate": lightweight,
        "nodes": nodes,
        "escalation": {"l2_rules": ["需要付费购买数据时"]},
    }, ensure_ascii=False)


# ── JSON 提取 ───────────────────────────────────────────────────────────────


def test_extract_first_json_block_variants():
    obj = {"a": {"b": 1}}
    assert _extract_first_json_block(json.dumps(obj)) == obj
    assert _extract_first_json_block("前置说明\n```json\n" + json.dumps(obj) + "\n```") == obj
    assert _extract_first_json_block('回答：{"t": "含 } 花括号"} 尾巴') == {"t": "含 } 花括号"}
    with pytest.raises(DecompositionError):
        _extract_first_json_block("完全没有 JSON")


# ── 合法 DAG ────────────────────────────────────────────────────────────────


async def test_decompose_valid_dag():
    """3 节点依赖链（0 ← 1 ← 2）拆解成功，字段与 meta 齐全，standard 类型。"""
    payload = _valid_payload(nodes=[
        {"title": "搜集资料", "description": "", "acceptance_criteria": "资料清单",
         "capability_tags": ["backend"], "depends_on": []},
        {"title": "整理对比", "description": "", "acceptance_criteria": "对比表",
         "capability_tags": ["document-writing"], "depends_on": [0]},
        {"title": "页面呈现", "description": "", "acceptance_criteria": "页面",
         "capability_tags": ["frontend", "backend"], "depends_on": [0, 1]},
    ])
    chat, prompts = _fake_chat([payload])
    result = await decompose("做一份竞品分析", VOCAB, chat=chat)

    assert result.mission_title == "竞品分析报告"
    assert result.mission_type == "standard"
    assert len(result.nodes) == 3
    assert result.nodes[2].depends_on == [0, 1]
    assert result.l2_rules == ["需要付费购买数据时"]
    assert result.coordinator_meta == {
        "engine": "builtin", "prompt_version": "v1", "schema_version": "v1",
    }
    # prompt 必须带词表（tag + 中文名 + 语义）与需求原文
    assert "backend | 后端开发 | 服务端与 API 开发" in prompts[0]
    assert "做一份竞品分析" in prompts[0]


async def test_decompose_lightweight():
    """candidate + 单节点 → lightweight。"""
    payload = _valid_payload(nodes=[
        {"title": "直接写报告", "description": "", "acceptance_criteria": "报告",
         "capability_tags": ["document-writing"], "depends_on": []},
    ], lightweight=True)
    chat, _ = _fake_chat([payload])
    result = await decompose("写一篇短文", VOCAB, chat=chat)
    assert result.mission_type == "lightweight"


# ── 非法输入被拒 ────────────────────────────────────────────────────────────


async def test_decompose_cycle_rejected():
    """0 依赖 1、1 依赖 0 成环 → 两次尝试均失败 → DecompositionError。"""
    payload = _valid_payload(nodes=[
        {"title": "A", "description": "", "acceptance_criteria": "",
         "capability_tags": ["backend"], "depends_on": [1]},
        {"title": "B", "description": "", "acceptance_criteria": "",
         "capability_tags": ["backend"], "depends_on": [0]},
    ])
    chat, prompts = _fake_chat([payload, payload])
    with pytest.raises(DecompositionError, match="环"):
        await decompose("成环需求", VOCAB, chat=chat)
    assert len(prompts) == 2  # 重试 1 次


async def test_decompose_self_dependency_rejected():
    payload = _valid_payload(nodes=[
        {"title": "A", "description": "", "acceptance_criteria": "",
         "capability_tags": ["backend"], "depends_on": [0]},
    ])
    chat, _ = _fake_chat([payload, payload])
    with pytest.raises(DecompositionError, match="自依赖"):
        await decompose("自依赖", VOCAB, chat=chat)


async def test_decompose_unknown_tag_rejected():
    """词表外标签（design 示例 web-frontend 未入词表）被拒。"""
    payload = _valid_payload(nodes=[
        {"title": "A", "description": "", "acceptance_criteria": "",
         "capability_tags": ["web-frontend"], "depends_on": []},
    ])
    chat, _ = _fake_chat([payload, payload])
    with pytest.raises(DecompositionError, match="词表外"):
        await decompose("未知标签", VOCAB, chat=chat)


async def test_decompose_dep_index_out_of_range():
    payload = _valid_payload(nodes=[
        {"title": "A", "description": "", "acceptance_criteria": "",
         "capability_tags": ["backend"], "depends_on": [5]},
    ])
    chat, _ = _fake_chat([payload, payload])
    with pytest.raises(DecompositionError, match="越界"):
        await decompose("越界依赖", VOCAB, chat=chat)


async def test_decompose_empty_vocab_rejected():
    async def chat(prompt: str) -> str:
        return "{}"
    with pytest.raises(DecompositionError, match="词表为空"):
        await decompose("需求", [], chat=chat)


# ── 重试成功 ────────────────────────────────────────────────────────────────


async def test_decompose_retry_then_success():
    """第一次输出烂文本，第二次合法 → 成功且共调用 2 次。"""
    payload = _valid_payload(nodes=[
        {"title": "A", "description": "", "acceptance_criteria": "",
         "capability_tags": ["backend"], "depends_on": []},
    ])
    chat, prompts = _fake_chat(["抱歉我不会输出 JSON", payload])
    result = await decompose("先错后对", VOCAB, chat=chat)
    assert result.mission_title == "竞品分析报告"
    assert len(prompts) == 2
