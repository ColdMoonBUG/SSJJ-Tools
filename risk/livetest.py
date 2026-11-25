# -*- coding: utf-8 -*-
r"""实机联调：直接拉起战斗端，等 mono 加载后注入载荷，读回日志。

用途：验证「风险模式」注入链路在真实进程里成立（不是只在 python 靶子上成立）。
战斗端在没有大厅登录票据时一般会自己退出，所以这里只看两件事：
  1) mono 模块什么时候出现、注入返回什么
  2) %LOCALAPPDATA%\ssjj-toolbox\risk_inject.log 里载荷有没有真的跑起来
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from risk import injector as I  # noqa: E402

GAME = r"C:\Program Files (x86)\Wooduan\SSJJ-4399"
CANDIDATES = [
    os.path.join(GAME, "battle", "10_64", "SSJJ_BattleClient_Unity.exe"),
    os.path.join(GAME, "battle", "SSJJ_BattleClient_Unity.exe"),
]


def find_exe():
    for p in CANDIDATES:
        if os.path.isfile(p):
            return p
    for root, _dirs, files in os.walk(os.path.join(GAME, "battle")):
        for f in files:
            if f.lower() == "ssjj_battleclient_unity.exe":
                return os.path.join(root, f)
    return None


def log_tail(path, n=40):
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            return [ln.rstrip() for ln in f.readlines()[-n:]]
    except Exception as e:
        return ["<读不到日志: %r>" % (e,)]


def say(text):
    """控制台可能是 GBK，日志里有中文和不可打印字符，别让 print 把整个流程炸掉"""
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("gbk", "replace").decode("gbk", "replace"))


def main():
    exe = find_exe()
    if not exe:
        print("[!] 没找到战斗端 exe")
        return 1
    print("[*] 战斗端: %s" % exe)
    log_path = os.path.join(os.environ.get("LOCALAPPDATA", ""), "ssjj-toolbox", "risk_inject.log")
    if os.path.isfile(log_path):
        os.replace(log_path, log_path + ".prev")

    proc = subprocess.Popen([exe], cwd=os.path.dirname(exe))
    print("[*] 已启动 pid=%d，开始等 mono" % proc.pid)
    mono = None
    deadline = time.time() + 60
    while time.time() < deadline:
        if proc.poll() is not None:
            print("[!] 进程自己退出了，退出码 %s（没有登录票据时正常）" % proc.returncode)
            break
        mono = I.find_mono(proc.pid)
        if mono:
            print("[*] mono 已加载: %s base=0x%X size=%s"
                  % (mono.get("name"), mono.get("base", 0), mono.get("size")))
            break
        time.sleep(1.0)

    if not mono:
        print("[!] 等到超时也没看到 mono")
    else:
        res = I.inject(proc.pid, timeout=40)
        for ln in res.get("log") or []:
            print("  " + ln)
        print("[%s] 注入结果" % ("OK" if res.get("ok") else "FAIL"))

    time.sleep(2.5)
    print("--- risk_inject.log ---")
    for ln in log_tail(log_path):
        print("  " + ln)

    try:
        if proc.poll() is None:
            print("[*] 收尾：结束战斗端")
            proc.terminate()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
