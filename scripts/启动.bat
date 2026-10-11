@echo off
chcp 65001 >nul
title SysAgent (portable)
cd /d "%~dp0"

if not exist "SysAgent.exe" (
  echo [错误] 未找到 SysAgent.exe，请在解压后的目录内运行本脚本。
  pause
  exit /b 1
)

echo ============================================
echo   SysAgent 便携版  -  启动中...
echo   控制台:  http://127.0.0.1:8765/
echo   关闭本窗口即停止服务（或用托盘/任务管理器）
echo ============================================

rem 首次启动会自动创建 config.yaml / data / logs（都在本目录）
start "SysAgent Server" "%~dp0SysAgent.exe"

rem 等服务就绪后打开浏览器
timeout /t 4 /nobreak >nul
start "" http://127.0.0.1:8765/
exit /b 0
