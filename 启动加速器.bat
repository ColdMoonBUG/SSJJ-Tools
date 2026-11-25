@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0"
set "PY=C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"
set "PYTHONIOENCODING=gbk:replace"
title SSJJ 加速器 - 图形界面
echo ============================================================
echo   SSJJ 加速器 - 图形界面
echo   预下载战斗资源 / 大厅资源 / 启停本地加速服务
echo ============================================================
echo.
"%PY%" ssjj_boost.py gui
if errorlevel 1 (
  echo.
  echo GUI 启动失败，请确认 Python 带 tkinter。
  pause
)
