# -*- coding: utf-8 -*-
"""风险模式统一命令行：python -m risk <子命令>

    status              看全局状态（模式 / 注入 / 大厅 / 本地化 / 证书）
    on [缓动] [fps]     开风险模式（默认 柔和、不改帧率）
    off                 关风险模式并还原大厅
    inject              对已在跑的战斗端立即注入一次
    build               重新编译托管载荷
    lobby <缓动> [fps]  只改大厅动效（原版/轻微/柔和/丝滑/无回弹）
    lobby-off           只还原大厅
    mitm install        装根证书（443 拦截的前提）
    mitm test           自检 TLS 解密 + 本地命中
    mitm off            卸载根证书
    mitm status         证书 / 开关状态
    selftest            注入 plumbing 自检（不碰游戏）
    livetest            实机联调：自己起一次战斗端，注入并回读载荷日志
"""
import sys


USAGE = __doc__.strip()


def _fmt_pairs(d, keys):
    return "\n".join("%-14s %s" % (k, d.get(k)) for k in keys)


def cmd_status():
    from . import guard, mitm
    st = guard.status()
    print("风险模式      %s" % ("开" if st["enabled"] else "关"))
    print("注入守护      %s" % ("运行中" if st["thread_alive"] else "停止"))
    print("战斗端进程    %s" % (st["processes"] or "未运行"))
    print("已注入 pid    %s" % (st["injected_pids"] or "无"))
    lb = st["lobby"]
    print("大厅          %s" % (lb.get("name") or "--"))
    print("  变体        %s（%s）" % (lb.get("variant") or "无",
                                     "启用中" if lb.get("active") else "未启用"))
    print("  描述符      frameRate=%s renderMode=%s"
          % ((lb.get("air") or {}).get("frameRate"),
             (lb.get("air") or {}).get("renderMode")))
    loc = st["localized"]
    print("本地命中      %d / %d（%.1f%%）" % (loc["hit"], loc["hit"] + loc["miss"],
                                             loc["rate"]))
    pay = st["payload"]
    print("载荷          %s" % (("%d 字节" % pay["size"]) if pay["built"] else "未编译"))
    ms = mitm.status()
    print("MITM          %s（证书 %s，缩略码 %s）"
          % ("生效" if ms["enabled"] else "未启用",
             "已装" if ms["ca_installed"] else "未装", ms["thumbprint"]))
    last = st.get("last") or {}
    if last.get("text"):
        print("最近事件      %s" % last["text"])


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "status"
    args = argv[1:]

    if cmd in ("-h", "--help", "help"):
        print(USAGE)
        return 0

    if cmd == "status":
        cmd_status()
        return 0

    if cmd == "on":
        from . import guard
        easing = args[0] if args else None
        fps = int(args[1]) if len(args) > 1 and args[1].isdigit() else None
        ok, msgs = guard.enable(easing=easing, fps=fps)
        print("\n".join(str(m) for m in msgs))
        return 0

    if cmd == "off":
        from . import guard
        ok, msgs = guard.disable()
        print("\n".join(str(m) for m in msgs))
        return 0

    if cmd == "inject":
        from . import guard
        ok, detail = guard.inject_now()
        print(detail)
        return 0 if ok else 1

    if cmd == "build":
        from . import injector
        lines, _ = injector.build_payload(force="--force" in args)
        print("\n".join(lines))
        return 0

    if cmd == "lobby":
        from . import lobby
        easing = args[0] if args else "柔和"
        fps = int(args[1]) if len(args) > 1 and args[1].isdigit() else 0
        print("\n".join(str(m) for m in lobby.apply(easing=easing, fps=fps)))
        return 0

    if cmd == "lobby-off":
        from . import lobby
        print("\n".join(str(m) for m in lobby.restore()))
        return 0

    if cmd == "mitm":
        from . import mitm
        sub = args[0] if args else "status"
        if sub == "status":
            st = mitm.status()
            print(_fmt_pairs(st, ("enabled", "admin", "ca_exists", "ca_installed",
                                  "thumbprint", "hosts", "dir")))
            return 0
        if sub == "init":
            _, m1 = mitm.ensure_ca()
            _, _, m2 = mitm.ensure_leaf()
            print("\n".join(m1 + m2))
            return 0
        if sub == "install":
            ok, lines = mitm.install_ca()
            print("\n".join(lines))
            return 0 if ok else 1
        if sub == "test":
            r = mitm.selftest()
            print("自检 %s  响应=%r  %d 字节  %s"
                  % ("通过" if r["ok"] else "失败", r["status"], r["bytes"],
                     r["hint"] or "（TLS 解密 + 本地命中 全链路正常）"))
            return 0 if r["ok"] else 1
        if sub in ("off", "uninstall"):
            ok, lines = mitm.uninstall_ca()
            print("\n".join(lines))
            return 0 if ok else 1
        if sub == "cleanup":
            print("已删除 %d 个证书文件" % mitm.cleanup())
            return 0
        print("未知 mitm 子命令: %s" % sub)
        return 2

    if cmd == "selftest":
        from . import selftest
        return selftest.main()
    if cmd == "livetest":
        from . import livetest
        return livetest.main()

    print(USAGE)
    return 2


if __name__ == "__main__":
    sys.exit(main())
