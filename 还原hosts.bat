@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0"
set "PY=C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"
set "PYTHONIOENCODING=gbk:replace"
title 还原 hosts
net session >nul 2>&1
if errorlevel 1 (
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
echo 还原 hosts（移除 ssjj-local-cdn 标记行）...
"%PY%" "D:\Ccode\ssjjmeihua\ssjj-tool\local_cdn.py" uninstall
echo.
echo 完成。
pause
