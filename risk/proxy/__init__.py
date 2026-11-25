# -*- coding: utf-8 -*-
"""version.dll 代理模式：把换肤载荷从「跨进程注入」改成「进程内加载」。

背景（2026-11-24 夜）：
    注入链路 `OpenProcess -> VirtualAllocEx -> WriteProcessMemory ->
    CreateRemoteThread` 被 GameGuard 当场封死。injection_trace.jsonl 记到
    `remote_thread.created` 与 `npggNT64.des+0x22a04f 抛 0x80000004
    (STATUS_SINGLE_STEP)` 在同一毫秒 —— 反作弊 hook 了线程创建，只要新线程
    的起始地址不在任何模块镜像内（匿名 RX 页）就直接杀进程，载荷一行都没跑。

本模式：
    `SSJJ_BattleClient_Unity.exe` 只导入 KERNEL32 + UnityPlayer.dll，而
    `UnityPlayer.dll` 的导入表里有 VERSION.dll，且 `version.dll` 不在
    KnownDLLs 里 —— 因此放在 `battle\\*_64\\version.dll` 的同名代理会被
    加载器优先加载。我们的代码从 DllMain 起就在进程内、跑在真实模块里：
      * 不碰 OpenProcess / WriteProcessMemory / CreateRemoteThread
      * 不产生匿名可执行页
      * 载荷由代理自己 load 进 mono（mono_image_open_from_data_with_name）
    对反作弊来说，这就是一个正常加载的模块。

代价：
    * 需要在游戏目录写一个 version.dll（管理员）
    * 换肤逻辑仍然在游戏进程内跑（还是「客户端改动」，不是资源层替换）
    * 代理只在战斗端启动时生效，装了之后新开一局即生效，不需要工具箱常开

安装/卸载：
    python -m risk proxy install / uninstall / status
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import shutil
import sys
import time

NAME = "version.dll"

try:
    from . import paths as _paths
except Exception:                                     # pragma: no cover
    _paths = None


def _state_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    p = os.path.join(base, "ssjj-toolbox")
    try:
        os.makedirs(p, exist_ok=True)
    except Exception:
        pass
    return p


STATE = os.path.join(_state_dir(), "proxy_state.json")
LOG = os.path.join(_state_dir(), "proxy.log")


def _pkg_dir():
    return os.path.dirname(os.path.abspath(__file__))


def dll_source():
    """代理 DLL 的源文件（源码模式在 risk/proxy/，打包版在 _MEIPASS 里）。"""
    cands = []
    base = getattr(sys, "_MEIPASS", None)
    if base:
        cands.append(os.path.join(base, "risk", "proxy", NAME))
    cands.append(os.path.join(_pkg_dir(), NAME))
    for c in cands:
        if os.path.isfile(c):
            return c
    return None


def battle_dirs():
    """所有含 SSJJ_BattleClient_Unity.exe 的 battle 子目录。"""
    try:
        import ssjj_paths as P
        game = P.GAME_DIR
    except Exception:
        game = r"C:\Program Files (x86)\Wooduan\SSJJ-4399"
    base = os.path.join(game, "battle")
    out = []
    if os.path.isdir(base):
        for n in sorted(os.listdir(base)):
            d = os.path.join(base, n)
            if os.path.isfile(os.path.join(d, "SSJJ_BattleClient_Unity.exe")):
                out.append(d)
    return out


def _md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def our_md5():
    src = dll_source()
    return _md5(src) if src else ""


def deployed():
    """已经放了我们代理的 battle 目录。"""
    want = our_md5()
    out = []
    for d in battle_dirs():
        f = os.path.join(d, NAME)
        if os.path.isfile(f):
            try:
                if want and _md5(f) == want:
                    out.append(d)
            except Exception:
                pass
    return out


def any_version_dll():
    """battle 目录里存在 version.dll 的（不论是不是我们的）。"""
    return [d for d in battle_dirs() if os.path.isfile(os.path.join(d, NAME))]


def _load_state():
    try:
        with open(STATE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(d):
    try:
        tmp = STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE)
    except Exception:
        pass


RENAMED = ".ver.dat"


def _alts(d):
    """目录里代理可能藏成的名字：.ver.dat / .ver1.dat … .ver9.dat"""
    out = []
    alt = os.path.join(d, RENAMED)
    if os.path.isfile(alt):
        out.append(alt)
    for i in range(1, 10):
        p = os.path.join(d, ".ver%d.dat" % i)
        if os.path.isfile(p):
            out.append(p)
    return out


def heal(emit=None):
    """把被代理自己改名藏起来的 version.dll 补回来。

    代理定时器一启动就会把自身文件改名（.ver.dat，占不到就 .ver1.dat …），躲开
    RuntimeCheatGuard 的「exe 旁有可疑 DLL」目录扫描；所以跑过一局之后目录里没有
    version.dll 了。工具箱闲置时调用这里恢复，否则下一局代理不会被加载。返回 (ok, msg)。"""
    src = dll_source()
    fixed, kept, stale, locked = 0, 0, 0, 0
    src_md5 = ""
    try:
        src_md5 = _md5(src) if src else ""
    except Exception:
        src_md5 = ""
    for d in battle_dirs():
        cur = os.path.join(d, NAME)
        alts = _alts(d)
        if os.path.isfile(cur):
            fresh = True
            try:
                fresh = (not src_md5) or _md5(cur) == src_md5
            except Exception:
                fresh = True
            if not fresh and src:        # 目录里是旧版本代理：换成本次的
                try:
                    shutil.copy2(src, cur)
                    fixed += 1
                except Exception as e:
                    locked += 1
                    if emit:
                        emit("代理文件被占用，本局先用目录里已有的版本（重启电脑后自动更新）：%s（%s）" % (d, e))
            else:
                kept += 1
            for a in alts:          # 有 version.dll 时把藏起来的副本清掉
                try:
                    os.remove(a)
                    stale += 1
                except Exception:
                    pass
            continue
        moved = False
        for a in alts:
            try:
                if src_md5 and _md5(a) != src_md5:   # 旧版本残影，搬回来也没用
                    os.remove(a)
                    stale += 1
                    continue
                os.replace(a, cur)
                fixed += 1
                moved = True
                break
            except Exception:
                continue
        if not moved and src:
            try:
                shutil.copy2(src, cur)
                fixed += 1
            except Exception as e:
                if emit:
                    emit("补回代理失败 %s（%s）" % (d, e))
    msg = "代理文件：正常 %d 个、补回 %d 个" % (kept, fixed)
    if stale:
        msg += "、清掉旧残留 %d 个" % stale
    if locked:
        msg += "、%d 个被占用（重启后更新）" % locked
    if emit and fixed:
        emit(msg)
    return (fixed + kept) > 0, msg


def install(dirs=None, emit=None, all_dirs=True):
    """把代理复制进 battle 目录。返回 (ok, msg)。"""
    src = dll_source()
    if not src:
        return False, "找不到代理 DLL（risk\\proxy\\version.dll），先跑 risk\\proxy\\build_proxy.py"

    targets = dirs or battle_dirs()
    if not targets:
        return False, "没找到 battle 目录（游戏没装？）"
    if not all_dirs and len(targets) > 1:
        # 只装正在跑的那一份；找不到就装第一个
        try:
            import toolbox.backend as _be
            live = _be.running_battle_dir()
        except Exception:
            live = None
        targets = [live] if live in targets else [targets[0]]

    state = _load_state()
    state.setdefault("installed", {})
    done, failed = [], []
    for d in targets:
        dst = os.path.join(d, NAME)
        try:
            if os.path.isfile(dst):
                if _md5(dst) == _md5(src):
                    state["installed"][d] = {"ts": time.time(), "md5": _md5(src), "skip": True}
                    done.append(d)
                    continue
                # 清掉历史备份：exe 旁边堆一堆 version.dll.bak-* 会被游戏自检当成
                # 「exe 旁有可疑 DLL」，所以每次部署前先扫干净
                for old in glob.glob(dst + ".bak-*"):
                    try:
                        os.remove(old)
                    except OSError:
                        pass
                bak = dst + time.strftime(".bak-%Y%m%d-%H%M%S")
                shutil.move(dst, bak)
                if emit:
                    emit("已有 version.dll，已备份为 %s" % os.path.basename(bak))
            shutil.copy2(src, dst)
            state["installed"][d] = {"ts": time.time(), "md5": _md5(src)}
            done.append(d)
        except PermissionError:
            failed.append("%s（被占用，先关游戏）" % d)
        except Exception as e:
            failed.append("%s（%s）" % (d, e))

    state["ts"] = time.time()
    state["md5"] = _md5(src)
    _save_state(state)

    if emit:
        for d in done:
            emit("已部署代理 DLL: %s" % d)
        for f in failed:
            emit("部署失败: %s" % f)
    if failed and not done:
        return False, "；".join(failed)
    msg = "已部署到 %d 个战斗端目录（新开一局生效，不需要工具箱常开）" % len(done)
    if failed:
        msg += "；失败 %d 个：%s" % (len(failed), "；".join(failed))
    return True, msg


def uninstall(emit=None):
    """删掉我们放的代理（保留用户原有的 version.dll 备份不还原，只是提示）。"""
    state = _load_state()
    want = our_md5()
    removed, kept = [], []
    for d in any_version_dll():
        f = os.path.join(d, NAME)
        try:
            mine = bool(want) and _md5(f) == want
            if mine:
                os.remove(f)
                removed.append(d)
            else:
                kept.append(d)
        except Exception:
            kept.append(d)
    state["installed"] = {}
    state["ts"] = time.time()
    _save_state(state)
    if emit:
        for d in removed:
            emit("已移除代理 DLL: %s" % d)
        for d in kept:
            emit("保留（不是我们的文件）: %s" % d)
    return True, "已移除 %d 个目录的代理 DLL" % len(removed)


def status():
    src = dll_source()
    ds = deployed()
    payload = None
    try:
        from . import paths as _p
        f = os.path.join(_p.PAYLOAD_DIR, "System.Configuration.Install.dll")
        if os.path.isfile(f):
            payload = {"path": f, "size": os.path.getsize(f)}
    except Exception:
        pass
    st = _load_state()
    return {
        "source": src,
        "source_size": (os.path.getsize(src) if src else 0),
        "md5": our_md5() if src else "",
        "installed": ds,
        "installed_count": len(ds),
        "battle_dirs": battle_dirs(),
        "any_version_dll": any_version_dll(),
        "payload": payload,
        "built_at": st.get("built_at"),
        "log": LOG,
    }


def tail(n=20):
    try:
        with open(LOG, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
        return lines[-n:]
    except Exception:
        return []


def build(emit=None):
    """重新编译代理（把当前载荷嵌进去）。"""
    import subprocess
    script = os.path.join(_pkg_dir(), "build_proxy.py")
    if not os.path.isfile(script):
        return False, "找不到 build_proxy.py（打包版不能现场编译，用发行版里的 DLL）"
    r = subprocess.run([sys.executable, script], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "编译失败").strip()[-800:]
    return True, (r.stdout or "编译完成").strip().splitlines()[-1]


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    act = argv[0] if argv else "status"
    if act == "install":
        ok, msg = install(emit=print)
    elif act == "uninstall":
        ok, msg = uninstall(emit=print)
    elif act == "build":
        ok, msg = build(emit=print)
    else:
        ok, msg = True, json.dumps(status(), ensure_ascii=False, indent=2)
    print(("[OK] " if ok else "[FAIL] ") + str(msg))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
