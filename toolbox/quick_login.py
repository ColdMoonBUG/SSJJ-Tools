# -*- coding: utf-8 -*-
"""4399 生死狙击一键登录模块（纯标准库实现，无第三方依赖）。

流程：
  1. 使用 4399 官方 ptlogin 接口进行账号密码认证，获取会话 Cookie；
  2. 请求 my.4399.com/yxssjj/play-sid-<sid>-... 获取对应区服的一次性免登跳转链接
     (http://login.4399ssjj.com/login.php?...)；
  3. 直接以该链接为参数启动官方微端 MicroClient.exe，跳过登录框与选区界面，
     直达目标区服游戏大厅，同时完整保留官方 microPort / 聊天 / 战斗端启动能力。
"""
import base64
import ctypes
import ctypes.wintypes as wt
import http.cookiejar
import json
import os
import random
import re
import ssl
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import boot

DATA = boot.bootstrap()
import ssjj_paths as P

CONFIG_PATH = Path(DATA) / "quick_login.json"

UA = ("Mozilla/5.0 (Windows NT 6.1; Win64; x64; rv:56.0) Gecko/20100101 "
      "Firefox/56.0 BrowserDefine Wooduan/SSJJ1")

# 4399 官方全部 20 个区服及对应 sid（实测提取自 https://my.4399.com/yxssjj/wd）
SERVERS = [
    ("双线一区", 1),
    ("电信一区", 2),
    ("联通一区", 3),
    ("电信二区", 4),
    ("联通二区", 5),
    ("电信三区", 6),
    ("电信四区", 7),
    ("联通三区", 8),
    ("电信五区", 9),
    ("联通四区", 10),
    ("电信六区", 11),
    ("双线二区", 12),
    ("电信七区", 13),
    ("联通五区", 14),
    ("电信八区", 15),
    ("联通六区", 16),
    ("双线三区", 17),
    ("双线四区", 18),
    ("双线五区", 19),
    ("双线七区", 21),
]

SERVER_BY_NAME = {name: sid for name, sid in SERVERS}
SERVER_BY_SID = {sid: name for name, sid in SERVERS}


# ---------------------------------------------------------------- DPAPI 密码保护
class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi_protect(text):
    if not text:
        return ""
    raw = text.encode("utf-8")
    try:
        crypt32 = ctypes.windll.crypt32
        k32 = ctypes.windll.kernel32
        in_buf = ctypes.create_string_buffer(raw, len(raw))
        in_blob = _DATA_BLOB(len(raw), in_buf)
        out_blob = _DATA_BLOB()
        if crypt32.CryptProtectData(ctypes.byref(in_blob), "ssjj_ql", None, None,
                                    None, 0x01, ctypes.byref(out_blob)):
            try:
                enc = ctypes.string_at(out_blob.pbData, out_blob.cbData)
                return "dpapi:" + base64.b64encode(enc).decode("ascii")
            finally:
                k32.LocalFree(out_blob.pbData)
    except Exception:
        pass
    return "b64:" + base64.b64encode(raw).decode("ascii")


def _dpapi_unprotect(stored):
    if not stored:
        return ""
    if stored.startswith("dpapi:"):
        try:
            raw = base64.b64decode(stored[6:])
            crypt32 = ctypes.windll.crypt32
            k32 = ctypes.windll.kernel32
            in_buf = ctypes.create_string_buffer(raw, len(raw))
            in_blob = _DATA_BLOB(len(raw), in_buf)
            out_blob = _DATA_BLOB()
            if crypt32.CryptUnprotectData(ctypes.byref(in_blob), None, None, None,
                                          None, 0x01, ctypes.byref(out_blob)):
                try:
                    return ctypes.string_at(out_blob.pbData, out_blob.cbData).decode("utf-8")
                finally:
                    k32.LocalFree(out_blob.pbData)
        except Exception:
            return ""
    if stored.startswith("b64:"):
        try:
            return base64.b64decode(stored[4:]).decode("utf-8")
        except Exception:
            return ""
    return stored


# ---------------------------------------------------------------- 配置读写
def load_profile():
    """读取保存的账号配置。"""
    default = {"username": "", "password": "", "server": "联通二区", "sid": 5, "remember": True}
    if not CONFIG_PATH.is_file():
        return default
    try:
        d = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        srv = d.get("server") or "联通二区"
        sid = int(d.get("sid") or SERVER_BY_NAME.get(srv, 5))
        if sid not in SERVER_BY_SID:
            sid = SERVER_BY_NAME.get(srv, 5)
        srv = SERVER_BY_SID.get(sid, srv)
        return {
            "username": str(d.get("username") or "").strip(),
            "password": _dpapi_unprotect(d.get("password_enc") or ""),
            "server": srv,
            "sid": sid,
            "remember": bool(d.get("remember", True)),
        }
    except Exception:
        return default


def save_profile(username, password, server_name, remember=True):
    """加密保存账号配置到本地 JSON。"""
    username = (username or "").strip()
    sid = SERVER_BY_NAME.get(server_name, 5)
    server_name = SERVER_BY_SID.get(sid, "联通二区")
    payload = {
        "username": username if remember else "",
        "password_enc": _dpapi_protect(password) if (remember and password) else "",
        "server": server_name,
        "sid": sid,
        "remember": bool(remember),
    }
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return True, "已保存账号配置（%s · %s）" % (username or "未填账号", server_name)


# ---------------------------------------------------------------- 登录与拉起微端
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def find_microclient_exe():
    def _probe(gd):
        for rel in (
            os.path.join("MicroClient64", "MicroClient.exe"),
            os.path.join("MicroClient", "MicroClient.exe"),
            "MicroClient.exe",
        ):
            c = os.path.join(gd, rel)
            if os.path.isfile(c):
                return c
        return None

    found = _probe(P.GAME_DIR)
    if found:
        return found
    # 动态重新探测一次（兼容游戏刚安装或路径变更）
    if hasattr(P, "refresh_game_dir"):
        found = _probe(P.refresh_game_dir())
        if found:
            return found
    # 从正在运行的 MicroClient.exe / WDlauncher.exe 进程反查路径
    try:
        ps = (
            "Get-CimInstance Win32_Process -Filter \"Name='MicroClient.exe' OR Name='WDlauncher.exe'\" "
            "| ForEach-Object { $_.ExecutablePath }"
        )
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", ps],
            capture_output=True, encoding="utf-8", errors="replace", timeout=5,
            creationflags=0x08000000,
        )
        for ln in (r.stdout or "").splitlines():
            p = ln.strip()
            if not p or not os.path.isfile(p):
                continue
            if os.path.basename(p).lower() == "microclient.exe":
                return p
            found = _probe(os.path.dirname(p))
            if found:
                return found
    except Exception:
        pass
    return None


def _build_opener():
    cj = http.cookiejar.CookieJar()
    ctx = ssl.create_default_context()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cj),
        urllib.request.HTTPSHandler(context=ctx),
        _NoRedirect(),
    )
    return opener, cj


def get_login_url(username, password, sid, emit=None):
    """执行 4399 官方认证并获取指定区服的一次性免登跳转 URL。

    返回 (ok, url_or_err_msg, need_captcha)
    """
    username = (username or "").strip()
    if not username or not password:
        return False, "请先填写 4399 账号和密码", False
    sid = int(sid)
    srv_name = SERVER_BY_SID.get(sid, "区服 %d" % sid)
    if emit:
        emit("正在登录 4399 账号 %s…" % username)

    opener, cj = _build_opener()
    form = urllib.parse.urlencode({
        "loginFrom": "uframe",
        "postLoginHandler": "default",
        "layoutSelfAdapting": "true",
        "externalLogin": "qq",
        "displayMode": "popup",
        "layout": "vertical",
        "bizId": "2100001792",
        "appId": "www_home",
        "gameId": "",
        "css": "",
        "redirectUrl": "",
        "sessionId": "",
        "mainDivId": "popup_login_div",
        "includeFcmInfo": "false",
        "level": "8",
        "regLevel": "8",
        "userIdTip": "请输入4399用户名",
        "username": username,
        "password": password,
        "autoLogin": "true",
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://ptlogin.4399.com/ptlogin/login.do?v=1",
        data=form,
        headers={
            "User-Agent": UA,
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": "https://ptlogin.4399.com/ptlogin/loginFrame.do?v=1&appId=www_home",
        },
        method="POST",
    )
    try:
        resp = opener.open(req, timeout=20)
        body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace") if e.fp else ""
    except Exception as e:
        return False, "连接 4399 登录服务器失败：%r" % (e,), False

    cnames = {c.name for c in cj}
    if "Uauth" not in cnames:
        # 优先提取 4399 返回的具体错误信息（如密码错误、账号不存在、验证码提示等）
        m_err = re.search(r"loginErrorMsg\s*=\s*['\"]([^'\"]+)", body)
        err_txt = m_err.group(1).strip() if m_err else ""
        need_cap = (
            ("验证码" in err_txt)
            or ("captcha.do" in body)
            or ("inputCaptcha" in body and "none" not in body[max(0, body.find("inputCaptcha") - 80):body.find("inputCaptcha") + 80].lower())
        )
        if need_cap:
            return False, "4399 要求完成图形验证码，已为你打开官方登录器，请手动登录后再试", True
        if err_txt:
            return False, "登录失败：%s" % err_txt, False
        return False, "4399 登录未通过，请检查账号或密码是否正确", False

    if emit:
        emit("认证成功，正在获取【%s】入口链接…" % srv_name)

    play_url = ("https://my.4399.com/yxssjj/play-sid-%d-site-2_11-ref-wd-channel-my-randsj-%s"
                % (sid, random.random()))
    req2 = urllib.request.Request(
        play_url,
        headers={
            "User-Agent": UA,
            "Referer": "https://my.4399.com/yxssjj/wd",
        },
    )
    loc = None
    try:
        r2 = opener.open(req2, timeout=20)
        loc = r2.headers.get("Location")
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            loc = e.headers.get("Location")
        else:
            return False, "选区接口返回 HTTP %d" % e.code, False
    except Exception as e:
        return False, "获取区服入口失败：%r" % (e,), False

    if not loc or "login.php" not in loc:
        return False, "未能获取【%s】的登录入口（可能该区服维护中或无访问权限）" % srv_name, False

    return True, loc, False


WM_CLOSE = 0x0010
_OFFICIAL_IMAGES = ("MicroClient.exe", "AirAloneChat.exe", "WDlauncher.exe",
                    "AirLobbyPreloader.exe")
_WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)


def _top_windows_by_pid(pids):
    """枚举顶层窗口，返回 {pid: [hwnd, ...]}（只收 pids 里的）。"""
    out = {}
    if not pids:
        return out
    user32 = ctypes.windll.user32

    def _cb(hwnd, _lparam):
        pid = ctypes.c_ulong(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids:
            out.setdefault(pid.value, []).append(hwnd)
        return True

    user32.EnumWindows(_WNDENUMPROC(_cb), None)
    return out


def active_official_lobby():
    """仅识别当前安装目录中带可见主窗口的微端，忽略 CEF 子进程。"""
    exe = find_microclient_exe()
    if not exe:
        return False
    try:
        import psutil
        pids = set()
        for proc in psutil.process_iter(["pid", "name", "exe"]):
            if (proc.info.get("name") or "").lower() != "microclient.exe":
                continue
            path = proc.info.get("exe")
            if path and os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(exe)):
                pids.add(proc.info["pid"])
        windows = _top_windows_by_pid(pids)
        return any(ctypes.windll.user32.IsWindowVisible(hwnd)
                   for handles in windows.values() for hwnd in handles)
    except Exception:
        return False


def find_launcher_exe():
    """查找官方启动器 WDlauncher.exe（优先 P.GAME_DIR，找不到时从 find_microclient_exe 反推）。"""
    cand = os.path.join(P.GAME_DIR, "WDlauncher.exe")
    if os.path.isfile(cand):
        return cand
    mc = find_microclient_exe()
    if mc:
        mc_dir = os.path.dirname(mc)
        for d in (os.path.dirname(mc_dir), mc_dir):
            cand = os.path.join(d, "WDlauncher.exe")
            if os.path.isfile(cand):
                return cand
    return None


def launch_official(emit=None):
    """按官方入口无参数启动（避免触发 MicroClient 的 localtest 模式导致进局后大厅卡死）。"""
    if active_official_lobby():
        return True, "官方大厅已打开，沿用当前会话"
    launcher = find_launcher_exe()
    if launcher:
        target_exe = launcher
        work_dir = os.path.dirname(launcher)
        label = "官方启动器"
    else:
        mc = find_microclient_exe()
        if not mc:
            return False, "未找到官方启动器或微端（WDlauncher.exe / MicroClient.exe），请检查游戏安装路径"
        target_exe = mc
        mc_dir = os.path.dirname(mc)
        work_dir = os.path.dirname(mc_dir) if os.path.basename(mc_dir).lower().startswith("microclient") else mc_dir
        label = "官方微端"
    if emit:
        emit("正在打开%s；请在游戏窗口登录并选区" % label)
    try:
        result = ctypes.windll.shell32.ShellExecuteW(
            None, "open", target_exe, None, work_dir, 1
        )
        if result <= 32:
            return False, "%s启动失败（错误码 %d）" % (label, result)
        _log_launch("官方无参数启动 %s (cwd=%s)" % (os.path.basename(target_exe), work_dir))
        _watch_launch()
        return True, "已打开%s，请在游戏窗口登录并选区" % label
    except Exception as e:
        return False, "%s启动失败：%r" % (label, e)


def graceful_close_official(timeout=5.0):
    """先请官方微端/大厅自己退出（WM_CLOSE），再给进程树留时间落盘。

    为什么必须优雅关：MicroClient 是 CEF 多进程宿主，直接 taskkill /F 会把它和
    renderer 子进程在半写状态一起砍掉，CEF 的 profile / Cookie 数据库就可能损坏，
    之后每次启动都在 `MicroClient+0x6c4b9` 处 0xc0000005 崩溃（事件日志里那个
    偏移连续出现多次就是这个形状）。优雅关一次成本几秒，能省掉后面反复崩溃。
    """
    try:
        import psutil
    except Exception:
        return 0
    try:
        procs = [p for p in psutil.process_iter(["name", "pid"])
                 if (p.info.get("name") or "") in _OFFICIAL_IMAGES]
    except Exception:
        return 0
    if not procs:
        return 0
    pids = set()
    for p in procs:
        pids.add(p.pid)
        # 官方目录下的 AirLobbyPreloader 才算我们的目标，别误杀私服
        if (p.info.get("name") or "").lower() == "airlobbypreloader.exe":
            try:
                if not os.path.normcase(p.exe()).startswith(
                        os.path.normcase(os.path.abspath(P.GAME_DIR))):
                    pids.discard(p.pid)
            except Exception:
                pids.discard(p.pid)
    wins = _top_windows_by_pid(pids)
    for hwnd in [h for hs in wins.values() for h in hs]:
        try:
            ctypes.windll.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        except Exception:
            pass
    closed = 0
    try:
        alive = {p.pid for p in procs}
        for p in procs:
            try:
                p.wait(timeout=timeout / max(1, len(procs)))
                closed += 1
            except Exception:
                pass
        _ = alive
    except Exception:
        pass
    return closed


def cleanup_official_game_procs():
    """清理官方微端/大厅残留进程：先优雅关，剩下的才强杀（保护私服进程）。"""
    killed = 0
    graceful_close_official(timeout=4.0)
    # MicroClient.exe / AirAloneChat.exe / WDlauncher.exe 仅官方客户端使用，直接 /T 清进程树
    for img in ("MicroClient.exe", "AirAloneChat.exe", "WDlauncher.exe"):
        try:
            r = subprocess.run(
                ["taskkill", "/F", "/IM", img, "/T"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=6,
                creationflags=0x08000000,
            )
            if r.returncode == 0:
                killed += 1
        except Exception:
            pass
    # AirLobbyPreloader.exe 仅清理官方安装目录下的，避免误杀私服
    game_dir = os.path.normcase(os.path.abspath(P.GAME_DIR))
    try:
        ps_cmd = (
            "$gd = '%s'; "
            "Get-CimInstance Win32_Process -Filter \"Name='AirLobbyPreloader.exe'\" | Where-Object { "
            "  $_.ExecutablePath -and $_.ExecutablePath.ToLower().StartsWith($gd) "
            "} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $_.ProcessId }"
        ) % game_dir.replace("'", "''")
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, encoding="utf-8", errors="replace", timeout=6,
            creationflags=0x08000000,
        )
        killed += len([ln for ln in (r.stdout or "").splitlines() if ln.strip().isdigit()])
    except Exception:
        pass
    return killed


def launch_direct(username, password, server_name, emit=None):
    """兼容旧调用入口：统一走无参数官方启动，避免传入 login.php 触发 platform localtest 导致大厅卡死。"""
    return launch_official(emit=emit)


def _log_launch(text):
    """把每次一键启动的进程时间线写进 launch.log —— 「第一次进不去」只能靠这个定责。"""
    try:
        import time as _t
        base = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "ssjj-toolbox")
        os.makedirs(base, exist_ok=True)
        with open(os.path.join(base, "launch.log"), "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (_t.strftime("%Y-%m-%d %H:%M:%S"), text))
    except Exception:
        pass


def _watch_launch():
    """后台记录 5/15/30 秒时各进程是否起来、战斗端有没有提前退出。"""
    import threading
    import time as _t

    def run():
        import psutil
        marks = (5, 15, 30, 60)
        t0 = _t.time()
        seen = {}
        for sec in marks:
            _t.sleep(max(0, t0 + sec - _t.time()))
            now = {}
            for p in psutil.process_iter(["name", "pid", "create_time"]):
                try:
                    n = (p.info.get("name") or "")
                except Exception:
                    continue
                if n in ("MicroClient.exe", "WDlauncher.exe", "AirLobbyPreloader.exe",
                         "SSJJ_BattleClient_Unity.exe"):
                    now.setdefault(n.replace(".exe", ""), []).append(p.info["pid"])
            gone = [k for k in seen if k not in now]
            seen = now
            parts = ["%s=%s" % (k, v) for k, v in sorted(now.items())]
            _log_launch("+%ds  %s%s" % (sec, " ".join(parts) or "（无进程）",
                                        "  已退出:%s" % ",".join(gone) if gone else ""))

    try:
        threading.Thread(target=run, name="ssjj-launch-watch", daemon=True).start()
    except Exception:
        pass
