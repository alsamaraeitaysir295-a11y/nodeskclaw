# DeskClaw: 注册当前用户级开机自启任务计划（无需管理员）。
# 适用：Windows + WSL2 Docker Compose 部署的宿主机（见 docs/DeskClaw-WSL2部署问题与解决方案.md）。
# 1. DeskClaw-WSL-Keepalive : 登录时常驻 wsl sleep（防 WSL 自动关机，部署文档坑 1）
# 2. DeskClaw-ComposeUp    : 登录 2 分钟后 docker compose up -d 兜底
# 3. DeskClaw-LAN-Forwarder: 登录 3 分钟后常驻内网端口转发（坑 2 兜底，需 node）
#
# 用法（在仓库根目录的宿主机 PowerShell）:
#   powershell -ExecutionPolicy Bypass -File deploy\windows\setup-scheduled-tasks.ps1
# 可选参数: -Distro Ubuntu -LanFromPort 24517 -LanToPort 14517
param(
    [string]$Distro = "Ubuntu",
    [int]$LanFromPort = 24517,
    [int]$LanToPort = 14517
)
$ErrorActionPreference = 'Stop'

# 路径自动定位（脚本随仓库走，不依赖固定盘符/目录）
$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$ForwarderJs = Join-Path $RepoRoot "deploy\windows\lan-forward.js"
# Windows 路径 → WSL 路径（E:\a\b → /mnt/e/a/b）
$drive = $RepoRoot.Substring(0, 1).ToLower()
$rest = $RepoRoot.Substring(3).Replace('\', '/')
$WslRepo = "/mnt/$drive/$rest"

$user = "$env:USERDOMAIN\$env:USERNAME"

# --- 1. WSL keepalive（常驻）---
$t1 = New-ScheduledTaskTrigger -AtLogOn -User $user
$s1 = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew
$a1 = New-ScheduledTaskAction -Execute 'wsl.exe' -Argument "-d $Distro -- sleep infinity"
Register-ScheduledTask -TaskName 'DeskClaw-WSL-Keepalive' -Action $a1 -Trigger $t1 -Settings $s1 -Force | Out-Null
Write-Output 'registered DeskClaw-WSL-Keepalive'

# --- 2. compose up 兜底（登录 + 2 分钟）---
$t2 = New-ScheduledTaskTrigger -AtLogOn -User $user
$t2.Delay = 'PT2M'
$s2 = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::FromMinutes(30)) -StartWhenAvailable -MultipleInstances IgnoreNew
$a2 = New-ScheduledTaskAction -Execute 'wsl.exe' -Argument "-d $Distro -- bash -c 'cd $WslRepo && docker compose up -d'"
Register-ScheduledTask -TaskName 'DeskClaw-ComposeUp' -Action $a2 -Trigger $t2 -Settings $s2 -Force | Out-Null
Write-Output 'registered DeskClaw-ComposeUp'

# --- 3. 内网转发兜底（登录 + 3 分钟，需 node；没有 node 则跳过）---
$nodeCmd = Get-Command node.exe -ErrorAction SilentlyContinue
if ($nodeCmd) {
    $t3 = New-ScheduledTaskTrigger -AtLogOn -User $user
    $t3.Delay = 'PT3M'
    $s3 = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1) `
        -MultipleInstances IgnoreNew
    $a3 = New-ScheduledTaskAction -Execute $nodeCmd.Source `
        -Argument "`"$ForwarderJs`"" -WorkingDirectory $RepoRoot
    # 端口经环境变量传入（lan-forward.js 读取 LAN_FROM_PORT / LAN_TO_PORT）
    # 任务计划不便传 env，这里通过 cmd 包装
    $a3 = New-ScheduledTaskAction -Execute 'cmd.exe' `
        -Argument "/c set LAN_FROM_PORT=$LanFromPort&& set LAN_TO_PORT=$LanToPort&& `"$($nodeCmd.Source)`" `"$ForwarderJs`"" `
        -WorkingDirectory $RepoRoot
    Register-ScheduledTask -TaskName 'DeskClaw-LAN-Forwarder' -Action $a3 -Trigger $t3 -Settings $s3 -Force | Out-Null
    Write-Output 'registered DeskClaw-LAN-Forwarder'
} else {
    Write-Warning 'node.exe not found - skipped DeskClaw-LAN-Forwarder (run fix-hyperv-fw.ps1 instead for LAN access)'
}

Write-Output '--- all DeskClaw tasks ---'
Get-ScheduledTask -TaskName 'DeskClaw-*' | Select-Object TaskName, State | Format-Table -AutoSize | Out-String
