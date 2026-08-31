# DeskClaw: 放行 WSL 入站的 Hyper-V 防火墙（内网直连的根治方案，部署文档坑 2）。
# 说明：WSL2 mirrored 模式下局域网入站由 Hyper-V 防火墙管控（与常规 Windows 防火墙
# 无关，后者全开也没用）；执行后内网可直接访问 http://<本机IP>:14517。
# GUID 是 WSL 的 VMCreatorId（通用，非本机特有）。
# 需要管理员 PowerShell 运行：右键 → 以管理员身份运行，然后：
#   powershell -ExecutionPolicy Bypass -File deploy\windows\fix-hyperv-fw.ps1
$ErrorActionPreference = 'Stop'

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { throw '需要管理员权限运行（右键管理员 PowerShell）' }

$log = Join-Path $env:TEMP 'deskclaw-hyperv-fw-fix.log'
Start-Transcript -Path $log -Force
Set-NetFirewallHyperVVMSetting -Name '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -DefaultInboundAction Allow
Get-NetFirewallHyperVVMSetting -Name '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' | Format-List Name, DefaultInboundAction
Stop-Transcript
Write-Output "日志: $log"
