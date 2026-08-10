"""EE operation_audit hook 注册测试 — 验证 EE 模式下 handler 真正订阅了事件。"""

from __future__ import annotations

import pytest

from app.core import hooks
from app.services.audit_handler import _on_operation_audit
from ee.backend.hooks.operation_audit import register_hooks


@pytest.fixture(autouse=True)
def _clear_hooks():
    hooks.clear("operation_audit")
    yield
    hooks.clear("operation_audit")


def test_register_hooks_subscribes_operation_audit_handler():
    register_hooks()
    assert _on_operation_audit in hooks._handlers["operation_audit"]
