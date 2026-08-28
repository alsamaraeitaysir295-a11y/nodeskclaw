# EE Backend Models
# 注意：必须在这里显式导入全部模型，否则 alembic autogenerate 与运行时
# 的 Base.metadata 都看不到 EE 表（此前为空文件导致 plans/subscriptions
# 从未被建表，2026-08-27 修复）
from ee.backend.models.plan import Plan  # noqa: F401
from ee.backend.models.subscription import Subscription  # noqa: F401
