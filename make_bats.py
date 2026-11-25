# -*- coding: utf-8 -*-
"""生成快速启动 bat（GBK + CRLF，避免 cmd 中文乱码）

这是 bat 的唯一来源。改 bat 请改这里再跑一遍，不要手改生成的 bat ——
上一版就是因为手改了 `3-停止加速并还原.bat` 而没同步回来，重跑一次就把它覆盖了。
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(os.path.dirname(sys.executable), "python.exe")
if not os.path.isfile(PY):
    PY = sys.executable
PYW = os.path.join(os.path.dirname(PY), "pythonw.exe")
if not os.path.isfile(PYW):
    PYW = PY
GAME = r"C:\Program Files (x86)\Wooduan\SSJJ-4399\WDlauncher.exe"

# 提权：需要写 hosts 的操作必须管理员
ELEV = (
    "net session >nul 2>&1\n"
    "if errorlevel 1 (\n"
    "  powershell -NoProfile -Command \"Start-Process -FilePath '%~f0' -Verb RunAs\"\n"
    "  exit /b\n"
    ")\n"
)
# 通用头部：定位自身目录、选 python、设编码
HEAD = (
    "@echo off\n"
    "chcp 936 >nul\n"
    "setlocal\n"
    "cd /d \"%~dp0\"\n"
    "set \"PY=" + PY + "\"\n"
    "if not exist \"%PY%\" set \"PY=python\"\n"
    "set \"PYTHONIOENCODING=gbk:replace\"\n"
)


def w(name, body):
    p = os.path.join(HERE, name)
    with open(p, "wb") as f:
        f.write((HEAD + body).replace("\n", "\r\n").encode("gbk", "replace"))
    print("写入", name)


w("1-启动加速并进游戏.bat", f"""title SSJJ 本地加速
{ELEV}echo [1/3] 启动本地资源服务（需要管理员权限写 hosts）...
start "" "{PYW}" "{HERE}\\local_cdn.py" daemon
timeout /t 3 >nul
echo [2/3] 检查服务状态...
"%PY%" "{HERE}\\local_cdn.py" status
echo.
echo [3/3] 启动游戏...
start "" "{GAME}"
echo.
echo 完成。活动/签到/背包等页面命中本地库后是毫秒级打开。
echo 若某个页面仍然卡，说明该资源还没缓存过：第一次回源时会自动存下来，之后就快了。
echo.
pause
""")

w("2-只启动加速服务.bat", f"""title SSJJ 本地加速服务
{ELEV}echo 启动本地资源服务...
start "" "{PYW}" "{HERE}\\local_cdn.py" daemon
timeout /t 3 >nul
"%PY%" "{HERE}\\local_cdn.py" status
echo.
echo 日志: {HERE}\\local_cdn.log
pause
""")

w("3-停止加速并还原.bat", f"""title 停止 SSJJ 本地加速
{ELEV}echo 停止服务并还原 hosts...
"%PY%" "{HERE}\\local_cdn.py" uninstall
echo.
echo 已停止，hosts 已还原，游戏恢复直连官方 CDN。
pause
""")

w("4-查看加速状态.bat", """title SSJJ 加速状态
"%PY%" "%CD%\\local_cdn.py" status
echo.
echo ===== 最近 20 条请求 =====
powershell -NoProfile -Command "Get-Content '%CD%\\local_cdn.log' -Tail 20"
echo.
pause
""")

w("5-安装开机自启.bat", """title 安装 SSJJ 加速开机自启
"%PY%" "%CD%\\local_cdn.py" startup
pause
""")

w("6-取消开机自启.bat", """title 取消 SSJJ 加速开机自启
"%PY%" "%CD%\\local_cdn.py" nostartup
pause
""")

w("启动加速器.bat", """title SSJJ 加速器 - 图形界面
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
""")

w("一键预下载全部.bat", """title SSJJ 全量资源预下载
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
""")

w("大厅资源-补齐缺失.bat", """title 补齐大厅资源
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
""")

w("大厅资源-查看覆盖率.bat", """title 查看命中覆盖率
echo ============================================================
echo  覆盖率 = 客户端实际请求过的资源中，本地已有的比例
echo  100%% 表示再进游戏不会为任何静态资源等下载
echo ============================================================
echo.
"%PY%" exhaustive_fetch.py cover --dry-run
echo.
pause
""")

w("大厅资源-穷举探测.bat", """title 穷举探测大厅零碎资源
echo ============================================================
echo  穷举探测：武器/道具/皮肤图标这类动态拼名的资源
echo  404 记录 7 天后会自动重探（新版本上线后能补到）
echo ============================================================
echo.
"%PY%" exhaustive_fetch.py fetch --jobs 16
echo.
pause
""")

w("大厅资源-本地库统计.bat", """title 本地资源库统计
"%PY%" exhaustive_fetch.py stats
echo.
pause
""")

w("清理IE缓存条目.bat", """title 清理 IE 缓存条目（可选）
echo ============================================================
echo  说明：早期版本曾试图往 IE/WinINet 缓存写条目来加速，
echo  实测无效（AIR 走原生 socket，不读系统缓存）。
echo  这里只是把当年写进去的条目清掉，不影响现在的加速服务。
echo ============================================================
echo.
"%PY%" lobby_cache_tool.py clean
echo.
pause
""")

# 旧的 还原hosts.bat 指向 fps_proxy（已废弃），改成走 local_cdn
w("还原hosts.bat", f"""title 还原 hosts
{ELEV}echo 还原 hosts（移除 ssjj-local-cdn 标记行）...
"%PY%" "{HERE}\\local_cdn.py" uninstall
echo.
echo 完成。
pause
""")
