# DeskClaw 在 Windows + WSL2 Docker 部署：问题与解决方案

> 交接文档。目标读者：接手维护本机 DeskClaw 部署的智能体/工程师。
> 日期：2026-08-26

## 〇、快速启动自检（先看这里）

> 日常开机后 1-2 分钟内应自动就绪；不通时按下面顺序排查。

```powershell
# 1. 任务计划：Keepalive / LAN-Forwarder 应为 Running
Get-ScheduledTask -TaskName 'DeskClaw-*' | Format-Table TaskName, State
```

```bash
# 2. 容器全部 Up（postgres healthy）
wsl -d Ubuntu -- docker ps
```

```bash
# 3. 服务健康
curl http://localhost:14510/api/v1/health   # {"status":"ok"}
curl -o /dev/null -w '%{http_code}\n' http://localhost:14517   # 200
```

**分诊**：
- 全不通 + `wsl -l -v` 显示 Stopped → 保活挂了：`Start-ScheduledTask DeskClaw-WSL-Keepalive`（坑 1）
- WSL Running 但容器 Exited → `wsl -d Ubuntu -- bash -c "cd /mnt/e/claude_workspace/nodeskclaw && docker compose up -d"`
- 容器反复重启 → `docker logs <容器名> --tail 30`，先看是否迁移撞表（坑 5）

---

## 一、环境快照

| 项 | 值 |
|---|---|
| 宿主机 | Windows（域机 VHST，用户 vhst-260005） |
| WSL | WSL2 2.6.3.0，发行版 Ubuntu（systemd 已开启，`/etc/wsl.conf` 里 `[boot] systemd=true`） |
| Docker | WSL Ubuntu 内原生 docker-ce（非 Docker Desktop；Docker Desktop 已卸载） |
| 项目 | DeskClaw（nodeskclaw），compose 项目名 `nodeskclaw` |
| 主代码/部署目录 | Windows: `E:\claude_workspace\nodeskclaw`（WSL 内 `/mnt/e/claude_workspace/nodeskclaw`）。**代码以这里为主**，`E:\workBuddy_workspace\2026-08-26-09-59-29\nodeskclaw` 只是测试副本（注意：两边目录同名导致 compose 项目名相同，在副本里跑 compose 会操作同一组容器） |
| 镜像 | `nodesk-center-cn-beijing.cr.volces.com/public/nodeskclaw-{backend,llm-proxy,portal}:v0.8.3` + `postgres:16-alpine` |
| `.wslconfig` | memory=8GB, processors=8, swap=4GB, vmIdleTimeout=999999999, **networkingMode=mirrored** |
| Windows 防火墙 | 三个配置文件全部关闭（但与本案无关，见问题二） |

### 端口布局（经项目根 `.env` 覆盖）

| 服务 | 容器内 | 宿主端口 | 备注 |
|---|---|---|---|
| portal (nginx) | 80 | **14517** | Windows 80 被 svchost(PID 4140) 占用 |
| backend (FastAPI) | 8000 | **14510** | Windows 4510 被 svchost 占用 |
| llm-proxy | 8080 | 4511 | 不变 |
| postgres | 5432 | 未映射 | 仅 compose 内网 |

- `.env` 关键项：`BACKEND_PORT=14510`、`PORTAL_PORT=14517`、`NODESKCLAW_DATA_DIR=/home/vhst-260005/.nodeskclaw/docker-instances`（必须是 WSL 路径，不能写 `C:\...`）
- portal 的 nginx 内置 `/api → nodeskclaw-backend:8000` 反代，**对外只需暴露 portal 一个端口**
- 登录账号 `admin`；数据库 pg_data 卷是旧部署遗留，密码为用户上次修改值。忘记密码：`.env` 设 `RESET_ADMIN_PASSWORD=true` 后重启 backend，新密码打印在容器日志里

### 启动命令（WSL 内）

```bash
cd /mnt/e/claude_workspace/nodeskclaw
docker compose up -d
```

容器均 `restart: unless-stopped`，WSL VM 冷启动后 docker 服务自启会自动拉起全部容器。

---

## 二、踩过的坑（按时间序）

### 坑 0：旧目录容器网络沙箱损坏

- 之前从 `E:\claude_workspace\nodeskclaw` 起过同名 compose 项目，残留容器报 `network sandbox for container ... not found`，backend 崩溃循环、portal nginx 解析不到主机名
- **解法**：`docker rm -f` 损坏容器后，从新目录重新 `docker compose up -d`（项目名相同可直接接管卷和网络）

### 坑 1：WSL 虚拟机被反复自动关机（服务"时好时坏"的根因）

**现象**：本机 localhost 访问间歇性失败；每次进 WSL 查容器都是 "Up N seconds"，看起来刚重启过。

**根因**：WSL 2.6.3 + systemd 开启时的已知行为——**最后一个 wsl.exe 客户端会话退出后几秒，Windows 对整个 VM 执行 `systemctl poweroff`**。`.wslconfig` 的 `vmIdleTimeout` 无效（已设 999999999 仍被关）。

**证据**（WSL 内 `journalctl`）：
```
systemd-logind: The system will power off now!
WSL: InitTerminateInstanceInternal: systemctl poweroff did not terminate the instance in 10000 ms, calling reboot(RB_POWER_OFF)
```
`journalctl --list-boots` 显示短时间内多次 boot。

**无效尝试**（别浪费时间）：
- WSL 内 `setsid nohup sleep infinity` 后台进程——不阻止 poweroff
- WSL 内跑 keepalive docker 容器——同样无效

**有效解法**：必须保持一个 **Windows 侧的 wsl.exe 客户端连接**：

```powershell
# PowerShell 隐藏窗口常驻
Start-Process wsl.exe -ArgumentList '-d','Ubuntu','-e','bash','-c','exec sleep infinity' -WindowStyle Hidden
```

或 bash 后台任务：`wsl.exe -d Ubuntu -- sleep infinity &`

### 坑 2：内网其他电脑无法访问（本机 localhost 正常）

**现象**：本机 `curl http://10.50.54.212:14517` 超时（000），WSL 内部走同一 IP 却 200。端口绑定确认是 `0.0.0.0`（`ss -tln` 可见），不是绑定问题。

**根因**：WSL2 **mirrored 网络模式**下，局域网入站流量由 **Hyper-V 防火墙**管控（独立于常规 Windows 防火墙，后者即使全关也无效），默认策略拦截局域网入站。

**根治（需管理员 PowerShell，一次性）**：

```powershell
Set-NetFirewallHyperVVMSetting -Name '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -DefaultInboundAction Allow
```

- `{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}` 是 WSL 的 VMCreatorId
- 验证：`Get-NetFirewallHyperVVMSetting`（DefaultInboundAction 应为 Allow）
- 生效后内网直接访问 `http://10.50.54.212:14517`
- 注意：无管理员权限执行会静默失败或报"拒绝访问"，务必确认提权

**临时替代（无管理员权限，已实施）**：用户态 TCP 转发器。mirrored 模式下 0.0.0.0 端口被镜像独占（同端口转发报 `EADDRINUSE`），所以换监听端口：

- 文件：`deploy/windows/lan-forward.js`（Node，net 模块 pipe 转发；**2026-08-31 已收编入库并参数化**，原 `E:\workBuddy_workspace\...\lan-forward.js` 为历史位置）
- 逻辑：监听 `0.0.0.0:24517` → 转发 `127.0.0.1:14517`（mirrored 回环通道是通的）
- 运行：`node lan-forward.js`（需保持后台运行；端口可用 `LAN_FROM_PORT`/`LAN_TO_PORT` 覆盖）
- 内网入口：`http://10.50.54.212:24517`
- Hyper-V 防火墙根治后，转发器可停用

### 坑 3：docker.io 基础镜像拉不动（加速器全灭）

- 现象：`docker compose build` 卡在 `python:3.12-slim` 元数据解析，报 `dial tcp ... i/o timeout`
- 根因：`/etc/docker/daemon.json` 配的 dockerproxy.com / baidubce / ustc 三个加速器全部失效
- 已根治（2026-08-26）：换成 `https://docker.m.daocloud.io`，并预拉齐基础镜像 `python:3.12-slim` / `node:22-alpine` / `nginx:1.27-alpine`（原配置备份 `/etc/docker/daemon.json.bak`）
- 应急拉取：`docker pull docker.m.daocloud.io/library/<镜像>` 后 `docker tag` 回原名

### 坑 4：重部署误用官方镜像，本地代码修改"消失"

- 现象：portal 上自己的功能（主题切换、调用历史折叠、座位分配修复等）全部不见
- 根因：compose 镜像 tag 固定为 registry 的 `v0.8.3`，抢救性重部署时直接拉了官方成品镜像，镜像构建日（8月21日）之后的本地改动都不在里面
- 解法：从主仓库本地构建覆盖同名 tag：
  ```bash
  cd /mnt/e/claude_workspace/nodeskclaw
  docker compose build nodeskclaw-backend portal
  docker compose up -d nodeskclaw-backend portal
  ```
- 官方原版备份在 `v0.8.3-upstream` tag，可随时回退对比
- **以后改代码发版一律走上面的 build 命令；禁止 `docker compose pull`**（会把本地构建覆盖回官方镜像）

### 坑 5：迁移撞表 DuplicateTable（表已存在）

- 现象：backend 反复重启，日志 `relation "external_agent_invocations" already exists`
- 根因：应用曾用 create_all 提前建表（索引是旧命名 `ix_eai_*`），alembic 版本号停在上一版；换回本地构建镜像后，新迁移 `6f131501f1cc` 重跑撞表
- 已处理（2026-08-26）：手工补齐迁移期望的 4 个索引、删旧命名索引、`UPDATE alembic_version SET version_num='6f131501f1cc'`
- 再遇同类问题：**勿删表**（表里有业务数据），对齐索引结构后 stamp 版本号即可

### 坑 6：AI 员工"发消息不回复"（存量实例 env 端口过期）

- 现象：门户给 AI 员工发消息无回复，后端日志 `POST .../chat 400`；实例容器日志 tunnel WebSocket 反复重连失败；实例陷入重启循环
- 根因链：实例容器创建时把 `NODESKCLAW_API_URL` 烧进 env（当时正确 4510）→ 端口改 14510 后，**平台的"重启实例"是 `docker restart`（同容器），env 永不刷新** → 隧道连不上
- 已处理（2026-08-27）：
  1. 主仓库 `.env` 显式设 `AGENT_API_BASE_URL=http://host.docker.internal:14510/api/v1`（`config.py` 默认推导写死 4510，是坑）+ 重建 backend
  2. `sed` 批量修正 `~/.nodeskclaw/docker-instances/*/docker-compose.yml` 里的 4510 → 14510（19 个，文件属主 root 需 `wsl -u root`）
  3. 对运行实例目录 `docker compose up -d --force-recreate`（这才刷新 env；`docker restart` 无效）
- **教训：改后端端口后必须同时刷存量实例的 compose + force-recreate**；排查顺序：backend 日志 grep `Tunnel:`（无连接即断）→ 实例容器 env 查 `NODESKCLAW_API_URL`
- 备注：mirrored 模式下 WSL 绑 4510 会 `Address in use`（Windows svchost 占同端口空间），socat 转发救急方案不可行，只能走实例重建

---

## 三、当前状态（截至 2026-08-26 15:30）

> **2026-08-31 更新**：本机内网 IP 经 DHCP 已从 `10.50.54.212` 变为 **`10.50.54.221`**
> （.212 入口失效，坑 2 正文中的 .212 为当时现场记录保留不改）。当前入口：
> - 本机：`http://localhost:14517`
> - 内网（转发器，已验证 200）：`http://10.50.54.221:24517`
> - 内网（直连，仍待外部设备实测）：`http://10.50.54.221:14517`
> IP 是 DHCP 分配，建议做 MAC 绑定保留，否则每次变更都要同步此文档。

- 8 个容器全部健康（postgres healthy），已验证 WSL 冷启动后全链路自动恢复
- **自启已固化**：三个当前用户级任务计划（任务计划程序 → `DeskClaw-*`，无需管理员）：
  | 任务名 | 触发 | 作用 |
  |---|---|---|
  | DeskClaw-WSL-Keepalive | 登录时 | 常驻 `wsl -d Ubuntu -- sleep infinity`（防坑 1），无限时长、失败自动重启 |
  | DeskClaw-ComposeUp | 登录后 2 分钟 | 在主仓库目录 `docker compose up -d` 兜底（容器本身 restart: unless-stopped 已自启） |
  | DeskClaw-LAN-Forwarder | 登录后 3 分钟 | 常驻 `node lan-forward.js` 24517→14517（Hyper-V 防火墙的兜底） |
  - 注册脚本：`deploy/windows/setup-scheduled-tasks.ps1`（**2026-08-31 收编入库并参数化**，路径自动定位仓库，原 workBuddy 位置为历史现场；可重跑覆盖）
- **坑 2 已根治**：Hyper-V 防火墙 `DefaultInboundAction=Allow` 已执行（2026-08-26，经 UAC 提权，日志 `hyperv-fw-fix.log`）
- **坑 3/4/5 均已处理**：加速器换 DaoCloud；当前运行镜像是主仓库本地构建（含 feature/new-ui 全部改动），官方原版在 `v0.8.3-upstream`；迁移已 stamp 到 `6f131501f1cc`
- 访问入口：
  - 本机：`http://localhost:14517`
  - 内网（转发器，已验证 200）：`http://10.50.54.212:24517`
  - 内网（直连，待外部设备实测）：`http://10.50.54.212:14517`

## 四、待办（可交接给下一个智能体）

1. **用外部设备（手机/其他电脑）实测** `http://10.50.54.212:14517` 直连是否通（mirrored 模式下本机 curl 自己的 LAN IP 不通不代表外部不通）。通了之后 LAN-Forwarder 任务保留作兜底即可（域机组策略可能回滚防火墙设置）
2. 若遇"服务突然全断"：先看 `DeskClaw-WSL-Keepalive` 任务是否 Running（坑 1），再看容器（`wsl -d Ubuntu -- docker ps`），最后才是 docker 层排查
3. 代理会话（Claude 等）的后台保活进程会随会话退出而死，**不要再依赖会话级保活**，一切以任务计划为准
4. **技能市场统计功能**（需求已提出、方案未做，2026-08-26 搁置）：统计各 skill 的调用次数/下载次数，按 总计/月/周 三档维度，做一个简单展示页；需要后端埋点/聚合 API + 前端页面
5. 仓库工作区有两批未提交改动：① portal i18n + InstanceDetail（用户自己的改动）；② GeneMarket 筛选栏合并单行+归属改下拉（2026-08-26 会话产出，建议单独 commit `fix(portal): 技能市场筛选栏合并为单行+归属改下拉`）；③ 根目录 `.env` 新增了端口覆盖键（勿删）
