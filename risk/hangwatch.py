# -*- coding: utf-8 -*-
"""MicroClient 返回大厅卡死现场看门狗。

只读监控，不注入、不改游戏：
  每 0.5s 采样一次进程；
  发现 MicroClient / 大厅 / 战斗端的顶层窗口 IsHungAppWindow 时，写时间线到 log；
  默认**不抓 dump**（见下），需要现场时加 --dumps。

为什么不默认抓 dump：写 minidump 要给目标进程挂一个线程并挂起它全部线程。
战斗端带 nProtect GameGuard（npggNT64），被外部挂起/遍历内存极易触发它自己的
0xc0000005（事件日志里 `npggNT64.des_unloaded` 那个崩溃就是这一类），
而 GameGuard 也可能把转储行为当成调试器。所以：
  * 默认只记日志（零侵入，卡死时间线已经足够定位）；
  * --dumps 才抓，且默认只抓 MicroClient / 大厅；
  * 战斗端另需 --dump-battle 显式打开。

用法:
  python risk/hangwatch.py                 # 只记日志（推荐）
  python risk/hangwatch.py --dumps         # 卡死时抓 MicroClient / 大厅
  python risk/hangwatch.py --dumps --dump-battle
  python risk/hangwatch.py --once-test     # 采样一次就退出，验证监控能跑
"""
import os
import subprocess
import sys
import time
import ctypes
import ctypes.wintypes as wt

import psutil

LOG_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                       "ssjj-toolbox")
DUMP_DIR = os.path.join(LOG_DIR, "hangdumps")
LOG_PATH = os.path.join(LOG_DIR, "hangwatch.log")
POLL = 0.5
HUNG_BEFORE_DUMP = 12.0
MAX_DUMPS = 10
DUMP_ENABLED = False     # 默认不抓 dump
DUMP_BATTLE = False      # 战斗端带 GameGuard，默认不碰

user32 = ctypes.windll.user32
EnumWindows = user32.EnumWindows
GetWindowThreadProcessId = user32.GetWindowThreadProcessId
IsHungAppWindow = user32.IsHungAppWindow
IsWindowVisible = user32.IsWindowVisible

WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def log(text):
    line = "%s  %s" % (time.strftime("%m-%d %H:%M:%S"), text)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line, flush=True)


def hung_windows():
    """返回 {pid: [窗口标题, ...]}，只含被判定为无响应的可见顶层窗口。"""
    out = {}

    def cb(hwnd, _):
        try:
            if not IsWindowVisible(hwnd):
                return True
            if not IsHungAppWindow(hwnd):
                return True
            pid = wt.DWORD()
            GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            n = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            out.setdefault(pid.value, []).append(buf.value)
        except Exception:
            pass
        return True

    EnumWindows(WNDENUMPROC(cb), 0)
    return out


def snapshot():
    """{exe: [pid,...]}"""
    keep = ("MicroClient.exe", "SSJJ_BattleClient_Unity.exe",
            "AirLobbyPreloader.exe", "UnityCrashHandler64.exe")
    out = {k: [] for k in keep}
    for p in psutil.process_iter(["pid", "name"]):
        try:
            if p.info["name"] in out:
                out[p.info["name"]].append(p.info["pid"])
        except Exception:
            pass
    for k in out:
        out[k].sort()
    return out


def dump(pid, tag):
    os.makedirs(DUMP_DIR, exist_ok=True)
    path = os.path.join(DUMP_DIR, "%s_%d_%s.dmp" % (tag, pid, time.strftime("%H%M%S")))
    try:
        subprocess.run(
            # "0" = MiniDumpNormal：只有线程/模块/栈。之前的 "full" 有 0.5~2GB，
            # WER 在卡死 8 秒后就杀进程，文件被截断根本解析不了。
            ["rundll32.exe", "C:\\Windows\\System32\\comsvcs.dll,MiniDump",
             str(pid), path, "0"],
            timeout=90, creationflags=0x08000000)
        ok = os.path.isfile(path) and os.path.getsize(path) > 1024
        log("  dump %s pid=%d -> %s (%s)" % (tag, pid, path,
            "%.0fMB" % (os.path.getsize(path) / 1048576.0) if ok else "失败"))
        return ok
    except Exception as e:
        log("  dump %s pid=%d 异常 %r" % (tag, pid, e))
        return False


def main():
    global DUMP_ENABLED, DUMP_BATTLE
    argv = [a.lower() for a in sys.argv[1:]]
    DUMP_ENABLED = any(a in ("--dumps", "-d") for a in argv)
    DUMP_BATTLE = "--dump-battle" in argv
    once = "--once-test" in argv

    os.makedirs(LOG_DIR, exist_ok=True)
    log("=" * 60)
    log("看门狗启动 pid=%d  dump=%s  dump_battle=%s"
        % (os.getpid(), DUMP_ENABLED, DUMP_BATTLE))
    prev = snapshot()
    log("初始: " + ", ".join("%s=%s" % (k, v or "-") for k, v in prev.items()))
    if once:
        hung = hung_windows()
        log("当前无响应窗口: %s" % (hung or "无"))
        return
    hang_since = None
    dumped_pids = set()
    dumped = 0

    while True:
        time.sleep(POLL)
        now = snapshot()
        for exe in ("SSJJ_BattleClient_Unity.exe", "MicroClient.exe", "AirLobbyPreloader.exe"):
            added = set(now[exe]) - set(prev[exe])
            removed = set(prev[exe]) - set(now[exe])
            if added:
                log("%s 启动 %s" % (exe, sorted(added)))
            if removed:
                log("%s 退出 %s" % (exe, sorted(removed)))
        prev = now

        hung = hung_windows()
        battle_pids = set(now["SSJJ_BattleClient_Unity.exe"])
        micro_pids = set(now["MicroClient.exe"])
        lobby_pids = set(now["AirLobbyPreloader.exe"])
        hung_battle = {p: t for p, t in hung.items() if p in battle_pids}
        hung_micro = {p: t for p, t in hung.items() if p in micro_pids}
        hung_lobby = {p: t for p, t in hung.items() if p in lobby_pids}

        if hung_battle or hung_micro or hung_lobby:
            if hang_since is None:
                hang_since = time.time()
                for tag, d in (("Battle", hung_battle), ("MicroClient", hung_micro),
                               ("Lobby", hung_lobby)):
                    if d:
                        log("[!] %s 无响应 pid=%s 标题=%s" %
                            (tag, sorted(d), sum(d.values(), [])))
            elif (DUMP_ENABLED and time.time() - hang_since >= HUNG_BEFORE_DUMP
                  and dumped < MAX_DUMPS):
                log("[!] 卡死持续 %.1fs，抓现场。hung: battle=%s micro=%s lobby=%s" %
                    (time.time() - hang_since, sorted(hung_battle) or "-",
                     sorted(hung_micro) or "-", sorted(hung_lobby) or "-"))
                # 只采集真正无响应的窗口所属进程；CEF 渲染进程没有顶层窗口，
                # 抓它们无法解释主窗口卡在何处，还会拖慢现场采集。
                order = [("MicroClient", sorted(hung_micro)),
                         ("Lobby", sorted(hung_lobby))]
                if DUMP_BATTLE:      # 战斗端带 GameGuard，默认不碰（见文件头说明）
                    order.insert(0, ("Battle", sorted(hung_battle)))
                for tag, pids in order:
                    for pid in pids:
                        if pid in dumped_pids:
                            continue
                        if dump(pid, tag):
                            dumped_pids.add(pid)
                            dumped += 1
                log("--- 现场抓取结束 ---")
        else:
            if hang_since is not None:
                log("全部恢复响应（或已退出），卡死持续 %.1fs" % (time.time() - hang_since))
                hang_since = None
                dumped_pids.clear()


if __name__ == "__main__":
    main()
