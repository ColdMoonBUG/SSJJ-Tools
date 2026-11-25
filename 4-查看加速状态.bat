@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0"
set "PY=C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"
set "PYTHONIOENCODING=gbk:replace"
title SSJJ 加速状态
"%PY%" "%CD%\local_cdn.py" status
echo.
echo ===== 最近 20 条请求 =====
powershell -NoProfile -Command "Get-Content '%CD%\local_cdn.log' -Tail 20"
echo.
pause
