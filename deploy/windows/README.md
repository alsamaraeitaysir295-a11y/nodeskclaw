# Windows 宿主机部署辅助脚本

适用形态：**Windows + WSL2 + Docker Compose 部署**（本目录脚本跑在 Windows 宿主机上，仓库根目录的 `docker-compose.yml` 跑在 WSL 内）。完整踩坑手册见 `docs/DeskClaw-WSL2部署问题与解决方案.md`。

## 一次性初始化（新机器拿到代码后）

```powershell
# 1. 注册开机自启三件套（当前用户级，无需管理员）
powershell -ExecutionPolicy Bypass -File deploy\windows\setup-scheduled-tasks.ps1
#    可选参数：-Distro Ubuntu -LanFromPort 24517 -LanToPort 14517

# 2. 内网直连根治（需管理员 PowerShell，可选——不做则走上面注册的转发器兜底）
powershell -ExecutionPolicy Bypass -File deploy\windows\fix-hyperv-fw.ps1

# 3. 首次启动（WSL 内）
wsl bash -c "cd <仓库的WSL路径> && docker compose up -d"
```

## 三个脚本与注册的任务

| 脚本/产物 | 作用 | 对应文档 |
|---|---|---|
| `setup-scheduled-tasks.ps1` | 注册 `DeskClaw-WSL-Keepalive`（防 WSL 自动关机）、`DeskClaw-ComposeUp`（登录后 compose 兜底）、`DeskClaw-LAN-Forwarder`（内网转发兜底） | 坑 1 / 坑 2 |
| `lan-forward.js` | 用户态 TCP 转发（默认 24517→14517，环境变量 `LAN_FROM_PORT`/`LAN_TO_PORT` 可改），由上面的任务常驻运行 | 坑 2 |
| `fix-hyperv-fw.ps1` | Hyper-V 防火墙放行 WSL 入站（内网直连根治，需管理员） | 坑 2 |

## 路径说明

脚本自动按自身所在位置定位仓库（不依赖固定盘符），WSL 仓库路径由 Windows 路径换算（`E:\a\b` → `/mnt/e/a/b`）；换克隆位置无需改脚本。

## 卸载

```powershell
Get-ScheduledTask -TaskName 'DeskClaw-*' | Unregister-ScheduledTask -Confirm:$false
```
