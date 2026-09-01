"""任务空间（Mission P1）服务包。

模块规划见 docs/mission-space-p1-design.md §4-§8/§11：
- event_service：统一事件流写入（计数行 seq，T2）
- orchestrator：编排器 v1（T3）/ matcher：匹配器 v1（T4）/ scheduler：调度器 v1（T5）
- ingest_service：隧道上行接入（T8）
"""
