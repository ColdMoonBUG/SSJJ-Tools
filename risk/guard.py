# -*- coding: utf-8 -*-
"""风险模式总控：把大厅美化、托管注入、过检测降级串成一条启动链。

对外只暴露四件事：
    status()        当前状态（本地化 / 大厅变体 / 注入器 / 进程）
    enable()        开风险模式：大厅变体 + 后台常驻注入器
    disable()       关风险模式：还原大厅 + 停注入器
    inject_now()    立刻对已在跑的战斗端注入一次

设计取舍
--------
* 注入器是「常驻守护」而不是「一次性动作」：战斗端进程随时可能被重开，守护线程
  每 1.5 秒扫一次进程，见到新 pid 且 mono 已加载就注入，注入过的 pid 记在内存
  集合里，避免同一进程反复注入。
* mono 运行时在战斗端启动后几秒才加载，早期注入会找不到 mono。所以发现进程
  之后先等 mono 出现（最多 WAIT_MONO 秒），超时就跳过这轮等下一轮。
* 一切路径都走 risk.config，不在这里写死，方便 GUI 改开关。
"""
import os
import sys
import threading
import time

from . import config, injector

POLL_INTERVAL = 1.5
WAIT_MONO = 90.0
LOG_PATH = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                        "ssjj-toolbox", "risk_mode.log")

_lock = threading.Lock()
_thread = None
_stop = threading.Event()
_done_pids = set()
_elevated = set()          # 已经为哪些 pid 弹过 UAC，别反复刷
_last = {"ts": 0, "text": "未启动"}

# 同一 pid 只允许一个守护实例注入：GUI 和命令行各起一个守护时，内存里的
# _done_pids 互不可见，同一秒会把两个载荷实例打进同一个进程。
_k32 = None
_claim_handles = {}
_claimed = set()


def _claim(pid):
    global _k32
    if pid in _claimed:
        return False
    try:
        import ctypes
        from ctypes import wintypes
        if _k32 is None:
            _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            _k32.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
            _k32.CreateMutexW.restype = wintypes.HANDLE
            _k32.CloseHandle.argtypes = (wintypes.HANDLE,)
            _k32.CloseHandle.restype = wintypes.BOOL
        h = _k32.CreateMutexW(None, False, "Local\\ssjj_risk_inject_%d" % pid)
        err = ctypes.get_last_error()
        if not h:
            return False
        if err == 183:                      # ERROR_ALREADY_EXISTS
            _k32.CloseHandle(h)
            return False
        _claim_handles[pid] = h
        _claimed.add(pid)
        return True
    except Exception:
        return False


def _release(pid):
    _claimed.discard(pid)
    h = _claim_handles.pop(pid, None)
    if h and _k32:
        try:
            _k32.CloseHandle(h)
        except Exception:
            pass


def log(text):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), text)
    with _lock:
        _last["ts"] = time.time()
        _last["text"] = text
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            need_trim = f.tell() > 2 * 1024 * 1024
        if need_trim:
            with open(LOG_PATH, "rb") as f:
                f.seek(-256 * 1024, os.SEEK_END)
                tail = f.read()
            nl = tail.find(b"\n")
            if nl >= 0:
                tail = tail[nl + 1:]
            with open(LOG_PATH, "wb") as f:
                f.write(tail)
    except Exception:
        pass
    return line


def log_tail(n=40):
    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()[-n:]
    except Exception:
        return []


def last_event():
    return dict(_last)


# ------------------------------------------------------------------ 注入
def inject_now(pid=None, target=None, allow_elevate=True):
    """对已在跑的战斗端注入一次，返回 (ok, 日志行列表)

    代理模式下这里必须直接拒绝：远程线程注入会被 GameGuard 当场杀进程，
    手动点「立即注入一次」等于把这一局打死。
    """
    target = target or config.get("inject.targets")[0] or injector.DEFAULT_TARGET
    if str(config.get("inject.method") or "proxy").lower() == "proxy":
        from . import proxy as _proxy
        n = 0
        try:
            n = len(_proxy.deployed())
        except Exception:
            n = 0
        msg = ("代理模式：载荷由 version.dll 在战斗端启动时自加载，不需要也不允许跨进程注入"
               "（远程线程会被 GameGuard 用 0x80000004 杀掉）。当前已部署 %d 个目录。" % n)
        log("[inject] 拒绝执行远程线程注入：当前是代理模式")
        return bool(n), msg
    if pid is None:
        pid = injector.find_pid(target)
    if not pid:
        return False, log("[inject] %s 没在运行" % target)
    guard_attempt = "guard-%d-%d-%d" % (pid, os.getpid(), time.time_ns())
    injector.trace_event(pid, guard_attempt, "guard.request", target=target,
                         executable=sys.executable, frozen=bool(getattr(sys, "frozen", False)),
                         config_path=config.CONFIG_PATH,
                         enabled=bool(config.get("inject.enabled")),
                         auto=bool(config.get("inject.auto")),
                         settle_seconds=config.get("inject.settle_seconds"))
    if not _claim(pid):
        _done_pids.add(pid)
        injector.trace_event(pid, guard_attempt, "guard.claim_failed")
        return False, log("[inject] pid=%d 无法取得独占标记或已由另一个实例处理，未确认载荷运行" % pid)
    try:
        injector.trace_event(pid, guard_attempt, "guard.build.begin")
        _, built_path = injector.build_payload()
        injector.trace_event(pid, guard_attempt, "guard.build.end", payload_path=built_path)
        res = injector.inject(pid, target_name=target)
        injector.trace_event(pid, guard_attempt, "guard.inject.return", ok=res["ok"],
                             attempted=res.get("attempted"))
        for l in res["log"]:
            log("[inject] " + l.lstrip("[*] "))
        if res["ok"] or res.get("attempted"):
            _done_pids.add(pid)
        else:
            _release(pid)
        return res["ok"], "\n".join(res["log"])
    except injector.NeedAdmin as e:
        injector.trace_event(pid, guard_attempt, "guard.need_admin", error=str(e))
        _release(pid)
        log("[inject] 权限不足：%s" % e)
        if not allow_elevate:
            return False, str(e)
        ok, detail = _elevated_inject(pid)
        if ok:
            _done_pids.add(pid)
        return ok, detail
    except injector.InjectionAttempted as e:
        injector.trace_event(pid, guard_attempt, "guard.inject.attempted_error", error=str(e))
        _done_pids.add(pid)
        for line in e.log:
            log("[inject] " + line)
        return False, log("[inject] pid=%d 远程线程已尝试启动，结果读取失败；本局不重试：%s" %
                          (pid, e))
    except Exception as e:
        injector.trace_event(pid, guard_attempt, "guard.error", error=repr(e))
        _release(pid)
        return False, log("[inject] 失败: %r" % (e,))


# ------------------------------------------------------------ 提权注入握手
def _result_path():
    import os
    base = os.environ.get("LOCALAPPDATA") or os.path.dirname(LOG_PATH)
    return os.path.join(base, "ssjj-toolbox", "risk_inject_result.json")


def write_result(pid, ok, detail=""):
    """提权副本用这个把结果交回给未提权的主界面。"""
    import json
    import os
    p = _result_path()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"pid": int(pid), "ok": bool(ok), "detail": detail,
                       "ts": time.time()}, f, ensure_ascii=False)
        os.replace(tmp, p)
    except Exception:
        pass


def read_result():
    import json
    try:
        with open(_result_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _elevated_inject(pid, timeout=75):
    """自己不是管理员：拉起一个提权副本 `--risk-inject <pid>`，等它回话。

    走 ShellExecuteW("runas") —— 会弹一次 UAC。用户点了「否」就直接返回失败，
    不重试、不刷日志，让界面去显示「以管理员重启工具箱」的按钮。
    """
    from . import privilege
    if privilege.is_admin():
        return False, "已经是管理员仍然拿不到句柄（战斗端可能以更高完整性/系统账户运行）"
    if pid in _elevated:
        return False, "pid=%d 已经请求过提权，不重复弹 UAC" % pid
    _elevated.add(pid)
    ok, msg = privilege.relaunch_admin(["--risk-inject", str(pid)])
    log("[inject] 提权：%s" % msg)
    if not ok:
        return False, msg
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = read_result()
        if r and int(r.get("pid") or 0) == int(pid) and float(r.get("ts") or 0) >= t0 - 1:
            log("[inject] 提权副本回报：%s" % ("成功" if r.get("ok") else "失败"))
            return bool(r.get("ok")), str(r.get("detail") or "")
        time.sleep(0.5)
    return False, "提权副本 %d 秒内没有回话（UAC 可能还在等确认）" % timeout


def _watch():
    target = config.get("inject.targets")[0] or injector.DEFAULT_TARGET
    from . import proxy as _proxy
    mode = str(config.get("inject.method") or "proxy").lower()
    if mode == "proxy":
        log("注入守护启动（代理模式），目标 %s：载荷由 battle\\*_64\\version.dll 在战斗端启动时自加载" % target)
    else:
        log("注入守护启动（远程线程模式），目标 %s" % target)
    waiting = {}          # pid -> 首次见到的时间
    mono_ready = {}       # pid -> Mono 首次就绪时间
    seen = set()          # 只记录一次发现过程，避免每轮重复落盘
    fail_counts = {}      # pid -> 连续失败次数（避免受保护子进程无限重试刷日志）
    while not _stop.is_set():
        try:
            if not config.get("inject.enabled"):
                time.sleep(POLL_INTERVAL)
                continue
            ps = injector.list_processes(target)
            if mode == "proxy":
                # 代理模式：什么都不用做，载荷随 version.dll 在进程启动时进来。
                # 这里只负责记录「这一局已经由代理接管」，让界面显示已生效。
                for p in ps:
                    pid = p["pid"]
                    if pid not in _done_pids:
                        _done_pids.add(pid)
                        log("[proxy] pid=%d 战斗端已启动，载荷由 version.dll 代理加载（不做跨进程注入）" % pid)
                _done_pids.intersection_update({p["pid"] for p in ps} or set())
                # 没有战斗端在跑时把代理文件补回去：代理线程启动时会把自身改名成
                # .ver.dat 躲开目录扫描，不补回来下一局就不会加载了。
                # 代理每局会把自身改名藏起来，所以只要目录里 version.dll 不全就补。
                # 不再等“一个战斗端都没有”——僵尸进程会让 ps 永远非空，第二局就失效。
                try:
                    dirs = _proxy.battle_dirs()
                    if dirs and len(_proxy.deployed()) < len(dirs):
                        ok, msg = _proxy.heal()
                        if ok and "补回 0 个" not in msg and "补回" in msg:
                            log("[proxy] " + msg)
                except Exception as e:
                    log("[proxy] 补回代理失败: %r" % (e,))
                _stop.wait(POLL_INTERVAL)
                continue
            live = set()
            for p in ps:
                pid = p["pid"]
                live.add(pid)
                watch_attempt = "watch-%d-%d" % (pid, os.getpid())
                if pid not in seen:
                    seen.add(pid)
                    injector.trace_event(pid, watch_attempt, "watch.process_seen",
                                         name=target)
                if pid in _done_pids:
                    continue
                mono = None
                try:
                    mono = injector.find_mono(pid)
                except Exception:
                    pass
                if not mono:
                    mono_ready.pop(pid, None)
                    first = waiting.setdefault(pid, time.time())
                    if time.time() - first > WAIT_MONO:
                        waiting.pop(pid, None)
                        log("[inject] pid=%d 等了 %ds 还没加载 mono，放弃这轮" % (pid, WAIT_MONO))
                    continue
                waiting.pop(pid, None)
                if pid not in mono_ready:
                    injector.trace_event(pid, watch_attempt, "watch.mono_ready",
                                         name=mono["name"], base=hex(mono["base"]),
                                         path=mono["path"])
                ready = mono_ready.setdefault(pid, time.monotonic())
                try:
                    settle = min(15.0, max(0.0, float(config.get("inject.settle_seconds", 0.8))))
                except (TypeError, ValueError):
                    settle = 0.8
                remaining = settle - (time.monotonic() - ready)
                if remaining > 0:
                    log("[inject] pid=%d Mono 已就绪，等待 %.1fs 后再注入" % (pid, remaining))
                    if _stop.wait(remaining):
                        break
                    if not config.get("inject.enabled"):
                        continue
                    try:
                        if not injector.find_mono(pid):
                            mono_ready.pop(pid, None)
                            continue
                    except Exception:
                        mono_ready.pop(pid, None)
                        continue
                mono_ready.pop(pid, None)
                injector.trace_event(pid, watch_attempt, "watch.inject_due",
                                     settle_seconds=settle)
                log("[inject] pid=%d 已加载 %s，开始注入" % (pid, mono["name"]))
                ok, detail = inject_now(pid, target,
                                        allow_elevate=bool(config.get("inject.auto_elevate")))
                if not ok:
                    if pid in _done_pids:
                        log("[inject] pid=%d 已尝试注入但未确认运行，本局不再重复注入" % pid)
                        continue
                    if "拒绝访问" in (detail or "") or "权限" in (detail or ""):
                        # 权限问题重试没有意义：记一次、把 pid 标成已处理，
                        # 让界面去显示「以管理员身份重启工具箱」。
                        log("[inject] pid=%d 权限不足，本轮放弃（界面可一键提权）" % pid)
                        _done_pids.add(pid)
                        continue
                    cnt = fail_counts.get(pid, 0) + 1
                    fail_counts[pid] = cnt
                    if cnt >= 2:
                        log("[inject] pid=%d 连续 %d 次未成功，跳过该进程不再重复尝试" % (pid, cnt))
                        _done_pids.add(pid)
                        continue
                    log("[inject] pid=%d 注入未成功，10 秒后重试（第 %d/2 次）" % (pid, cnt))
                    _done_pids.discard(pid)
                    time.sleep(10)
                else:
                    fail_counts.pop(pid, None)
            for pid in list(waiting):
                if pid not in live:
                    waiting.pop(pid, None)
            for pid in list(mono_ready):
                if pid not in live:
                    mono_ready.pop(pid, None)
                    log("[inject] pid=%d 等待期间已退出，未执行注入" % pid)
            for pid in list(fail_counts):
                if pid not in live:
                    fail_counts.pop(pid, None)
            seen.intersection_update(live)
            # 进程退出就清掉记录，否则 pid 被系统复用后会误判为"已注入"
            _done_pids.intersection_update(live)
            for pid in list(_claimed):
                if pid not in live:
                    _release(pid)
        except Exception as e:
            log("[inject] 守护异常: %r" % (e,))
        _stop.wait(POLL_INTERVAL)
    log("注入守护停止")


def _ensure_thread():
    global _thread
    if _thread and _thread.is_alive():
        return _thread
    _stop.clear()
    _thread = threading.Thread(target=_watch, name="ssjj-risk-inject", daemon=True)
    _thread.start()
    return _thread


def _stop_thread():
    _stop.set()
    if _thread:
        _thread.join(timeout=3)


# ------------------------------------------------ 只开注入（不依赖总开关）
def inject_state():
    """注入守护自身状态，跟总开关 enabled 无关。"""
    from . import privilege
    target = config.get("inject.targets")[0] or injector.DEFAULT_TARGET
    try:
        pids = [p["pid"] for p in injector.list_processes(target)]
    except Exception:
        pids = []
    try:
        from . import proxy as _proxy
        px = _proxy.status()
        px_info = {"installed": px.get("installed_count", 0),
                   "dirs": px.get("installed") or [],
                   "source": px.get("source"),
                   "source_size": px.get("source_size", 0),
                   "log": px.get("log")}
    except Exception as e:
        px_info = {"installed": 0, "dirs": [], "source": None, "error": repr(e)}
    return {
        "enabled": bool(config.get("inject.enabled")),
        "auto": bool(config.get("inject.auto")),
        "method": str(config.get("inject.method") or "proxy"),
        "thread_alive": bool(_thread and _thread.is_alive()),
        "target": target,
        "processes": pids,
        "injected": sorted(_done_pids),
        "admin": privilege.is_admin(),
        "debug_privilege": privilege.enable_debug_privilege()[0],
        "auto_elevate": bool(config.get("inject.auto_elevate")),
        "cold_swap": bool(config.get("inject.cold_swap")),
        "ace_mode": config.get("inject.ace_mode"),
        "proxy": px_info,
    }


def inject_start(elevate=False):
    """只启动注入守护，不碰总开关、不碰 MITM、不碰大厅。

    代理模式下会顺手确认 version.dll 已部署（没部署就当场装一次），
    因为那条路的载荷不靠守护、靠游戏加载器。

    返回 (ok, 说明)。非管理员时守护照样起，只是注入那一刻会走
    「权限不足 → 界面一键提权」这条路。
    """
    from . import privilege
    config.set_("inject.enabled", True)
    mode = str(config.get("inject.method") or "proxy").lower()
    note = ""
    if mode == "proxy":
        try:
            from . import proxy as _proxy
            # 上一局结束时代理会把自己改名成 .ver.dat 躲自检，所以每次开守护都先补回
            _proxy.heal(emit=log)
            st = _proxy.status()
            if not st.get("installed_count"):
                ok, msg = _proxy.install(emit=log)
                note = "；" + msg
            else:
                note = "；代理已部署在 %d 个战斗端目录" % st.get("installed_count", 0)
        except Exception as e:
            note = "；代理部署失败：%r" % (e,)
    if not _thread or not _thread.is_alive():
        _ensure_thread()
        log("注入守护已单独启动（总开关未动）")
    if elevate and not privilege.is_admin():
        ok, msg = privilege.relaunch_admin([])
        return ok, msg
    if mode == "proxy":
        return True, "代理模式已就绪" + note
    if not privilege.is_admin():
        return True, "守护已启动；当前不是管理员，注入时需要在界面上点一次「提权」"
    return True, "守护已启动（管理员）"


def inject_stop():
    config.set_("inject.enabled", False)
    _stop_thread()
    log("注入守护已停止（总开关未动）")
    return True


def elevate(reinject=False):
    """以管理员身份重启工具箱；reinject=True 时顺便把当前在跑的战斗端注入一次。"""
    from . import privilege
    if privilege.is_admin():
        return True, "已经是管理员"
    args = []
    pid = injector.find_pid(config.get("inject.targets")[0] or injector.DEFAULT_TARGET)
    if reinject and pid:
        args = ["--risk-inject", str(pid)]
    ok, msg = privilege.relaunch_admin(args, wait=False)
    log("[elevate] %s" % msg)
    return ok, msg


# ------------------------------------------------------------------ 总控
def lobby_state():
    try:
        from . import lobby
        return lobby.status()
    except Exception as e:
        return {"applied": False, "error": repr(e)}


def _live_stats():
    """从正在跑的服务进程取统计。

    不能用 import local_cdn 然后读 STATS：服务端是以 __main__ 跑的，
    import 进来的是另一个模块实例，计数器永远是 0。走 /__stats__ 才是真数据。
    """
    import http.client as hc
    import json
    c = hc.HTTPConnection("127.0.0.1", 80, timeout=3)
    try:
        c.request("GET", "/__stats__")
        return json.loads(c.getresponse().read().decode("utf-8"))
    finally:
        c.close()


def localized_state():
    """本地化覆盖率（复用普通模式的缓存统计）"""
    try:
        st = _live_stats()
        hit, miss = st.get("hit", 0), st.get("miss", 0)
        total = hit + miss
        return {"hit": hit, "miss": miss, "https_mitm": st.get("https_mitm", 0),
                "rate": (100.0 * hit / total) if total else 0.0}
    except Exception as e:
        return {"hit": 0, "miss": 0, "https_mitm": 0, "rate": 0.0, "error": repr(e)}


def https_state():
    """https 侧（globalResurl / 活动 / 广告位）台账与覆盖率。"""
    out = {"seen": 0, "cached": 0, "bytes": 0, "total_bytes": 0}
    try:
        from . import mitm
        import local_cdn as L
        data = mitm.seen()
        out["seen"] = len(data)
        for path, info in data.items():
            out["total_bytes"] += int(info.get("size") or 0)
            dst = L.path_to_file(path)
            if dst and os.path.isfile(dst) and os.path.getsize(dst) > 0:
                out["cached"] += 1
                out["bytes"] += os.path.getsize(dst)
    except Exception as e:
        out["error"] = repr(e)
    return out



def prewarm_https():
    """把 https 台账里还没落本地的路径回源填满缓存。"""
    try:
        from . import mitm
        r = mitm.prewarm()
        log("[mitm] 预热 新填 %d / 已存在 %d / 失败 %d / 台账 %d"
            % (r["ok"], r["skip"], r["fail"], r["total"]))
        return r
    except Exception as e:
        log("[mitm] 预热失败 %r" % (e,))
        return {"ok": 0, "skip": 0, "fail": 0, "total": 0, "error": repr(e)}


def status():
    cfg = config.load()
    ps = []
    try:
        targets = config.get("inject.targets") or [injector.DEFAULT_TARGET]
        ps = injector.list_processes(targets[0] or injector.DEFAULT_TARGET)
    except Exception:
        pass
    return {
        "enabled": bool(cfg.get("enabled")),
        "thread_alive": bool(_thread and _thread.is_alive()),
        "processes": ps,
        "injected_pids": sorted(_done_pids),
        "lobby": lobby_state(),
        "localized": localized_state(),
        "https": https_state(),
        "last": last_event(),
        "payload": {"path": injector.payload_path(),
                    "built": injector.payload_path() is not None,
                    "size": (os.path.getsize(injector.payload_path())
                             if injector.payload_path() else 0)},
        "config": cfg,
    }


def enable(easing=None, fps=None, use_descriptor_fps=None):
    """开风险模式。返回 (ok, 消息列表)"""
    msgs = []
    cfg = config.load()
    cfg["enabled"] = True
    config.save(cfg)
    msgs.append(log("风险模式开启"))

    # https 一级接管只在配置里已开启时才做（默认关闭，避免悄悄装证书、磁盘被缓存占满）
    mc = cfg.get("mitm") or {}
    if mc.get("enabled"):
        try:
            from . import mitm
            st = mitm.status()
            if not st["ca_exists"]:
                mitm.ensure_ca()
                mitm.ensure_leaf()
            if not st["ca_installed"]:
                okc, lines2 = mitm.install_ca()
                msgs.append(log("[mitm] 装根证书: %s" % ("成功" if okc else lines2[-1:])))
            msgs.append(log("[mitm] https 拦截 %s" %
                            ("就绪" if mitm.enabled() else "未就绪（根证书没进 Root 存储）")))
        except Exception as e:
            msgs.append(log("[mitm] https 接管失败: %r" % (e,)))
    else:
        msgs.append(log("[mitm] 本地加速未启用（默认关闭），跳过证书与 https 接管"))


    ic = cfg.get("inject") or {}
    if ic.get("enabled"):
        try:
            lines, _ = injector.build_payload()
            msgs += [log("[inject] " + l.split("[*] ")[-1]) for l in lines]
        except Exception as e:
            msgs.append(log("[inject] 载荷编译失败: %r" % (e,)))
        if ic.get("auto", True):
            _ensure_thread()
            msgs.append(log("注入守护已启动"))
        else:
            ok, detail = inject_now()
            msgs.append(log("[inject] 手动注入 %s" % ("成功" if ok else "未成功")))
    return True, msgs


def disable():
    msgs = [log("风险模式关闭")]
    _stop_thread()
    cfg = config.load()
    cfg["enabled"] = False
    config.save(cfg)
    # 大厅动效不归这里管：它在「大厅帧率」页，总开关只负责换肤与检测降级
    _done_pids.clear()
    return True, msgs


def main(argv=None):
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "status"
    if cmd == "status":
        st = status()
        print("风险模式: %s" % ("开" if st["enabled"] else "关"))
        print("注入守护: %s" % ("运行中" if st["thread_alive"] else "停止"))
        print("战斗端进程: %s" % (st["processes"] or "未运行"))
        print("已注入 pid: %s" % (st["injected_pids"] or "无"))
        print("大厅变体: %s" % st["lobby"])
        print("本地命中率: %.1f%% (%d/%d)" % (st["localized"]["rate"],
                                             st["localized"]["hit"],
                                             st["localized"]["hit"] + st["localized"]["miss"]))
        print("载荷: %s" % st["payload"])
        hs = st["https"]
        print("https 台账: %d 条，已本地 %d 条（%.1f MB）"
              % (hs["seen"], hs["cached"], hs["bytes"] / 1048576.0))
        for l in log_tail(15):
            print("  " + l)
        return
    if cmd == "on":
        ok, msgs = enable()
        print("\n".join(msgs))
        return
    if cmd == "off":
        ok, msgs = disable()
        print("\n".join(msgs))
        return
    if cmd == "inject":
        ok, detail = inject_now()
        print(detail)
        return
    print("用法: python -m risk.guard [status|on|off|inject]")


if __name__ == "__main__":
    main()
