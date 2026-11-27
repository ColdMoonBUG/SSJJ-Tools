using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Threading;

namespace SsjjRisk
{
    /// <summary>
    /// 退出保护：在游戏真正退出之前，先把自己改过的补丁字节全部还原，
    /// 再留一小段等待时间让 GameGuard(npggNT64) 的收尾扫描跑完，然后才放行退出。
    ///
    /// 为什么需要：GameGuard 在进程收尾阶段会重扫代码段，发现入口被写成
    /// `48 B8 &lt;addr&gt; FF E0` 就主动下发 STATUS_SINGLE_STEP(0x80000004) 杀进程，
    /// 表现就是「退出时偶发崩溃」。挂在 ProcessExit 太晚（那时扫描已经开始），
    /// 所以要抢在退出流程启动前还原。
    ///
    /// 触发点优先级：
    ///   1) UnityEngine.Application.wantsToQuit（Unity 2018.1+，最干净，不动原生）
    ///   2) 主窗口 WndProc 拦截 WM_CLOSE / WM_QUERYENDSESSION（可选，默认关：
    ///      挂窗口过程本身也是反作弊会看的点，只有 wantsToQuit 不可用时才建议开）
    /// </summary>
    public static class ExitGuard
    {
        static readonly object _lock = new object();
        static bool _installed;
        static bool _wantsHooked;
        static bool _quittingHooked;
        static bool _wndProcHooked;
        static bool _processExitHooked;
        static bool _held;
        static Delegate _wantsDelegate;
        static Delegate _quittingDelegate;
        static EnumProc _enumProc;
        static WndProcDelegate _wndProc;
        static readonly Dictionary<IntPtr, IntPtr> _orig = new Dictionary<IntPtr, IntPtr>();

        const string FLAG_HOLD = "risk_exit_hold.txt";
        const string FLAG_DELAY = "risk_exit_delay.txt";
        const string FLAG_WNDPROC = "risk_exit_wndproc.txt";

        const uint WM_CLOSE = 0x0010;
        const uint WM_QUERYENDSESSION = 0x0011;
        const int GWLP_WNDPROC = -4;

        static string Dir
        {
            get
            {
                string d = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
                return Path.Combine(Path.Combine(d, "ssjj-toolbox"), "");
            }
        }

        static string FlagPath(string name) { return Path.Combine(Dir, name); }

        /// <summary>默认开：只有显式写 "off"/"0"/"false" 才关。</summary>
        public static bool Enabled()
        {
            try
            {
                string p = FlagPath(FLAG_HOLD);
                if (!File.Exists(p)) return true;
                string t = (File.ReadAllText(p) ?? "").Trim().ToLowerInvariant();
                return !(t == "off" || t == "0" || t == "false" || t == "no");
            }
            catch { return true; }
        }

        public static double Delay()
        {
            try
            {
                string p = FlagPath(FLAG_DELAY);
                if (!File.Exists(p)) return 2.0;
                double v;
                if (!double.TryParse((File.ReadAllText(p) ?? "").Trim(), out v)) return 2.0;
                if (v < 0) v = 0;
                if (v > 15) v = 15;
                return v;
            }
            catch { return 2.0; }
        }

        static bool UseWndProc()
        {
            try { return File.Exists(FlagPath(FLAG_WNDPROC)); } catch { return false; }
        }

        public static bool Install(bool allowWndProcFallback = true)
        {
            if (!Enabled())
            {
                lock (_lock)
                {
                    if (!_installed)
                    {
                        _installed = true;
                        Loader.Log("[exit] 退出保护已关闭（配置里 exit_hold=off）");
                    }
                }
                return true;
            }
            lock (_lock)
            {
                if (_installed) return true;
            }

            if (!_wantsHooked)
            {
                bool ok = HookWantsToQuit();
                if (ok)
                {
                    _wantsHooked = true;
                    Loader.Log("[exit] 已挂 Application.wantsToQuit：退出前先还原补丁");
                }
                else
                {
                    Loader.LogQ("[exit] wantsToQuit 暂未就绪（UnityEngine 未加载或反射失败）");
                }
            }

            if (!_quittingHooked)
            {
                bool ok2 = HookQuitting();
                if (ok2)
                {
                    _quittingHooked = true;
                    Loader.Log("[exit] 已挂 Application.quitting：游戏主动退出时也会先还原");
                }
                else
                {
                    Loader.LogQ("[exit] quitting 事件暂未就绪");
                }
            }

            if (UseWndProc() && !_wndProcHooked)
            {
                int n = HookWindows();
                if (n > 0) _wndProcHooked = true;
                Loader.Log("[exit] WndProc 兜底已挂 " + n + " 个窗口（可选，默认关）");
            }
            else if (allowWndProcFallback && !_wantsHooked && !_wndProcHooked)
            {
                int n = HookWindows();
                if (n > 0) _wndProcHooked = true;
                Loader.Log("[exit] 只剩 WndProc 兜底，已挂 " + n + " 个窗口");
            }

            if (!_processExitHooked)
            {
                try
                {
                    AppDomain.CurrentDomain.ProcessExit += delegate { Restore("ProcessExit"); };
                    _processExitHooked = true;
                }
                catch { }
            }

            // 只有真正挂上 wantsToQuit（或显式启用了 WndProc 且挂到窗口）才算安装完成；
            // 否则返回 false 交由 StartPatchRetry 继续重试，避免早期注入时假成功。
            bool ready = _wantsHooked || (UseWndProc() && _wndProcHooked);
            if (ready)
            {
                lock (_lock) { _installed = true; }
            }
            return ready;
        }

        static bool HookWantsToQuit()
        {
            try
            {
                Type app = null;
                foreach (Assembly asm in AppDomain.CurrentDomain.GetAssemblies())
                {
                    try
                    {
                        Type t = asm.GetType("UnityEngine.Application", false);
                        if (t != null) { app = t; break; }
                    }
                    catch { }
                }
                if (app == null) return false;

                EventInfo ev = app.GetEvent("wantsToQuit", BindingFlags.Public | BindingFlags.Static);
                if (ev == null) return false;

                MethodInfo mi = typeof(ExitGuard).GetMethod("OnWantsToQuit",
                    BindingFlags.Public | BindingFlags.Static);
                if (mi == null) return false;

                _wantsDelegate = Delegate.CreateDelegate(ev.EventHandlerType, mi);
                ev.AddEventHandler(null, _wantsDelegate);
                return true;
            }
            catch (Exception e)
            {
                Loader.Log("[exit] wantsToQuit 异常: " + e.GetType().Name + " " + e.Message);
                return false;
            }
        }

        /// <summary>Unity 的 wantsToQuit 是 Func&lt;bool&gt;：返回 true 才允许退出。</summary>
        public static bool OnWantsToQuit()
        {
            Restore("wantsToQuit");
            return true;
        }

        /// <summary>
        /// Application.quitting：Unity 2017+ 的静态事件，在退出流程最开头触发。
        /// 游戏自己调 Application.Quit()（例如 GameguardDataHandler 认证失败、
        /// 服务端下发结束、返回大厅）时，走的是这条而不是 wantsToQuit，
        /// 实测对局结束时 wantsToQuit 从来没被调用过，所以把这条也挂上。
        /// </summary>
        static bool HookQuitting()
        {
            try
            {
                Type app = FindApplicationType();
                if (app == null) return false;
                EventInfo ev = app.GetEvent("quitting", BindingFlags.Public | BindingFlags.Static);
                if (ev == null) return false;

                Type dt = ev.EventHandlerType;
                if (dt == null) return false;
                int argc = dt.GetMethod("Invoke") != null ? dt.GetMethod("Invoke").GetParameters().Length : 0;
                MethodInfo mi = typeof(ExitGuard).GetMethod(
                    argc == 0 ? "OnQuittingNoArg" : "OnQuittingArgs",
                    BindingFlags.Public | BindingFlags.Static);
                if (mi == null) return false;

                _quittingDelegate = Delegate.CreateDelegate(dt, mi);
                ev.AddEventHandler(null, _quittingDelegate);
                return true;
            }
            catch (Exception e)
            {
                Loader.Log("[exit] quitting 异常: " + e.GetType().Name + " " + e.Message);
                return false;
            }
        }

        public static void OnQuittingNoArg()
        {
            Restore("quitting");
        }

        public static void OnQuittingArgs(object sender, EventArgs e)
        {
            Restore("quitting");
        }

        static Type FindApplicationType()
        {
            try
            {
                foreach (Assembly asm in AppDomain.CurrentDomain.GetAssemblies())
                {
                    try
                    {
                        Type t = asm.GetType("UnityEngine.Application", false);
                        if (t != null) return t;
                    }
                    catch { }
                }
            }
            catch { }
            return null;
        }

        /// <summary>还原补丁 + 等待。只生效一次，可重复调用。</summary>
        public static void Restore(string reason)
        {
            lock (_lock)
            {
                if (_held) return;
                _held = true;
            }
            try
            {
                UnhookWindows();
                int n = Loader.RestoreEverything();
                double d = Delay();
                Loader.Log("[exit] 退出保护触发(" + reason + ")：已还原 " + n +
                           " 个补丁，等待 " + d.ToString("0.0") + "s 让 GameGuard 收尾");
                if (d > 0) Thread.Sleep((int)(d * 1000));
                Loader.Log("[exit] 等待结束，放行退出");
            }
            catch (Exception e)
            {
                Loader.Log("[exit] 还原异常: " + e.GetType().Name + " " + e.Message);
            }
        }

        static void UnhookWindows()
        {
            if (IntPtr.Size != 8 || _orig.Count == 0) return;
            try
            {
                foreach (KeyValuePair<IntPtr, IntPtr> kv in _orig)
                {
                    if (kv.Key != IntPtr.Zero && kv.Value != IntPtr.Zero)
                        SetWindowLongPtrW(kv.Key, GWLP_WNDPROC, kv.Value);
                }
                _orig.Clear();
            }
            catch { }
        }

        // ------------------------------------------------------------ WndProc 兜底
        delegate bool EnumProc(IntPtr hWnd, IntPtr lParam);
        delegate IntPtr WndProcDelegate(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);

        [DllImport("user32.dll")]
        static extern bool EnumWindows(EnumProc cb, IntPtr lParam);

        [DllImport("user32.dll")]
        static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);

        [DllImport("user32.dll")]
        static extern bool IsWindowVisible(IntPtr hWnd);

        [DllImport("user32.dll", SetLastError = true)]
        static extern IntPtr SetWindowLongPtrW(IntPtr hWnd, int nIndex, IntPtr dwNewLong);

        [DllImport("user32.dll", SetLastError = true)]
        static extern int SetWindowLongW(IntPtr hWnd, int nIndex, int dwNewLong);

        [DllImport("user32.dll")]
        static extern IntPtr CallWindowProcW(IntPtr prev, IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);

        static int HookWindows()
        {
            if (IntPtr.Size != 8) return 0;    // 只做 64 位战斗端
            int count = 0;
            try
            {
                uint me = (uint)System.Diagnostics.Process.GetCurrentProcess().Id;
                _enumProc = delegate(IntPtr h, IntPtr p)
                {
                    uint pid;
                    GetWindowThreadProcessId(h, out pid);
                    if (pid != me) return true;
                    if (!IsWindowVisible(h)) return true;
                    IntPtr prev;
                    if (!_orig.ContainsKey(h))
                    {
                        prev = SetWindowLongPtrW(h, GWLP_WNDPROC, Marshal.GetFunctionPointerForDelegate(_wndProc));
                        if (prev != IntPtr.Zero)
                        {
                            _orig[h] = prev;
                            count++;
                        }
                    }
                    return true;
                };
                _wndProc = delegate(IntPtr h, uint msg, IntPtr w, IntPtr l)
                {
                    IntPtr prev;
                    _orig.TryGetValue(h, out prev);
                    if (msg == WM_CLOSE || msg == WM_QUERYENDSESSION) Restore("wndproc:0x" + msg.ToString("X"));
                    if (prev != IntPtr.Zero)
                        return CallWindowProcW(prev, h, msg, w, l);
                    return IntPtr.Zero;
                };
                EnumWindows(_enumProc, IntPtr.Zero);
            }
            catch (Exception e)
            {
                Loader.Log("[exit] WndProc 失败: " + e.GetType().Name + " " + e.Message);
            }
            return count;
        }
    }
}
