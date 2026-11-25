@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0"
set "PY=C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"
set "PYTHONIOENCODING=gbk:replace"
title 穷举探测大厅零碎资源
echo ============================================================
echo  穷举探测：武器/道具/皮肤图标这类动态拼名的资源
echo  404 记录 7 天后会自动重探（新版本上线后能补到）
echo ============================================================
echo.
"%PY%" exhaustive_fetch.py fetch --jobs 16
echo.
pause
