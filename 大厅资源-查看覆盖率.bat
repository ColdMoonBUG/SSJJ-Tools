@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0"
set "PY=C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"
set "PYTHONIOENCODING=gbk:replace"
title 查看命中覆盖率
echo ============================================================
echo  覆盖率 = 客户端实际请求过的资源中，本地已有的比例
echo  100%% 表示再进游戏不会为任何静态资源等下载
echo ============================================================
echo.
"%PY%" exhaustive_fetch.py cover --dry-run
echo.
pause
