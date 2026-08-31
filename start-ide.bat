@echo off
rem DeskClaw IDE 模式一键启动（无 Docker）。双击运行，详情见 scripts/start-ide.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-ide.ps1"
pause
