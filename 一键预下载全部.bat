@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0"
set "PY=C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"
set "PYTHONIOENCODING=gbk:replace"
title SSJJ 全量资源预下载
echo ============================================================
echo  SSJJ 全量资源预下载（战斗包约 17.3 GB）
echo  已下载的自动跳过；可 Ctrl+C 中断，进度记在本地
echo ============================================================
echo.
"%PY%" ssjj_boost.py status
echo.
"%PY%" ssjj_boost.py preload --match all --jobs 12
echo.
pause
