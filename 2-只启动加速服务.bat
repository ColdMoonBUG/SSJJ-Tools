@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0"
set "PY=C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"
set "PYTHONIOENCODING=gbk:replace"
title SSJJ 本地加速服务
net session >nul 2>&1
if errorlevel 1 (
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
echo 启动本地资源服务...
start "" "C:\Users\Administrator\AppData\Local\Programs\Python\Python312\pythonw.exe" "D:\Ccode\ssjjmeihua\ssjj-tool\local_cdn.py" daemon
timeout /t 3 >nul
"%PY%" "D:\Ccode\ssjjmeihua\ssjj-tool\local_cdn.py" status
echo.
echo 日志: D:\Ccode\ssjjmeihua\ssjj-tool\local_cdn.log
pause
