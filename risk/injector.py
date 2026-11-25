# -*- coding: utf-8 -*-
"""风险模式：Mono 托管程序集内存注入（不落盘）。

为什么这样注入
--------------
游戏端 GameGuard（npsc64.des）抓外部模块的三条主力手段：
  1. EnumProcessModules 找没签名的模块
  2. GetMappedFileNameW 反查模块路径（%TEMP%\\xxx.dll 之类）
  3. WinVerifyTrust 验签名
以上三条的共性是「模块必须是个文件映射」。所以本注入器：

  · 不写任何 dll 到磁盘
  · 把托管程序集的字节直接交给 mono_image_open_from_data_with_name(need_copy=1)
  · mono 在目标进程里分配匿名内存放 image → 不进 PEB 模块链、没有文件路径、
    没有签名可验

调用链（全部在目标进程内由一段手写 x64 stub 顺序执行）：
  mono_get_root_domain()
  mono_thread_attach(root)
  mono_image_open_from_data_with_name(data, len, 1, &status, 0, name)
  mono_assembly_load_from_full(image, name, &status, 0)
  mono_class_from_name(image, "SsjjRisk", "Loader")
  mono_class_get_method_from_name(klass, "Load", 0)
  mono_runtime_invoke(method, NULL, NULL, &exc)

工程实现要点：参数里要塞指针、返回值要串给下一次调用，所以 stub 用一小块
「暂存区」把中间结果落盘到目标进程的临时内存里，全程只需要一次
CreateRemoteThread。
"""
import ctypes
import ctypes.wintypes as wt
import datetime
import hashlib
import json
import os
import struct
import subprocess
import sys
import threading
import time

try:                        # 包内导入；单独 python injector.py 调试时降级
    from . import privilege
except Exception:           # pragma: no cover
    privilege = None

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PAYLOAD_DIR = os.path.join(HERE, "payload")
# 载荷程序集文件名 = 程序集元数据名（csc 不给 AssemblyName 特性时两者一致）。
# 故意用系统组件名而不是 SsjjRisk.dll：反作弊枚举模块 / AppDomain.GetAssemblies() 时，
# 一个叫 SsjjRisk 的托管程序集是硬特征；系统组件名至少不会被一眼点名。
ASM_NAME = "System.Configuration.Install.dll"
ASM_LEGACY_NAMES = ("SsjjRisk.dll",)
# csc 直接引用「游戏自己的 mscorlib」编出来的载荷。走 csc 这条路的原因见 csc_build()。
PAYLOAD_DLL = os.path.join(PAYLOAD_DIR, "bin", ASM_NAME)
# 旧路线（dotnet build netstandard2.0）的产物位置，只作兜底候选
PAYLOAD_DLL_LEGACY = os.path.join(PAYLOAD_DIR, "bin", "Release", "netstandard2.0", "SsjjRisk.dll")
def _resolve_game_managed():
    gd = r"C:\Program Files (x86)\Wooduan\SSJJ-4399"
    try:
        import ssjj_paths as _P
        if getattr(_P, "GAME_DIR", None):
            gd = _P.GAME_DIR
    except Exception:
        pass
    battle_dir = os.path.join(gd, "battle")
    if os.path.isdir(battle_dir):
        cands = []
        for sub in os.listdir(battle_dir):
            m = os.path.join(battle_dir, sub, "SSJJ_BattleClient_Unity_Data", "Managed")
            if os.path.isfile(os.path.join(m, "mscorlib.dll")):
                cands.append((sub.endswith("_64"), sub, m))
        if cands:
            cands.sort(reverse=True)
            return gd, cands[0][2]
    return gd, os.path.join(gd, "battle", "10_64", "SSJJ_BattleClient_Unity_Data", "Managed")


GAME_DIR, GAME_MANAGED = _resolve_game_managed()
GAME_REFS = ("mscorlib.dll", "System.dll", "System.Core.dll")
DEFAULT_TARGET = "SSJJ_BattleClient_Unity.exe"
TRACE_PATH = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                          "ssjj-toolbox", "injection_trace.jsonl")


def trace_event(pid, attempt, stage, **details):
    """每个阶段独立落盘；目标端崩溃时不依赖 inject() 正常返回。"""
    record = {
        "time": datetime.datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "monotonic_ns": time.monotonic_ns(),
        "host_pid": os.getpid(),
        "pid": int(pid),
        "attempt": attempt,
        "stage": stage,
    }
    record.update(details)
    line = (json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str) + "\n"
            ).encode("utf-8")
    for path in (TRACE_PATH, os.path.join(os.environ.get("TEMP", os.getcwd()),
                                          "ssjj_injection_trace.jsonl")):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, line)
                os.fsync(fd)
            finally:
                os.close(fd)
            return path
        except OSError:
            pass
    return None


def _fingerprint(path):
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return {"path": path, "size": os.path.getsize(path), "sha256": digest.hexdigest()}
    except OSError as exc:
        return {"path": path, "error": str(exc)}


def payload_candidates():
    r"""载荷 DLL 的候选位置。

    源码跑：csc 编在 risk\payload\bin\<ASM_NAME>（首选），旧名与 dotnet build 产物作兜底。
    打包跑：exe 旁边没有 dotnet、也没有工程文件，所以编译产物必须提前复制到
    <数据目录>/risk_payload/，运行时直接读那一份。
    """
    bundled = [PAYLOAD_DLL, PAYLOAD_DLL_LEGACY]
    external = []
    try:
        from .paths import PAYLOAD_DIR as _PD
        for n in (ASM_NAME,) + ASM_LEGACY_NAMES:
            external.append(os.path.join(_PD, n))
    except Exception:
        pass
    # 打包版允许 exe 旁的重编译载荷覆盖包内旧副本。
    # 源码版仍优先取刚编出的 bin 目录。
    cands = (external + bundled) if getattr(sys, "frozen", False) else (bundled + external)
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        for n in (ASM_NAME,) + ASM_LEGACY_NAMES:
            cands.append(os.path.join(meipass, "risk", "payload", "bin", n))
            cands.append(os.path.join(meipass, "risk", "payload", n))
    return cands


def payload_path():
    for p in payload_candidates():
        try:
            if os.path.isfile(p) and os.path.getsize(p) > 0:
                return p
        except Exception:
            pass
    return None

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll")

PROCESS_ALL_ACCESS = 0x1F0FFF
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
PAGE_EXECUTE_READWRITE = 0x40
PAGE_EXECUTE_READ = 0x20
PAGE_READONLY = 0x02
PAGE_READWRITE = 0x04
MEM_RELEASE = 0x8000
LIST_MODULES_ALL = 0x03
TH32CS_SNAPPROCESS = 0x02

# ---------------------------------------------------------------- API 原型
# 64 位下不声明 restype，句柄与指针会被 ctypes 当 32 位有符号整数处理：
# VirtualAllocEx 返回的高地址会变成负数（实测 0x-5440000），接着写内存、
# 建线程全部失败，而且报错信息完全指不到真正原因。全部显式声明一遍。
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.OpenProcess.restype = wt.HANDLE
k32.VirtualAllocEx.argtypes = [wt.HANDLE, wt.LPVOID, ctypes.c_size_t, wt.DWORD, wt.DWORD]
k32.VirtualAllocEx.restype = wt.LPVOID
k32.VirtualFreeEx.argtypes = [wt.HANDLE, wt.LPVOID, ctypes.c_size_t, wt.DWORD]
k32.VirtualFreeEx.restype = wt.BOOL
k32.VirtualProtectEx.argtypes = [wt.HANDLE, wt.LPVOID, ctypes.c_size_t, wt.DWORD,
                                 ctypes.POINTER(wt.DWORD)]
k32.VirtualProtectEx.restype = wt.BOOL
k32.WriteProcessMemory.argtypes = [wt.HANDLE, wt.LPVOID, wt.LPCVOID, ctypes.c_size_t,
                                   ctypes.POINTER(ctypes.c_size_t)]
k32.WriteProcessMemory.restype = wt.BOOL
k32.ReadProcessMemory.argtypes = [wt.HANDLE, wt.LPCVOID, wt.LPVOID, ctypes.c_size_t,
                                  ctypes.POINTER(ctypes.c_size_t)]
k32.ReadProcessMemory.restype = wt.BOOL
k32.CreateRemoteThread.argtypes = [wt.HANDLE, wt.LPVOID, ctypes.c_size_t, wt.LPVOID,
                                   wt.LPVOID, wt.DWORD, ctypes.POINTER(wt.DWORD)]
k32.CreateRemoteThread.restype = wt.HANDLE
k32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
k32.WaitForSingleObject.restype = wt.DWORD
k32.GetExitCodeThread.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
k32.GetExitCodeThread.restype = wt.BOOL
k32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
k32.GetExitCodeProcess.restype = wt.BOOL
k32.CloseHandle.argtypes = [wt.HANDLE]
k32.CloseHandle.restype = wt.BOOL
k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
k32.Process32First.argtypes = [wt.HANDLE, ctypes.c_void_p]
k32.Process32First.restype = wt.BOOL
k32.Process32Next.argtypes = [wt.HANDLE, ctypes.c_void_p]
k32.Process32Next.restype = wt.BOOL
k32.Thread32First.argtypes = [wt.HANDLE, ctypes.c_void_p]
k32.Thread32First.restype = wt.BOOL
k32.Thread32Next.argtypes = [wt.HANDLE, ctypes.c_void_p]
k32.Thread32Next.restype = wt.BOOL
psapi.EnumProcessModulesEx.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD,
                                       ctypes.POINTER(wt.DWORD), wt.DWORD]
psapi.EnumProcessModulesEx.restype = wt.BOOL
psapi.GetModuleBaseNameW.argtypes = [wt.HANDLE, wt.HMODULE, wt.LPWSTR, wt.DWORD]
psapi.GetModuleBaseNameW.restype = wt.DWORD
psapi.GetModuleFileNameExW.argtypes = [wt.HANDLE, wt.HMODULE, wt.LPWSTR, wt.DWORD]
psapi.GetModuleFileNameExW.restype = wt.DWORD


class MEMORY_BASIC_INFORMATION64(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_ulonglong), ("AllocationBase", ctypes.c_ulonglong),
                ("AllocationProtect", wt.DWORD), ("__a1", wt.DWORD),
                ("RegionSize", ctypes.c_ulonglong), ("State", wt.DWORD),
                ("Protect", wt.DWORD), ("Type", wt.DWORD), ("__a2", wt.DWORD)]


k32.VirtualQueryEx.argtypes = [wt.HANDLE, wt.LPCVOID,
                               ctypes.POINTER(MEMORY_BASIC_INFORMATION64), ctypes.c_size_t]
k32.VirtualQueryEx.restype = ctypes.c_size_t

_PROTECT_NAMES = {0x01: "NOACCESS", 0x02: "R", 0x04: "RW", 0x08: "WC", 0x10: "X",
                  0x20: "RX", 0x40: "RWX", 0x80: "WX"}


def protect_of(h, addr):
    """查某地址所在页的保护属性（用于自检：确认代码页不是 RWX）。"""
    mbi = MEMORY_BASIC_INFORMATION64()
    if not k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi),
                              ctypes.sizeof(mbi)):
        return "?"
    return _PROTECT_NAMES.get(mbi.Protect & 0xFF, "0x%X" % mbi.Protect)


# ---------------------------------------------------------------- 进程 / 模块
class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
                ("th32ProcessID", wt.DWORD), ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wt.DWORD), ("szExeFile", ctypes.c_char * 260)]


def list_processes(name=None):
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    out = []
    if snap in (0, ctypes.c_void_p(-1).value):
        return out
    try:
        e = PROCESSENTRY32()
        e.dwSize = ctypes.sizeof(e)
        ok = k32.Process32First(snap, ctypes.byref(e))
        while ok:
            exe = e.szExeFile.decode("mbcs", "replace")
            if not name or exe.lower() == name.lower():
                out.append({"pid": int(e.th32ProcessID), "exe": exe})
            ok = k32.Process32Next(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    if not out:
        return out
    # 线程快照中明确为 0 的条目不再作为运行中的目标。
    # 快照失败或未列出 PID 时保留进程条目，避免误判为进程不存在。
    counts = _thread_counts()
    if not counts:                       # 快照失败：不过滤，避免把活进程也判死
        return out
    return [p for p in out if counts.get(p["pid"], 0) > 0]


TH32CS_SNAPTHREAD = 0x00000004


class THREADENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
                ("th32ThreadID", wt.DWORD), ("th32OwnerProcessID", wt.DWORD),
                ("tpBasePri", wt.LONG), ("tpDeltaPri", wt.LONG),
                ("dwFlags", wt.DWORD)]


def _thread_counts():
    """pid -> 线程数。用 TH32CS_SNAPTHREAD 数一遍，一次快照覆盖所有进程。"""
    out = {}
    try:
        h = k32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
        if not h or h == ctypes.c_void_p(-1).value:
            return out
        try:
            e = THREADENTRY32()
            e.dwSize = ctypes.sizeof(e)
            ok = k32.Thread32First(h, ctypes.byref(e))
            while ok:
                owner = int(e.th32OwnerProcessID)
                out[owner] = out.get(owner, 0) + 1
                ok = k32.Thread32Next(h, ctypes.byref(e))
        finally:
            k32.CloseHandle(h)
    except Exception:
        return {}
    return out


def find_pid(name):
    ps = list_processes(name)
    return ps[0]["pid"] if ps else None


class NeedAdmin(RuntimeError):
    """拿不到句柄是因为权限（WinError 5 / 拒绝访问）。

    单独一个异常类型，让上层可以走「自动提权一次」这条路，而不是把
    「需要管理员权限」当终端错误弹给用户。
    """
    def __init__(self, detail, winerror=5):
        RuntimeError.__init__(self, detail)
        self.need_admin = True
        self.winerror = winerror


# 只读模块列举用不到写权限；PROCESS_ALL_ACCESS 在低权限下第一枪就打空。
_RIGHTS_QUERY = 0x0410   # PROCESS_QUERY_INFORMATION | PROCESS_VM_READ
_RIGHTS_INJECT = (0x0002 |  # PROCESS_VM_WRITE
                  0x0008 |  # PROCESS_VM_OPERATION
                  0x0010 |  # PROCESS_VM_READ
                  0x0020 |  # PROCESS_CREATE_THREAD
                  0x0400 |  # PROCESS_QUERY_INFORMATION
                  0x1000)   # PROCESS_QUERY_LIMITED_INFORMATION


def _open_target(pid, rights=None, what="进程"):
    """OpenProcess，失败时给出可判定的两类结果。

    先按最小权限试，再退化到 ALL_ACCESS；两次都失败且错误是 5（拒绝访问）
    就抛 NeedAdmin —— 上层据此决定「自动提权再试一次」。
    """
    if privilege is not None:
        privilege.enable_debug_privilege()      # 有就开，没有也不报错
    err = 0
    for r in ([rights] if rights else [_RIGHTS_INJECT, PROCESS_ALL_ACCESS]):
        h = k32.OpenProcess(r, False, pid)
        if h:
            return h
        err = ctypes.get_last_error()
        if err not in (5, 0):
            break
    admin = bool(privilege and privilege.is_admin())
    if err == 5:
        raise NeedAdmin(
            "OpenProcess(%s pid=%d) 被拒绝：错误 5 拒绝访问。\n"
            "  当前进程管理员=%s。战斗端是管理员启动的话，工具箱也必须提权才能拿到句柄。"
            % (what, pid, "是" if admin else "否"), err)
    raise OSError(err, "OpenProcess(%s pid=%d) 失败（错误 %d）" % (what, pid, err))


def _modules(pid, tries=6, gap=0.4):
    """列模块。进程刚起来时 PEB 还没填完，EnumProcessModulesEx 会报 299
    (ERROR_PARTIAL_COPY)，这时候等一下再试，别当成硬错误。"""
    last = None
    for i in range(max(1, tries)):
        h = _open_target(pid, _RIGHTS_QUERY, "枚举模块")
        try:
            needed = wt.DWORD(0)
            arr = (ctypes.c_void_p * 1024)()
            if psapi.EnumProcessModulesEx(h, arr, ctypes.sizeof(arr), ctypes.byref(needed),
                                          LIST_MODULES_ALL):
                n = needed.value // ctypes.sizeof(ctypes.c_void_p)
                out = []
                for k in range(n):
                    base = int(arr[k] or 0)
                    if not base:
                        continue
                    buf = ctypes.create_unicode_buffer(520)
                    if not psapi.GetModuleBaseNameW(h, ctypes.c_void_p(base), buf, 520):
                        continue
                    path = ctypes.create_unicode_buffer(1024)
                    psapi.GetModuleFileNameExW(h, ctypes.c_void_p(base), path, 1024)
                    out.append({"base": base, "name": buf.value, "path": path.value})
                return out
            last = ctypes.get_last_error()
        finally:
            k32.CloseHandle(h)
        if last not in (299, 5, 6):
            break
        time.sleep(gap)
    if last in (299, 5, 6):
        return []                    # 进程还在初始化 / 刚退出，让调用方下一轮再试
    raise OSError(last, "EnumProcessModulesEx 失败（错误 %s）" % last)


def find_mono(pid):
    for m in _modules(pid):
        n = m["name"].lower()
        if n.startswith("mono") and n.endswith(".dll"):
            return m
    return None


# ---------------------------------------------------------------- PE 导出表
def pe_exports(path):
    """读 DLL 的导出表，返回 {名字: RVA}（纯解析，不加载到本进程）"""
    with open(path, "rb") as f:
        blob = f.read()
    if blob[:2] != b"MZ":
        raise ValueError("不是 PE 文件: %s" % path)
    e_lfanew = struct.unpack("<I", blob[0x3C:0x40])[0]
    if blob[e_lfanew:e_lfanew + 4] != b"PE\0\0":
        raise ValueError("PE 头损坏: %s" % path)
    coff = e_lfanew + 4
    nsec = struct.unpack("<H", blob[coff + 2:coff + 4])[0]
    opt_size = struct.unpack("<H", blob[coff + 16:coff + 18])[0]
    opt = coff + 20
    magic = struct.unpack("<H", blob[opt:opt + 2])[0]
    if magic == 0x20B:
        dd = opt + 112
    else:
        dd = opt + 96
    exp_rva, exp_size = struct.unpack("<II", blob[dd:dd + 8])
    if not exp_rva:
        return {}
    secs = []
    sp = opt + opt_size
    for i in range(nsec):
        off = sp + i * 40
        va, rawsz, rawptr = struct.unpack("<III", blob[off + 12:off + 24])
        secs.append((va, rawsz, rawptr))

    def rva2off(rva):
        for va, sz, ptr in secs:
            if va <= rva < va + max(sz, 1) + 0x1000:
                return ptr + (rva - va)
        return None

    eo = rva2off(exp_rva)
    if eo is None:
        return {}
    n_names = struct.unpack("<I", blob[eo + 24:eo + 28])[0]
    names_rva = struct.unpack("<I", blob[eo + 32:eo + 36])[0]
    funcs_rva = struct.unpack("<I", blob[eo + 28:eo + 32])[0]
    no = rva2off(names_rva)
    fo = rva2off(funcs_rva)
    out = {}
    for i in range(n_names):
        nrva = struct.unpack("<I", blob[no + i * 4:no + i * 4 + 4])[0]
        npo = rva2off(nrva)
        if npo is None:
            continue
        end = blob.index(b"\x00", npo)
        name = blob[npo:end].decode("latin-1")
        idx = struct.unpack("<H", blob[no + n_names * 4 + i * 2:no + n_names * 4 + i * 2 + 2])[0]
        frva = struct.unpack("<I", blob[fo + idx * 4:fo + idx * 4 + 4])[0]
        out[name] = frva
    return out


# ---------------------------------------------------------------- x64 stub
class Asm(object):
    REG_PREFIX = {"rax": b"\x48\xB8", "rcx": b"\x48\xB9",
                  "rdx": b"\x48\xBA", "r8": b"\x49\xB8", "r9": b"\x49\xB9"}

    def __init__(self):
        self.b = bytearray()

    def sub_rsp(self, n=0x38):
        self.b += b"\x48\x83\xEC" + bytes([n])

    def add_rsp(self, n=0x38):
        self.b += b"\x48\x83\xC4" + bytes([n])

    def mov_reg(self, reg, value):
        self.b += self.REG_PREFIX[reg] + struct.pack("<Q", value)

    def load_rax(self, addr):
        self.b += b"\x48\xA1" + struct.pack("<Q", addr)      # mov rax, [addr]

    def store_rax(self, addr):
        self.b += b"\x48\xA3" + struct.pack("<Q", addr)      # mov [addr], rax

    def rax_to(self, reg):
        self.b += {"rcx": b"\x48\x89\xC1", "rdx": b"\x48\x89\xC2",
                   "r8": b"\x49\x89\xC0", "r9": b"\x49\x89\xC1"}[reg]

    def stack_imm32(self, slot, value):
        self.b += b"\x48\xC7\x44\x24" + bytes([slot]) + struct.pack("<i", value)

    def stack_rax(self, slot):
        self.b += b"\x48\x89\x44\x24" + bytes([slot])

    def call(self, addr):
        self.mov_reg("rax", addr)
        self.b += b"\xFF\xD0"

    def test_rax(self):
        self.b += b"\x48\x85\xC0"                            # test rax, rax

    def jz8(self):
        """条件跳转占位，返回 rel8 操作数在 stub 内的位置，稍后用 patch8 回填"""
        self.b += b"\x74\x00"
        return len(self.b) - 1

    def jnz8(self):
        self.b += b"\x75\x00"
        return len(self.b) - 1

    def patch8(self, pos, target):
        """把 pos 处的 rel8 操作数指向 target（stub 内偏移）；越界立刻报错，别产出哑弹"""
        delta = target - (pos + 1)
        if not -128 <= delta <= 127:
            raise ValueError("条件跳转过远：pos=%d target=%d delta=%d" % (pos, target, delta))
        self.b[pos] = delta & 0xFF

    def ret(self):
        self.b += b"\xC3"


# 远程内存布局
L_CODE, L_ASMNAME, L_NS, L_CLS, L_METHOD = 0x000, 0x400, 0x440, 0x480, 0x4C0
L_STATUS, L_IMAGE, L_ASSEMBLY, L_KLASS, L_MMETHOD, L_EXC, L_ROOT = (
    0x500, 0x508, 0x510, 0x518, 0x520, 0x528, 0x530)
L_IMAGE2, L_DESC, L_MMETHOD_B = 0x538, 0x540, 0x548
L_DESCSTR = 0x560
L_DATA = 0x800


def build_stub(base, exports, assembly_len, mono_base=0, name=ASM_NAME,
               ns="SsjjRisk", cls="Loader", method="Load"):
    """生成远程载荷模板。

    返回 (payload, stub_len)。payload 是 0x2000+len(asm) 大小的模板，里面所有绝对地址
    都指向 `base`（数据区）；stub 本身不含 RIP 相对寻址，所以可以另分配到代码区执行。
    """
    need = ["mono_image_open_from_data_with_name", "mono_assembly_load_from_full",
            "mono_class_from_name", "mono_class_get_method_from_name", "mono_runtime_invoke"]
    for n in need:
        if n not in exports:
            raise KeyError("mono 导出表里缺少 %s" % n)
    desc_route = all(k in exports for k in ("mono_assembly_get_image", "mono_method_desc_new",
                                            "mono_method_desc_search_in_image"))
    desc_str = "%s.%s:%s()" % (ns, cls, method)

    # exports 里的地址是 RVA，目标进程里的绝对地址 = 远程 mono 基址 + RVA
    def A(n):
        return mono_base + exports[n]

    a = Asm()
    a.sub_rsp(0x38)
    if "mono_get_root_domain" in exports and "mono_thread_attach" in exports:
        a.call(A("mono_get_root_domain"))
        a.store_rax(base + L_ROOT)
        a.load_rax(base + L_ROOT)
        a.rax_to("rcx")
        a.call(A("mono_thread_attach"))

    # image = mono_image_open_from_data_with_name(data, len, 1, &status, 0, name)
    a.mov_reg("rcx", base + L_DATA)
    a.mov_reg("rdx", assembly_len)
    a.mov_reg("r8", 1)
    a.mov_reg("r9", base + L_STATUS)
    a.stack_imm32(0x20, 0)
    a.mov_reg("rax", base + L_ASMNAME)
    a.stack_rax(0x28)
    a.call(A("mono_image_open_from_data_with_name"))
    a.store_rax(base + L_IMAGE)

    # assembly = mono_assembly_load_from_full(image, name, &status, 0)
    a.load_rax(base + L_IMAGE)
    a.rax_to("rcx")
    a.mov_reg("rdx", base + L_ASMNAME)
    a.mov_reg("r8", base + L_STATUS)
    a.mov_reg("r9", 0)
    a.call(A("mono_assembly_load_from_full"))
    a.store_rax(base + L_ASSEMBLY)

    # img2 = mono_assembly_get_image(assembly)；用程序集自己的 image 查类型才稳，
    # 直接用 open 出来的原始 image 在部分 mono 版本上查不到类（实测 class=0）。
    if desc_route:
        a.load_rax(base + L_ASSEMBLY)
        a.rax_to("rcx")
        a.call(A("mono_assembly_get_image"))
        a.store_rax(base + L_IMAGE2)
    else:
        a.load_rax(base + L_IMAGE)
        a.store_rax(base + L_IMAGE2)

    # klass = mono_class_from_name(img2, ns, cls)
    a.load_rax(base + L_IMAGE2)
    a.rax_to("rcx")
    a.mov_reg("rdx", base + L_NS)
    a.mov_reg("r8", base + L_CLS)
    a.call(A("mono_class_from_name"))
    a.store_rax(base + L_KLASS)

    # method = mono_class_get_method_from_name(klass, "Load", 0)；klass 为 0 就别喂 NULL
    a.load_rax(base + L_KLASS)
    a.test_rax()
    j_no_klass = a.jz8()
    a.rax_to("rcx")
    a.mov_reg("rdx", base + L_METHOD)
    a.mov_reg("r8", 0)
    a.call(A("mono_class_get_method_from_name"))
    a.store_rax(base + L_MMETHOD)
    a.patch8(j_no_klass, len(a.b))

    # 兜底：descriptor 路线 mono_method_desc_new("SsjjRisk.Loader:Load()", 1) +
    # mono_method_desc_search_in_image(desc, img2)。klass 路线拿不到就吃这条的结果。
    if desc_route:
        a.mov_reg("rcx", base + L_DESCSTR)
        a.mov_reg("rdx", 1)
        a.call(A("mono_method_desc_new"))
        a.store_rax(base + L_DESC)
        a.load_rax(base + L_DESC)
        a.test_rax()
        j_no_desc = a.jz8()
        a.rax_to("rcx")
        a.load_rax(base + L_IMAGE2)
        a.rax_to("rdx")
        a.call(A("mono_method_desc_search_in_image"))
        a.store_rax(base + L_MMETHOD_B)
        a.patch8(j_no_desc, len(a.b))

        a.load_rax(base + L_MMETHOD)
        a.test_rax()
        j_have = a.jnz8()
        a.load_rax(base + L_MMETHOD_B)
        a.store_rax(base + L_MMETHOD)
        a.patch8(j_have, len(a.b))

    # mono_runtime_invoke(method, NULL, NULL, &exc)
    a.load_rax(base + L_MMETHOD)
    a.rax_to("rcx")
    a.mov_reg("rdx", 0)
    a.mov_reg("r8", 0)
    a.mov_reg("r9", base + L_EXC)
    a.call(A("mono_runtime_invoke"))

    a.add_rsp(0x38)
    a.ret()
    stub = bytes(a.b)

    payload = bytearray(0x2000)
    payload[L_CODE:L_CODE + len(stub)] = stub
    for off, text in ((L_ASMNAME, name), (L_NS, ns), (L_CLS, cls), (L_METHOD, method),
                      (L_DESCSTR, desc_str)):
        raw = text.encode("utf-8") + b"\x00"
        payload[off:off + len(raw)] = raw
    return bytes(payload), len(stub)


# ---------------------------------------------------------------- 注入
class InjectionAttempted(RuntimeError):
    """远程线程已创建，但后续诊断失败；同一 PID 不得自动重试。"""

    def __init__(self, cause, log):
        super().__init__("%s: %s" % (type(cause).__name__, cause))
        self.log = list(log)


def _payload_start_logged(pid, paths, offsets):
    marker = ("载荷载入 pid=%d " % pid).encode("utf-8")
    for path in paths:
        try:
            with open(path, "rb") as proof:
                size = os.fstat(proof.fileno()).st_size
                proof.seek(offsets.get(path, 0) if size >= offsets.get(path, 0) else 0)
                if marker in proof.read():
                    return True
        except OSError:
            pass
    return False


def _observe_process_exit(pid, attempt, horizon_seconds=15):
    """只等待进程句柄；不暂停、读写或枚举目标进程。"""
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid)
    if not h:
        trace_event(pid, attempt, "follow.open_failed", winerror=ctypes.get_last_error())
        return
    try:
        trace_event(pid, attempt, "follow.begin", horizon_seconds=horizon_seconds)
        result = k32.WaitForSingleObject(h, int(horizon_seconds * 1000))
        code = wt.DWORD(0)
        got_code = bool(k32.GetExitCodeProcess(h, ctypes.byref(code)))
        trace_event(pid, attempt, "follow.end", wait_result=hex(result),
                    exit_code=hex(code.value) if got_code else None,
                    exit_code_error=None if got_code else ctypes.get_last_error())
    except Exception as exc:
        trace_event(pid, attempt, "follow.error", error=repr(exc))
    finally:
        k32.CloseHandle(h)


def inject(pid, dll_path=None, target_name=DEFAULT_TARGET, timeout=30,
           ns="SsjjRisk", cls="Loader", method="Load", asm_name=ASM_NAME):
    attempt = "%d-%d-%d" % (pid, os.getpid(), time.time_ns())
    trace_event(pid, attempt, "begin", target=target_name, trace_path=TRACE_PATH)
    guard_file = os.path.join(GAME_DIR, "MicroClient64", "GameGuard", "npggNT64.des")
    trace_event(pid, attempt, "guard.file", **_fingerprint(guard_file))
    dll_path = dll_path or payload_path()
    if not dll_path:
        trace_event(pid, attempt, "payload.missing")
        raise FileNotFoundError("载荷还没编译，也没找到预编译副本。\n"
                                "源码模式先跑 python -m risk build；打包模式需要 "
                                "risk_payload\\" + ASM_NAME)
    trace_event(pid, attempt, "payload.selected", **_fingerprint(dll_path))
    blob = open(dll_path, "rb").read()

    trace_event(pid, attempt, "mono.lookup.begin")
    mono = find_mono(pid)
    if not mono:
        trace_event(pid, attempt, "mono.missing")
        raise RuntimeError("目标进程里没有 mono 运行时（是不是大厅？大厅是 AIR，不注入）")
    mono_base = mono["base"]
    trace_event(pid, attempt, "mono.found", name=mono["name"], base=hex(mono_base),
                path=mono["path"])
    if not mono["path"] or not os.path.isfile(mono["path"]):
        trace_event(pid, attempt, "mono.path_missing", path=mono["path"])
        raise FileNotFoundError("拿不到 mono 的本地文件路径，无法解析导出表: %r" % mono)
    exports = pe_exports(mono["path"])
    trace_event(pid, attempt, "mono.exports", count=len(exports))
    log = ["[*] 目标 pid=%d  mono=%s @0x%X" % (pid, mono["name"], mono_base),
           "[*] mono 导出符号 %d 个" % len(exports)]

    trace_event(pid, attempt, "target.open.begin", rights=hex(_RIGHTS_INJECT))
    try:
        h = _open_target(pid, _RIGHTS_INJECT, "注入")
    except Exception as exc:
        trace_event(pid, attempt, "target.open.error", error=repr(exc),
                    winerror=ctypes.get_last_error())
        raise
    trace_event(pid, attempt, "target.open.ok")
    remote = None
    code_page = None
    attempted = False
    proof_paths = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "ssjj-toolbox", "risk_inject.log"),
        os.path.join(os.environ.get("TEMP", ""), "ssjj_risk.log"),
        os.path.join(os.path.dirname(mono["path"]), "ssjj_risk.log"),
    ]
    proof_offsets = {}
    for path in proof_paths:
        try:
            proof_offsets[path] = os.path.getsize(path)
        except OSError:
            proof_offsets[path] = 0
    try:
        size = 0x2000 + len(blob)
        # 数据区：PAGE_READWRITE，只放 stub 用的槽位 / 字符串 / 程序集字节
        trace_event(pid, attempt, "data.allocate.begin", size=size)
        remote = k32.VirtualAllocEx(h, None, size, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE)
        if not remote:
            raise OSError(ctypes.get_last_error(), "VirtualAllocEx（数据区）失败")
        trace_event(pid, attempt, "data.allocate.ok", address=hex(remote), size=size)
        log.append("[*] 已分配数据区 0x%X（%d 字节，PAGE_READWRITE、非执行）" % (remote, size))

        # 代码区：单独一页，写完立刻降权成 PAGE_EXECUTE_READ。这样进程里不存在任何
        # RWX 页（旧实现整块 PAGE_EXECUTE_READWRITE，是反作弊最容易命中的特征之一）。
        trace_event(pid, attempt, "code.allocate.begin", size=0x1000)
        code_page = k32.VirtualAllocEx(h, None, 0x1000, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE)
        if not code_page:
            raise OSError(ctypes.get_last_error(), "VirtualAllocEx（代码区）失败")
        trace_event(pid, attempt, "code.allocate.ok", address=hex(code_page))

        template, stub_len = build_stub(remote, exports, len(blob), mono_base=mono_base,
                                        name=asm_name, ns=ns, cls=cls, method=method)
        data = bytearray(template)
        data[L_DATA:L_DATA + len(blob)] = blob
        stub = bytes(template[L_CODE:L_CODE + stub_len])
        # 数据区里不留 stub 副本，只留槽位与程序集
        data[L_CODE:L_CODE + stub_len] = b"\x00" * stub_len
        trace_event(pid, attempt, "stub.ready", length=stub_len,
                    sha256=hashlib.sha256(stub).hexdigest(), result_offset=hex(L_STATUS),
                    image_offset=hex(L_IMAGE), method_offset=hex(L_MMETHOD))

        written = ctypes.c_size_t(0)
        trace_event(pid, attempt, "data.write.begin", size=len(data))
        if not k32.WriteProcessMemory(h, ctypes.c_void_p(remote), bytes(data), len(data),
                                      ctypes.byref(written)):
            raise OSError(ctypes.get_last_error(), "WriteProcessMemory（数据区）失败")
        trace_event(pid, attempt, "data.write.ok", written=written.value, expected=len(data))
        trace_event(pid, attempt, "code.write.begin", size=len(stub))
        if not k32.WriteProcessMemory(h, ctypes.c_void_p(code_page), stub, len(stub),
                                      ctypes.byref(written)):
            raise OSError(ctypes.get_last_error(), "WriteProcessMemory（代码区）失败")
        trace_event(pid, attempt, "code.write.ok", written=written.value, expected=len(stub))

        old = wt.DWORD(0)
        trace_event(pid, attempt, "code.protect.begin", address=hex(code_page))
        if not k32.VirtualProtectEx(h, ctypes.c_void_p(code_page), 0x1000, PAGE_EXECUTE_READ,
                                    ctypes.byref(old)):
            raise OSError(ctypes.get_last_error(), "VirtualProtectEx（代码区 → RX）失败")
        trace_event(pid, attempt, "code.protect.ok", previous=hex(old.value), current="RX")
        log.append("[*] 代码区 0x%X 已降权 PAGE_EXECUTE_READ（stub %d 字节）"
                   % (code_page, len(stub)))
        log.append("[*] 数据区已写入 %d 字节（槽位 + 程序集 %d 字节）" % (len(data), len(blob)))

        tid = wt.DWORD(0)
        # 调用跨过这里后，即使 API 返回错误，也不确定目标端是否已执行。
        # 保守记录尝试，避免守护再次对同一 PID 调用。
        trace_event(pid, attempt, "remote_thread.begin", start_address=hex(code_page))
        attempted = True
        th = k32.CreateRemoteThread(h, None, 0, ctypes.c_void_p(code_page), None, 0,
                                    ctypes.byref(tid))
        if not th:
            raise OSError(ctypes.get_last_error(), "CreateRemoteThread 失败")
        trace_event(pid, attempt, "remote_thread.created", tid=tid.value)
        try:
            wait_result = k32.WaitForSingleObject(th, int(timeout * 1000))
            code = wt.DWORD(0)
            got_thread_code = bool(k32.GetExitCodeThread(th, ctypes.byref(code)))
        finally:
            k32.CloseHandle(th)
        thread_finished = wait_result == 0 and got_thread_code
        trace_event(pid, attempt, "remote_thread.wait_end", wait_result=hex(wait_result),
                    tid=tid.value, thread_exit=hex(code.value) if got_thread_code else None,
                    finished=thread_finished)
        log.append("[*] 远程线程 wait=0x%X exit=0x%X finished=%s"
                   % (wait_result, code.value, thread_finished))
        process_code = wt.DWORD(0)
        process_alive = bool(k32.GetExitCodeProcess(h, ctypes.byref(process_code))
                             and process_code.value == 259)
        trace_event(pid, attempt, "target.state", alive=process_alive,
                    process_exit=hex(process_code.value))
        log.append("[*] 目标进程仍运行=%s exit=0x%X" %
                   (process_alive, process_code.value))
        prot = protect_of(h, code_page)
        log.append("[*] 代码页保护属性复查: %s（RWX 已消除）" % prot)
        if prot not in ("RX", "R", "?"):
            log.append("[!] 代码页保护属性异常: %s" % prot)

        scratch = (ctypes.c_ubyte * 0x60)()
        got = ctypes.c_size_t(0)
        read_ok = bool(thread_finished and process_alive and
                       k32.ReadProcessMemory(h, ctypes.c_void_p(remote + L_STATUS),
                                             scratch, 0x60, ctypes.byref(got)) and
                       got.value == 0x60)
        trace_event(pid, attempt, "result.read", ok=read_ok, bytes=got.value)
        buf = bytes(scratch)
        status = struct.unpack("<i", buf[0:4])[0]
        image = struct.unpack("<Q", buf[8:16])[0]
        assembly = struct.unpack("<Q", buf[16:24])[0]
        klass = struct.unpack("<Q", buf[24:32])[0]
        meth = struct.unpack("<Q", buf[32:40])[0]
        exc = struct.unpack("<Q", buf[40:48])[0]
        image2 = struct.unpack("<Q", buf[0x38:0x40])[0]
        desc = struct.unpack("<Q", buf[0x40:0x48])[0]
        meth_b = struct.unpack("<Q", buf[0x48:0x50])[0]
        log.append("    status=%d image=0x%X assembly=0x%X class=0x%X method=0x%X exc=0x%X"
                   % (status, image, assembly, klass, meth, exc))
        log.append("    image2=0x%X desc=0x%X descMethod=0x%X"
                   % (image2, desc, meth_b))
        if not read_ok:
            log.append("[!] 结果区尚未可靠读回，以上地址及状态不可作成功依据")
        # mono_image_open_from_data_with_name 的 need_copy=1，它会自己复制一份到自己的堆，
        # 原始字节只在加载瞬间需要。加载完成后立刻抹掉：进程里再搜不到这 36KB 的 PE 头/
        # 类型名/metadata，磁盘和内存都不留可扫的东西。
        if image and read_ok:
            zeros = b"\x00" * len(blob)
            w2 = ctypes.c_size_t(0)
            if k32.WriteProcessMemory(h, ctypes.c_void_p(remote + L_DATA), zeros, len(zeros),
                                      ctypes.byref(w2)):
                log.append("[*] 已抹除数据区里的程序集字节（%d 字节，mono 已复制）" % len(zeros))
            else:
                log.append("[!] 抹除程序集字节失败，错误 %d" % ctypes.get_last_error())
        # 用完立刻把两块私有内存整块释放（MEM_RELEASE）。
        # 私有 + 可执行、又不对应任何模块镜像的内存页，是内存扫描器最直接的注入特征：
        # 进程里凭空多出一页 RX，首字节是 mov rax,imm64 / jmp rax，一眼就是外挂。
        # mono 已经把程序集复制到自己的堆里，stub 也已执行完，这两块留着没有任何用处，
        # 释放后进程的虚拟内存布局与注入前完全一致。
        released = []
        if thread_finished and read_ok:
            # MEM_RELEASE 要求 dwSize 必须为 0，整块按分配时的 BaseAddress 一起还回去
            for label, addr in (("代码区", code_page), ("数据区", remote)):
                try:
                    if k32.VirtualFreeEx(h, ctypes.c_void_p(addr), 0, MEM_RELEASE):
                        released.append(label)
                    else:
                        log.append("[!] 释放%s 0x%X 失败，错误 %d"
                                   % (label, addr, ctypes.get_last_error()))
                except Exception as exc:
                    log.append("[!] 释放%s 异常: %r" % (label, exc))
            trace_event(pid, attempt, "memory.release", released=released)
            if released:
                log.append("[*] 已释放远程内存: %s（进程里不留匿名可执行页）"
                           % "、".join(released))
                code_page = None
                remote = None

        payload_started = _payload_start_logged(pid, proof_paths, proof_offsets)
        trace_event(pid, attempt, "payload.marker", found=payload_started)
        log.append("[*] 载荷入口日志确认=%s" % payload_started)
        ok = bool(read_ok and thread_finished and process_alive and image and meth
                  and not exc and payload_started)
        if status not in (0, 1):
            log.append("[!] mono 打开 image 失败 status=%d" % status)
        if not klass and not meth_b:
            log.append("[!] 类和方法都没找到：载荷元数据或命名空间对不上")
        if not meth:
            log.append("[!] 找不到方法 Load()")
        if exc:
            log.append("[!] 托管侧抛异常 exc=0x%X（看 risk_inject.log）" % exc)
        if not thread_finished:
            log.append("[!] 远程线程未正常结束，保留数据区供线程使用")
        if not process_alive:
            log.append("[!] 目标进程在注入过程中退出")
        if not payload_started:
            log.append("[!] 未见 Loader.Load 启动记录，不能判定载荷运行成功")
        log.append("[%s] %s" % ("OK" if ok else "FAIL",
                                "载荷入口已执行（不代表本局稳定）" if ok else "注入未达成"))
        trace_event(pid, attempt, "result", ok=ok, status=status, image=hex(image),
                    method=hex(meth), exception=hex(exc), process_alive=process_alive)
        return {"ok": ok, "attempted": True, "log": log, "status": status, "image": image,
                "klass": klass, "method": meth, "exc": exc, "remote": remote,
                "code": code_page, "prot": prot if code_page is None else protect_of(h, code_page),
                "released": released}
    except Exception as e:
        trace_event(pid, attempt, "error", type=type(e).__name__, message=str(e),
                    attempted=attempted, winerror=ctypes.get_last_error())
        if attempted:
            raise InjectionAttempted(e, log) from e
        raise
    finally:
        k32.CloseHandle(h)
        if attempted:
            observer = threading.Thread(target=_observe_process_exit, args=(pid, attempt),
                                        name="ssjj-injection-observer", daemon=True)
            observer.start()


def _csc():
    r"""在已安装的 dotnet SDK 里找最新的 Roslyn csc.dll。"""
    root = r"C:\Program Files\dotnet\sdk"
    best = None
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return None
    for name in names:
        p = os.path.join(root, name, "Roslyn", "bincore", "csc.dll")
        if os.path.isfile(p):
            best = (name, p)
    return best[1] if best else None


def _game_refs():
    if not os.path.isdir(GAME_MANAGED):
        return []
    return [os.path.join(GAME_MANAGED, n) for n in GAME_REFS
            if os.path.isfile(os.path.join(GAME_MANAGED, n))]


def csc_build(out_path=None):
    r"""引用「游戏自带 mscorlib」编译载荷 —— 这是唯一跑得通的路线。

    实测对比（同一个 stub、同一个进程）：
      * net48 第三方程序集（SkyCoreUtils.dll）：class 和 method 都查得到，invoke 成功
      * 我们的 netstandard2.0 载荷：mono_class_from_name 返回 0，desc 路线也返回 0
    原因：游戏 Mono 的 Managed 目录里根本没有 netstandard.dll 门面（只有 mscorlib /
    System / System.Core），netstandard 目标的类型基类解析不出来，类创建直接失败。
    所以这里用 csc + -nostdlib 直接吃游戏的 mscorlib/System/System.Core。
    """
    outs = []
    csc = _csc()
    refs = _game_refs()
    srcs = sorted(os.path.join(PAYLOAD_DIR, f) for f in os.listdir(PAYLOAD_DIR)
                  if f.lower().endswith(".cs"))
    if not csc:
        return None, ["[!] 找不到 csc（dotnet SDK 的 Roslyn 目录）"]
    if not srcs:
        return None, ["[!] 找不到载荷源码: %s" % PAYLOAD_DIR]
    if len(refs) < 2:
        return None, ["[!] 游戏 Managed 目录里找不到引用程序集: %s" % GAME_MANAGED]
    out_path = out_path or PAYLOAD_DLL
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    cmd = ["dotnet", "exec", csc, "-noconfig", "-nostdlib+", "-nologo",
           "-target:library", "-langversion:7.3", "-optimize+", "-deterministic+",
           "-out:" + out_path]
    cmd += ["-r:" + r for r in refs]
    cmd += srcs
    r = subprocess.run(cmd, capture_output=True)
    txt = ((r.stdout or b"") + (r.stderr or b"")).decode("mbcs", "replace")
    lines = [l for l in txt.splitlines() if l.strip()]
    outs.append("[*] csc: %s" % csc)
    outs.append("[*] 引用: %s" % ", ".join(os.path.basename(x) for x in refs))
    if r.returncode != 0 or not os.path.isfile(out_path):
        return None, outs + ["[!] csc 编译失败"] + lines[-16:]
    outs += lines[-6:]
    outs.append("[*] 载荷编译完成: %s (%d 字节)" % (out_path, os.path.getsize(out_path)))
    _publish_payload()
    return out_path, outs


def build_payload(force=False):
    out = []
    if getattr(sys, "frozen", False):
        found = payload_path()
        if found:
            out.append("[*] 打包运行，使用现有载荷: %s" % found)
            return out, found
    if os.path.isfile(PAYLOAD_DLL) and not force:
        out.append("[*] 载荷已存在: %s" % PAYLOAD_DLL)
        _publish_payload()
        return out, PAYLOAD_DLL
    if not os.path.isfile(os.path.join(PAYLOAD_DIR, "Loader.cs")):
        found = payload_path()
        if found:
            out.append("[*] 没有源码（打包运行），用预编译载荷: %s" % found)
            return out, found
        raise FileNotFoundError("找不到载荷源码，也没有预编译载荷: %s" % PAYLOAD_DIR)

    built, lines = csc_build()
    out += lines
    if built:
        return out, built

    # 兜底：csc 不可用时退回 dotnet build（netstandard2.0 产物在游戏里查不到类，
    # 只用于应急编译验证，正式载荷必须走 csc 路线）
    out.append("[!] 退到 dotnet build 兜底编译")
    r = subprocess.run(["dotnet", "build", "-c", "Release", "-v", "m"],
                       cwd=PAYLOAD_DIR, capture_output=True)
    tail = (r.stdout or b"").decode("mbcs", "replace").strip().splitlines()
    out += tail[-6:]
    if r.returncode != 0 or not os.path.isfile(PAYLOAD_DLL_LEGACY):
        raise RuntimeError("载荷编译失败:\n%s" % "\n".join(tail[-20:]))
    out.append("[*] 兜底产物: %s (%d 字节)"
               % (PAYLOAD_DLL_LEGACY, os.path.getsize(PAYLOAD_DLL_LEGACY)))
    return out, PAYLOAD_DLL_LEGACY


def _publish_payload():
    """把编译产物复制到 <数据目录>\risk_payload\，供打包版运行时使用。"""
    if getattr(sys, "frozen", False):
        return None
    try:
        from .paths import PAYLOAD_DIR as dest_dir
    except Exception:
        return None
    try:
        if os.path.abspath(dest_dir).lower() == os.path.abspath(PAYLOAD_DIR).lower():
            return None
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, ASM_NAME)
        src = payload_path()
        if not src:
            return None
        blob = open(src, "rb").read()
        old = None
        try:
            old = open(dest, "rb").read()
        except Exception:
            pass
        if old != blob:
            open(dest, "wb").write(blob)
        return dest
    except Exception:
        return None


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "status"
    if cmd == "status":
        pids = list_processes(DEFAULT_TARGET)
        print("战斗端进程: %s" % (pids or "未运行"))
        for p in pids:
            try:
                mono = find_mono(p["pid"])
                print("  pid=%d mono=%s" % (p["pid"], mono["name"] if mono else "未加载"))
            except Exception as e:
                print("  pid=%d 读取模块失败: %r" % (p["pid"], e))
        cur = payload_path()
        print("载荷: %s" % ("%s（%d 字节）" % (cur, os.path.getsize(cur)) if cur else "未编译"))
        return
    if cmd == "build":
        lines, _ = build_payload(force="--force" in argv)
        print("\n".join(lines))
        return
    if cmd == "inject":
        pid = find_pid(DEFAULT_TARGET)
        if not pid:
            print("[!] %s 没在运行" % DEFAULT_TARGET)
            return
        res = inject(pid)
        print("\n".join(res["log"]))
        return
    print("用法: python -m risk.injector [status|build|inject]")


if __name__ == "__main__":
    main()
