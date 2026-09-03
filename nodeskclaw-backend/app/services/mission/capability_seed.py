"""平台预置能力词表种子数据（设计 §3.6）。

同源副本：alembic/versions/bbd32dcd7f62_add_mission_tables.py 的
PLATFORM_CAPABILITY_TAGS（迁移需自包含故各持一份）——新增/修改标签必须两处同步。
此模块供测试基建（tests/conftest.py 的 setup_db）在 drop_all 重建空表后
重新播种使用；venv 安装的 alembic 库会遮蔽本地 alembic/ 目录，测试侧
不能直接 import 迁移模块。
"""
from sqlalchemy import insert as sa_insert
from sqlalchemy import select as sa_select
from sqlalchemy.engine import Connection

from app.models.capability_tag import CapabilityTag

PLATFORM_CAPABILITY_TAGS = [
    ("backend", "后端开发", "Backend", "服务端程序与 API 设计、数据库建模与编码实现"),
    ("frontend", "前端开发", "Frontend", "Web 页面开发、交互实现与样式还原"),
    ("fullstack", "全栈开发", "Full-stack", "前后端一体的小型应用或工具开发"),
    ("mobile", "移动端开发", "Mobile", "移动端 App 或小程序开发"),
    ("ui-design", "UI 设计", "UI Design", "界面视觉设计、原型与设计稿产出"),
    ("data-analysis", "数据分析", "Data Analysis", "数据清洗、统计分析与图表结论产出"),
    ("data-engineering", "数据工程", "Data Engineering", "数据管道、ETL 与数据仓库建设"),
    ("devops", "运维部署", "DevOps", "部署、容器编排、CI/CD 与环境维护"),
    ("testing", "测试", "Testing", "测试用例设计、自动化测试编写与缺陷验证"),
    ("security", "安全", "Security", "安全审查、漏洞分析与加固建议"),
    ("payment", "支付", "Payment", "支付、对账、清结算等资金相关功能开发"),
    ("e-commerce", "电商", "E-commerce", "电商业务功能（商品、订单、营销）开发"),
    ("copywriting", "文案写作", "Copywriting", "营销文案、品牌话术与内容创作"),
    ("document-writing", "文档撰写", "Document Writing", "方案文档、报告、说明书的撰写与排版"),
    ("translation", "翻译", "Translation", "中英文等多语种互译与本地化"),
    ("research", "调研", "Research", "行业或技术调研、资料搜集与摘要提炼"),
    ("project-management", "项目管理", "Project Management", "任务拆解、进度跟踪与风险同步"),
    ("customer-service", "客户服务", "Customer Service", "客户咨询应答、工单处理与话术维护"),
    ("automation", "自动化脚本", "Automation", "重复流程的脚本化与自动化处理"),
    ("integration", "系统集成", "Integration", "第三方系统对接、API 集成与联调"),
]


def seed_platform_tags(conn: Connection) -> int:
    """在同步连接上幂等播种平台词表（org_id IS NULL + exists 检查，
    NULL 不受唯一索引拦截）。返回本次插入条数。"""
    existing = set(conn.execute(
        sa_select(CapabilityTag.tag).where(CapabilityTag.org_id.is_(None))
    ).scalars())
    inserted = 0
    for tag, label_zh, label_en, description in PLATFORM_CAPABILITY_TAGS:
        if tag not in existing:
            conn.execute(sa_insert(CapabilityTag).values(
                org_id=None, tag=tag, label_zh=label_zh,
                label_en=label_en, description=description, status="active",
            ))
            inserted += 1
    return inserted
