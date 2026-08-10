"""batch_resolve_actor_display_names 单元测试 — 姓名优先、邮箱兜底、无匹配则不入字典。"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.audit_actor_names import batch_resolve_actor_display_names


def _mock_db(rows: list[tuple[str, str | None, str | None]]):
    db = AsyncMock()
    result = MagicMock()
    result.all.return_value = rows
    db.execute = AsyncMock(return_value=result)
    return db


@pytest.mark.asyncio
async def test_empty_actor_ids_returns_empty_dict_without_query():
    db = _mock_db([])
    out = await batch_resolve_actor_display_names(db, set())
    assert out == {}
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_name_preferred_over_email():
    uid = str(uuid.uuid4())
    db = _mock_db([(uid, "顾明强", "guo@example.com")])
    out = await batch_resolve_actor_display_names(db, {uid})
    assert out[uid] == "顾明强"


@pytest.mark.asyncio
async def test_falls_back_to_email_when_name_empty():
    uid = str(uuid.uuid4())
    db = _mock_db([(uid, None, "guo@example.com")])
    out = await batch_resolve_actor_display_names(db, {uid})
    assert out[uid] == "guo@example.com"


@pytest.mark.asyncio
async def test_both_empty_not_included_in_result():
    uid = str(uuid.uuid4())
    db = _mock_db([(uid, None, None)])
    out = await batch_resolve_actor_display_names(db, {uid})
    assert uid not in out
