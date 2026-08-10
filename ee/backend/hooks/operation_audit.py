"""Operation audit hook — EE 模式下复用 CE 的 user 操作审计逻辑。

EE 与 CE 目前记录同样范围的操作审计（仅 actor_type=user）；
agent（AI 员工）操作审计是后续独立需求，此处未处理。
"""
import logging

from app.services.audit_handler import register_ce_audit_handler

logger = logging.getLogger(__name__)


def register_hooks() -> None:
    register_ce_audit_handler()
    logger.info("EE 操作审计 handler 已注册（复用 CE 逻辑）")
