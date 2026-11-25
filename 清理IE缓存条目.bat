@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0"
set "PY=C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"
set "PYTHONIOENCODING=gbk:replace"
title 清理 IE 缓存条目（可选）
echo ============================================================
echo  说明：早期版本曾试图往 IE/WinINet 缓存写条目来加速，
echo  实测无效（AIR 走原生 socket，不读系统缓存）。
echo  这里只是把当年写进去的条目清掉，不影响现在的加速服务。
echo ============================================================
echo.
"%PY%" lobby_cache_tool.py clean
echo.
pause
