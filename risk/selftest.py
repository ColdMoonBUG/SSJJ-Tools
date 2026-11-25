# -*- coding: utf-8 -*-
"""注入 plumbing 自检：远程内存写入 + 远程线程执行 + 结果读回 是否通。

不碰游戏，随便起一个长跑进程当靶子，跑一段「把魔数写进 scratch 再 return」
的极小 stub。验证的是 injector 的底层 plumbing（OpenProcess / VirtualAllocEx /
WriteProcessMemory / CreateRemoteThread / ReadProcessMemory），不是 mono 本身。
改注入器之后先跑这个，能立刻区分「plumbing 坏了」和「mono 交互坏了」。
"""
import ctypes
import struct
import subprocess
import sys
import time

from . import injector as I
from .injector import k32, wt, MEM_COMMIT, MEM_RESERVE, PAGE_EXECUTE_READWRITE

MAGIC = 0x53534A4A  # "SSJJ"


def main():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        time.sleep(1.0)
        pid = child.pid
        print("[*] 靶子 pid=%d" % pid)
        h = k32.OpenProcess(I.PROCESS_ALL_ACCESS, False, pid)
        if not h:
            print("[!] OpenProcess 失败 %d" % ctypes.get_last_error())
            return 1
        try:
            remote = k32.VirtualAllocEx(h, None, 0x1000, MEM_COMMIT | MEM_RESERVE,
                                        PAGE_EXECUTE_READWRITE)
            print("[*] 分配 0x%X" % remote)
            # 汇编：mov qword ptr [rip+disp] ... 太麻烦，直接用手写编码
            # 48 C7 04 25 <addr64> <imm32>  —— mov qword ptr [addr], imm32 不合法
            # 用 mov rax, imm64(scratch); mov rdx, imm64(MAGIC); mov [rax], rdx; ret
            code = bytearray()
            code += b"\x48\xB8" + struct.pack("<Q", remote + 0x800)      # mov rax, scratch
            code += b"\x48\xBA" + struct.pack("<Q", MAGIC)               # mov rdx, MAGIC
            code += b"\x48\x89\x10"                                       # mov [rax], rdx
            code += b"\x48\xB8" + struct.pack("<Q", MAGIC)               # mov rax, MAGIC
            code += b"\xC3"                                               # ret
            buf = bytes(code) + b"\x00" * (0x800 - len(code))
            written = ctypes.c_size_t(0)
            if not k32.WriteProcessMemory(h, ctypes.c_void_p(remote), buf, len(buf),
                                          ctypes.byref(written)):
                print("[!] WriteProcessMemory 失败 %d" % ctypes.get_last_error())
                return 1
            print("[*] 写入 %d 字节" % written.value)
            tid = wt.DWORD(0)
            th = k32.CreateRemoteThread(h, None, 0, ctypes.c_void_p(remote), None, 0,
                                        ctypes.byref(tid))
            if not th:
                print("[!] CreateRemoteThread 失败 %d" % ctypes.get_last_error())
                return 1
            k32.WaitForSingleObject(th, 5000)
            code_out = wt.DWORD(0)
            k32.GetExitCodeThread(th, ctypes.byref(code_out))
            k32.CloseHandle(th)
            print("[*] 远程线程退出码 0x%X（应为 0x%X）" % (code_out.value, MAGIC))
            scratch = (ctypes.c_ubyte * 8)()
            got = ctypes.c_size_t(0)
            k32.ReadProcessMemory(h, ctypes.c_void_p(remote + 0x800), scratch, 8,
                                  ctypes.byref(got))
            val = struct.unpack("<Q", bytes(scratch))[0]
            print("[*] scratch 读回 0x%X" % val)
            ok = val == MAGIC and code_out.value == MAGIC
            print("[%s] 远程注入 plumbing %s" % ("OK" if ok else "FAIL",
                                                "全链路可用" if ok else "有问题"))
            return 0 if ok else 1
        finally:
            k32.CloseHandle(h)
    finally:
        child.kill()


if __name__ == "__main__":
    sys.exit(main())
