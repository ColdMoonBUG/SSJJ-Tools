@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0"
set "PY=C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"
set "PYTHONIOENCODING=gbk:replace"
title 补齐大厅资源
echo ============================================================
echo  补齐大厅资源（两步，都要跑）
echo.
echo  1) prewarm：按 res_config 清单下的 5264 个资源，只下本地缺的
echo  2) cover  ：把上局游戏实际请求过、但清单里没有的资源补下来
echo     （角色动画/角色模型/大厅武器预览属于这类，清单枚举不到）
echo ============================================================
echo.
echo [1/2] 清单资源...
"%PY%" exhaustive_fetch.py prewarm --jobs 12
echo.
echo [2/2] 实际请求过的资源...
"%PY%" exhaustive_fetch.py cover
echo.
pause
