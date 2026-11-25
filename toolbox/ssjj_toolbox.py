# -*- coding: utf-8 -*-
"""生死狙击工具箱入口。

  ssjj_toolbox.exe                      打开主界面
  ssjj_toolbox.exe --accel-daemon       后台加速服务（守护 + 服务同进程）
  ssjj_toolbox.exe --risk-inject <pid>  只做一次注入（用提权副本调用，做完就退）
"""
import os
import sys
import threading
import time


class _Null:
    def write(self, s):
        return len(s)

    def flush(self):
        pass


def _fix_stdio():
    if sys.stdout is None:
        sys.stdout = _Null()
    if sys.stderr is None:
        sys.stderr = _Null()
    if getattr(sys, "frozen", False):
        def hook(exc_type, exc, tb):
            try:
                import traceback
                import backend
                with open(os.path.join(str(backend.DATA), "toolbox_error.log"),
                          "a", encoding="utf-8") as f:
                    traceback.print_exception(exc_type, exc, tb, file=f)
            except Exception:
                pass
        sys.excepthook = hook


def _risk_inject_once(pid):
    """提权副本的活：注入一次，把结果写进 risk_inject_result.json，然后退出。

    退出码 0 = 成功。主界面那边轮询结果文件，所以这里绝不能弹窗、不能留窗口。
    """
    from risk import guard
    ok = False
    detail = ""
    try:
        ok, detail = guard.inject_now(pid=pid, allow_elevate=False)
    except Exception as e:
        detail = repr(e)
    try:
        guard.write_result(pid, ok, detail if isinstance(detail, str) else str(detail))
    except Exception:
        pass
    return 0 if ok else 1


def _install_quit_guard(app):
    """退出兜底：1.5 秒后强制 os._exit，避免守护线程/Qt 子对象把退出拖死。

    普通退出路径先跑完（窗口 close、事件循环返回、sys.exit），只有它卡住时
    这个看门线程才会真的开枪。
    """
    def arm():
        def shoot():
            # 先等收尾线程把「后台已全部停止」做完（最多 20 秒），再硬退。
            # 以前是固定 1.5 秒后 os._exit，正好把 shutdown_all 掐在半路：
            # 注入守护停了、风险模式停了，加速守护和 hosts 还原根本没跑到。
            for _ in range(100):
                try:
                    if app.property("ssjj_shutdown_done"):
                        break
                except Exception:
                    break
                time.sleep(0.2)
            try:
                os._exit(0)
            except Exception:
                pass
        t = threading.Thread(target=shoot, name="ssjj-hard-exit", daemon=True)
        t.start()
    try:
        app.aboutToQuit.connect(arm)
    except Exception:
        pass


def _selfcheck():
    """打包版自检：确认静态换肤要用的模块和清单真的进包了。

    结果写到 %LOCALAPPDATA%\\ssjj-toolbox\\selfcheck.log（打包版没有控制台，
    这里也不 import backend，避免导入失败时连日志都写不出来）。
    """
    base = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "ssjj-toolbox")
    try:
        os.makedirs(base, exist_ok=True)
    except Exception:
        base = os.path.dirname(sys.executable)
    log = os.path.join(base, "selfcheck.log")
    lines = ["argv=%r" % (sys.argv,), "frozen=%s" % getattr(sys, "frozen", False),
             "exe=%s" % sys.executable, "meipass=%s" % getattr(sys, "_MEIPASS", None)]
    try:
        import skin_pack as SP
        lines.append("index entries=%d" % len(SP.index()))
        lines.append("ak47 skins=%d" % len(SP.available_skins("ak47")))
        p = SP.bundle_path("weapon/ak47")
        lines.append("ak47 bundle=%s exists=%s" % (p, bool(p and p.is_file())))
        import UnityPy
        lines.append("UnityPy=%s" % getattr(UnityPy, "__version__", "?"))
        if p and p.is_file():
            env = UnityPy.load(str(p))
            tex = [o for o in env.objects if o.type.name == "Texture2D"]
            lines.append("textures=%d" % len(tex))
            if tex:
                d = tex[0].read()
                lines.append("first=%s %dx%d fmt=%s" % (d.m_Name, d.m_Width, d.m_Height, d.m_TextureFormat))
                lines.append("decoded=%s" % (d.image.size,))
        import oneclick
        lines.append("oneclick modes=%s" % (list(oneclick.MODE_NAMES),))
        # 真跑一次静态换肤再还原，确认打包版能改也能回
        try:
            import skin_pack as SP2
            p2 = SP2.bundle_path("weapon/ak47")
            if p2 and p2.is_file():
                before = p2.stat().st_size
                ok2, msg2 = SP2.apply_weapon_skin("ak47", "raise")
                mid = p2.stat().st_size
                n2 = SP2.restore_all()
                after = p2.stat().st_size
                lines.append("swap ok=%s %d->%d restored=%d back=%d msg=%s" % (ok2, before, mid, n2, after, msg2))
        except Exception:
            import traceback
            lines.append("swap FAIL\n" + traceback.format_exc())
        # 离屏建一次主窗口，确认默认落在「一键启动」页
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        from PySide6.QtWidgets import QApplication
        from ui.main_window import MainWindow
        app = QApplication([])
        win = MainWindow()
        lines.append("current=%s widget=%s nav=%s" % (
            win._current, type(win.stack.currentWidget()).__name__,
            [k for k in win.nav_buttons]))
        one = win.pages.get("one")
        if one is not None:
            lines.append("one combos weapons=%d skins=%d" % (one.cmb_weapon.count(), one.cmb_skin.count()))
            try:
                import backend
                d = {"cat": backend.skin_catalog(), "st": oneclick.status(), "admin": backend.is_admin()}
                one._apply(d)
                lines.append("after apply weapons=%d cur=%s skins=%d skin0=%s cat=%d cattop=%s" % (
                    one.cmb_weapon.count(), one.cmb_weapon.currentData(),
                    one.cmb_skin.count(), one.cmb_skin.itemText(0),
                    one.cmb_cat.count(), one.cmb_cat.itemText(1) if one.cmb_cat.count() > 1 else ""))
            except Exception:
                import traceback
                lines.append("apply FAIL\n" + traceback.format_exc())
        win.deleteLater()
        app.quit()
    except Exception as exc:
        import traceback
        lines.append("FAIL %r" % (exc,))
        lines.append(traceback.format_exc())
    try:
        with open(log, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except Exception:
        pass
    return 0


_SINGLETON_MUTEX = None


def _kill_other_gui_instances(dry_run=False):
    """关掉所有别的工具箱 GUI（跨目录），只放行本次父子进程和后台辅助进程。

    以前只认同目录，于是 release\生死工具箱1.1 和 release\生死工具箱last 各起一份，
    两个 GUI 就有两个注入守护抢同一个战斗端（日志里「已由另一个守护进程接管」）。
    """
    import subprocess
    folder = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
              else os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    env = os.environ.copy()
    env["SSJJ_TOOLBOX_CLEANUP_DIR"] = folder
    env["SSJJ_TOOLBOX_CLEANUP_ME"] = str(os.getpid())
    env["SSJJ_TOOLBOX_CLEANUP_PARENT"] = str(os.getppid())
    env["SSJJ_TOOLBOX_CLEANUP_DRY_RUN"] = "1" if dry_run else "0"
    ps = r"""
$folder = [IO.Path]::GetFullPath($env:SSJJ_TOOLBOX_CLEANUP_DIR).TrimEnd('\')
$me = [int]$env:SSJJ_TOOLBOX_CLEANUP_ME
$parent = [int]$env:SSJJ_TOOLBOX_CLEANUP_PARENT
Get-CimInstance Win32_Process | Where-Object {
    $_.ProcessId -ne $me -and $_.ProcessId -ne $parent -and
    $_.Name -match '^(生死狙击工具箱|生死工具箱)' -and
    $_.Name.EndsWith('.exe', [StringComparison]::OrdinalIgnoreCase) -and
    (-not $_.CommandLine -or $_.CommandLine -notmatch '--accel-daemon|--risk-inject')
} | ForEach-Object {
    try {
        if ($env:SSJJ_TOOLBOX_CLEANUP_DRY_RUN -ne '1') {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop
        }
        [string]$_.ProcessId
    } catch {}
}
"""
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=10, env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return [int(line) for line in result.stdout.splitlines() if line.strip().isdigit()]
    except Exception:
        return []


def _ensure_single_instance():
    """启动时自动清理同目录旧版，随后取得单实例锁。"""
    global _SINGLETON_MUTEX
    _kill_other_gui_instances()
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        k32.CloseHandle.restype = wintypes.BOOL
        name = "Local\\ssjj_toolbox_gui_singleton"
        # 旧版被终止后，PyInstaller 父进程可能需要片刻才释放句柄。
        for _ in range(10):
            h = k32.CreateMutexW(None, False, name)
            err = ctypes.get_last_error()
            if h and err != 183:
                _SINGLETON_MUTEX = h
                return True
            if h:
                k32.CloseHandle(h)
            time.sleep(0.2)
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.warning(None, "工具箱已在运行",
                            "旧实例未能退出，请检查任务管理器中的工具箱进程。")
        return False
    except Exception:
        return False


def main():
    _fix_stdio()
    argv = sys.argv[1:]

    if "--selfcheck" in argv:
        sys.exit(_selfcheck())

    if "--accel-daemon" in argv:
        import backend
        backend.accel_daemon_main()
        return

    if "--risk-inject" in argv:
        i = argv.index("--risk-inject")
        pid = int(argv[i + 1]) if i + 1 < len(argv) else 0
        sys.exit(_risk_inject_once(pid))

    import diagnostics
    diagnostics.start()
    old_hook = sys.excepthook

    def _log_exception(exc_type, exc, tb):
        import traceback
        diagnostics.record("未处理异常 " + "".join(
            traceback.format_exception(exc_type, exc, tb)))
        old_hook(exc_type, exc, tb)

    sys.excepthook = _log_exception
    old_thread_hook = threading.excepthook

    def _log_thread_exception(args):
        import traceback
        diagnostics.record("线程异常 %s %s" % (
            getattr(args.thread, "name", "?"),
            "".join(traceback.format_exception(args.exc_type, args.exc_value,
                                               args.exc_traceback))))
        old_thread_hook(args)

    threading.excepthook = _log_thread_exception
    from PySide6.QtWidgets import QApplication

    from ui.main_window import MainWindow
    from ui.theme import apply_theme

    app = QApplication(sys.argv)
    app.setApplicationName("生死狙击工具箱")
    app.setOrganizationName("ssjj-tools")
    apply_theme(app)
    if "--startup-smoke" in argv:
        win = MainWindow()
        diagnostics.record("启动自检通过：主窗口已创建")
        diagnostics.finish()
        return 0
    if not _ensure_single_instance():
        diagnostics.finish()
        sys.exit(0)
    try:
        import backend as _bk
        threading.Thread(target=_bk.startup_housekeeping,
                         name="ssjj-housekeeping", daemon=True).start()
    except Exception:
        pass
    _install_quit_guard(app)
    win = MainWindow()
    win.show()
    result = app.exec()
    diagnostics.finish()
    sys.exit(result)


if __name__ == "__main__":
    main()
