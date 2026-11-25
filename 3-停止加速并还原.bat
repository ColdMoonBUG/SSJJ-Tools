@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0"
set "PY=C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"
set "PYTHONIOENCODING=gbk:replace"
title 停止 SSJJ 本地加速
net session >nul 2>&1
if errorlevel 1 (
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
echo 停止服务并还原 hosts...
"%PY%" "D:\Ccode\ssjjmeihua\ssjj-tool\local_cdn.py" uninstall
echo.
echo 已停止，hosts 已还原，游戏恢复直连官方 CDN。
pause
