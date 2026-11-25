# -*- coding: utf-8 -*-
"""权限工具：让注入在「工具箱没提权」的情况下也能自己解决。

为什么单独一个模块
------------------
注入战斗端需要两件事：以管理员（高完整性）身份在跑，以及进程令牌里打开了
SeDebugPrivilege。以前这两件都靠「用户右键以管理员运行」，一旦用户从源码跑
（python toolbox\\ssjj_toolbox.py）或者 UAC 点了「否」，注入就直接抛
「OpenProcess 失败（需要管理员权限）」，而那个提示既没说清原因，也没给出路。

这里提供三条：
    is_admin()                 当前进程是否高完整性
    enable_debug_privilege()   给当前进程令牌打开 SeDebugPrivilege（不需要提权时也能开）
    relaunch_admin(args)       用 ShellExecuteW("runas") 拉起一个提权的自己

提权重启的约定：主程序（toolbox/ssjj_toolbox.py）认识 `--risk-inject <pid>`，
提权副本只做注入这一件事，做完就退，不弹界面。
"""
import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys

SE_DEBUG_NAME = "SeDebugPrivilege"
TOKEN_ADJUST_PRIVILEGES = 0x0020
TOKEN_QUERY = 0x0008
SE_PRIVILEGE_ENABLED = 0x0002
ERROR_NOT_ALL_ASSIGNED = 1300

_advapi = ctypes.WinDLL("advapi32", use_last_error=True)
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_shell = ctypes.WinDLL("shell32", use_last_error=True)


class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", wt.DWORD), ("HighPart", wt.LONG)]


class _LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Luid", _LUID), ("Attributes", wt.DWORD)]


class _TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [("PrivilegeCount", wt.DWORD),
                ("Privileges", _LUID_AND_ATTRIBUTES * 1)]


_advapi.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
_advapi.OpenProcessToken.restype = wt.BOOL
_advapi.LookupPrivilegeValueW.argtypes = [wt.LPCWSTR, wt.LPCWSTR, ctypes.POINTER(_LUID)]
_advapi.LookupPrivilegeValueW.restype = wt.BOOL
_advapi.AdjustTokenPrivileges.argtypes = [wt.HANDLE, wt.BOOL,
                                          ctypes.POINTER(_TOKEN_PRIVILEGES), wt.DWORD,
                                          ctypes.c_void_p, ctypes.c_void_p]
_advapi.AdjustTokenPrivileges.restype = wt.BOOL
_k32.GetCurrentProcess.restype = wt.HANDLE
_k32.CloseHandle.argtypes = [wt.HANDLE]
_k32.CloseHandle.restype = wt.BOOL

_PRIV_ONCE = {"done": False, "ok": False, "msg": ""}


def is_admin():
    """当前进程是否以管理员（高完整性）运行。"""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def enable_debug_privilege():
    """给当前进程打开 SeDebugPrivilege。返回 (ok, 说明)。

    普通用户令牌里如果压根没有这个权限，LookupPrivilegeValue 还是能查到，
    AdjustTokenPrivileges 会以 ERROR_NOT_ALL_ASSIGNED 返回成功但没真正启用，
    所以必须看 GetLastError 判断。
    """
    if _PRIV_ONCE["done"]:
        return _PRIV_ONCE["ok"], _PRIV_ONCE["msg"]
    _PRIV_ONCE["done"] = True
    h = wt.HANDLE()
    if not _advapi.OpenProcessToken(_k32.GetCurrentProcess(),
                                    TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
                                    ctypes.byref(h)):
        _PRIV_ONCE["msg"] = "OpenProcessToken 失败（错误 %d）" % ctypes.get_last_error()
        return False, _PRIV_ONCE["msg"]
    try:
        luid = _LUID()
        if not _advapi.LookupPrivilegeValueW(None, SE_DEBUG_NAME, ctypes.byref(luid)):
            _PRIV_ONCE["msg"] = "LookupPrivilegeValue 失败（错误 %d）" % ctypes.get_last_error()
            return False, _PRIV_ONCE["msg"]
        tp = _TOKEN_PRIVILEGES()
        tp.PrivilegeCount = 1
        tp.Privileges[0].Luid = luid
        tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
        if not _advapi.AdjustTokenPrivileges(h, False, ctypes.byref(tp), 0, None, None):
            _PRIV_ONCE["msg"] = "AdjustTokenPrivileges 失败（错误 %d）" % ctypes.get_last_error()
            return False, _PRIV_ONCE["msg"]
        err = ctypes.get_last_error()
        if err == ERROR_NOT_ALL_ASSIGNED:
            _PRIV_ONCE["ok"] = False
            _PRIV_ONCE["msg"] = "令牌里没有 SeDebugPrivilege（不是管理员）"
            return False, _PRIV_ONCE["msg"]
        _PRIV_ONCE["ok"] = True
        _PRIV_ONCE["msg"] = "SeDebugPrivilege 已启用"
        return True, _PRIV_ONCE["msg"]
    finally:
        _k32.CloseHandle(h)


def self_exe():
    """当前程序的可执行路径（打包后是 exe，源码跑是 python.exe）。"""
    if getattr(sys, "frozen", False):
        return sys.executable
    return sys.executable


def is_frozen():
    return bool(getattr(sys, "frozen", False))


def relaunch_admin(extra_args, wait=False, timeout=90):
    """以管理员身份再拉起一份自己，参数走 extra_args（列表或字符串）。

    返回 (ok, 说明)。源码运行时用 python.exe + 入口脚本；打包后用 exe 本身。
    """
    args = extra_args if isinstance(extra_args, str) else " ".join(extra_args or [])
    if not is_frozen():
        entry = _source_entry()
        if not entry:
            return False, "找不到入口脚本，没法提权"
        target = sys.executable
        params = '"%s" %s' % (entry, args)
        cwd = os.path.dirname(entry)
    else:
        target = sys.executable
        params = args
        cwd = os.path.dirname(sys.executable)
    try:
        r = _shell.ShellExecuteW(None, "runas", target, params, cwd, 1)
        if r <= 32:
            # 5 = 用户点了「否」
            return False, "提权被拒绝或失败（ShellExecuteW 返回 %d）" % r
    except Exception as e:
        return False, "提权异常：%r" % (e,)
    if not wait:
        return True, "已拉起提权副本"
    return True, "已拉起提权副本（后台执行）"


def _source_entry():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ("toolbox\\ssjj_toolbox.py", "toolbox/ssjj_toolbox.py"):
        p = os.path.join(here, name)
        if os.path.isfile(p):
            return p
    return None


def run_admin_inject(pid):
    """提权副本注入一次：`--risk-inject <pid>`，用 returncode 判断成败。

    提权副本自己写 risk_inject.log，所以这里只看退出码 + 日志尾巴。
    """
    args = ["--risk-inject", str(int(pid))]
    if not is_frozen():
        entry = _source_entry()
        if not entry:
            return False, "找不到入口脚本"
        argv = [sys.executable, entry] + args
    else:
        argv = [sys.executable] + args
    try:
        r = subprocess.run(argv, capture_output=True, encoding="utf-8",
                           errors="replace", timeout=120,
                           creationflags=0x08000000)   # CREATE_NO_WINDOW
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        return r.returncode == 0, out or ("退出码 %d" % r.returncode)
    except Exception as e:
        return False, "调用提权副本失败：%r" % (e,)


def status():
    ok_priv, msg_priv = enable_debug_privilege()
    return {
        "admin": is_admin(),
        "frozen": is_frozen(),
        "debug_privilege": ok_priv,
        "detail": msg_priv,
        "exe": self_exe(),
    }


def main():
    import json
    print(json.dumps(status(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
