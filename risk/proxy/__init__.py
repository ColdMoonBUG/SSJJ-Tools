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
import threading
import time
from pathlib import Path

NAME = "hid.dll"
RENAMED = ".hid.dat"
LEGACY_NAME = "version.dll"
LEGACY_RENAMED = ".ver.dat"
_STATE_LOCK = threading.RLock()

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


def any_hid_dll():
    """battle 目录里存在 hid.dll 的（不论是不是我们的）。"""
    return [d for d in battle_dirs() if os.path.isfile(os.path.join(d, NAME))]


def any_version_dll():
    """battle 目录里存在旧版 version.dll 残留的（不论是不是我们的）。"""
    return [d for d in battle_dirs() if os.path.isfile(os.path.join(d, LEGACY_NAME))]


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


def _norm_dir(p):
    return os.path.normcase(os.path.abspath(os.fspath(p)))


def _alt_candidates(d):
    """目录里代理可能藏成的全部候选路径：.hid.dat / .hid1.dat … .hid9.dat"""
    d = os.fspath(d)
    return [os.path.join(d, RENAMED)] + [os.path.join(d, ".hid%d.dat" % i) for i in range(1, 10)]


def _alts(d):
    """目录里代理可能藏成的名字：.hid.dat / .hid1.dat … .hid9.dat"""
    return [p for p in _alt_candidates(d) if os.path.isfile(p)]


def _baks(d):
    """目录里历史备份 hid.dll.bak-*。"""
    return [p for p in sorted(glob.glob(os.path.join(os.fspath(d), NAME + ".bak-*"))) if os.path.isfile(p)]


def _legacy_alts(d):
    """目录里旧版 version.dll 代理可能遗留的 .ver.dat / .ver1.dat … .ver9.dat。"""
    d = os.fspath(d)
    cands = [os.path.join(d, LEGACY_RENAMED)] + [os.path.join(d, ".ver%d.dat" % i) for i in range(1, 10)]
    return [p for p in cands if os.path.isfile(p)]


def _legacy_baks(d):
    """目录里旧版历史备份 version.dll.bak-*。"""
    return [p for p in sorted(glob.glob(os.path.join(os.fspath(d), LEGACY_NAME + ".bak-*"))) if os.path.isfile(p)]


_SYS_MD5_CACHE: dict[str, str] = {}


def _system_dll_md5(dll_name=NAME):
    """系统真身 System32 DLL 的 md5（缓存）。用来区分「别人的 DLL」与「Windows 自己的」。"""
    key = str(dll_name or NAME).lower()
    if key in _SYS_MD5_CACHE:
        return _SYS_MD5_CACHE[key]
    try:
        val = _md5(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", dll_name))
    except Exception:
        val = ""
    _SYS_MD5_CACHE[key] = val
    return val


def _system_hid_md5():
    return _system_dll_md5(NAME)


def _system_version_md5():
    return _system_dll_md5(LEGACY_NAME)


def _known_hashes(state=None):
    """收集确认属于本工具的代理 DLL MD5 集合（当前内置 + proxy_state.json 历史记录）。"""
    st = _load_state() if state is None else (state or {})
    hashes = {h for h in (our_md5(), st.get("md5")) if h}
    for info in (st.get("installed") or {}).values():
        if isinstance(info, dict) and info.get("md5"):
            hashes.add(info["md5"])
    return hashes


def _try_remove(path):
    try:
        os.remove(path)
        return True, None
    except FileNotFoundError:
        return True, None
    except OSError as exc:
        return False, exc


def _clean_extras(d, remove_alts=True, remove_baks=True):
    """清理未锁定的多余 .hid*.dat 与 hid.dll.bak-*（含旧版 version.dll.bak-* / .ver*.dat），返回 (alts_removed, baks_removed)。"""
    alt_n = bak_n = 0
    if remove_alts:
        for a in _alts(d) + _legacy_alts(d):
            if os.path.isfile(a):
                ok, _ = _try_remove(a)
                if ok:
                    alt_n += 1
    if remove_baks:
        for b in _baks(d) + _legacy_baks(d):
            if os.path.isfile(b):
                ok, _ = _try_remove(b)
                if ok:
                    bak_n += 1
    return alt_n, bak_n


def _clean_legacy_version_dll(d, hashes=None, only_known=False):
    """清理非活跃目录中旧版遗留的 version.dll（系统真身不动）。返回是否移除。"""
    ver = os.path.join(os.fspath(d), LEGACY_NAME)
    if not os.path.isfile(ver):
        return False
    try:
        cur_md5 = _md5(ver)
    except OSError:
        return False
    sys_md5 = _system_version_md5()
    if cur_md5 and sys_md5 and cur_md5 == sys_md5:
        return False
    if only_known and (not hashes or cur_md5 not in hashes):
        return False
    ok, _ = _try_remove(ver)
    return ok


def has_legacy_residuals(d):
    """检查目录中是否存在旧版 version.dll / .ver*.dat / version.dll.bak-* 残留。"""
    d = os.fspath(d)
    if _legacy_alts(d) or _legacy_baks(d):
        return True
    ver = os.path.join(d, LEGACY_NAME)
    if os.path.isfile(ver):
        try:
            sys_md5 = _system_version_md5()
            if not sys_md5 or _md5(ver) != sys_md5:
                return True
        except OSError:
            return True
    return False


def record_existing_deployed(dirs=None):
    """确保目录中沿用的我方 version.dll 全部补记入 proxy_state.json。"""
    targets = list(dirs) if dirs is not None else battle_dirs()
    with _STATE_LOCK:
        state = _load_state()
        hashes = _known_hashes(state)
        want = our_md5()
        installed = state.setdefault("installed", {})
        now = time.time()
        recorded = []
        dirty = False
        for d in targets:
            cur = os.path.join(d, NAME)
            if not os.path.isfile(cur):
                continue
            try:
                cur_md5 = _md5(cur)
            except OSError:
                continue
            if cur_md5 not in hashes and not (want and cur_md5 == want):
                continue
            prev = installed.get(d)
            if not isinstance(prev, dict) or prev.get("md5") != cur_md5:
                installed[d] = {"ts": now, "md5": cur_md5, "existing": True}
                dirty = True
            recorded.append(d)
        if want and not state.get("md5") and recorded:
            state["md5"] = want
            dirty = True
        if dirty:
            state["ts"] = now
            _save_state(state)
        return recorded


def hide_in_dir(battle_dir: Path) -> tuple[bool, str]:
    """从外部协同将 battle_dir 中的我方 hid.dll 改名为 .hid.dat（冲突试 .hid1.dat~.hid9.dat）。"""
    d = os.fspath(battle_dir)
    cur = os.path.join(d, NAME)
    if not os.path.isfile(cur):
        # 幂等：目录里本来就没有 hid.dll，目标状态已达成
        return True, "目录中无 %s（无需处理）" % NAME
    try:
        cur_md5 = _md5(cur)
    except OSError as exc:
        return False, "读取 %s 失败: %s" % (NAME, exc)
    with _STATE_LOCK:
        if cur_md5 not in _known_hashes():
            # 不是我们部署的版本也照样挪走：战斗端运行期间，exe 旁边任何非系统 hid.dll
            # 都会让 RuntimeCheatGuard 报「Suspicious proxy DLL exists beside game exe」
            # 并踢出对局（别的工具箱/旧版本残留都会踩这条）。只有 Windows 自己的那份不动。
            if cur_md5 and cur_md5 == _system_hid_md5():
                return False, "这是系统自带的 %s，不动它" % NAME
            foreign = True
        else:
            foreign = False
        # 只清 .bak-*（它们同样会被 guard 判可疑），保留已有的 .hid*.dat：
        # 那是上一局代理自己藏起来的副本，删掉 heal() 就没东西可搬回来。
        _clean_extras(d, remove_alts=False, remove_baks=True)
        last_err = None
        for cand in _alt_candidates(d):
            if os.path.exists(cand):
                continue
            try:
                os.rename(cur, cand)
                return True, os.path.basename(cand) + ("（非本工具部署的残留）" if foreign else "")
            except FileNotFoundError:
                if not os.path.isfile(cur):
                    return False, "%s 已不在原位" % NAME
            except OSError as exc:
                last_err = exc
        if last_err is not None:
            return False, "改名失败: %s" % last_err
        return False, "无可用 .hid*.dat 槽位"


def cleanup_stale(remove_dll=False, skip_dirs=None):
    """启动自愈：清理未锁定的 .bak-* 与非活跃目录中的孤立 .hid*.dat / 旧版 version.dll 残留；按需清理残留我方 hid.dll。"""
    skip_set = {_norm_dir(p) for p in (skip_dirs or ()) if p}
    counts = {"bak": 0, "alt": 0, "dll": 0, "legacy": 0}
    with _STATE_LOCK:
        state = _load_state()
        hashes = _known_hashes(state)
        installed = state.get("installed")
        dirty = False
        for d in battle_dirs():
            is_active = _norm_dir(d) in skip_set
            for b in _baks(d) + _legacy_baks(d):
                if os.path.isfile(b):
                    ok, _ = _try_remove(b)
                    if ok:
                        counts["bak"] += 1
            if is_active:
                continue
            for a in _alts(d):
                if os.path.isfile(a):
                    ok, _ = _try_remove(a)
                    if ok:
                        counts["alt"] += 1
            for la in _legacy_alts(d):
                if os.path.isfile(la):
                    ok, _ = _try_remove(la)
                    if ok:
                        counts["legacy"] += 1
            if _clean_legacy_version_dll(d, hashes=hashes, only_known=False):
                counts["legacy"] += 1
            if remove_dll:
                cur = os.path.join(d, NAME)
                if os.path.isfile(cur):
                    try:
                        if _md5(cur) in hashes:
                            ok, _ = _try_remove(cur)
                            if ok:
                                counts["dll"] += 1
                                if isinstance(installed, dict) and d in installed:
                                    installed.pop(d, None)
                                    dirty = True
                    except OSError:
                        pass
                elif isinstance(installed, dict) and d in installed:
                    installed.pop(d, None)
                    dirty = True
        if dirty:
            state["ts"] = time.time()
            _save_state(state)
    return counts


_APPCOMPAT_LAYERS_SUBKEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers"


def clear_battle_appcompat_shims() -> list[tuple[str, str, str]]:
    """清理 HKCU/HKLM AppCompatFlags\\Layers 下针对 SSJJ_BattleClient_Unity.exe 的兼容层 Shim。

    Windows PCA 自动写入的 `$ IgnoreFreeLibrary<npggNT>` 等 Shim 会触发 apphelp/AcLayers
    在进程早期抢先加载 System32\\version.dll，导致同目录代理 version.dll 被完全绕过。
    返回实际删除的条目列表：[(hive_label, value_name, value_data), ...]。
    """
    try:
        import winreg
    except ImportError:
        return []
    removed: list[tuple[str, str, str]] = []
    targets = [
        ("HKCU", winreg.HKEY_CURRENT_USER, winreg.KEY_WOW64_64KEY),
        ("HKLM", winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
        ("HKCU(32)", winreg.HKEY_CURRENT_USER, winreg.KEY_WOW64_32KEY),
        ("HKLM(32)", winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
    ]
    for hive_label, hive, view in targets:
        try:
            key = winreg.OpenKey(hive, _APPCOMPAT_LAYERS_SUBKEY, 0,
                                 winreg.KEY_READ | winreg.KEY_SET_VALUE | view)
        except (FileNotFoundError, PermissionError, OSError):
            continue
        try:
            to_delete = []
            idx = 0
            while True:
                try:
                    name, val, _vtype = winreg.EnumValue(key, idx)
                except OSError:
                    break
                idx += 1
                low = str(name or "").strip().lower()
                if (low == "ssjj_battleclient_unity.exe"
                        or low.endswith("\\ssjj_battleclient_unity.exe")
                        or low.endswith("/ssjj_battleclient_unity.exe")):
                    to_delete.append((name, str(val)))
            for name, val_str in to_delete:
                try:
                    winreg.DeleteValue(key, name)
                    removed.append((hive_label, name, val_str))
                except (FileNotFoundError, PermissionError, OSError):
                    continue
        finally:
            try:
                winreg.CloseKey(key)
            except Exception:
                pass
    return removed


def heal(emit=None, skip_dirs=None):
    """把被代理自己改名藏起来的 hid.dll 补回来（严格跳过 skip_dirs 中的活跃目录），并清理旧版 version.dll 残留。

    代理定时器一启动就会把自身文件改名（.hid.dat，占不到就 .hid1.dat …），躲开
    RuntimeCheatGuard 的「exe 旁有可疑 DLL」目录扫描；所以跑过一局之后目录里没有
    hid.dll 了。战斗端彻底退出后调用这里恢复，否则下一局代理不会被加载。返回 (ok, msg)。"""
    src = dll_source()
    shims_cleared = clear_battle_appcompat_shims()
    if emit and shims_cleared:
        emit("已清理 Windows 兼容层 Shim %d 条（防止抢先加载 System32 DLL）" % len(shims_cleared))
    skip_set = {_norm_dir(p) for p in (skip_dirs or ()) if p}
    fixed, kept, stale, locked = 0, 0, 0, 0
    src_md5 = ""
    try:
        src_md5 = _md5(src) if src else ""
    except Exception:
        src_md5 = ""
    with _STATE_LOCK:
        state = _load_state()
        installed = state.setdefault("installed", {})
        dirty = False
        now = time.time()
        for d in battle_dirs():
            if _norm_dir(d) in skip_set:
                continue
            if _clean_legacy_version_dll(d, only_known=False):
                stale += 1
            cur = os.path.join(d, NAME)
            alts = _alts(d)
            if os.path.isfile(cur):
                cur_md5 = ""
                fresh = True
                try:
                    cur_md5 = _md5(cur)
                    fresh = (not src_md5) or cur_md5 == src_md5
                except Exception:
                    fresh = True
                if not fresh and src:        # 目录里是旧版本代理：换成本次的
                    try:
                        shutil.copy2(src, cur)
                        fixed += 1
                        installed[d] = {"ts": now, "md5": src_md5}
                        dirty = True
                    except Exception as e:
                        locked += 1
                        if emit:
                            emit("代理文件被占用，本局先用目录里已有的版本（重启电脑后自动更新）：%s（%s）" % (d, e))
                else:
                    kept += 1
                    rec_md5 = cur_md5 or src_md5
                    prev = installed.get(d)
                    if rec_md5 and (not isinstance(prev, dict) or prev.get("md5") != rec_md5):
                        installed[d] = {"ts": now, "md5": rec_md5, "existing": True}
                        dirty = True
                alt_n, _bak_n = _clean_extras(d, remove_alts=True, remove_baks=True)
                stale += alt_n
                continue
            moved = False
            for a in alts:
                try:
                    a_md5 = _md5(a)
                    if src_md5 and a_md5 != src_md5:   # 旧版本残影，搬回来也没用
                        os.remove(a)
                        stale += 1
                        continue
                    os.replace(a, cur)
                    fixed += 1
                    moved = True
                    installed[d] = {"ts": now, "md5": src_md5 or a_md5}
                    dirty = True
                    break
                except Exception:
                    continue
            if moved:
                alt_n, _bak_n = _clean_extras(d, remove_alts=True, remove_baks=True)
                stale += alt_n
            elif src:
                try:
                    shutil.copy2(src, cur)
                    fixed += 1
                    installed[d] = {"ts": now, "md5": src_md5}
                    dirty = True
                    alt_n, _bak_n = _clean_extras(d, remove_alts=True, remove_baks=True)
                    stale += alt_n
                except Exception as e:
                    if emit:
                        emit("补回代理失败 %s（%s）" % (d, e))
        if dirty:
            if src_md5:
                state["md5"] = src_md5
            state["ts"] = now
            _save_state(state)
    msg = "代理文件：正常 %d 个、补回 %d 个" % (kept, fixed)
    if stale:
        msg += "、清掉旧残留 %d 个" % stale
    if locked:
        msg += "、%d 个被占用（重启后更新）" % locked
    if emit and fixed:
        emit(msg)
    return (fixed + kept) > 0, msg


def install(dirs=None, emit=None, all_dirs=True, skip_dirs=None):
    """把代理复制进 battle 目录（跳过 skip_dirs 中的活跃目录，并清理旧版 version.dll 残留）。返回 (ok, msg)。"""
    src = dll_source()
    if not src:
        return False, "找不到代理 DLL（risk\\proxy\\%s），先跑 risk\\proxy\\build_proxy.py" % NAME
    shims_cleared = clear_battle_appcompat_shims()
    if emit and shims_cleared:
        emit("已清理 Windows 兼容层 Shim %d 条（防止抢先加载 System32 DLL）" % len(shims_cleared))

    targets = list(dirs) if dirs is not None else battle_dirs()
    if not targets:
        return False, "没找到 battle 目录（游戏没装？）"
    skip_set = {_norm_dir(p) for p in (skip_dirs or ()) if p}
    if skip_set:
        targets = [d for d in targets if _norm_dir(d) not in skip_set]
        if not targets:
            return True, "活跃战斗端目录正在运行中，暂跳过部署（退出后自动恢复）"
    if not all_dirs and len(targets) > 1:
        # 只装正在跑的那一份；找不到就装第一个
        try:
            import toolbox.backend as _be
            live = _be.running_battle_dir()
        except Exception:
            live = None
        targets = [live] if live in targets else [targets[0]]

    src_md5 = _md5(src)
    with _STATE_LOCK:
        state = _load_state()
        state.setdefault("installed", {})
        done, failed = [], []
        for d in targets:
            dst = os.path.join(d, NAME)
            try:
                _clean_extras(d, remove_alts=True, remove_baks=True)
                _clean_legacy_version_dll(d, only_known=False)
                if os.path.isfile(dst):
                    if _md5(dst) == src_md5:
                        state["installed"][d] = {"ts": time.time(), "md5": src_md5, "skip": True}
                        done.append(d)
                        continue
                    bak = dst + time.strftime(".bak-%Y%m%d-%H%M%S")
                    shutil.move(dst, bak)
                    if emit:
                        emit("已有 %s，已备份为 %s" % (NAME, os.path.basename(bak)))
                shutil.copy2(src, dst)
                state["installed"][d] = {"ts": time.time(), "md5": src_md5}
                done.append(d)
            except PermissionError:
                failed.append("%s（被占用，先关游戏）" % d)
            except Exception as e:
                failed.append("%s（%s）" % (d, e))

        state["ts"] = time.time()
        state["md5"] = src_md5
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
    """移除确认属于本工具的代理，包括运行时改名的 .hid*.dat、备份 hid.dll.bak-* 以及旧版 version.dll/.ver*.dat/version.dll.bak-* 残留。"""
    with _STATE_LOCK:
        state = _load_state()
        hashes = _known_hashes(state)
        removed, kept, failed = [], [], []
        for d in battle_dirs():
            for f in [os.path.join(d, NAME)] + _alts(d):
                if not os.path.isfile(f):
                    continue
                try:
                    if _md5(f) not in hashes:
                        kept.append(f)
                        continue
                    os.remove(f)
                    removed.append(f)
                except OSError as exc:
                    failed.append("%s (%s)" % (f, exc))
            legacy_ver = os.path.join(d, LEGACY_NAME)
            if os.path.isfile(legacy_ver):
                try:
                    if _md5(legacy_ver) in hashes:
                        os.remove(legacy_ver)
                        removed.append(legacy_ver)
                    else:
                        kept.append(legacy_ver)
                except OSError as exc:
                    failed.append("%s (%s)" % (legacy_ver, exc))
            for lf in _legacy_alts(d) + _baks(d) + _legacy_baks(d):
                if not os.path.isfile(lf):
                    continue
                try:
                    os.remove(lf)
                    removed.append(lf)
                except OSError as exc:
                    failed.append("%s (%s)" % (lf, exc))
        if not failed:
            state["installed"] = {}
        state["ts"] = time.time()
        _save_state(state)
    if emit:
        for f in removed:
            emit("已移除代理文件: %s" % f)
        for f in kept:
            emit("保留（无法确认属于本工具）: %s" % f)
        for f in failed:
            emit("移除失败: %s" % f)
    msg = "已移除 %d 个代理文件；保留 %d 个其他文件" % (len(removed), len(kept))
    if failed:
        msg += "；%d 个移除失败：%s" % (len(failed), "；".join(failed))
    return not failed, msg


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
        "any_hid_dll": any_hid_dll(),
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
