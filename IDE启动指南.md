# DeskClaw IDE 一键启动指南（无 Docker）

> 面向拿到代码的同学：双击一个文件即可把整个平台跑起来。
> 日期：2026-08-31

## 一分钟版

1. 装两个前置（只装一次）：[uv](https://docs.astral.sh/uv/getting-started/installation/) 和 [Node.js ≥18](https://nodejs.org/)
2. 双击仓库根目录的 **`start-ide.bat`**
3. 首次运行会自动下载便携版 PostgreSQL（约 300MB，需联网，几分钟）；之后自动装依赖、建库、起服务
4. 浏览器自动打开 `http://localhost:4517`，登录信息见控制台或仓库根目录 **`IDE-登录信息.txt`**：
   - 账号 `admin@deskclaw.com`
   - 密码 `DeskClaw@2026`（可用环境变量 `DEMO_PASSWORD` 改）

## 会发生什么

- 弹出**三个命令行窗口**（llm-proxy / backend / portal），**关窗口即停对应服务**
- 数据库是解压在 `.tools/` 里的便携版 PostgreSQL（127.0.0.1:15432），**不需要装 Docker、不装 Windows 服务、不写注册表**；数据都在 `.tools/pgdata`，删掉该目录即完全重置
- 首次启动后端要重放全部数据库迁移（约 1 分钟），窗口里会滚很多日志，属正常

## 常见问题

| 现象 | 说明 |
|---|---|
| 后端端口不是 4510 | 正常。Windows 上 4510 常被系统占用，脚本自动从 24510 起选空闲端口（可用 `BACKEND_PORT` 指定） |
| 4517/4511 端口被占报错 | 关掉占用进程重跑；脚本会在报错里说明是哪个端口 |
| 想完全重来（重置数据库+账号） | 关掉三个窗口 → 删除 `.tools/pgdata` 目录 → 再跑 `start-ide.bat` |
| 服务窗口中文乱码 | 无害（旧窗口），新窗口已设 UTF-8；不影响功能 |
| AI 员工实例相关功能不可用 | 正常。IDE 模式不含容器运行时，员工实例需 Docker/K8s 环境；任务空间可完整走"提交→拆解→确认"流程 |

## 给开发者

- 脚本本体：`scripts/start-ide.ps1`（bat 只是包装）；可用环境变量 `TOOLS_DIR` / `PG_PORT` / `BACKEND_PORT` / `DEMO_PASSWORD` 覆盖默认值
- 等价的 bash 版一键启动（Git Bash / Linux）：`./dev.sh ee`（支持 `BACKEND_PORT` 覆盖；`--docker-pg` 可选用 Docker 起库）
- VSCode 里调试：后端 `uv run uvicorn app.main:app --port 24510`（记得带 `DATABASE_URL` / `LLM_PROXY_URL` 环境变量，参考 ps1 里的值）
- 门户开发代理：vite 默认把 `/api` 代理到 4510，后端端口不同时设 `API_PROXY_TARGET`
