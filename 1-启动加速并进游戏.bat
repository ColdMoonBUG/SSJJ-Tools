@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0"
set "PY=C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"
set "PYTHONIOENCODING=gbk:replace"
title SSJJ 本地加速
net session >nul 2>&1
if errorlevel 1 (
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
echo [1/3] 启动本地资源服务（需要管理员权限写 hosts）...
start "" "C:\Users\Administrator\AppData\Local\Programs\Python\Python312\pythonw.exe" "D:\Ccode\ssjjmeihua\ssjj-tool\local_cdn.py" daemon
timeout /t 3 >nul
echo [2/3] 检查服务状态...
"%PY%" "D:\Ccode\ssjjmeihua\ssjj-tool\local_cdn.py" status
echo.
echo [3/3] 启动游戏...
start "" "C:\Program Files (x86)\Wooduan\SSJJ-4399\WDlauncher.exe"
echo.
echo 完成。活动/签到/背包等页面命中本地库后是毫秒级打开。
echo 若某个页面仍然卡，说明该资源还没缓存过：第一次回源时会自动存下来，之后就快了。
echo.
pause
