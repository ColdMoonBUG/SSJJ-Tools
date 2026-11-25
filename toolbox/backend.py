# -*- coding: utf-8 -*-
"""工具箱后端：把 ssjj-tool 的加速 / 预下载 / 帧率逻辑包成 UI 可调用的函数。"""
import http.client
import io
import json
import os
import re
import subprocess
import sys
import time
import winreg
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from types import SimpleNamespace

import boot

DATA = boot.bootstrap()


def export_diagnostics(destination):
    import diagnostics
    return diagnostics.export(destination)

import ssjj_paths as P
import local_cdn as C
import swf_fps
import core
import ssjj_boost
import exhaustive_fetch as EF

HERE = Path(__file__).resolve().parent
IS_FROZEN = bool(getattr(sys, "frozen", False))
GAME_LAUNCHER = os.path.join(P.GAME_DIR, "WDlauncher.exe")
CREATE_FLAGS = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


def human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return "%.1f %s" % (n, unit)
        n /= 1024
    return "%.1f TB" % n


def is_admin():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# ---------------------------------------------------------------- 进程拉起
def _self_cmd(argv):
    if IS_FROZEN:
        return [sys.executable] + list(argv)
    pyw = Path(sys.executable).with_name("pythonw.exe")
    exe = str(pyw) if pyw.is_file() else sys.executable
    return [exe, str(HERE / "ssjj_toolbox.py")] + list(argv)


PYI_ENV = ("_PYI_APPLICATION_HOME_DIR", "_PYI_PARENT_PROCESS_LEVEL",
           "_PYI_SPLASH_IPC", "_PYI_ARCHIVE_FILE")


def _spawn(argv):
    # 不清掉这些变量，冻结版 PyInstaller 会让子进程复用父进程的 _MEI 解包目录；
    # 常驻的 --accel-daemon 比 GUI 活得久，GUI 退出时目录里的 dll/pyd 仍被映射，
    # 删不掉就弹 “Failed to remove temporary directory”，还会在 Temp 里攒下上百 MB。
    # 清掉后子进程自己解包一份，双方退出各删各的。
    env = os.environ.copy()
    for k in PYI_ENV:
        env.pop(k, None)
    subprocess.Popen(_self_cmd(argv), cwd=str(DATA), creationflags=CREATE_FLAGS,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True, env=env)


def accel_daemon_main():
    """守护进程：在本进程里跑服务，挂了就重启；daemon.pid 被删就退出。"""
    C.hosts_set(True)
    try:
        Path(C.DAEMON_PID).write_text(str(os.getpid()))
    except Exception:
        pass
    while os.path.exists(C.DAEMON_PID):
        try:
            sys.argv = ["local_cdn.py", "run"]
            C.main()
        except SystemExit:
            pass
        except Exception:
            pass
        time.sleep(2)


# ---------------------------------------------------------------- 加速服务
def accel_running():
    return C._ping()


def accel_stats():
    if not accel_running():
        return None
    try:
        conn = http.client.HTTPConnection("127.0.0.1", 80, timeout=3)
        conn.request("GET", "/__stats__")
        return json.loads(conn.getresponse().read().decode("utf-8"))
    except Exception:
        return {}


def upstream_info():
    try:
        return json.loads(Path(C.UP_FILE).read_text(encoding="utf-8"))
    except Exception:
        return {}


def hosts_marked():
    return C._hosts_has_mark()


def log_tail(n=300):
    try:
        raw = Path(C.LOG).read_text(encoding="utf-8", errors="replace")
        return "\n".join(raw.splitlines()[-n:])
    except Exception:
        return ""


def _kill_all_accel_daemons():
    """按命令行兜底终止所有 --accel-daemon 进程（不依赖当前目录的 pid 文件）。"""
    try:
        ps = (
            "$me = %d; "
            "Get-CimInstance Win32_Process | Where-Object { "
            "  $_.ProcessId -ne $me -and $_.CommandLine -and $_.CommandLine -match '--accel-daemon' "
            "} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
        ) % os.getpid()
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", ps],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=6,
            creationflags=0x08000000,
        )
    except Exception:
        pass


def accel_start():
    _kill_all_accel_daemons()      # 别的目录起的守护会占着 80/443，先清干净
    if not C.hosts_set(True):
        return False, "写 hosts 失败：需要以管理员身份运行"
    save_app_setting("accel_enabled", True)
    if C._ping():
        return True, "加速服务已在运行"
    _spawn(["--accel-daemon"])
    for _ in range(80):
        if C._ping():
            return True, "加速服务已启动"
        time.sleep(0.25)
    # 启动超时自动回滚 hosts 与残留进程，防止把用户网络留在劫持状态
    accel_stop()
    return False, "启动超时（已自动还原 hosts），请到「本地加速」页看日志"


def accel_unified_enable(with_https=False):
    """统一开启本地加速：按需自动装根证书 + 写配置 + 写 hosts + 启动代理守护。"""
    notes = []
    try:
        cfg = _risk_mod("config")
        if with_https:
            mitm = _risk_mod("mitm")
            st = mitm.status()
            if not st.get("ca_exists"):
                mitm.ensure_ca()
                mitm.ensure_leaf()
            if not st.get("ca_installed"):
                okc, lines = mitm.install_ca()
                if not okc:
                    return False, "安装本地根证书失败（需要管理员权限）：%s" % (lines[-1] if lines else "")
                notes.append("根证书已自动安装")
            cfg.set_("mitm.enabled", True)
            notes.append("HTTPS 加速已启用")
        else:
            cfg.set_("mitm.enabled", False)
    except Exception as e:
        if with_https:
            return False, "配置 HTTPS 加速失败：%r" % (e,)

    # 每次开启加速前清空动态短缓存，确保先实时拿到官方最新版本与公告
    try:
        C.dyn_clear()
    except Exception:
        pass
    # 若服务已在运行且切换了 HTTPS 模式，重启让 443 立即生效
    if C._ping():
        accel_stop()
        time.sleep(0.5)
    ok, msg = accel_start()
    if not ok:
        try:
            _risk_mod("config").set_("mitm.enabled", False)
        except Exception:
            pass
        return False, msg
    return True, ("；".join(notes + [msg])) if notes else msg


def accel_unified_disable():
    """统一关闭本地加速：关 HTTPS 拦截 + 终止全部加速守护 + 还原 hosts。"""
    try:
        _risk_mod("config").set_("mitm.enabled", False)
    except Exception:
        pass
    return accel_stop()


def accel_uninstall_ca():
    """卸载本地根证书并关闭 HTTPS 拦截。"""
    try:
        _risk_mod("config").set_("mitm.enabled", False)
        mitm = _risk_mod("mitm")
        ok, lines = mitm.uninstall_ca()
        return ok, (lines[-1] if lines else ("已卸载根证书" if ok else "卸载失败"))
    except Exception as e:
        return False, "卸载根证书失败：%r" % (e,)


def accel_ca_status():
    try:
        mitm = _risk_mod("mitm")
        cfg = _risk_mod("config")
        st = mitm.status()
        st["mitm_enabled"] = bool(cfg.get("mitm.enabled"))
        return st
    except Exception:
        return {"ca_installed": False, "ca_exists": False, "mitm_enabled": False}


def accel_restart():
    accel_stop()
    time.sleep(0.8)
    return accel_start()


def accel_stop():
    save_app_setting("accel_enabled", False)
    C.hosts_set(False)
    C._kill_pid_file(C.DAEMON_PID)
    try:
        os.remove(C.DAEMON_PID)
    except FileNotFoundError:
        pass
    except Exception:
        C._del(C.DAEMON_PID)
    C._kill_pid_file(C.PID_FILE)
    try:
        os.remove(C.PID_FILE)
    except FileNotFoundError:
        pass
    except Exception:
        C._del(C.PID_FILE)
    _kill_all_accel_daemons()
    # 二次确认 hosts 干净（防止守护退出瞬间刚写回）
    time.sleep(0.4)
    C.hosts_set(False)
    return True, "已停止加速服务，hosts 已还原"


# ---------------------------------------------------------------- 资源库
_cache_info = {"ts": 0.0, "val": None}


def cache_stats(ttl=0):
    now = time.time()
    if ttl and _cache_info["val"] and now - _cache_info["ts"] < ttl:
        return _cache_info["val"]
    base = Path(P.CACHE_DIR)
    rows, total_n, total_b = [], 0, 0
    if base.is_dir():
        for d in sorted(base.iterdir()):
            if not d.is_dir():
                continue
            n = b = 0
            for root, _, files in os.walk(d):
                for f in files:
                    if f.endswith(".part"):
                        continue
                    n += 1
                    try:
                        b += os.path.getsize(os.path.join(root, f))
                    except OSError:
                        pass
            rows.append((d.name, n, b))
            total_n += n
            total_b += b
    val = (rows, total_n, total_b)
    _cache_info.update(ts=now, val=val)
    return val


def coverage():
    req = EF._requests_from_log()
    neg = EF._load_neg()
    have = sum(1 for k in req if os.path.isfile(EF._cache_path(k)))
    known = sum(1 for k in req if k in neg and not os.path.isfile(EF._cache_path(k)))
    requested = len(req)
    missing = max(0, requested - have - known)
    pct = (100.0 * have / requested) if requested else 0.0
    return {"requested": requested, "have": have, "missing": missing,
            "known404": known, "pct": pct}


class _LineWriter(io.TextIOBase):
    def __init__(self, emit):
        self.emit = emit
        self.buf = ""

    def write(self, s):
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            line = line.rstrip()
            if line:
                self.emit(line)
        return len(s)

    def flush(self):
        line = self.buf.strip()
        self.buf = ""
        if line:
            self.emit(line)


def _run_captured(fn, emit):
    w = _LineWriter(emit)
    old = sys.stdout
    sys.stdout = w
    try:
        return fn()
    finally:
        sys.stdout = old
        w.flush()


def prewarm(jobs=12, emit=print):
    return _run_captured(lambda: EF.cmd_prewarm(SimpleNamespace(jobs=jobs)), emit)


def cover(emit=print):
    return _run_captured(lambda: EF.cmd_cover(SimpleNamespace(dry_run=False)), emit)


def battle_manifest(refresh=False):
    items, src = ssjj_boost.load_manifest(core.DEFAULT_BASE, core.DEFAULT_PLATFORM,
                                          refresh=refresh)
    return items, src


_manifest_info = {"ts": 0.0, "val": None}


def battle_manifest_counts(ttl=600):
    now = time.time()
    if _manifest_info["val"] and now - _manifest_info["ts"] < ttl:
        return _manifest_info["val"]
    items, src = battle_manifest()
    val = (len(items), sum(i["size"] for i in items), src)
    _manifest_info.update(ts=now, val=val)
    return val


def battle_preload_dir():
    return core.default_preload_dir()


def battle_cache_stats():
    d = battle_preload_dir()
    n = b = 0
    if os.path.isdir(d):
        for name in os.listdir(d):
            p = os.path.join(d, name, "data")
            if os.path.isfile(p):
                n += 1
                try:
                    b += os.path.getsize(p)
                except OSError:
                    pass
    return d, n, b


def battle_preload(patterns, jobs=8, emit=print, progress=None):
    items, src = battle_manifest()
    selected = core.select_items(items, patterns or ["all"])
    selected.sort(key=lambda x: x["size"])
    total = sum(i["size"] for i in selected)
    emit("[*] 清单 %d 个包 / %s，来源 %s" % (len(selected), human(total), src))
    preload_dir = battle_preload_dir()
    stats = {}
    done_bytes = 0
    with ThreadPoolExecutor(max_workers=max(2, int(jobs))) as ex:
        futs = {ex.submit(core.download_one, it, preload_dir, core.DEFAULT_BASE,
                          core.DEFAULT_PLATFORM, False, True): it for it in selected}
        for idx, fu in enumerate(as_completed(futs), 1):
            st, it, info = fu.result()
            stats[st] = stats.get(st, 0) + 1
            if st in ("ok", "skip"):
                done_bytes += it["size"]
            else:
                emit("  [!] %s %s %s" % (st, it["name"][:40], info))
            if progress and (idx % 10 == 0 or idx == len(selected)):
                progress(int(100.0 * idx / max(1, len(selected))),
                         "%d/%d | %s/%s" % (idx, len(selected),
                                            human(done_bytes), human(total)))
    emit("[*] 完成: %s" % stats)
    return stats


# ---------------------------------------------------------------- 大厅帧率
def fps_state():
    paras = P.read_paras()
    name = paras.get("lobbyFileName")
    official = None
    if name:
        path = os.path.join(P.CACHE_DIR, name)
        if os.path.isfile(path):
            try:
                official = swf_fps.read_fps(path)
            except Exception:
                official = None
    rec = {}
    try:
        rec = json.loads(Path(P.HERE, "fps_override.json").read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"name": name, "official": official,
            "override": int(rec.get("fps") or 0),
            "renamed": bool(rec.get("renamed")), "dst": rec.get("dst")}


def fps_set(fps, rename=True):
    lines = []
    argv = ["set", str(int(fps))] + (["--rename"] if rename else [])
    _run_captured(lambda: C.cmd_fps(argv), lines.append)
    return lines


def fps_off():
    lines = []
    _run_captured(lambda: C.cmd_fps(["off"]), lines.append)
    return lines


# ---------------------------------------------------------------- 游戏内帧率
WOODUAN_KEY = r"Software\Wooduan"
BATTLE_EXE = "SSJJ_BattleClient_Unity.exe"


def _enum_names(key):
    names, i = [], 0
    while True:
        try:
            name, _val, _typ = winreg.EnumValue(key, i)
        except OSError:
            break
        names.append(name)
        i += 1
    return names


def _pref_subs():
    """Wooduan 下含 Unity PlayerPrefs 帧率项的注册表子键。"""
    out = []
    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, WOODUAN_KEY)
    except OSError:
        return out
    with root:
        i = 0
        while True:
            try:
                sub = winreg.EnumKey(root, i)
            except OSError:
                break
            i += 1
            try:
                with winreg.OpenKey(root, sub) as k:
                    names = _enum_names(k)
            except OSError:
                continue
            if any(re.match(r"^FrameLimit_h\d+$", n) for n in names):
                out.append(sub)
    return out


def _pref_name(sub, pattern):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            WOODUAN_KEY + "\\" + sub) as k:
            for n in _enum_names(k):
                if re.match(pattern, n):
                    return n, winreg.QueryValueEx(k, n)[0]
    except OSError:
        pass
    return None, None


def battle_running():
    try:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq " + BATTLE_EXE,
                            "/FO", "CSV", "/NH"],
                           capture_output=True, text=True, encoding="gbk",
                           errors="replace", timeout=8,
                           creationflags=0x08000000)
        return BATTLE_EXE.lower() in (r.stdout or "").lower()
    except Exception:
        return False


def game_fps_state():
    subs = _pref_subs()
    st = {"found": False, "key": None, "limit": None, "vsync": None,
          "running": battle_running()}
    if not subs:
        return st
    sub = subs[0]
    fname, limit = _pref_name(sub, r"^FrameLimit_h\d+$")
    vname, vsync = _pref_name(sub, r"^VSync_h\d+$")
    st.update(found=fname is not None, key="HKCU\\%s\\%s" % (WOODUAN_KEY, sub),
              limit=int(limit) if fname else None,
              vsync=(int(vsync) != 0) if vname else None)
    return st


def game_fps_set(limit=None, vsync_off=None):
    """直接改 Unity PlayerPrefs（注册表）。游戏退出时会回写，所以要求先关客户端。"""
    if battle_running():
        return False, "战斗客户端正在运行：先退出游戏再写，否则退出时会被游戏覆盖。"
    subs = _pref_subs()
    if not subs:
        return False, "没有找到游戏设置项。先启动一次战斗客户端，让它生成 FrameLimit 后再回来。"
    sub = subs[0]
    path = WOODUAN_KEY + "\\" + sub
    done = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0,
                            winreg.KEY_READ | winreg.KEY_SET_VALUE) as k:
            fname, _old = _pref_name(sub, r"^FrameLimit_h\d+$")
            if limit is not None and fname:
                winreg.SetValueEx(k, fname, 0, winreg.REG_DWORD, int(limit))
                done.append("帧率上限 %d fps" % int(limit))
            vname, _v = _pref_name(sub, r"^VSync_h\d+$")
            if vsync_off is not None and vname:
                winreg.SetValueEx(k, vname, 0, winreg.REG_DWORD,
                                  0 if vsync_off else 1)
                done.append("垂直同步" + ("关闭" if vsync_off else "开启"))
    except OSError as e:
        return False, "写注册表失败：%r" % (e,)
    if not done:
        return False, "游戏还没生成对应设置项，先启动一次战斗客户端。"
    return True, "已写入游戏设置：" + "，".join(done) + "。启动战斗端后生效。"


# ---------------------------------------------------------------- 游戏与系统
def game_installed():
    try:
        import quick_login as QL
        return bool(QL.find_launcher_exe() or QL.find_microclient_exe())
    except Exception:
        return os.path.isfile(GAME_LAUNCHER)


def launch_game():
    import quick_login as QL
    return QL.launch_official()


TASK_NAME = "SSJJ Toolbox Accel"


def autostart_state():
    try:
        r = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME],
                           capture_output=True, timeout=8,
                           creationflags=0x08000000)
        if r.returncode == 0:
            return True
    except Exception:
        pass
    return os.path.isfile(C.STARTUP_VBS)


def autostart(add):
    if add:
        return False, "开机自启已永久关闭"
    try:
        subprocess.run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME],
                       capture_output=True, timeout=15,
                       creationflags=0x08000000)
    except Exception:
        pass
    try:
        os.remove(C.STARTUP_VBS)
    except FileNotFoundError:
        pass
    return True, "已取消开机自启"


def open_path(p):
    if p and os.path.exists(p):
        os.startfile(p)
        return True
    return False


def elevate():
    """以管理员身份重启自己。"""
    try:
        import ctypes
        parts = _self_cmd([])
        exe, args = parts[0], " ".join('"%s"' % a for a in parts[1:])
        r = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, args, str(DATA), 1)
        return r > 32
    except Exception:
        return False


# ---------------------------------------------------------------- 外观（美化插件配置）
def battle_dirs():
    base = os.path.join(P.GAME_DIR, "battle")
    out = []
    if os.path.isdir(base):
        for n in sorted(os.listdir(base)):
            p = os.path.join(base, n)
            if os.path.isfile(os.path.join(p, "SSJJ_BattleClient_Unity.exe")):
                out.append(p)
    return out


def running_battle_dir():
    try:
        r = subprocess.run(["wmic", "process", "where",
                            "name='SSJJ_BattleClient_Unity.exe'", "get",
                            "ExecutablePath"], capture_output=True, text=True,
                           encoding="gbk", errors="replace", timeout=8,
                           creationflags=0x08000000)
        for line in (r.stdout or "").splitlines():
            line = line.strip()
            if line.lower().endswith(".exe"):
                return os.path.dirname(line)
    except Exception:
        pass
    return None


def _mod_root_of(d):
    return os.path.join(d, "SingleSkinMod")


def mod_state():
    dirs = battle_dirs()
    live = running_battle_dir()
    chosen = None
    if live and os.path.isdir(_mod_root_of(live)):
        chosen = live
    if chosen is None:
        with_mod = [d for d in dirs if os.path.isdir(_mod_root_of(d))]
        chosen = with_mod[0] if with_mod else (dirs[0] if dirs else None)
    info = {"dir": chosen, "bepinex": False, "plugin": False, "mod_dir": None,
            "config_path": None, "config": {}, "running": live}
    if chosen:
        mod_dir = _mod_root_of(chosen)
        info["mod_dir"] = mod_dir
        info["bepinex"] = os.path.isdir(os.path.join(chosen, "BepInEx"))
        info["plugin"] = os.path.isfile(os.path.join(chosen, "BepInEx", "plugins",
                                                     "SingleSkinMod.dll"))
        cfg = os.path.join(mod_dir, "config.json")
        info["config_path"] = cfg
        try:
            info["config"] = json.loads(Path(cfg).read_text(encoding="utf-8"))
        except Exception:
            info["config"] = {}
    return info


def save_mod_config(mod_dir, cfg):
    if not mod_dir or not os.path.isdir(mod_dir):
        return False, "没有找到已部署的 SingleSkinMod 目录"
    os.makedirs(mod_dir, exist_ok=True)
    path = os.path.join(mod_dir, "config.json")
    try:
        Path(path).write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                              encoding="utf-8")
        return True, path
    except Exception as e:
        return False, "写配置失败：%r" % (e,)


def set_background(mod_dir, src, fit=0, enable=True):
    if not mod_dir:
        return False, "没有找到已部署的 SingleSkinMod 目录"
    dst_dir = mod_dir if os.path.basename(mod_dir) == "SingleSkinMod" else _mod_root_of(mod_dir)
    os.makedirs(dst_dir, exist_ok=True)
    ext = os.path.splitext(src)[1].lower() or ".jpg"
    dst = os.path.join(dst_dir, "users_background" + ext)
    try:
        import shutil
        shutil.copyfile(src, dst)
    except Exception as e:
        return False, "复制图片失败：%r" % (e,)
    return True, dst


def list_skins(mod_dir):
    if not mod_dir:
        return "", []
    d = os.path.join(mod_dir, "skin")
    out = []
    if os.path.isdir(d):
        for f in sorted(os.listdir(d)):
            if f.lower().endswith((".png", ".jpg", ".jpeg", ".tga")):
                out.append(f)
    return d, out


def find_panel_exe():
    cands = []
    for d in battle_dirs():
        cands.append(os.path.join(d, "SingleSkinMod_Panel", "SingleSkinModPanel.exe"))
    cands.append(r"D:\Ccode\SSJJYOLO\gua\bin\Release\net8.0-windows\win-x64\SingleSkinModPanel.exe")
    cands.append(r"D:\Ccode\SSJJYOLO\gua\bin\panel\SingleSkinModPanel.exe")
    for c in cands:
        if os.path.isfile(c):
            return c
    return None


# ---------------------------------------------------------------- 风险模式
# 这一段的每个入口都延迟 import risk.*：risk 包会 import cryptography 之类的
# 第三方模块，普通模式下用不到，import 失败不该把整个工具箱带崩。
def _risk_mod(name):
    import importlib
    return importlib.import_module("risk." + name)


def risk_ready():
    # risk 包是否可用（缺 cryptography 之类会返回 False + 原因）
    try:
        import risk  # noqa: F401
        return True, ''
    except Exception as e:
        return False, repr(e)


def risk_status():
    try:
        guard = _risk_mod('guard')
        mitm = _risk_mod('mitm')
        st = guard.status()
        st['mitm'] = mitm.status()
        st['ready'] = True
        return st
    except Exception as e:
        return {'ready': False, 'error': repr(e), 'enabled': False,
                'processes': [], 'injected_pids': [], 'lobby': {},
                'localized': {'hit': 0, 'miss': 0, 'rate': 0.0},
                'payload': {'path': '', 'built': False, 'size': 0},
                'mitm': {'installed': False, 'available': False}}


def risk_enable(easing=None, fps=None, use_descriptor_fps=None):
    save_app_setting("risk_enabled", True)
    guard = _risk_mod('guard')
    return guard.enable(easing=easing, fps=fps, use_descriptor_fps=use_descriptor_fps)


def risk_disable():
    save_app_setting("risk_enabled", False)
    guard = _risk_mod('guard')
    return guard.disable()


def shutdown_all(emit=None):
    """关软件时把后台全部收掉：注入守护、MITM、大厅、加速守护、hosts。

    以前只关窗口，守护线程和 --accel-daemon 子进程留在后台，开第二个实例就两个
    守护抢同一个战斗端（日志里的「已由另一个守护进程接管」就是这么来的）。
    每一步都单独兜住，前一步炸了后面的照样执行；结果写进 risk_mode.log 便于核对。
    """
    logs = []

    def note(text):
        logs.append(text)
        try:
            _risk_mod('guard').log("[exit] " + text)
        except Exception:
            pass
        if emit:
            try:
                emit(text)
            except Exception:
                pass

    for label, fn in (("注入守护", risk_inject_stop),
                      ("风险模式", risk_disable),
                      ("本地加速", accel_stop)):
        try:
            fn()
            note("已关闭 %s" % label)
        except Exception as e:
            note("关闭 %s 失败：%r" % (label, e))
    # 退出时自动清理 battle\*_64\ 下属于本工具的代理残留，避免关闭工具箱后仍被游戏拦截
    try:
        ok_p, msg_p = _risk_mod("proxy").uninstall(emit=lambda s: note("[proxy] " + str(s)))
        note("退出清理代理（%s）：%s" % ("完成" if ok_p else "部分失败", msg_p))
    except Exception as e:
        note("退出清理代理失败：%r" % (e,))
    # hosts 兜底还原：accel_stop 里那一步万一没跑成，绝不能让用户网络留在劫持状态
    try:
        if C._hosts_has_mark():
            C.hosts_set(False)
            note("hosts 已还原")
        else:
            note("hosts 干净")
    except Exception as e:
        note("hosts 还原失败：%r" % (e,))
    try:
        _kill_all_accel_daemons()
    except Exception:
        pass
    note("后台已全部停止")
    return logs


def risk_inject_now(allow_elevate=True):
    guard = _risk_mod('guard')
    return guard.inject_now(allow_elevate=allow_elevate)


def risk_inject_state():
    """注入守护自己的状态（与总开关无关）。"""
    try:
        guard = _risk_mod('guard')
        return guard.inject_state()
    except Exception as e:
        return {'enabled': False, 'thread_alive': False, 'processes': [],
                'injected': [], 'admin': False, 'error': repr(e)}


def risk_inject_start(elevate=False):
    guard = _risk_mod('guard')
    return guard.inject_start(elevate=elevate)


def risk_inject_stop():
    guard = _risk_mod('guard')
    return guard.inject_stop()


def risk_admin_state():
    priv = _risk_mod('privilege')
    return priv.status()


def risk_elevate(reinject=False):
    """以管理员身份重开工具箱（可选顺便注入当前在跑的战斗端）。"""
    guard = _risk_mod('guard')
    return guard.elevate(reinject=reinject)


# --------------------------------------------------- 代理 DLL 模式（version.dll）
def risk_proxy_state():
    """version.dll 代理的部署状态。"""
    try:
        proxy = _risk_mod('proxy')
        return proxy.status()
    except Exception as e:
        return {'installed_count': 0, 'installed': [], 'battle_dirs': [], 'error': repr(e)}


def risk_proxy_install(all_dirs=True, emit=None):
    proxy = _risk_mod('proxy')
    return proxy.install(emit=emit, all_dirs=all_dirs)


def risk_proxy_uninstall(emit=None):
    # 避免移除时守护线程马上 heal()，或下次启动按旧设置重新部署。
    risk_inject_stop()
    save_app_setting("risk_enabled", False)
    try:
        _risk_mod("guard").disable()
    except Exception:
        pass
    proxy = _risk_mod('proxy')
    return proxy.uninstall(emit=emit)


def risk_proxy_build(emit=None):
    proxy = _risk_mod('proxy')
    return proxy.build(emit=emit)


def risk_proxy_tail(n=20):
    try:
        proxy = _risk_mod('proxy')
        return proxy.tail(n)
    except Exception:
        return []


def _payload_flag(name, text):
    """把策略开关同时写到数据目录和 %LOCALAPPDATA%\\ssjj-toolbox。

    载荷跑在游戏进程里，它只认 %LOCALAPPDATA%\\ssjj-toolbox；数据目录那份
    给「源码运行」和排查时看，两边都写最稳。
    """
    targets = [os.path.join(str(DATA), name)]
    try:
        targets.append(os.path.join(os.environ.get('LOCALAPPDATA') or
                                    os.path.expanduser('~'), 'ssjj-toolbox', name))
    except Exception:
        pass
    for path in targets:
        try:
            d = os.path.dirname(path)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(str(text))
        except Exception:
            pass


def risk_inject_option(key, value=None):
    """读/写 inject.* 下的冷加载、ACE 策略、自动提权开关。

    载荷在游戏进程里读不到这里的 json，所以两个「策略」类开关会同步成
    %LOCALAPPDATA%\\ssjj-toolbox 下的标志文件：文件存在 == 打开。
    """
    cfg = _risk_mod('config')
    if value is None:
        return cfg.get('inject.' + key)
    if key == 'ace_mode' and value not in ('none', 'teardown', 'full'):
        return cfg.get('inject.ace_mode')
    if key == 'method' and value not in ('proxy', 'thread'):
        return cfg.get('inject.method')
    cfg.set_('inject.' + key, value)
    if key == 'exit_hold':
        _payload_flag('risk_exit_hold.txt', 'on' if value else 'off')
    elif key == 'exit_delay':
        try:
            _payload_flag('risk_exit_delay.txt', '%.1f' % float(value or 0))
        except Exception:
            pass
    try:
        fname = content = None
        want = False
        if key == 'cold_swap':
            fname, content = 'risk_swap_hot.txt', 'hot'
            want = not bool(value)                  # 冷加载开 ⇒ 不启用热加载
        elif key == 'ace_mode':
            # 三档都写显式内容（none / teardown / full），载荷按内容分支。
            # 以前的写法是「full 才存在」，导致「文件不存在 == full」，改策略时
            # 删文件这一步一失败就静默退回 full —— 正是这次 ACE 全打回来的原因。
            fname, content = 'risk_ace_full.txt', str(value)
            want = True
        if fname:
            # 同步写入 DATA 目录与 %LOCALAPPDATA%\ssjj-toolbox（载荷从 LocalAppData 读取）
            local_dir = os.path.join(
                os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'), 'ssjj-toolbox')
            for base_dir in (str(DATA), local_dir):
                fpath = os.path.join(base_dir, fname)
                if want:
                    os.makedirs(base_dir, exist_ok=True)
                    with open(fpath, 'w', encoding='utf-8') as f:
                        f.write(content)
                elif os.path.isfile(fpath):
                    try:
                        os.remove(fpath)
                    except OSError:
                        pass
    except Exception:
        pass
    return value


def risk_build_payload(force=False):
    inj = _risk_mod('injector')
    return inj.build_payload(force=force)


def risk_lobby_presets():
    sw = _risk_mod('swfpatch')
    return list(sw.EASING_PRESETS.items())


def risk_lobby_inspect():
    lobby = _risk_mod('lobby')
    return lobby.status()


def risk_lobby_apply(easing='柔和', fps=0, use_descriptor_fps=False):
    lobby = _risk_mod('lobby')
    return lobby.apply(easing=easing, fps=fps, use_descriptor_fps=use_descriptor_fps)


def risk_lobby_restore():
    lobby = _risk_mod('lobby')
    return lobby.restore()


def risk_mitm(action='status'):
    mitm = _risk_mod('mitm')
    if action == 'status':
        return True, [str(mitm.status())]
    if action == 'init':
        _, m1 = mitm.ensure_ca()
        _, _, m2 = mitm.ensure_leaf()
        return True, list(m1) + list(m2)
    if action == 'install':
        return mitm.install_ca()
    if action == 'uninstall':
        return mitm.uninstall_ca()
    if action == 'cleanup':
        return True, ['已删除 %d 个证书文件' % mitm.cleanup()]
    if action == 'selftest':
        r = mitm.selftest()
        return bool(r.get('ok')), ['MITM 自检 %s  响应=%r  %d 字节  %s' % (
            '通过' if r.get('ok') else '失败', r.get('status'), r.get('bytes'),
            r.get('hint') or '（TLS 解密 + 本地命中 全链路正常）')] + [
            '错误: ' + e for e in r.get('errors') or []]
    if action == 'seen':
        data = mitm.seen()
        rows = sorted(data.items(), key=lambda kv: -(kv[1].get('size') or 0))
        lines = ['https 路径台账 %d 条  文件 %s' % (len(rows), mitm.SEEN_PATH)]
        for path, info in rows[:40]:
            lines.append('  %8dB x%-3d %s %s' % (info.get('size') or 0, info.get('n') or 0,
                                                 '本地' if info.get('local') else '回源',
                                                 path[:90]))
        return True, lines
    if action == 'prewarm':
        guard = _risk_mod('guard')
        r = guard.prewarm_https()
        return not r.get('error'), ['预热: 新填 %d / 已存在 %d / 失败 %d / 台账 %d'
                                    % (r.get('ok', 0), r.get('skip', 0),
                                       r.get('fail', 0), r.get('total', 0))]
    return False, ['未知动作 %r' % action]


def risk_log(n=200):
    try:
        guard = _risk_mod('guard')
        return guard.log_tail(n)
    except Exception as e:
        return ['读取日志失败: %r' % (e,)]


def risk_config(key=None, value=None):
    cfg = _risk_mod('config')
    if key is None:
        return cfg.load(reload=True)
    if value is None:
        return cfg.get(key)
    cfg.set_(key, value)
    return cfg.get(key)


def ecs_cache_state():
    # /ecs 下的补丁包有没有被本地化（每次启动省 23MB / 约 3 秒）
    d = os.path.join(P.CACHE_DIR, 'ecs')
    total = 0
    count = 0
    if os.path.isdir(d):
        for root, _dirs, files in os.walk(d):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                    count += 1
                except Exception:
                    pass
    return {'dir': d, 'files': count, 'bytes': total}


# ---------------------------------------------------------------- 一键登录
def quick_login_servers():
    import quick_login as QL
    return list(QL.SERVERS)


def quick_login_load():
    import quick_login as QL
    return QL.load_profile()


def quick_login_save(username, password, server_name, remember=True):
    import quick_login as QL
    return QL.save_profile(username, password, server_name, remember=remember)


def quick_login_launch(username, password, server_name, with_accel=True, emit=None):
    """首页主按钮：开启换肤，再按官方流程打开游戏。"""
    import quick_login as QL
    ok, msg = risk_inject_start()
    if emit:
        emit(msg)
    if not ok:
        return False, msg
    return QL.launch_official(emit=emit)


# ---------------------------------------------------------------- 武器皮肤与建模替换
def skin_catalog(force=False):
    import skin_swap as SS
    return SS.load_catalog(force=force)


def skin_rebuild_catalog():
    import skin_swap as SS
    ok, msg, _cat = SS.build_catalog_from_sjsf()
    if ok:
        SS.load_catalog(force=True)
    return ok, msg


def skin_load_rules():
    import skin_swap as SS
    return SS.load_rules()


def skin_save_rules(data):
    import skin_swap as SS
    return SS.save_rules(data)


def skin_ensure_bundles(codes, emit=None):
    import skin_swap as SS
    return SS.ensure_bundles_cached(codes, emit=emit)


# ---------------------------------------------------------------- 全局设置持久化与启动自动恢复
SETTINGS_PATH = Path(DATA) / "toolbox_settings.json"


def load_app_settings():
    default = {
        "accel_enabled": False,
        "accel_autostart": False,
        "risk_enabled": None,
        "auto_check_update": True,
    }
    if not SETTINGS_PATH.is_file():
        return default
    try:
        d = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            default.update(d)
    except Exception:
        pass
    # 开机自启默认关闭且永远不启用
    default["accel_autostart"] = False
    return default


def app_version():
    import updater as U
    return U.APP_VERSION


def check_update():
    import updater as U
    return U.check_update()


def download_update(download_url, asset_name, expected_size=0, progress_cb=None):
    import updater as U
    return U.download_update(download_url, asset_name, expected_size=expected_size,
                             progress_cb=progress_cb)


def launch_update_script(bat_path):
    import updater as U
    return U.launch_update_script(bat_path)


def save_app_setting(key, value):
    d = load_app_settings()
    d[key] = value
    try:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return d


def restore_on_startup():
    """工具箱启动时自动恢复上次保存的所有状态：加速服务、风险模式注入守护、皮肤替换配置。"""
    logs = []
    st = load_app_settings()

    # 1. 本地加速默认不随软件启动（只有用户在「加速服务」页手动开启才会跑）。
    #    这样软件启动时只做一件事：准备换肤注入守护，不会静默写 hosts、不会偷偷缓存资源。
    if st.get("accel_autostart") and st.get("accel_enabled", False) and not accel_running():
        ok, msg = accel_start()
        logs.append("加速服务: " + msg)

    # 2. 自动恢复风险模式注入守护（如果 risk_config.json 里 enabled=True）
    try:
        cfg = risk_config()
        want_risk = bool(cfg.get("enabled")) if st.get("risk_enabled") is None else bool(st.get("risk_enabled"))
        if want_risk:
            guard = _risk_mod("guard")
            if not guard.status().get("thread_alive"):
                guard.enable()
                logs.append("风险模式守护已自动恢复")
    except Exception as e:
        logs.append("风险模式恢复跳过: %r" % (e,))

    # 2b. 只开注入：总开关关着、inject.enabled=True 时，单独把注入守护拉起来。
    #     以前这条路走不通 —— 守护线程只由 enable() 创建，只开注入等于没开。
    try:
        cfg = risk_config()
        if not bool(cfg.get("enabled")) and bool((cfg.get("inject") or {}).get("enabled")):
            guard = _risk_mod("guard")
            if not guard.inject_state().get("thread_alive"):
                _ok, msg = guard.inject_start()
                logs.append("注入守护: " + str(msg))
    except Exception as e:
        logs.append("注入守护恢复跳过: %r" % (e,))

    # 3. 确保皮肤替换配置已就绪
    try:
        sr = skin_load_rules()
        if sr.get("enabled") and sr.get("rules"):
            logs.append("已自动加载 %d 条武器皮肤/建模替换规则" % len(sr["rules"]))
    except Exception:
        pass

    return logs


def startup_housekeeping():
    """启动时后台自动清理：过期崩溃转储(.dmp)、无窗口残留 MicroClient 与废弃缓存碎片。

    - 只在没有可见官方大厅窗口时清理无界面的残留 MicroClient.exe，避免锁死 CEF 缓存。
    - 绝不碰私服进程；对用户态无法终止的内核态僵尸句柄仅记录提示。
    """
    summary = {"dumps_removed": 0, "dumps_mb": 0.0, "mc_killed": 0,
               "cache_removed": 0, "zombies": []}
    local_app = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    tb_dir = os.path.join(local_app, "ssjj-toolbox")

    # 1. 清理 hangdumps 里的超大 .dmp 崩溃转储（保留 .txt 诊断日志）
    dump_dir = os.path.join(tb_dir, "hangdumps")
    if os.path.isdir(dump_dir):
        freed = 0
        for name in os.listdir(dump_dir):
            if not name.lower().endswith(".dmp"):
                continue
            fp = os.path.join(dump_dir, name)
            try:
                sz = os.path.getsize(fp)
                os.remove(fp)
                summary["dumps_removed"] += 1
                freed += sz
            except Exception:
                pass
        summary["dumps_mb"] = round(freed / 1048576.0, 1)

    # 2. 如果当前没有可见的官方大厅主窗口，清理后台无窗口残留的 MicroClient.exe
    try:
        import psutil
        import quick_login as QL
        has_lobby = QL.active_official_lobby()
        mc_alive = []
        for proc in psutil.process_iter(["pid", "name"]):
            pname = (proc.info.get("name") or "").lower()
            if pname == "microclient.exe":
                if not has_lobby:
                    try:
                        proc.terminate()
                        try:
                            proc.wait(timeout=2.0)
                        except Exception:
                            proc.kill()
                        summary["mc_killed"] += 1
                    except Exception:
                        summary["zombies"].append("MicroClient.exe(%d)" % proc.info["pid"])
                        mc_alive.append(proc.info["pid"])
                else:
                    mc_alive.append(proc.info["pid"])
            elif pname == "ssjj_battleclient_unity.exe":
                try:
                    if proc.num_threads() == 0:
                        summary["zombies"].append("SSJJ_BattleClient_Unity.exe(%d)" % proc.info["pid"])
                except Exception:
                    summary["zombies"].append("SSJJ_BattleClient_Unity.exe(%d)" % proc.info["pid"])

        # 3. 无活跃 MicroClient 时清理 CEF 失败遗留的 old_Microclient_64_* 临时碎片目录
        if not has_lobby and not mc_alive:
            wd_dir = os.path.join(local_app, "Wooduan")
            if os.path.isdir(wd_dir):
                for name in os.listdir(wd_dir):
                    if name.lower().startswith("old_microclient"):
                        fp = os.path.join(wd_dir, name)
                        try:
                            shutil.rmtree(fp, ignore_errors=True)
                            if not os.path.exists(fp):
                                summary["cache_removed"] += 1
                        except Exception:
                            pass
    except Exception:
        pass

    # 3.5 代理残留自愈：异常崩溃未走 shutdown_all() 时，清理未锁定的 .bak-* 与孤立 .ver*.dat；
    # 若注入开关未开启，顺便清理残留的我方 version.dll，避免“关着工具箱进不去游戏”。
    proxy_cleaned = {"bak": 0, "alt": 0, "dll": 0}
    try:
        cfg = _risk_mod("config").load(reload=True)
        inject_on = bool((cfg.get("inject") or {}).get("enabled"))
        active_dirs = _risk_mod("guard").active_battle_dirs()
        proxy_cleaned = _risk_mod("proxy").cleanup_stale(
            remove_dll=(not inject_on),
            skip_dirs=active_dirs,
        )
    except Exception:
        pass
    summary["proxy_cleaned"] = proxy_cleaned

    # 4. 写一行简洁记录到 housekeeping.log
    try:
        os.makedirs(tb_dir, exist_ok=True)
        line = (
            "%s  dumps=%d(%.1fMB) mc_killed=%d cache_removed=%d "
            "proxy_bak=%d proxy_alt=%d proxy_dll=%d zombies=%s\n"
            % (time.strftime("%Y-%m-%d %H:%M:%S"),
               summary["dumps_removed"], summary["dumps_mb"],
               summary["mc_killed"], summary["cache_removed"],
               proxy_cleaned["bak"], proxy_cleaned["alt"], proxy_cleaned["dll"],
               ",".join(summary["zombies"]) if summary["zombies"] else "none")
        )
        with open(os.path.join(tb_dir, "housekeeping.log"), "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass

    return summary
