// 风险模式托管载荷（不落盘注入战斗端 Mono 运行时）
// ---------------------------------------------------------------
// 编译： dotnet build -c Release  (netstandard2.0，不引用任何游戏程序集)
// 注入： 由 risk/injector.py 走 mono_image_open_from_data_with_name 直接从
//         内存加载本程序集，磁盘上不会出现 dll 文件 —— 因此
//           · EnumProcessModules 里没有它
//           · GetMappedFileNameW 反查不到路径
//           · WinVerifyTrust 无从校验
//         这正是 GameGuard 抓外部模块的三条主力手段。
//
// 运行时只做一件事：把战斗端自带的作弊自检链掐掉，并且不报警退出。
//   RuntimeCheatGuard   —— 后台扫描线程 + Detected 事件（触发就 GameController 退出）
//   AceClientManager    —— ACE 反作弊客户端初始化与上报
// 方法名来自 F:\ssjjgua\SSJJ-Neverlose\src\SingleSkinMod.Core\Guard\CheatGuardBypass.cs
// 与 SSJJ-Source-Only\Managed\Assembly-CSharp 里的原始实现。
using System;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

namespace SsjjRisk
{
    public static class Loader
    {
        #region 日志
        static readonly object _lock = new object();
        static string _logPath;

        static string LogPath
        {
            get
            {
                if (_logPath != null) return _logPath;
                _logPath = PickLogPath();
                return _logPath;
            }
        }

        /// <summary>
        /// 选一个「真的能追加写」的日志路径。上一版只算路径不试写，一旦写入被拒
        /// （目录 ACL、被别的进程独占、GameGuard 拦住），Log() 的 catch 会把整份
        /// 载荷日志吞成静默 —— 表现就是「注入成功但日志一行都没长」，
        /// 分不清是没跑起来还是跑了但写不进去。这里落盘前先试写一次。
        /// </summary>
        static string PickLogPath()
        {
            string[] cands;
            try
            {
                string dir = Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                    "ssjj-toolbox");
                cands = new string[]
                {
                    Path.Combine(dir, "risk_inject.log"),
                    Path.Combine(Path.GetTempPath(), "ssjj_risk.log"),
                    Path.Combine(AppDomain.CurrentDomain.BaseDirectory ?? Path.GetTempPath(), "ssjj_risk.log"),
                };
            }
            catch
            {
                cands = new string[] { Path.Combine(Path.GetTempPath(), "ssjj_risk.log") };
            }
            foreach (string p in cands)
            {
                if (p == null || p.Length == 0) continue;
                try
                {
                    string d = Path.GetDirectoryName(p);
                    if (d != null && d.Length > 0) Directory.CreateDirectory(d);
                    using (FileStream fs = new FileStream(p, FileMode.Append, FileAccess.Write,
                                                          FileShare.ReadWrite | FileShare.Delete))
                    {
                        // 试写标记：读日志时看到这行说明载荷确实进来了，只是换了文件
                        if (p != cands[0] && fs.Length == 0)
                        {
                            byte[] mark = new UTF8Encoding(false).GetBytes(
                                "=== 载荷日志回退到 " + p + "（主路径不可写）" + Environment.NewLine);
                            fs.Write(mark, 0, mark.Length);
                        }
                    }
                    return p;
                }
                catch { }
            }
            return Path.Combine(Path.GetTempPath(), "ssjj_risk.log");
        }

        static int _verboseFlag = -1; // -1=未检测, 0=静默, 1=开启日志

        /// <summary>
        /// 默认写日志：只认「显式关闭」。旧实现是「文件存在才写」，结果默认全静默，
        /// 用户那边表现成「注入成功但 risk_inject.log 一行不涨」，诊断包拿不到现场。
        /// 想要干净就写 off/0/false/no 到 risk_verbose.txt。
        /// </summary>
        static bool IsVerbose()
        {
            if (_verboseFlag >= 0) return _verboseFlag == 1;
            try
            {
                string dir = Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                    "ssjj-toolbox");
                string f = Path.Combine(dir, "risk_verbose.txt");
                if (!File.Exists(f)) { _verboseFlag = 1; return true; }
                string v = "";
                try { v = File.ReadAllText(f).Trim().ToLowerInvariant(); } catch { }
                _verboseFlag = (v == "off" || v == "0" || v == "false" || v == "no") ? 0 : 1;
            }
            catch { _verboseFlag = 1; }
            return _verboseFlag == 1;
        }

        public static void Log(string msg)
        {
            if (!IsVerbose()) return;
            lock (_lock)
            {
                string line = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss.fff") + "  " + msg + Environment.NewLine;
                // 用不带 BOM 的 UTF-8：BOM 会让外部读取工具第一行前多一个 \ufeff
                foreach (string p in new string[] { LogPath, Path.Combine(Path.GetTempPath(), "ssjj_risk.log") })
                {
                    if (p == null || p.Length == 0) continue;
                    try
                    {
                        File.AppendAllText(p, line, new UTF8Encoding(false));
                        if (p != LogPath) _logPath = p;   // 主路径挂了就永久切到备用
                        return;
                    }
                    catch { }
                }
            }
        }
        #endregion

        // 运行期依赖 mono 的 mono_compile_method 才拿得到 GetFunctionPointer，
        // 这里只用 mscorlib 公开 API + kernel32，不引第三方。
        [DllImport("kernel32.dll", SetLastError = true)]
        static extern bool VirtualProtect(IntPtr addr, UIntPtr size, uint flNewProtect, out uint lpflOldProtect);

        [DllImport("kernel32.dll", CharSet = CharSet.Ansi, SetLastError = true)]
        static extern IntPtr GetModuleHandleA(string name);

        [DllImport("kernel32.dll", CharSet = CharSet.Ansi, SetLastError = true)]
        static extern IntPtr LoadLibraryA(string name);

        /// <summary>GameGuard 的用户态模块在不在本进程里。
        /// 真会话里它比我们早加载；我们裸起测试时它要 3 秒才出现——
        /// 这条日志用来区分两种环境，别再拿裸起的结果当准。</summary>
        static string NpggState()
        {
            try
            {
                if (GetModuleHandleA("npggNT64.des") != IntPtr.Zero) return "npggNT64.des=已加载";
                if (GetModuleHandleA("npggNT.des") != IntPtr.Zero) return "npggNT.des=已加载";
                if (LoadLibraryA("npggNT64.des") != IntPtr.Zero) return "npggNT64.des=延迟加载成功";
                return "npgg=未加载（当前是裸起环境，测不出真会话的问题）";
            }
            catch { return "npgg=未知"; }
        }

        /// <summary>分阶段模式：每 stage 秒推进一步，崩在哪一步就知道是哪一步被检测。
        /// %LOCALAPPDATA%\ssjj-toolbox\risk_stage.txt 里的整数就是间隔秒数，0/不存在=一次性打完。</summary>
        static int StageDelay()
        {
            try
            {
                string p = Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                    "ssjj-toolbox", "risk_stage.txt");
                if (!File.Exists(p)) return 0;
                int v;
                if (int.TryParse(File.ReadAllText(p).Trim(), out v) && v >= 0 && v <= 60) return v;
            }
            catch { }
            return 0;
        }

        const uint PAGE_EXECUTE_READWRITE = 0x40;

        static readonly BindingFlags All = BindingFlags.Public | BindingFlags.NonPublic |
                                           BindingFlags.Static | BindingFlags.Instance |
                                           BindingFlags.FlattenHierarchy;

        static bool _done;
        static bool _guardPatched;
        static bool _acePatched;
        static volatile bool _retryQuiet;

        static void LogQ(string msg) { if (!_retryQuiet) Log(msg); }

        /// <summary>注入入口：无参静态方法，由 mono_runtime_invoke 调用。</summary>
        public static void Load()
        {
            if (_done) return;
            _done = true;
            try
            {
                Log("=== 风险模式载荷载入 pid=" + System.Diagnostics.Process.GetCurrentProcess().Id +
                    " 运行时=" + Environment.Version + " ===");
                // 进程要退出时把 patch 过的首字节写回去。改内存不改文件，
                // 但「改了不还原」会让 ACE/gameguard 在退出阶段看到不一致的代码段，
                // 是「退出回大厅偶发卡死」的一半来源。
                try { AppDomain.CurrentDomain.ProcessExit += delegate { RestoreEverything(); }; }
                catch { }
                Log("[*] 日志落盘: " + LogPath + "（写入自检通过）");
                Log("[*] 环境: " + NpggState());
                int stage = StageDelay();
                if (stage > 0)
                {
                    Log("[*] 分阶段模式：每步隔 " + stage + "s，逐步打补丁（进程死在哪一步，就是哪一步触发检测）");
                    System.Threading.Thread st = new System.Threading.Thread(delegate() { RunStaged(stage); });
                    st.IsBackground = true;
                    st.Start();
                    return;
                }
                _guardPatched = KillGuard();
                Log("[*] KillGuard 返回 " + _guardPatched);
                _acePatched = DowngradeAce();
                Log("[*] DowngradeAce 返回 " + _acePatched);
                // 退出保护要在补丁之后立刻挂上：抢在 GameGuard 收尾扫描之前把字节还原。
                try { ExitGuard.Install(); }
                catch (Exception ex) { Log("[exit] 安装失败: " + ex.GetType().Name + " " + ex.Message); }
                WeaponSwapper.Start();
                StartPatchRetry();
                Log("=== 载荷收工（guard=" + _guardPatched + " ace=" + _acePatched + "） ===");
            }
            catch (Exception ex)
            {
                Log("[!] 载荷顶层异常: " + ex);
            }
        }

        /// <summary>
        /// 分阶段推进：每步都先写「开始」再写「完成」。进程如果死在两步之间，
        /// 日志最后一行就是嫌疑最大的一步——一次进局就能定位到底哪类改动被 GameGuard 抓。
        /// </summary>
        static void RunStaged(int sec)
        {
            try
            {
                Log("[stage] 0/4 等待 " + sec + "s，让 GameGuard 先完成它自己的基线扫描");
                Thread.Sleep(sec * 1000);

                Log("[stage] 1/4 开始：关闭 RuntimeCheatGuard 自检");
                _guardPatched = KillGuard();
                Log("[stage] 1/4 完成 guard=" + _guardPatched);
                Thread.Sleep(sec * 1000);

                Log("[stage] 2/4 开始：ACE 降级（策略=" + AceMode() + "）");
                _acePatched = DowngradeAce();
                Log("[stage] 2/4 完成 ace=" + _acePatched);
                Thread.Sleep(sec * 1000);

                Log("[stage] 3/4 开始：挂退出保护");
                try { ExitGuard.Install(); Log("[stage] 3/4 完成"); }
                catch (Exception ex) { Log("[stage] 3/4 失败: " + ex.Message); }
                Thread.Sleep(sec * 1000);

                Log("[stage] 4/4 开始：装换肤 IL Hook");
                WeaponSwapper.Start();
                Log("[stage] 4/4 完成");
                StartPatchRetry();
                Log("[stage] 分阶段全部完成（本局没被杀，说明这些改动都能过检测）");
            }
            catch (Exception ex)
            {
                Log("[stage] 顶层异常: " + ex);
            }
        }

        /// <summary>
        /// 早注入时 Assembly-CSharp 往往还没加载，反射找不到自检类/AceClientManager，
        /// 补丁会全部落空；而 ACE 一旦完成初始化，退出时它自己的 Exit() 会 Abort 原生
        /// tick 线程并调用原生登出，正是退出回大厅卡死的来源。所以没打上就每 500ms
        /// 重试，直到两者都打上（最长 120 秒）。
        /// </summary>
        static void StartPatchRetry()
        {
            if (_guardPatched && _acePatched) return;
            Thread t = new Thread(delegate()
            {
                try
                {
                    // 前 3 秒用 60ms 高频轮询：RuntimeCheatGuard 的自检在启动后 1 秒内就跑，
                    // 慢一拍就会被它先报「exe 旁边有可疑代理 DLL」。之后退回 500ms。
                    for (int i = 0; i < 900; i++)
                    {
                        if (_guardPatched && _acePatched) break;
                        Thread.Sleep(i < 50 ? 60 : 300);
                        _retryQuiet = true;
                        try
                        {
                            if (!_guardPatched)
                            {
                                bool ok = KillGuard();
                                if (ok) Log("[retry] 自检类补丁完成（第 " + (i + 1) + " 次重试）");
                                _guardPatched = ok;
                            }
                            if (!_acePatched)
                            {
                                bool ok = DowngradeAce();
                                if (ok) Log("[retry] ACE 补丁完成（第 " + (i + 1) + " 次重试）");
                                _acePatched = ok;
                            }
                        }
                        catch (Exception e) { Log("[retry] 异常: " + e.Message); }
                        finally { _retryQuiet = false; }
                    }
                    if (_guardPatched && _acePatched) Log("=== 自检/ACE 已全部切断 ===");
                    else Log("[!] 补丁重试结束仍未完成：guard=" + _guardPatched + " ace=" + _acePatched);
                }
                catch (Exception e) { Log("[retry] 线程异常: " + e); }
            });
            t.IsBackground = true;
            t.Name = "SsjjRiskPatchRetry";
            t.Start();
        }

        // ─────────────────────────────────────────────── RuntimeCheatGuard
        static bool KillGuard()
        {
            int done = 0;

            // 路线一：名字已知就直接掐（版本对上时最省事，实测当前线上版本就有这个类）
            Type t = FindType("Assets.Sources.Utils.RuntimeCheatGuard", "RuntimeCheatGuard");
            if (t != null)
            {
                Log("[*] 找到自检类（显式名）: " + t.FullName);
                done += PatchGuardType(t);
                Log("   自检类 patch " + done + " 处（显式名命中，不再重复扫描）");
                return done > 0;
            }

            LogQ("[*] 显式名 RuntimeCheatGuard 不在本版本里，改走特征扫描");

            // 路线二：按特征扫。检测类一定带后台扫描线程，名字里也一定带这些词。
            // 客户端每次发版都可能改类名，硬编码名字迟早失效，所以扫描才是主力。
            int cand = 0;
            foreach (Type c in ScanGuardTypes())
            {
                cand++;
                LogQ("[*] 候选自检类: " + c.FullName);
                done += PatchGuardType(c);
            }
            LogQ("   自检类候选 " + cand + " 个，实际 patch " + done + " 处");
            if (cand == 0) LogQ("[!] 没找到任何带扫描线程的自检类（可能本版本改成了纯原生检测）");
            return done > 0;
        }

        /// <summary>掐一个自检类的线程、事件、检测方法。返回 patch 处数。</summary>
        static int PatchGuardType(Type t)
        {
            int n = 0;

            // 1. 先掐线程，再断事件，最后才 patch 方法（顺序反了会被线程拉起来）
            try
            {
                foreach (FieldInfo f in t.GetFields(All))
                {
                    if (f.FieldType == typeof(bool) && f.Name.ToLowerInvariant().Contains("start"))
                    {
                        f.SetValue(null, true);
                        Log("    " + f.Name + " = true");
                    }
                }
            }
            catch (Exception e) { Log("    状态字段处理失败: " + e.Message); }

            // Thread.Abort 在 Mono 上会打断正在跑原生代码的线程，退出阶段极易死锁，
            // 之前唯一一次 Abort 的会话正好以启动器崩溃收场。这里不再 Abort：
            // 线程还在 Sleep 也没关系，它下一轮调用的 Check* 已经被 patch 成 ret。
            try
            {
                foreach (FieldInfo f in t.GetFields(All))
                {
                    if (f.FieldType != typeof(Thread)) continue;
                    Thread th = f.GetValue(null) as Thread;
                    if (th != null && th.IsAlive)
                        Log("    " + f.Name + " 仍在运行（不 Abort，检测方法已 patch）");
                    f.SetValue(null, null);
                    n++;
                }
            }
            catch (Exception e) { Log("    线程字段处理失败: " + e.Message); }

            try
            {
                foreach (EventInfo ev in t.GetEvents(All))
                {
                    FieldInfo backing = t.GetField(ev.Name, All);
                    if (backing != null) { backing.SetValue(null, null); Log("    事件 " + ev.Name + " 已清空"); }
                }
            }
            catch (Exception e) { Log("    事件清理失败: " + e.Message); }

            // 2. Detected 的唯一触发点，直接打掉，事件链就断了
            if (PatchAll(t, "ReportDetected")) n++;

            // 3. 名字以这些前缀开头、且无参的方法一律打掉（检测/上报）
            //    外加昨晚那版的 exact 名单（Start/Loop/Tick/Run/Update）。
            //    为什么 exact 名单要留着：昨晚那版就是靠它把自检线程的驱动方法
            //    一起掐掉的。BannedPatchName 只挡「类型上会误命中」的帧驱动方法，
            //    exact 名单里的这几个名字在自检类上是安全的（自检类自带 Detected
            //    事件、只被自己的线程驱动，不参与游戏主循环）。
            string[] prefixes = { "Check", "Report", "Scan", "Detect", "Verify", "Validate" };
            string[] exact = { "Start", "Loop", "Tick", "Run", "Update" };
            try
            {
                foreach (MethodInfo m in t.GetMethods(All))
                {
                    if (m == null || m.IsAbstract || m.IsGenericMethodDefinition) continue;
                    if (m.GetParameters().Length != 0) continue;
                    if (BannedPatchName(m.Name)) continue;
                    bool hit = false;
                    foreach (string p in prefixes) if (m.Name.StartsWith(p, StringComparison.OrdinalIgnoreCase)) { hit = true; break; }
                    if (!hit) foreach (string p in exact) if (m.Name.Equals(p, StringComparison.OrdinalIgnoreCase)) { hit = true; break; }
                    if (!hit) continue;
                    if (m.Name == "StartCoroutine" || m.Name == "StartCoroutine_Auto") continue;
                    if (Patch(m)) n++;
                }
            }
            catch (Exception e) { Log("    方法 patch 失败: " + e.Message); }
            return n;
        }

        /// <summary>
        /// 这些名字一律不动：Unity 生命周期与协程点，打断它们的代价是整局卡死。
        /// 注意这里刻意不加 Start/Loop/Tick/Run/Update —— 昨晚那版靠 exact 名单
        /// 把自检线程的驱动方法一起掐掉，那正是「过检测」生效的关键一环；
        /// 只保留生命周期/协程这几个真正会打断主循环的名字。
        /// </summary>
        static bool BannedPatchName(string n)
        {
            if (string.IsNullOrEmpty(n)) return true;
            string[] banned = { "Awake", "OnEnable", "OnDisable", "OnDestroy",
                                "StartCoroutine", "StartCoroutine_Auto", "Invoke", "MoveNext" };
            foreach (string b in banned) if (n.Equals(b, StringComparison.OrdinalIgnoreCase)) return true;
            return false;
        }

        /// <summary>
        /// 找出所有「像检测器」的类型：自带 Thread 字段或 Detected 事件，
        /// 且名字里带 cheat/guard/detect/security，并排除武器/UI 这类同名干扰。
        /// </summary>
        static System.Collections.Generic.List<Type> ScanGuardTypes()
        {
            var outp = new System.Collections.Generic.List<Type>();
            foreach (Assembly asm in AppDomain.CurrentDomain.GetAssemblies())
            {
                Type[] types;
                try { types = asm.GetTypes(); }
                catch (ReflectionTypeLoadException e) { types = e.Types; }
                catch { continue; }
                if (types == null) continue;
                foreach (Type t in types)
                {
                    if (t == null) continue;
                    string fn = t.FullName ?? "";
                    if (fn.Contains("SniperGuardian") || fn.Contains("Weapon") ||
                        fn.Contains(".Ui.") || fn.Contains("ViewModel") || fn.Contains("SkillProgress") ||
                        fn.Contains("GuardianEffect")) continue;
                    string low = fn.ToLowerInvariant();
                    bool nameHit = low.Contains("cheat") || low.Contains("guard") ||
                                   low.Contains("detect") || low.Contains("selfcheck") ||
                                   low.Contains("security");
                    if (!nameHit) continue;
                    bool hasThread = false, hasDetected = false;
                    try
                    {
                        foreach (FieldInfo f in t.GetFields(All))
                            if (f.FieldType == typeof(Thread)) { hasThread = true; break; }
                        foreach (EventInfo ev in t.GetEvents(All))
                            if (ev.Name == "Detected" || ev.Name.ToLowerInvariant().Contains("detect")) { hasDetected = true; break; }
                    }
                    catch { continue; }
                    // 只认「带线程」或「带 Detected 事件」的，普通数据类不碰
                    if (hasThread || hasDetected) outp.Add(t);
                }
            }
            return outp;
        }

        // ─────────────────────────────────────────────── AceClientManager
        // 两种策略：
        //   full      —— 2026-11-23 22:41 那版的行为：19 个方法全打掉，
        //                ACE 不会真正初始化、不上报。这是「过检测」的原始形态。
        //   teardown  —— 只拦退出清理那 4 个方法，登录/心跳/上报全部放行。
        //
        // 默认值由 %LOCALAPPDATA%\ssjj-toolbox\risk_ace_full.txt 决定：
        //   文件存在且内容含 full（或为空）→ full
        //   文件不存在                      → full（与昨晚那版一致，这是当前默认）
        //   文件存在且内容为 teardown 等     → teardown
        static readonly string[] ACE_TEARDOWN = { "Exit", "eixt_ace", "user_loginOff_ace", "Close" };

        static readonly string[] ACE_FULL = {
            "GameAceInit", "CheckAceSwitch", "CheckAceOpenRuleList",
            "CheckRuleOpen", "LoginSucc", "ReceiveData", "Exit",
            "AceSwitch", "OnAceSwitch", "HandleAceSwitch", "SetAceSwitch",
            "CheckAceOpen", "StartAce", "CheckPlugin", "DoCheckPlugin",
            "RunPluginCheck", "ReportPlugin", "SendReport", "CheckGame"
        };

        /// <summary>ACE 策略：none / teardown / full，来自 risk_ace_full.txt，缺省 none。</summary>
        static string AceMode()
        {
            // 2026-11-25 改成缺省 none：
            // teardown 会打掉 AceClientManager.Exit/Close，等于不让 nProtect 正常收尾，
            // 实测「对局结束 → 进程退出」时多次崩在 npggNT64.des 自己的卸载流程里
            // （事件日志：npggNT64.des_unloaded + 0xc0000005）。
            // none = 一点都不碰 ACE，登录/心跳/退出全走官方原样，客户端痕迹也最少。
            try
            {
                string p = System.IO.Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                    "ssjj-toolbox", "risk_ace_full.txt");
                if (!System.IO.File.Exists(p)) return "none";
                string s = System.IO.File.ReadAllText(p).Trim().ToLowerInvariant();
                if (s.Contains("teardown")) return "teardown";
                if (s.Contains("full")) return "full";
                if (s.Length == 0 || s.Contains("none")) return "none";
                return "none";
            }
            catch { return "none"; }
        }

        static bool AceFullMode()
        {
            return AceMode() == "full";
        }

        static bool DowngradeAce()
        {
            // none：一点都不碰 ACE。启动更慢一点（ACE 全量初始化），但客户端痕迹最少。
            string mode = AceMode();
            if (mode == "none")
            {
                Log("[*] ACE 策略=none：不 patch 任何 ACE 方法（完全原样）");
                return true;
            }
            Type t = FindType("Assets.Libs.ForAce.AceClientManager", "AceClientManager");
            if (t == null) { LogQ("[!] 没找到 AceClientManager"); return false; }
            Log("[*] 找到 ACE 管理类: " + t.FullName);

            bool full = mode == "full";
            string[] methods = full ? ACE_FULL : ACE_TEARDOWN;
            int n = 0;
            foreach (string m in methods) if (PatchAll(t, m, true)) n++;
            Log("      ACE 策略=" + (full ? "full（19 个方法全打，与昨晚那版一致）" : "teardown（只拦退出清理，保留登录/上报）")
                + "，已 patch " + n + " 个方法");

            // 补丁来得太晚、ACE 已经登录过的话，tick 线程还在跑；Exit 已经打掉了，
            // 至少退出时不会再走 Abort + 原生登出这条死路，这里只把情况记下来。
            try
            {
                FieldInfo isu = t.GetField("_initSucc", All);
                FieldInfo lsu = t.GetField("_loginSucc", All);
                bool late = (isu != null && isu.GetValue(null) is bool && (bool)isu.GetValue(null))
                         || (lsu != null && lsu.GetValue(null) is bool && (bool)lsu.GetValue(null));
                if (late) Log("[!] ACE 在本次 patch 前已初始化（晚注入），已补打 Exit；建议下次让守护更早注入");
            }
            catch { }

            return n > 0;
        }

        // ─────────────────────────────────────────────── 工具
        static Type FindType(params string[] names)
        {
            foreach (string n in names)
            {
                try
                {
                    Type t = Type.GetType(n + ", Assembly-CSharp");
                    if (t != null) return t;
                }
                catch { }
            }
            foreach (Assembly asm in AppDomain.CurrentDomain.GetAssemblies())
            {
                foreach (string n in names)
                {
                    try
                    {
                        Type t = asm.GetType(n, false);
                        if (t != null) return t;
                        int dot = n.LastIndexOf('.');
                        if (dot > 0)
                        {
                            t = asm.GetType(n.Substring(0, dot) + "." + n.Substring(dot + 1), false);
                            if (t != null) return t;
                        }
                    }
                    catch { }
                }
            }
            return null;
        }

        static Type ScanType(params string[] methodNames)
        {
            foreach (Assembly asm in AppDomain.CurrentDomain.GetAssemblies())
            {
                Type[] types;
                try { types = asm.GetTypes(); }
                catch (ReflectionTypeLoadException e) { types = e.Types; }
                catch { continue; }
                if (types == null) continue;
                foreach (Type t in types)
                {
                    if (t == null) continue;
                    try
                    {
                        foreach (string m in methodNames)
                            if (t.GetMethod(m, All) != null) return t;
                    }
                    catch { }
                }
            }
            return null;
        }

        static bool PatchAll(Type t, string name, bool silentIfMissing = false)
        {
            MethodInfo[] list;
            try { list = t.GetMethods(All); }
            catch { return false; }
            bool any = false;
            foreach (MethodInfo m in list)
            {
                if (m == null || m.Name != name) continue;
                if (m.IsAbstract || m.IsGenericMethodDefinition) continue;
                if (Patch(m)) any = true;
            }
            if (!any && !silentIfMissing) Log("      跳过 " + name + "（未找到）");
            return any;
        }

        // 每个被 patch 的方法地址 -> 原始首字节。进程退出时写回去。
        static readonly System.Collections.Generic.Dictionary<IntPtr, byte[]> _orig =
            new System.Collections.Generic.Dictionary<IntPtr, byte[]>();

        static bool Patch(MethodInfo m)
        {
            try
            {
                IntPtr p = m.MethodHandle.GetFunctionPointer();
                if (p == IntPtr.Zero) return false;
                uint old;
                if (!VirtualProtect(p, (UIntPtr)8, PAGE_EXECUTE_READWRITE, out old)) return false;
                bool returnsValue = m.ReturnType != typeof(void);
                lock (_orig)
                {
                    if (!_orig.ContainsKey(p))
                    {
                        byte[] save = new byte[3];
                        for (int i = 0; i < 3; i++) save[i] = Marshal.ReadByte(p, i);
                        _orig[p] = save;
                    }
                }
                if (returnsValue)
                {
                    // xor eax,eax ; ret  → 返回 0/false/null，比裸 ret 安全（裸 ret 会返回垃圾）
                    Marshal.WriteByte(p, 0, 0x31);
                    Marshal.WriteByte(p, 1, 0xC0);
                    Marshal.WriteByte(p, 2, 0xC3);
                }
                else
                {
                    Marshal.WriteByte(p, 0, 0xC3);
                }
                uint tmp;
                VirtualProtect(p, (UIntPtr)8, old, out tmp);
                Log("      patch " + m.DeclaringType.Name + "." + m.Name +
                    " @0x" + p.ToInt64().ToString("X") + (returnsValue ? " -> 0" : " -> ret"));
                return true;
            }
            catch (Exception e)
            {
                Log("      patch " + name(m) + " 失败: " + e.Message);
                return false;
            }
        }

        /// <summary>把所有 patch 过的首字节写回去（进程退出时自动调用）。</summary>
        public static int RestoreAll()
        {
            int n = 0;
            try
            {
                IntPtr[] keys;
                lock (_orig) { keys = new IntPtr[_orig.Count]; _orig.Keys.CopyTo(keys, 0); }
                foreach (IntPtr p in keys)
                {
                    byte[] save;
                    lock (_orig) { if (!_orig.TryGetValue(p, out save)) continue; }
                    try
                    {
                        uint old;
                        if (!VirtualProtect(p, (UIntPtr)8, PAGE_EXECUTE_READWRITE, out old)) continue;
                        for (int i = 0; i < save.Length; i++) Marshal.WriteByte(p, i, save[i]);
                        uint tmp;
                        VirtualProtect(p, (UIntPtr)8, old, out tmp);
                        n++;
                    }
                    catch { }
                }
                if (n > 0) Log("[restore] 已还原 " + n + " 个方法首字节");
                lock (_orig) { _orig.Clear(); }
            }
            catch (Exception e) { Log("[restore] 异常: " + e.Message); }
            return n;
        }

        /// <summary>
        /// 退出时一次性收干净：先撤 5 个 IL detour（两个 CopyToWeaponComponent、准星、
        /// 音效），再还原所有被改成 ret 的方法首字节。
        /// 只还原 ret 不撤 detour 的话，那几个方法入口还留着 48 B8 &lt;非模块地址&gt; FF E0，
        /// GameGuard 退出前的完整性扫描照样会以 0x80000004 把进程杀掉。
        /// </summary>
        public static int RestoreEverything()
        {
            int hooks = 0;
            try { hooks = WeaponSwapper.UninstallHooks(); }
            catch (Exception e) { Log("[restore] 撤销 detour 失败: " + e.GetType().Name + " " + e.Message); }
            int bytes = RestoreAll();
            Log("[restore] 全部还原完成：detour " + hooks + " 个 / 首字节 " + bytes + " 处");
            return hooks + bytes;
        }

        static string name(MethodInfo m) { return m == null ? "?" : m.DeclaringType.Name + "." + m.Name; }
    }
}
