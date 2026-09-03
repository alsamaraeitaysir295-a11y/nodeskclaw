"""Shared test fixtures."""

from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.core.deps import get_db
from app.main import app
from app.models import Base

import os

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://nodeskclaw:nodeskclaw123@localhost:5432/nodeskclaw_rbac_test",
)

engine = create_async_engine(TEST_DATABASE_URL, echo=False)
TestSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# 注：pytest-asyncio 1.x 不再支持通过覆盖 event_loop fixture 来自定义事件循环
# （旧写法在 1.x 下会被忽略，导致每个测试函数拿到不同的事件循环，
# 而 engine 是模块级单例，其 asyncpg 连接池绑定在首次使用时的事件循环上，
# 从而在第二个测试起报 InterfaceError / 跨事件循环 Future 错误）。
# 改为在 pyproject.toml 的 [tool.pytest.ini_options] 中设置
# asyncio_default_fixture_loop_scope = "session"，让整个会话共用同一个
# 事件循环，与本模块级 engine 单例的生命周期保持一致。


@pytest.fixture(autouse=True)
async def setup_db():
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            # 平台能力词表种子：本 fixture 的 drop_all 可能清掉迁移种的词表
            # （重建为空表），拆解/对话修改类测试依赖词表非空。注意不能 import
            # 迁移模块取种子——venv 安装的 alembic 库遮蔽本地 alembic/ 目录。
            from app.services.mission.capability_seed import seed_platform_tags

            await conn.run_sync(seed_platform_tags)
    except Exception:
        # CI / local 环境未提供测试库时，跳过数据库初始化，允许无 DB 的基础测试继续执行
        yield
        return

    yield

    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
    except Exception:
        pass


async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
    async with TestSessionLocal() as session:
        yield session


app.dependency_overrides[get_db] = override_get_db


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
