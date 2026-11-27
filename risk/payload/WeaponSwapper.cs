using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Reflection.Emit;
using System.Runtime.CompilerServices;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;

namespace SsjjRisk
{
    /// <summary>
    /// 战斗端运行时武器建模与武器皮肤替换引擎（零开销 DynamicMethod Inline Hook 版）。
    ///
    /// 1. 皮肤替换：使用 DynamicMethod 在运行时生成原生 IL 替代函数（零反射、零装箱、零 GC），
    ///    直接 Inline Hook WeaponUtility.CopyToWeaponComponent 两个重载，400 FPS 零掉帧、零闪烁。
    /// 2. 建模替换：仅在规则变更时克隆 WeaponInfo 的 24 个纯视觉字段，不改伤害/射速/后坐力。
    /// </summary>
    internal static class WeaponSwapper
    {
        private sealed class SwapRule
        {
            public string Src;
            public string Model;
            public string Skin;
            public string SkinName;
            public bool Enabled;
        }

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool VirtualProtect(IntPtr addr, UIntPtr size, uint flNewProtect, out uint lpflOldProtect);

        private const uint PAGE_EXECUTE_READWRITE = 0x40;

        private static readonly BindingFlags All =
            BindingFlags.Public | BindingFlags.NonPublic |
            BindingFlags.Static | BindingFlags.Instance |
            BindingFlags.FlattenHierarchy;

        private static readonly string[] VisualFields = new[]
        {
            "_assetBundleName",
            "_splitAssetBundleName",
            "_viewWeaponAssetName",
            "_thirdAssetName",
            "_sceneAssetName",
            "_firstKnifeAssetName",
            "_thirdKnifeAssetName",
            "_viewHandAnimationAssetName",
            "_viewWeaponAnimationAssetName",
            "_thirdWeaponAnimationAssetName",
            "_thirdWeaponAnimationXmlAssetName",
            "_viewCameraConfigAssetName",
            "_particleXmlAssetName",
            "_loigcXmlAssetName",
            "_soundXmlAssetName",
            "_effectXmlAssetName",
            "_eventParticleXmlAssetName",
            "_weaponConfigtXmlAssetName",
            "_backWeaponConfigtXmlAssetName",
            "_cameraEffectConfigAssetName",
            "_effectPath",
            "_iconAssetName",
            "_viewAnimConfigAssetName",
            "_weaponLayoutAssetName",
            "_weaponLogicAssetName",
            "_icon_2AssetName",
            "_icon_AssetName",
        };

        private static readonly string ConfigPath = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "ssjj-toolbox", "skin_swap.json");

        private static bool _started;
        private static bool _hooksInstalled;
        private static volatile bool _stopped;
        private static int _hookAttempts;
        private static bool _skinMapResolvedWithFactory;
        private static DateTime _lastMtime = DateTime.MinValue;
        private static volatile bool _globalEnabled = true;
        private static volatile bool _selfOnly = true;
        private static volatile bool _killFeedSim = true;
        private static List<SwapRule> _rules = new List<SwapRule>();

        public static bool SelfOnly { get { return _selfOnly; } }
        public static bool KillFeedSimEnabled { get { return _globalEnabled && _killFeedSim; } }

        // 供原生 IL Hook 无锁读取的不可变字典引用
        private static volatile Dictionary<string, string> _activeSkins =
            new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);

        private static volatile Dictionary<string, string> _activeModels =
            new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);

        // self_only=true 时供本地玩家独享的 WeaponInfo 克隆副本（不改全局 WeaponInfoFactory）
        private static volatile Dictionary<string, object> _localWeaponInfoClones =
            new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase);

        private static readonly Dictionary<string, string> _appliedLocalClones =
            new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);

        private static readonly MethodInfo _memberwiseCloneMethod =
            typeof(object).GetMethod("MemberwiseClone", BindingFlags.Instance | BindingFlags.NonPublic);

        // 本地玩家 ID / 组件归属反射缓存
        private static bool _localIdResolved;
        private static Func<int> _getLocalPlayerIdDel;
        private static bool _localCompResolved;
        private static PropertyInfo _pContextsShared;
        private static PropertyInfo _pPlayerCtx;
        private static PropertyInfo _pMyPlayerEntity;
        private static PropertyInfo _pMyCurrentWeapon;

        // 防止 DynamicMethod 委托被 GC 回收（被 Inline Hook 直接跳入其 JIT 代码地址）
        private static readonly List<Delegate> _pinnedDelegates = new List<Delegate>();

        // 每个被 detour 过的方法入口原始 12 字节。退出/对局结束时要原样写回去：
        // 只留 48 B8 <地址> FF E0「跳到我们进程内的托管代码」，GameGuard 收尾扫
        // 代码段时会看到一堆首字节异常的方法，这是退出崩溃里我们能控制的那一半。
        private static readonly List<KeyValuePair<IntPtr, byte[]>> _hookOrig =
            new List<KeyValuePair<IntPtr, byte[]>>();
        private static readonly object _hookLock = new object();

        private static readonly Dictionary<string, Dictionary<string, object>> _backups =
            new Dictionary<string, Dictionary<string, object>>(StringComparer.OrdinalIgnoreCase);

        private static readonly Dictionary<string, string> _appliedModels =
            new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);

        private static Type _weaponInfoFactoryType;
        private static object _weaponInfoFactoryInst;
        private static MethodInfo _getWeaponInfoMethod;

        public static void Start()
        {
            if (_started) return;
            _started = true;
            try
            {
                InspectProbe.Start();
                Thread th = new Thread(Loop);
                th.IsBackground = true;
                th.Name = "SsjjWeaponSwapper";
                th.Start();
                Loader.Log("[skin] 武器皮肤/建模替换守护已启动（零开销 IL Hook 模式），配置: " + ConfigPath);
            }
            catch (Exception ex)
            {
                Loader.Log("[skin] 启动守护失败: " + ex.Message);
            }
        }

        /// <summary>
        /// 热加载开关。默认关（冷加载）：规则只在武器第一次加载时应用一次，
        /// 之后不再每 500ms 全量重建。想回到旧行为就在这个文件里放一行 hot。
        /// </summary>
        private static bool HotSwapEnabled()
        {
            try
            {
                string p = System.IO.Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                    "ssjj-toolbox", "risk_swap_hot.txt");
                return System.IO.File.Exists(p);
            }
            catch { return false; }
        }

        private static void Loop()
        {
            bool appliedOnce = false;
            bool hot = HotSwapEnabled();
            DateTime modelRetryUntil = DateTime.MinValue;
            while (true)
            {
                if (_stopped) return;
                bool active = false;
                try
                {
                    if (!_hooksInstalled && _hookAttempts < 6)
                        TryInstallHooks();

                    bool changed = ReloadConfigIfChanged();
                    active = _globalEnabled && _rules.Count > 0;

                    if (!appliedOnce)
                    {
                        // 冷加载：进场只做一次。武器创建时走 ResolveSkin/ResolveModelWeapon
                        // 的字典查表，之后没有任何轮询开销。
                        if (active)
                        {
                            modelRetryUntil = DateTime.UtcNow.AddSeconds(20);
                            ApplyModelSwaps();
                            RebuildSkinMap(true);
                        }
                        appliedOnce = true;
                        hot = HotSwapEnabled();
                        Loader.Log("[skin] 冷加载完成：模式=" + (hot ? "热" : "冷") +
                                   " enabled=" + _globalEnabled + " rules=" + _rules.Count);
                    }
                    else if (changed)
                    {
                        if (hot)
                        {
                            if (active) { ApplyModelSwaps(); RebuildSkinMap(true); }
                            Loader.Log("[skin] 热加载配置：enabled=" + _globalEnabled +
                                       " rules=" + _rules.Count);
                        }
                        else
                        {
                            Loader.Log("[skin] 配置变了，冷加载模式不重载（下一局生效）");
                        }
                    }
                    else if (!active)
                    {
                        if (_activeSkins.Count > 0)
                            _activeSkins = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
                        if (_activeModels.Count > 0)
                            _activeModels = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
                        if (_localWeaponInfoClones.Count > 0)
                            _localWeaponInfoClones = new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase);
                        if (_appliedLocalClones.Count > 0)
                            _appliedLocalClones.Clear();
                        if (_appliedModels.Count > 0)
                            RestoreAllModels();
                    }
                }
                catch (Exception ex)
                {
                    Loader.Log("[skin] 守护异常: " + ex.Message);
                }
                // 建模表可能晚于 Hook 就绪；冷加载最多补查 20 秒，确保阵营条目也写入。
                if (!hot && appliedOnce && _hooksInstalled)
                {
                    Dictionary<string, string> desired = BuildModelMap();
                    if (active && desired.Count > 0 && !AllModelsApplied(desired))
                    {
                        if (DateTime.UtcNow < modelRetryUntil)
                        {
                            ApplyModelSwaps();
                            Thread.Sleep(500);
                            continue;
                        }
                        int appliedCount = _selfOnly ? _appliedLocalClones.Count : _appliedModels.Count;
                        Loader.Log("[skin] 建模补查到期：已应用 " + appliedCount +
                                   "/" + desired.Count + " 条；未就绪条目本局不再重试");
                    }
                    Loader.Log("[skin] 冷加载完成，后台线程退出（进程内不再有本工具常驻线程）");
                    return;
                }
                Thread.Sleep(hot ? 500 : 2000);
            }
        }

        /// <summary>获取当前本地玩家实体 ID（通过 UiIEventCondition.Get_uiMyPlayer_PlayerId，带安全兜底）。</summary>
        public static int GetLocalPlayerId()
        {
            try
            {
                Func<int> del = _getLocalPlayerIdDel;
                if (del == null && !_localIdResolved)
                {
                    Type uiCond = FindType("Assets.Sources.Modules.Ui.UiEventCondition.UiIEventCondition");
                    if (uiCond != null)
                    {
                        MethodInfo m = uiCond.GetMethod("Get_uiMyPlayer_PlayerId", BindingFlags.Public | BindingFlags.Static);
                        if (m != null && m.ReturnType == typeof(int) && m.GetParameters().Length == 0)
                        {
                            del = (Func<int>)Delegate.CreateDelegate(typeof(Func<int>), m);
                            _getLocalPlayerIdDel = del;
                        }
                        _localIdResolved = true;
                    }
                }
                if (del != null) return del();
            }
            catch { }
            return -1;
        }

        /// <summary>判断 CurrentWeaponComponent 是否归属本地玩家（无法确定时保守返回 true，避免切枪过渡帧失效）。</summary>
        private static bool IsLocalPlayerComponent(object comp)
        {
            if (comp == null) return true;
            try
            {
                if (!_localCompResolved)
                {
                    Type tCtx = FindType("Contexts");
                    if (tCtx != null)
                    {
                        _pContextsShared = tCtx.GetProperty("sharedInstance", All);
                        _pPlayerCtx = tCtx.GetProperty("player", All);
                        if (_pPlayerCtx != null)
                        {
                            _pMyPlayerEntity = _pPlayerCtx.PropertyType.GetProperty("myPlayerEntity", All);
                            if (_pMyPlayerEntity != null)
                                _pMyCurrentWeapon = _pMyPlayerEntity.PropertyType.GetProperty("currentWeapon", All);
                        }
                        _localCompResolved = true;
                    }
                }
                if (_pContextsShared == null || _pPlayerCtx == null || _pMyPlayerEntity == null || _pMyCurrentWeapon == null)
                    return true;
                object ctx = _pContextsShared.GetValue(null, null);
                if (ctx == null) return true;
                object playerCtx = _pPlayerCtx.GetValue(ctx, null);
                if (playerCtx == null) return true;
                object myPlayer = _pMyPlayerEntity.GetValue(playerCtx, null);
                if (myPlayer == null) return true;
                object myComp = _pMyCurrentWeapon.GetValue(myPlayer, null);
                if (myComp == null) return true;
                return ReferenceEquals(comp, myComp);
            }
            catch
            {
                return true;
            }
        }

        /// <summary>主线程热路径调用（零分配、零锁、零日志，极快）。</summary>
        public static string ResolveSkin(string weaponName, string origSkin)
        {
            if (!_globalEnabled || string.IsNullOrEmpty(weaponName))
                return origSkin;
            Dictionary<string, string> map = _activeSkins;
            string target;
            if (map != null && map.TryGetValue(weaponName, out target) && !string.IsNullOrEmpty(target))
                return target;
            return origSkin;
        }

        /// <summary>左下角通报使用规则中保存的皮肤中文名（含双阵营皮肤后缀）。</summary>
        public static string ResolveSkinDisplayName(string skinCode)
        {
            if (string.IsNullOrEmpty(skinCode)) return "";
            string baseCode = skinCode;
            if (baseCode.EndsWith("_ct", StringComparison.OrdinalIgnoreCase))
                baseCode = baseCode.Substring(0, baseCode.Length - 3);
            else if (baseCode.EndsWith("_t", StringComparison.OrdinalIgnoreCase))
                baseCode = baseCode.Substring(0, baseCode.Length - 2);
            List<SwapRule> rules = _rules;
            foreach (SwapRule rule in rules)
            {
                if (!rule.Enabled || string.IsNullOrEmpty(rule.SkinName) ||
                    string.Equals(rule.SkinName, rule.Skin, StringComparison.OrdinalIgnoreCase))
                    continue;
                if (string.Equals(skinCode, rule.Skin, StringComparison.OrdinalIgnoreCase) ||
                    string.Equals(baseCode, rule.Skin, StringComparison.OrdinalIgnoreCase))
                    return rule.SkinName;
            }
            return "";
        }

        /// <summary>按 PlayerEntityData.Id 过滤的皮肤解析（self_only=true 时跳过其他玩家）。</summary>
        public static string ResolveSkinForPlayer(int ownerId, string weaponName, string origSkin)
        {
            if (!_globalEnabled || string.IsNullOrEmpty(weaponName))
                return origSkin;
            if (_selfOnly)
            {
                int localId = GetLocalPlayerId();
                if (localId > 0 && ownerId > 0 && ownerId != localId)
                    return origSkin;
            }
            return ResolveSkin(weaponName, origSkin);
        }

        /// <summary>按 CurrentWeaponComponent 归属过滤的皮肤解析（self_only=true 时跳过其他玩家）。</summary>
        public static string ResolveSkinForComponent(object comp, string weaponName, string origSkin)
        {
            if (!_globalEnabled || string.IsNullOrEmpty(weaponName))
                return origSkin;
            if (_selfOnly && !IsLocalPlayerComponent(comp))
                return origSkin;
            return ResolveSkin(weaponName, origSkin);
        }

        /// <summary>按 PlayerEntityData.Id 过滤的本地建模 WeaponInfo 副本解析。</summary>
        public static object ResolveWeaponInfoForPlayer(int ownerId, string weaponName, object origWeaponInfo)
        {
            if (!_globalEnabled || !_selfOnly || string.IsNullOrEmpty(weaponName))
                return origWeaponInfo;
            int localId = GetLocalPlayerId();
            if (localId > 0 && ownerId > 0 && ownerId != localId)
                return origWeaponInfo;
            return LookupLocalWeaponInfoClone(weaponName, origWeaponInfo);
        }

        /// <summary>按 CurrentWeaponComponent 归属过滤的本地建模 WeaponInfo 副本解析。</summary>
        public static object ResolveWeaponInfoForComponent(object comp, string weaponName, object origWeaponInfo)
        {
            if (!_globalEnabled || !_selfOnly || string.IsNullOrEmpty(weaponName))
                return origWeaponInfo;
            if (!IsLocalPlayerComponent(comp))
                return origWeaponInfo;
            return LookupLocalWeaponInfoClone(weaponName, origWeaponInfo);
        }

        private static object LookupLocalWeaponInfoClone(string weaponName, object origWeaponInfo)
        {
            Dictionary<string, object> clones = _localWeaponInfoClones;
            if (clones == null || clones.Count == 0) return origWeaponInfo;
            object cloned;
            if (clones.TryGetValue(weaponName, out cloned) && cloned != null)
                return cloned;
            string baseName = FactionBase(weaponName);
            if (baseName != weaponName && clones.TryGetValue(baseName, out cloned) && cloned != null)
                return cloned;
            return origWeaponInfo;
        }

        /// <summary>供开镜准星与武器音效 Hook 调用：将原武器名映射为目标建模武器名（零锁零日志）。</summary>
        public static string ResolveModelWeapon(string weaponName)
        {
            if (!_globalEnabled || string.IsNullOrEmpty(weaponName))
                return weaponName;
            Dictionary<string, string> map = _activeModels;
            string target;
            if (map != null && map.TryGetValue(weaponName, out target) && !string.IsNullOrEmpty(target))
                return target;
            string baseName = FactionBase(weaponName);
            if (map != null && baseName != weaponName &&
                map.TryGetValue(baseName, out target) && !string.IsNullOrEmpty(target))
                return target;
            return weaponName;
        }

        private static readonly HashSet<string> _seenLogged = new HashSet<string>();

        /// <summary>只观测：把游戏真正传进来的武器名记下来（最多 15 个）。
        /// 作用：规则写了却一直不生效时，先看这里有没有你写的那把枪——
        /// 没有 = Hook 没拦到；有但名字不同 = 大小写/后缀（_ct/_t/_a）对不上。</summary>
        private static void LogSeen(string weaponName)
        {
            try
            {
                lock (_seenLogged)
                {
                    if (_seenLogged.Count >= 15) return;
                    if (!_seenLogged.Add(weaponName)) return;
                }
                Loader.Log("[skin] 见到武器: " + weaponName);
            }
            catch { }
        }

        private static readonly HashSet<string> _hitLogged = new HashSet<string>();

        // ---- 观测点：开镜准星 / 音效 到底有没有被调用、传进来的是什么 ----
        // 冷加载下没法靠肉眼看画面判断「没替换」是没命中还是没调用，所以各留 8 条日志做证据。
        private static readonly HashSet<string> _crossLogged = new HashSet<string>();
        private static readonly HashSet<string> _soundLogged = new HashSet<string>();

        /// <summary>开镜/准星资源查询会被每帧触发，按结果去重只记 8 条。</summary>
        public static void LogCrossRes(string mapped)
        {
            try
            {
                lock (_crossLogged)
                {
                    if (_crossLogged.Count >= 8) return;
                    if (!_crossLogged.Add(mapped)) return;
                }
                Loader.Log("[skin] 准星资源请求 -> " + mapped);
            }
            catch { }
        }

        /// <summary>音效资源查询（开火/换弹/切枪），按「武器+后缀」去重记 8 条。</summary>
        public static void LogSoundReq(string weapon, string suffix)
        {
            try
            {
                lock (_soundLogged)
                {
                    if (_soundLogged.Count >= 8) return;
                    if (!_soundLogged.Add(weapon + "|" + suffix)) return;
                }
                Loader.Log("[skin] 音效资源请求: " + weapon + suffix);
            }
            catch { }
        }


        /// <summary>
        /// 每种武器只记一条「替换真的命中了」。作用有两个：
        ///  1) 用户/我不用盯着画面也能确认换肤链路在实战里跑通（看 risk_inject.log 即可）；
        ///  2) 出问题时能立刻区分「Hook 没装上」和「装上了但没命中」。
        /// 上限 40 条，避免长局刷日志。
        /// </summary>
        private static void LogHit(string kind, string weaponName, string target)
        {
            try
            {
                lock (_hitLogged)
                {
                    if (_hitLogged.Count >= 40) return;
                    if (!_hitLogged.Add(kind + "|" + weaponName)) return;
                }
                Loader.Log("[skin] 实际替换生效: " + kind + " " + weaponName + " -> " + target);
            }
            catch { }
        }

        // ------------------------------------------------------------------ 零开销 DynamicMethod Hook
        private static void TryInstallHooks()
        {
            Type utilType = FindType("Assets.Sources.Utils.Weapon.WeaponUtility");
            if (utilType == null) return;

            int rougeOffset = 20;
            FieldInfo fOff = utilType.GetField("RougeIndexOffset", All);
            if (fOff != null)
            {
                try { rougeOffset = Convert.ToInt32(fOff.GetValue(null)); } catch { }
            }

            MethodInfo mFromPlayer = null;
            MethodInfo mFromWeapon = null;
            foreach (MethodInfo m in utilType.GetMethods(All))
            {
                if (m.Name != "CopyToWeaponComponent" || !m.IsStatic) continue;
                ParameterInfo[] ps = m.GetParameters();
                if (ps.Length != 2) continue;
                string p1 = ps[1].ParameterType.Name;
                if (p1 == "PlayerEntityData") mFromPlayer = m;
                else if (p1 == "WeaponEntity") mFromWeapon = m;
            }

            int hooked = 0;
            if (mFromPlayer != null)
            {
                try
                {
                    IntPtr ptr = BuildPlayerDataCopyIL(mFromPlayer);
                    if (ptr != IntPtr.Zero && InstallJumpHookPtr(mFromPlayer, ptr))
                    {
                        hooked++;
                        Loader.Log("[skin] 已安装零开销 IL Hook: CopyToWeaponComponent(comp, PlayerEntityData)");
                    }
                }
                catch (Exception ex)
                {
                    Loader.Log("[skin] 构建 PlayerEntityData IL Hook 失败: " + ex.Message);
                }
            }
            if (mFromWeapon != null)
            {
                try
                {
                    IntPtr ptr = BuildWeaponEntityCopyIL(mFromWeapon, rougeOffset);
                    if (ptr != IntPtr.Zero && InstallJumpHookPtr(mFromWeapon, ptr))
                    {
                        hooked++;
                        Loader.Log("[skin] 已安装零开销 IL Hook: CopyToWeaponComponent(comp, WeaponEntity)");
                    }
                }
                catch (Exception ex)
                {
                    Loader.Log("[skin] 构建 WeaponEntity IL Hook 失败: " + ex.Message);
                }
            }

            if (hooked > 0)
            {
                _hooksInstalled = true;
                TryInstallCrossAndSoundHooks();
                TryInstallVisualHooks();
            }
            else
                _hookAttempts++;
        }

        private static void TryInstallCrossAndSoundHooks()
        {
            // 1. 开镜效果（狙击镜 / 瞄具准星）：Hook UiIEventCondition.Get_uiCross_WeaponName()
            try
            {
                Type uiCond = FindType("Assets.Sources.Modules.Ui.UiEventCondition.UiIEventCondition");
                MethodInfo mCross = uiCond != null ? uiCond.GetMethod("Get_uiCross_WeaponName", BindingFlags.Public | BindingFlags.Static) : null;
                if (mCross != null)
                {
                    IntPtr ptr = BuildCrossWeaponIL(uiCond);
                    if (ptr != IntPtr.Zero && InstallJumpHookPtr(mCross, ptr))
                        Loader.Log("[skin] 已安装开镜准星 IL Hook: UiIEventCondition.Get_uiCross_WeaponName()");
                }
            }
            catch (Exception ex)
            {
                Loader.Log("[skin] 开镜准星 IL Hook 失败: " + ex.Message);
            }

            // 1.5 切枪 150ms 开镜闪屏去抖：Hook UiIEventCondition.Get_uiCross_AwpVisible()
            try
            {
                Type uiCond = FindType("Assets.Sources.Modules.Ui.UiEventCondition.UiIEventCondition");
                MethodInfo mAwpVis = uiCond != null ? uiCond.GetMethod("Get_uiCross_AwpVisible", BindingFlags.Public | BindingFlags.Static) : null;
                if (mAwpVis != null && mAwpVis.ReturnType == typeof(bool) && mAwpVis.GetParameters().Length == 0)
                {
                    IntPtr ptr = BuildAwpVisibleIL();
                    if (ptr != IntPtr.Zero && InstallJumpHookPtr(mAwpVis, ptr))
                        Loader.Log("[skin] Hook 已写入: UiIEventCondition.Get_uiCross_AwpVisible (切枪 150ms 开镜闪屏去抖)");
                }
            }
            catch (Exception ex)
            {
                Loader.Log("[skin] 开镜去抖 Hook 失败: " + ex.Message);
            }

            // 2. 武器音效（开火/切枪/换弹）：Hook SoundResourceUtility.GetWeaponSoundAssetInfo(string, string)
            try
            {
                Type sndUtil = FindType("Assets.Sources.Utils.SoundResourceUtility");
                MethodInfo mSound = sndUtil != null ? sndUtil.GetMethod("GetWeaponSoundAssetInfo", new[] { typeof(string), typeof(string) }) : null;
                if (mSound != null)
                {
                    IntPtr ptr = BuildWeaponSoundIL(mSound);
                    if (ptr != IntPtr.Zero && InstallJumpHookPtr(mSound, ptr))
                        Loader.Log("[skin] 已安装武器音效 IL Hook: SoundResourceUtility.GetWeaponSoundAssetInfo(weapon, suffix)");
                }
            }
            catch (Exception ex)
            {
                Loader.Log("[skin] 武器音效 IL Hook 失败: " + ex.Message);
            }
        }

        private static long _awpTrueSinceTick;

        public static bool FilterAwpCrossVisible(bool rawVisible)
        {
            if (!rawVisible)
            {
                _awpTrueSinceTick = 0;
                return false;
            }
            if (!InspectPlayback.IsInRaiseGuardWindow(600))
            {
                _awpTrueSinceTick = 0;
                return true;
            }
            long now = System.Diagnostics.Stopwatch.GetTimestamp();
            long since = _awpTrueSinceTick;
            if (since <= 0)
            {
                _awpTrueSinceTick = now;
                return false;
            }
            long heldMs = (now - since) * 1000 / System.Diagnostics.Stopwatch.Frequency;
            return heldMs >= 150;
        }

        private static IntPtr BuildAwpVisibleIL()
        {
            Type tCtx = FindType("Contexts");
            if (tCtx == null) return IntPtr.Zero;
            PropertyInfo pShared = tCtx.GetProperty("sharedInstance", All);
            FieldInfo fShared = pShared == null ? tCtx.GetField("sharedInstance", All) : null;
            if (pShared == null && fShared == null) return IntPtr.Zero;

            Type tCross = MemberType(tCtx, "cross");
            if (tCross == null || MemberType(tCross, "isAwpCross") != typeof(bool))
                return IntPtr.Zero;

            MethodInfo mFilter = typeof(WeaponSwapper).GetMethod(
                nameof(FilterAwpCrossVisible), BindingFlags.Public | BindingFlags.Static);
            if (mFilter == null) return IntPtr.Zero;

            DynamicMethod dm = new DynamicMethod(
                "SsjjCrossAwpVisible", typeof(bool), Type.EmptyTypes,
                typeof(WeaponSwapper).Module, true);
            ILGenerator il = dm.GetILGenerator();
            Label lblFalse = il.DefineLabel();
            LocalBuilder locCtx = il.DeclareLocal(tCtx);
            LocalBuilder locCross = il.DeclareLocal(tCross);

            if (pShared != null) il.Emit(OpCodes.Call, pShared.GetGetMethod(true));
            else il.Emit(OpCodes.Ldsfld, fShared);
            il.Emit(OpCodes.Stloc, locCtx);
            il.Emit(OpCodes.Ldloc, locCtx);
            il.Emit(OpCodes.Brfalse, lblFalse);

            il.Emit(OpCodes.Ldloc, locCtx);
            EmitGet(il, tCtx, "cross");
            il.Emit(OpCodes.Stloc, locCross);
            il.Emit(OpCodes.Ldloc, locCross);
            il.Emit(OpCodes.Brfalse, lblFalse);

            il.Emit(OpCodes.Ldloc, locCross);
            EmitGet(il, tCross, "isAwpCross");
            il.Emit(OpCodes.Call, mFilter);
            il.Emit(OpCodes.Ret);

            il.MarkLabel(lblFalse);
            il.Emit(OpCodes.Ldc_I4_0);
            il.Emit(OpCodes.Call, mFilter);
            il.Emit(OpCodes.Ret);

            Delegate del = dm.CreateDelegate(typeof(Func<bool>));
            _pinnedDelegates.Add(del);
            RuntimeHelpers.PrepareMethod(del.Method.MethodHandle);
            return del.Method.MethodHandle.GetFunctionPointer();
        }

        private static void TryInstallVisualHooks()
        {
            try
            {
                Type binding = FindType("SSJJUIEditor.RunTime.EditorUiBinding");
                MethodInfo refresh = binding == null ? null :
                    binding.GetMethod("RefreshTexture", BindingFlags.Public | BindingFlags.Instance);
                if (refresh != null)
                {
                    IntPtr ptr = BuildButterflyUiTintIL(refresh);
                    if (ptr != IntPtr.Zero && InstallJumpHookPtr(refresh, ptr))
                        Loader.Log("[skin] 已安装蝶舞准星 #66CCFF 着色 Hook");
                }
            }
            catch (Exception ex)
            {
                Loader.Log("[skin] 蝶舞准星着色 Hook 失败: " + ex.GetType().Name + " " + ex.Message);
            }

            // 蝶舞 ui_logic.xml 的 IsHoldWeapon 直接读 WeaponDataProvider，
            // 不会经过 DataProviderManager.GetWeaponName。
            try
            {
                Type provider = FindType("WeaponDataProvider");
                MethodInfo hold = provider == null ? null :
                    provider.GetMethod("IsHoldWeapon", BindingFlags.Public | BindingFlags.Instance,
                                       null, new[] { typeof(string) }, null);
                if (hold != null)
                {
                    IntPtr ptr = BuildVisualHoldWeaponIL(hold);
                    if (ptr != IntPtr.Zero && InstallJumpHookPtr(hold, ptr))
                        Loader.Log("[skin] 已安装蝶舞专属 UI 持有武器判断 Hook");
                    else
                        Loader.Log("[skin] 蝶舞专属 UI 持有武器判断接口未匹配");
                }
            }
            catch (Exception ex)
            {
                Loader.Log("[skin] 蝶舞专属 UI 判断 Hook 失败: " + ex.GetType().Name + " " + ex.Message);
            }

            try
            {
                Type viewSystem = FindType("Assets.Sources.Modules.Player.Anim.FirstPerson.ViewAnimSystem");
                MethodInfo play = viewSystem == null ? null :
                    viewSystem.GetMethod("PlayHandAnimation", BindingFlags.NonPublic | BindingFlags.Instance);
                if (play != null)
                {
                    IntPtr ptr = InspectPlayback.Build(play);
                    if (ptr != IntPtr.Zero && InstallJumpHookPtr(play, ptr))
                        Loader.Log("[skin] Hook 已写入: ViewAnimSystem.PlayHandAnimation (本地长检视 + 切枪原速方案B)");
                    else
                        Loader.Log("[skin] 长检视接口不匹配，本局保留原动画");
                }
            }
            catch (Exception ex)
            {
                Loader.Log("[skin] 长检视 Hook 失败: " + ex.GetType().Name + " " + ex.Message);
            }

            // 专属 UI 的逻辑资源已随 WeaponInfo 替换；其数据提供器仍读取原武器名。
            // 只在该 UI 读取武器名时映射到目标，供武器专属条件和扩展提供器使用。
            try
            {
                Type provider = FindType("Assets.Libs.UiEditor.RunTime.RunTimeLogic.RunTimeNode.DataProviderManager");
                MethodInfo get = provider == null ? null :
                    provider.GetMethod("GetWeaponName", BindingFlags.Public | BindingFlags.Instance);
                FieldInfo field = provider == null ? null :
                    provider.GetField("CurrentWeaponName", BindingFlags.NonPublic | BindingFlags.Instance);
                if (get != null && field != null && field.FieldType == typeof(string) &&
                    get.ReturnType == typeof(string) && get.GetParameters().Length == 0)
                {
                    DynamicMethod dm = new DynamicMethod("SsjjUiModelWeaponName",
                        typeof(string), new[] { provider }, typeof(WeaponSwapper).Module, true);
                    ILGenerator il = dm.GetILGenerator();
                    il.Emit(OpCodes.Ldarg_0);
                    il.Emit(OpCodes.Ldfld, field);
                    il.Emit(OpCodes.Call, typeof(WeaponSwapper).GetMethod(nameof(ResolveModelWeapon)));
                    il.Emit(OpCodes.Ret);
                    Delegate del = dm.CreateDelegate(typeof(Func<,>).MakeGenericType(provider, typeof(string)));
                    _pinnedDelegates.Add(del);
                    RuntimeHelpers.PrepareMethod(del.Method.MethodHandle);
                    if (InstallJumpHookPtr(get, del.Method.MethodHandle.GetFunctionPointer()))
                        Loader.Log("[skin] 已安装武器专属 UI 名称映射 Hook");
                }
                else
                    Loader.Log("[skin] 武器专属 UI 名称入口未匹配: provider=" +
                               (provider != null) + " get=" + (get != null) +
                               " field=" + (field != null));
            }
            catch (Exception ex)
            {
                Loader.Log("[skin] 武器专属 UI Hook 失败: " + ex.GetType().Name + " " + ex.Message);
            }

            // 特效皮肤（HasEffect=true）的 AssetBundle 路径在 WeaponSkinInfo.SetWeaponName(ref string)
            // 里按 weaponInfo.Name 拼接；跨武器换模时 weaponInfo.Name 仍保留原武器名，若不映射到目标建模名
            // 会错误回退到 WeaponList[0]（例如 skin_awm_legend_porcelain_t 回退成 awm_legend），导致模型与动画全空。
            try
            {
                Type skinInfoType = FindType("Assets.Sources.Info.Weapon.WeaponSkinInfo");
                MethodInfo mSetWeaponName = skinInfoType == null ? null :
                    skinInfoType.GetMethod("SetWeaponName", BindingFlags.NonPublic | BindingFlags.Public | BindingFlags.Instance,
                                           null, new[] { typeof(string).MakeByRefType() }, null);
                if (mSetWeaponName != null && mSetWeaponName.ReturnType == typeof(void))
                {
                    _fSkinWeaponList = skinInfoType.GetField("WeaponList", All);
                    _fSkinWeapon = skinInfoType.GetField("Weapon", All);
                    DynamicMethod dm = new DynamicMethod("SsjjSkinSetWeaponName",
                        typeof(void), new[] { typeof(object), typeof(string).MakeByRefType() },
                        typeof(WeaponSwapper).Module, true);
                    ILGenerator il = dm.GetILGenerator();
                    il.Emit(OpCodes.Ldarg_0);
                    il.Emit(OpCodes.Ldarg_1);
                    il.Emit(OpCodes.Call, typeof(WeaponSwapper).GetMethod(nameof(OnSetSkinWeaponName), BindingFlags.Public | BindingFlags.Static));
                    il.Emit(OpCodes.Ret);
                    Delegate del = dm.CreateDelegate(typeof(SetWeaponNameDelegate));
                    _pinnedDelegates.Add(del);
                    RuntimeHelpers.PrepareMethod(del.Method.MethodHandle);
                    if (InstallJumpHookPtr(mSetWeaponName, del.Method.MethodHandle.GetFunctionPointer()))
                        Loader.Log("[skin] Hook 已写入: WeaponSkinInfo.SetWeaponName");
                }
                else
                {
                    Loader.Log("[skin] WeaponSkinInfo.SetWeaponName 未匹配: type=" + (skinInfoType != null) +
                               " method=" + (mSetWeaponName != null));
                }
            }
            catch (Exception ex)
            {
                Loader.Log("[skin] WeaponSkinInfo.SetWeaponName Hook 失败: " + ex.GetType().Name + " " + ex.Message);
            }
        }

        private delegate void SetWeaponNameDelegate(object skinInfo, ref string weaponName);
        private static FieldInfo _fSkinWeaponList;
        private static FieldInfo _fSkinWeapon;

        public static void OnSetSkinWeaponName(object skinInfo, ref string weaponName)
        {
            if (skinInfo == null) return;
            try
            {
                List<string> weaponList = _fSkinWeaponList != null ? _fSkinWeaponList.GetValue(skinInfo) as List<string> : null;
                if (weaponList == null && _fSkinWeapon != null)
                {
                    string raw = _fSkinWeapon.GetValue(skinInfo) as string;
                    if (!string.IsNullOrEmpty(raw))
                    {
                        weaponList = new List<string>(raw.Split(':'));
                        if (_fSkinWeaponList != null) _fSkinWeaponList.SetValue(skinInfo, weaponList);
                    }
                }
                if (weaponList == null || weaponList.Count == 0) return;

                if (!string.IsNullOrEmpty(weaponName))
                {
                    string mapped = ResolveModelWeapon(weaponName);
                    if (!string.IsNullOrEmpty(mapped) &&
                        !string.Equals(mapped, weaponName, StringComparison.OrdinalIgnoreCase))
                    {
                        if (weaponList.Contains(mapped))
                        {
                            weaponName = mapped;
                        }
                        else
                        {
                            for (int i = 0; i < weaponList.Count; i++)
                            {
                                if (string.Equals(weaponList[i], mapped, StringComparison.OrdinalIgnoreCase))
                                {
                                    weaponName = weaponList[i];
                                    break;
                                }
                            }
                        }
                    }
                }

                if (!weaponList.Contains(weaponName))
                    weaponName = weaponList[0];
            }
            catch { }
        }

        public static bool IsVisualHoldWeapon(string actual, string requested)
        {
            if (string.IsNullOrEmpty(actual)) return false;
            if (string.Equals(requested, "butterfly_knife", StringComparison.Ordinal) &&
                string.Equals(ResolveModelWeapon(actual), requested, StringComparison.OrdinalIgnoreCase))
                return true;
            return actual.Equals(requested);
        }

        private static Type _uiImageType;
        private static Type _uiColorType;
        private static MethodInfo _uiGetComponent;
        private static PropertyInfo _uiColor;
        private static PropertyInfo _uiSprite;
        private static readonly object[] BlueCrossSprites = new object[2];
        // 26x41；保留原 crossProgress1/2 的透明轮廓，实色为 #66CCFF。
        private static readonly string[] BlueCrossPng =
        {
            "iVBORw0KGgoAAAANSUhEUgAAABoAAAApCAYAAAArpDnNAAACEklEQVR42tWWP0gjURDGfy45SKnFcVqmtVOwOLwDCQFbIaQKFmkMcthdZ6vlccUVIggWR2xiYSGiEJcUioKinQc2whUXrGK53dnMwvDYP++9TQQHHvsSZvd738ybb2Zi7fY/hpXJtggPC9S+CwyBDlBlxDahGA1TfFaB3qgY1dR/oeETM+0WYRRITuJ1AdSBKVlN4FhYfAEGEtpCOUqyE6AhoA0JYVUAq6ME0tYTsKb87riwC9RHzFylWQjMCGAVOLcFioxVszxkKEBTwB0wKSs3dA15th3C+UflKrTNkUv4TMsFCxISjmfNxGB7ecoQ69yT7CseSjAnJVExlSRIEMyW7Pc9WN0D/aR3SwnOfXkueapNS0Ult2D7BcW6ZR40SOk3JcmXj1JHwKloY5THqGa0hsKWpXW7Bb/96CuqrnZtA1QHfhcE+qvnjyBDMEPpO189gSKby4C0gabkaneclyFmNiv7BwGs+ACVLP3aSjh/yn4HOBtHK48ZrgjI+jgYaTsClgUwzT4Bz0UYDRRIVtgWfUIXzwJDqYsti9ws+uboSan6do7vZ9/LsGTIf579AA5dgaZVt7Spn015Xrncul8S60gU4iXFr6xq7Dvwzed6XwIbFtNPR9XZgSvQhkygAzVvA3xQxbqshDM0/JwYzRsnJmHszQRwUYZ4qF8APiqgfy5DjIsE3YyzTbzJcPI+gV4BaYBwFqi2/kAAAAAASUVORK5CYII=",
            "iVBORw0KGgoAAAANSUhEUgAAABoAAAApCAYAAAArpDnNAAACE0lEQVR42t2XvUsjQRiHn4QgKXOFWGtpKVpoCo8g2NyB+NGojSfYiOUVNif3B1ipHApHbCJCLKwEIUgs/CjCwbUWptPCwisDInfNO/DeuLOZ3dk0/iDMMLvZZ9+Z92tza62/BKrY5XoHIE/2qgA14FFGAAoZPXwKqGsLFLQfeAqxaBjYBZ4VxOhCzcdCLKqLFcgWXQKHMhqdAtNAKQ1oGjiWeQPYl7GrkoDMG3aABV9AUpCBzADnCawHaLpAdlxcy/ghQVxVlPc9+MSRgUwkPMtVGZfMQr4HEBM//7m5C7QdAKlFxJLTGWaBwYhI99GE/GdOL0ZZVDWeErBlSz7u/VFZk8m2RVm0CKwEJtiLqMV8RKJsyh53UpxPEejzAaU5fK2GSraxoLvAbdt3XbBBt/RIGlQC2oHPOwDWu4H+AC8pAZPAvRS+o9B65FJVQCtWhc0MtAOUgd/AUJYVFmAU2JT5T+CrbzgUEkK+A3uu6Pf1OqMBx70tYEvyWS0LUDnm/pZk5oq0WT0DAZxJF2R6uqL18wYBjHvkNNNutdNadKJKeZwWrBqWGHRj9Q0+sGoaEMA34Iv02HFn0ACWpdH/JUFcSgJqqNry7KoxoitgBNhQOfONcl2++OrAJ+sFXoEfam1eeeqIK1PkPD4ti9Zniq22WHMVV6F9UlAH+KzcXndITdNbZ51Ub5RXBqegnpfy9wH6BxbrbV3oPNX4AAAAAElFTkSuQmCC"
        };

        private static object BlueCrossSprite(int index)
        {
            if (BlueCrossSprites[index] != null) return BlueCrossSprites[index];
            Type tex = FindType("UnityEngine.Texture2D");
            Type convert = FindType("UnityEngine.ImageConversion");
            Type sprite = FindType("UnityEngine.Sprite");
            Type rect = FindType("UnityEngine.Rect");
            Type vector = FindType("UnityEngine.Vector2");
            if (tex == null || convert == null || sprite == null ||
                rect == null || vector == null) return null;
            MethodInfo load = convert.GetMethod("LoadImage", new[]
                { tex, typeof(byte[]), typeof(bool) });
            MethodInfo create = sprite.GetMethod("Create", new[]
                { tex, rect, vector, typeof(float) });
            if (load == null || create == null) return null;
            object texture = Activator.CreateInstance(tex, new object[] { 26, 41 });
            if (!(bool)load.Invoke(null, new object[]
                { texture, Convert.FromBase64String(BlueCrossPng[index]), false })) return null;
            object area = Activator.CreateInstance(rect, new object[] { 0f, 0f, 26f, 41f });
            object pivot = Activator.CreateInstance(vector, new object[] { 0.5f, 0.5f });
            return BlueCrossSprites[index] = create.Invoke(null,
                new object[] { texture, area, pivot, 100f });
        }

        // 资源中的 crossProgress1/2 与已填充 icon1/2 透明轮廓相同；
        // 仅将蝶舞的灰色默认图着色，不改武器能量或其它 UI。
        public static void TintButterflyCross(object gameObject, string imagePath)
        {
            if (gameObject == null || imagePath == null ||
                !imagePath.StartsWith("/weapon/butterfly_knife/ui/crossProgress",
                                      StringComparison.OrdinalIgnoreCase)) return;
            try
            {
                string name = gameObject.GetType().GetProperty("name", All)
                    .GetValue(gameObject, null) as string;
                if (name != "ImageLeft" && name != "ImageRight") return;
                if (_uiImageType == null) _uiImageType = FindType("UnityEngine.UI.Image");
                if (_uiColorType == null) _uiColorType = FindType("UnityEngine.Color");
                if (_uiImageType == null || _uiColorType == null) return;
                if (_uiGetComponent == null)
                    _uiGetComponent = gameObject.GetType().GetMethod(
                        "GetComponent", new[] { typeof(Type) });
                if (_uiColor == null) _uiColor = _uiImageType.GetProperty("color", All);
                if (_uiSprite == null) _uiSprite = _uiImageType.GetProperty("sprite", All);
                if (_uiGetComponent == null || _uiColor == null) return;
                object image = _uiGetComponent.Invoke(gameObject, new object[] { _uiImageType });
                if (image != null)
                {
                    int index = name == "ImageRight" ? 1 : 0;
                    object blue = BlueCrossSprite(index);
                    if (blue != null && _uiSprite != null)
                    {
                        if (!ReferenceEquals(_uiSprite.GetValue(image, null), blue))
                        {
                            _uiSprite.SetValue(image, blue, null);
                            _uiColor.SetValue(image, Activator.CreateInstance(_uiColorType,
                                new object[] { 1f, 1f, 1f, 1f }), null);
                        }
                    }
                    else
                        _uiColor.SetValue(image, Activator.CreateInstance(_uiColorType,
                            new object[] { 102f / 255f, 204f / 255f, 1f, 1f }), null);
                }
            }
            catch { }
        }

        private static IntPtr BuildButterflyUiTintIL(MethodInfo original)
        {
            Delegate del = CreateButterflyUiTint(original);
            if (del == null) return IntPtr.Zero;
            _pinnedDelegates.Add(del);
            RuntimeHelpers.PrepareMethod(del.Method.MethodHandle);
            return del.Method.MethodHandle.GetFunctionPointer();
        }

        private static Delegate CreateButterflyUiTint(MethodInfo original)
        {
            ParameterInfo[] ps = original.GetParameters();
            if (original.IsStatic || original.ReturnType != typeof(void) || ps.Length != 3)
                return null;
            Type go = ps[0].ParameterType;
            Type data = ps[1].ParameterType;
            Type manager = ps[2].ParameterType;
            FieldInfo path = data.GetField("StringValue", All);
            MethodInfo refresh = manager.GetMethod("RefreshImage", new[]
                { go, typeof(string), typeof(bool) });
            if (path == null || path.FieldType != typeof(string) ||
                refresh == null || refresh.ReturnType != typeof(void)) return null;
            DynamicMethod dm = new DynamicMethod("SsjjButterflyUiTint",
                typeof(void), new[] { original.DeclaringType, go, data, manager },
                typeof(WeaponSwapper).Module, true);
            ILGenerator il = dm.GetILGenerator();
            il.Emit(OpCodes.Ldarg_3);
            il.Emit(OpCodes.Ldarg_1);
            il.Emit(OpCodes.Ldarga_S, (byte)2);
            il.Emit(OpCodes.Ldfld, path);
            il.Emit(OpCodes.Ldc_I4_0);
            il.Emit(OpCodes.Callvirt, refresh);
            il.Emit(OpCodes.Ldarg_1);
            il.Emit(OpCodes.Ldarga_S, (byte)2);
            il.Emit(OpCodes.Ldfld, path);
            il.Emit(OpCodes.Call, typeof(WeaponSwapper).GetMethod(nameof(TintButterflyCross)));
            il.Emit(OpCodes.Ret);
            return dm.CreateDelegate(typeof(Action<,,,>).MakeGenericType(
                original.DeclaringType, go, data, manager));
        }

        private static IntPtr BuildVisualHoldWeaponIL(MethodInfo orig)
        {
            Delegate del = CreateVisualHoldWeapon(orig);
            if (del == null) return IntPtr.Zero;
            _pinnedDelegates.Add(del);
            RuntimeHelpers.PrepareMethod(del.Method.MethodHandle);
            return del.Method.MethodHandle.GetFunctionPointer();
        }

        private static Delegate CreateVisualHoldWeapon(MethodInfo orig)
        {
            if (orig.IsStatic || orig.ReturnType != typeof(bool)) return null;
            Type provider = orig.DeclaringType;
            FieldInfo current = provider.GetField("_currentWeapon", All);
            if (current == null) return null;
            Type entity = current.FieldType;
            Type basic = MemberType(entity, "basicInfo");
            Type data = MemberType(basic, "Data");
            if (MemberType(entity, "hasBasicInfo") != typeof(bool) ||
                MemberType(data, "WeaponName") != typeof(string))
                return null;

            DynamicMethod dm = new DynamicMethod("SsjjVisualHoldWeapon", typeof(bool),
                new[] { provider, typeof(string) }, typeof(WeaponSwapper).Module, true);
            ILGenerator il = dm.GetILGenerator();
            LocalBuilder weapon = il.DeclareLocal(entity);
            Label absent = il.DefineLabel();
            il.Emit(OpCodes.Ldarg_0);
            il.Emit(OpCodes.Ldfld, current);
            il.Emit(OpCodes.Stloc, weapon);
            il.Emit(OpCodes.Ldloc, weapon);
            il.Emit(OpCodes.Brfalse, absent);
            il.Emit(OpCodes.Ldloc, weapon);
            EmitGet(il, entity, "hasBasicInfo");
            il.Emit(OpCodes.Brfalse, absent);
            il.Emit(OpCodes.Ldloc, weapon);
            EmitGet(il, entity, "basicInfo");
            EmitGet(il, basic, "Data");
            EmitGet(il, data, "WeaponName");
            il.Emit(OpCodes.Ldarg_1);
            il.Emit(OpCodes.Call, typeof(WeaponSwapper).GetMethod(nameof(IsVisualHoldWeapon)));
            il.Emit(OpCodes.Ret);
            il.MarkLabel(absent);
            il.Emit(OpCodes.Ldc_I4_0);
            il.Emit(OpCodes.Ret);

            return dm.CreateDelegate(typeof(Func<,,>).MakeGenericType(provider, typeof(string), typeof(bool)));
        }

        private static IntPtr BuildCrossWeaponIL(Type uiCond)
        {
            PropertyInfo pCurWe = uiCond.GetProperty("CurrentWeaponEntity", All);
            FieldInfo fCurWe = pCurWe == null ? uiCond.GetField("CurrentWeaponEntity", All) : null;
            Type tWe = pCurWe != null ? pCurWe.PropertyType : (fCurWe != null ? fCurWe.FieldType : null);
            if (tWe == null) return IntPtr.Zero;

            Type tBi = MemberType(tWe, "basicInfo");
            Type tData = MemberType(tBi, "Data");
            if (tBi == null || tData == null || MemberType(tData, "WeaponName") == null)
                return IntPtr.Zero;

            MethodInfo mResolveModel = typeof(WeaponSwapper).GetMethod(nameof(ResolveModelWeapon), BindingFlags.Public | BindingFlags.Static);

            DynamicMethod dm = new DynamicMethod(
                "SsjjGetCrossWeapon", typeof(string), Type.EmptyTypes, typeof(WeaponSwapper).Module, true);
            ILGenerator il = dm.GetILGenerator();

            LocalBuilder locWe = il.DeclareLocal(tWe);
            LocalBuilder locRes = il.DeclareLocal(typeof(string));
            Label lblEmpty = il.DefineLabel();

            if (pCurWe != null && pCurWe.GetGetMethod(true) != null)
                il.Emit(OpCodes.Call, pCurWe.GetGetMethod(true));
            else
                il.Emit(OpCodes.Ldsfld, fCurWe);
            il.Emit(OpCodes.Stloc, locWe);

            il.Emit(OpCodes.Ldloc, locWe);
            il.Emit(OpCodes.Brfalse, lblEmpty);

            if (MemberType(tWe, "hasBasicInfo") == typeof(bool))
            {
                il.Emit(OpCodes.Ldloc, locWe);
                EmitGet(il, tWe, "hasBasicInfo");
                il.Emit(OpCodes.Brfalse, lblEmpty);
            }

            il.Emit(OpCodes.Ldloc, locWe);
            EmitGet(il, tWe, "basicInfo");
            EmitGet(il, tBi, "Data");
            EmitGet(il, tData, "WeaponName");
            il.Emit(OpCodes.Call, mResolveModel);
            il.Emit(OpCodes.Ret);

            il.MarkLabel(lblEmpty);
            il.Emit(OpCodes.Ldstr, "");
            il.Emit(OpCodes.Ret);

            Delegate del = dm.CreateDelegate(typeof(Func<string>));
            _pinnedDelegates.Add(del);
            MethodInfo mi = del.Method;
            RuntimeHelpers.PrepareMethod(mi.MethodHandle);
            return mi.MethodHandle.GetFunctionPointer();
        }

        private static IntPtr BuildWeaponSoundIL(MethodInfo orig)
        {
            Type tAssetInfo = orig.ReturnType;
            ConstructorInfo ctor2 = tAssetInfo.GetConstructor(new[] { typeof(string), typeof(string) });
            if (ctor2 == null) return IntPtr.Zero;

            MethodInfo mResolveModel = typeof(WeaponSwapper).GetMethod(nameof(ResolveModelWeapon), BindingFlags.Public | BindingFlags.Static);
            MethodInfo mBundle = typeof(WeaponSwapper).GetMethod(nameof(SoundBundle), BindingFlags.NonPublic | BindingFlags.Static);
            MethodInfo mAsset = typeof(WeaponSwapper).GetMethod(nameof(SoundAsset), BindingFlags.NonPublic | BindingFlags.Static);

            DynamicMethod dm = new DynamicMethod(
                "SsjjGetWeaponSound", tAssetInfo, new[] { typeof(string), typeof(string) }, typeof(WeaponSwapper).Module, true);
            ILGenerator il = dm.GetILGenerator();

            LocalBuilder locW = il.DeclareLocal(typeof(string));

            // w = ResolveModelWeapon(arg0)
            il.Emit(OpCodes.Ldarg_0);
            il.Emit(OpCodes.Call, mResolveModel);
            il.Emit(OpCodes.Stloc, locW);

            il.Emit(OpCodes.Ldloc, locW);
            il.Emit(OpCodes.Ldarg_1);
            il.Emit(OpCodes.Call, mBundle);

            il.Emit(OpCodes.Ldloc, locW);
            il.Emit(OpCodes.Ldarg_1);
            il.Emit(OpCodes.Call, mAsset);

            il.Emit(OpCodes.Newobj, ctor2);
            il.Emit(OpCodes.Ret);

            Type delType = typeof(Func<,,>).MakeGenericType(typeof(string), typeof(string), tAssetInfo);
            Delegate del = dm.CreateDelegate(delType);
            _pinnedDelegates.Add(del);
            MethodInfo mi = del.Method;
            RuntimeHelpers.PrepareMethod(mi.MethodHandle);
            return mi.MethodHandle.GetFunctionPointer();
        }

        // 蝶舞动画事件使用 fire2 / fire3。通用武器音效查询仍传进 _fire /
        // _sec_fire；它们在蝶舞音效包中不存在，且 fire2 单独打包。
        private static string SoundBundle(string weapon, string suffix)
        {
            if (string.Equals(weapon, "butterfly_knife", StringComparison.OrdinalIgnoreCase) &&
                string.Equals(suffix, "_fire", StringComparison.OrdinalIgnoreCase))
                return "sound/weapon/butterfly_knife_fire2";
            return "sound/weapon/" + weapon;
        }

        private static string SoundAsset(string weapon, string suffix)
        {
            if (string.Equals(weapon, "butterfly_knife", StringComparison.OrdinalIgnoreCase))
            {
                if (string.Equals(suffix, "_fire", StringComparison.OrdinalIgnoreCase))
                    suffix = "_fire2";
                else if (string.Equals(suffix, "_sec_fire", StringComparison.OrdinalIgnoreCase))
                    suffix = "_fire3";
            }
            return "Assets/Res/Assets/sound/weapon/" + weapon + suffix + ".mp3";
        }

        private static bool EmitGet(ILGenerator il, Type t, string name)
        {
            PropertyInfo p = t.GetProperty(name, All);
            if (p != null && p.GetGetMethod(true) != null)
            {
                il.Emit(OpCodes.Callvirt, p.GetGetMethod(true));
                return true;
            }
            FieldInfo f = t.GetField(name, All);
            if (f != null)
            {
                il.Emit(OpCodes.Ldfld, f);
                return true;
            }
            return false;
        }

        private static bool EmitSet(ILGenerator il, Type t, string name)
        {
            PropertyInfo p = t.GetProperty(name, All);
            if (p != null && p.GetSetMethod(true) != null)
            {
                il.Emit(OpCodes.Callvirt, p.GetSetMethod(true));
                return true;
            }
            FieldInfo f = t.GetField(name, All);
            if (f != null)
            {
                il.Emit(OpCodes.Stfld, f);
                return true;
            }
            return false;
        }

        private static Type MemberType(Type t, string name)
        {
            if (t == null) return null;
            PropertyInfo p = t.GetProperty(name, All);
            if (p != null) return p.PropertyType;
            FieldInfo f = t.GetField(name, All);
            if (f != null) return f.FieldType;
            return null;
        }

        private static void EmitConvert(ILGenerator il, Type src, Type dst)
        {
            if (src == null || dst == null || src == dst) return;
            if (!src.IsValueType && !dst.IsValueType)
            {
                if (!dst.IsAssignableFrom(src))
                    il.Emit(OpCodes.Castclass, dst);
                return;
            }
            bool srcUnsigned = (src == typeof(uint) || src == typeof(ulong) || src == typeof(ushort) || src == typeof(byte));
            if (dst == typeof(float))
            {
                if (srcUnsigned) il.Emit(OpCodes.Conv_R_Un);
                il.Emit(OpCodes.Conv_R4);
            }
            else if (dst == typeof(double))
            {
                if (srcUnsigned) il.Emit(OpCodes.Conv_R_Un);
                il.Emit(OpCodes.Conv_R8);
            }
            else if (dst == typeof(int)) il.Emit(OpCodes.Conv_I4);
            else if (dst == typeof(uint)) il.Emit(OpCodes.Conv_U4);
            else if (dst == typeof(long)) il.Emit(OpCodes.Conv_I8);
            else if (dst == typeof(ulong)) il.Emit(OpCodes.Conv_U8);
            else if (dst == typeof(short)) il.Emit(OpCodes.Conv_I2);
            else if (dst == typeof(ushort)) il.Emit(OpCodes.Conv_U2);
            else if (dst == typeof(byte)) il.Emit(OpCodes.Conv_U1);
            else if (dst == typeof(sbyte)) il.Emit(OpCodes.Conv_I1);
        }

        private static void EmitCopyDirect(ILGenerator il, Type tDst, string dstName, Type tSrc, string srcName, bool srcIsLocal, LocalBuilder srcLocal)
        {
            Type dstT = MemberType(tDst, dstName);
            Type srcT = MemberType(tSrc, srcName);
            if (dstT == null || srcT == null)
            {
                Loader.Log("[skin] 跳过缺失成员: " + tSrc.Name + "." + srcName + " -> " + tDst.Name + "." + dstName);
                return;
            }
            il.Emit(OpCodes.Ldarg_0);
            if (srcIsLocal) il.Emit(OpCodes.Ldloc, srcLocal);
            else il.Emit(OpCodes.Ldarg_1);
            EmitGet(il, tSrc, srcName);
            EmitConvert(il, srcT, dstT);
            EmitSet(il, tDst, dstName);
        }

        private static IntPtr CompileDynamicMethod(DynamicMethod dm, Type p0, Type p1)
        {
            Type delType = typeof(Action<,>).MakeGenericType(p0, p1);
            Delegate del = dm.CreateDelegate(delType);
            _pinnedDelegates.Add(del);
            // Mono 下 DynamicMethod.CreateDelegate 会立即编译，通过 MethodHandle 或 Marshal 取函数指针
            MethodInfo mi = del.Method;
            RuntimeHelpers.PrepareMethod(mi.MethodHandle);
            return mi.MethodHandle.GetFunctionPointer();
        }

        private static IntPtr BuildPlayerDataCopyIL(MethodInfo orig)
        {
            ParameterInfo[] ps = orig.GetParameters();
            Type tComp = ps[0].ParameterType;
            Type tPd = ps[1].ParameterType;
            Type tFactory = FindType("Assets.Sources.Info.Weapon.WeaponInfoFactory");
            FieldInfo fInst = tFactory != null ? tFactory.GetField("Instance", All) : null;
            MethodInfo mGetInfo = tFactory != null ? tFactory.GetMethod("GetWeaponInfo", new[] { typeof(string) }) : null;
            MethodInfo mResolve = typeof(WeaponSwapper).GetMethod(nameof(ResolveSkin), BindingFlags.Public | BindingFlags.Static);
            MethodInfo mResolveForPlayer = typeof(WeaponSwapper).GetMethod(nameof(ResolveSkinForPlayer), BindingFlags.Public | BindingFlags.Static);
            MethodInfo mResolveWiForPlayer = typeof(WeaponSwapper).GetMethod(nameof(ResolveWeaponInfoForPlayer), BindingFlags.Public | BindingFlags.Static);
            bool hasOwnerId = MemberType(tPd, "Id") == typeof(int);

            DynamicMethod dm = new DynamicMethod(
                "SsjjCopyFromPD", typeof(void), new[] { tComp, tPd }, typeof(WeaponSwapper).Module, true);
            ILGenerator il = dm.GetILGenerator();

            LocalBuilder locName = il.DeclareLocal(typeof(string));
            Label lblRet = il.DefineLabel();

            // if (comp == null || pd == null) return;
            il.Emit(OpCodes.Ldarg_0);
            il.Emit(OpCodes.Brfalse, lblRet);
            il.Emit(OpCodes.Ldarg_1);
            il.Emit(OpCodes.Brfalse, lblRet);

            EmitCopyDirect(il, tComp, "Weapon", tPd, "CurrentWeapon", false, null);
            EmitCopyDirect(il, tComp, "Index", tPd, "CurrentWeaponIndex", false, null);

            // wname = pd.CurrentWeaponName; comp.Name = wname
            il.Emit(OpCodes.Ldarg_1);
            EmitGet(il, tPd, "CurrentWeaponName");
            il.Emit(OpCodes.Stloc, locName);
            il.Emit(OpCodes.Ldarg_0);
            il.Emit(OpCodes.Ldloc, locName);
            EmitSet(il, tComp, "Name");

            EmitCopyDirect(il, tComp, "Level", tPd, "CurrentWeaponLevel", false, null);

            // comp.SkinName = ResolveSkinForPlayer(pd.Id, wname, pd.CurrentWeaponSkinName)
            if (MemberType(tComp, "SkinName") != null && MemberType(tPd, "CurrentWeaponSkinName") != null)
            {
                il.Emit(OpCodes.Ldarg_0);
                if (hasOwnerId && mResolveForPlayer != null)
                {
                    il.Emit(OpCodes.Ldarg_1);
                    EmitGet(il, tPd, "Id");
                    il.Emit(OpCodes.Ldloc, locName);
                    il.Emit(OpCodes.Ldarg_1);
                    EmitGet(il, tPd, "CurrentWeaponSkinName");
                    il.Emit(OpCodes.Call, mResolveForPlayer);
                }
                else
                {
                    il.Emit(OpCodes.Ldloc, locName);
                    il.Emit(OpCodes.Ldarg_1);
                    EmitGet(il, tPd, "CurrentWeaponSkinName");
                    il.Emit(OpCodes.Call, mResolve);
                }
                EmitSet(il, tComp, "SkinName");
            }

            EmitCopyDirect(il, tComp, "Worn", tPd, "CurrentWeaponWorn", false, null);
            EmitCopyDirect(il, tComp, "QuenchId", tPd, "CurrentWeaponQuenchId", false, null);

            // comp.WeaponInfo = (IEntitsWeaponInfo)ResolveWeaponInfoForPlayer(pd.Id, wname, WeaponInfoFactory.Instance.GetWeaponInfo(wname))
            if (fInst != null && mGetInfo != null && MemberType(tComp, "WeaponInfo") != null)
            {
                Type wiType = MemberType(tComp, "WeaponInfo");
                il.Emit(OpCodes.Ldarg_0);
                if (hasOwnerId && mResolveWiForPlayer != null)
                {
                    il.Emit(OpCodes.Ldarg_1);
                    EmitGet(il, tPd, "Id");
                    il.Emit(OpCodes.Ldloc, locName);
                    il.Emit(OpCodes.Ldsfld, fInst);
                    il.Emit(OpCodes.Ldloc, locName);
                    il.Emit(OpCodes.Callvirt, mGetInfo);
                    il.Emit(OpCodes.Call, mResolveWiForPlayer);
                    EmitConvert(il, typeof(object), wiType);
                }
                else
                {
                    il.Emit(OpCodes.Ldsfld, fInst);
                    il.Emit(OpCodes.Ldloc, locName);
                    il.Emit(OpCodes.Callvirt, mGetInfo);
                    EmitConvert(il, mGetInfo.ReturnType, wiType);
                }
                EmitSet(il, tComp, "WeaponInfo");
            }

            il.MarkLabel(lblRet);
            il.Emit(OpCodes.Ret);
            return CompileDynamicMethod(dm, tComp, tPd);
        }

        private static IntPtr BuildWeaponEntityCopyIL(MethodInfo orig, int rougeOffset)
        {
            ParameterInfo[] ps = orig.GetParameters();
            Type tComp = ps[0].ParameterType;
            Type tWe = ps[1].ParameterType;
            Type tSlot = MemberType(tWe, "slot");
            Type tRouge = MemberType(tWe, "rouge");
            Type tBi = MemberType(tWe, "basicInfo");
            Type tData = MemberType(tBi, "Data");
            MethodInfo mResolveForComp = typeof(WeaponSwapper).GetMethod(nameof(ResolveSkinForComponent), BindingFlags.Public | BindingFlags.Static);
            MethodInfo mResolveWiForComp = typeof(WeaponSwapper).GetMethod(nameof(ResolveWeaponInfoForComponent), BindingFlags.Public | BindingFlags.Static);

            DynamicMethod dm = new DynamicMethod(
                "SsjjCopyFromWE", typeof(void), new[] { tComp, tWe }, typeof(WeaponSwapper).Module, true);
            ILGenerator il = dm.GetILGenerator();

            LocalBuilder locData = il.DeclareLocal(tData);
            LocalBuilder locName = il.DeclareLocal(typeof(string));
            LocalBuilder locSkin = il.DeclareLocal(typeof(string));
            Label lblNoRouge = il.DefineLabel();
            Label lblRet = il.DefineLabel();

            // if (comp == null || weaponEntity == null) return;
            il.Emit(OpCodes.Ldarg_0);
            il.Emit(OpCodes.Brfalse, lblRet);
            il.Emit(OpCodes.Ldarg_1);
            il.Emit(OpCodes.Brfalse, lblRet);

            // comp.Weapon = weaponEntity.slot.Slot
            if (tSlot != null && MemberType(tSlot, "Slot") != null && MemberType(tComp, "Weapon") != null)
            {
                il.Emit(OpCodes.Ldarg_0);
                il.Emit(OpCodes.Ldarg_1);
                EmitGet(il, tWe, "slot");
                EmitGet(il, tSlot, "Slot");
                EmitConvert(il, MemberType(tSlot, "Slot"), MemberType(tComp, "Weapon"));
                EmitSet(il, tComp, "Weapon");
            }

            // if (weaponEntity.hasRouge) comp.Weapon = weaponEntity.rouge.rougeWeaponIndex + rougeOffset
            if (tRouge != null && MemberType(tWe, "hasRouge") == typeof(bool) && MemberType(tRouge, "rougeWeaponIndex") != null)
            {
                il.Emit(OpCodes.Ldarg_1);
                EmitGet(il, tWe, "hasRouge");
                il.Emit(OpCodes.Brfalse_S, lblNoRouge);
                il.Emit(OpCodes.Ldarg_0);
                il.Emit(OpCodes.Ldarg_1);
                EmitGet(il, tWe, "rouge");
                EmitGet(il, tRouge, "rougeWeaponIndex");
                il.Emit(OpCodes.Ldc_I4, rougeOffset);
                il.Emit(OpCodes.Add);
                EmitConvert(il, typeof(int), MemberType(tComp, "Weapon"));
                EmitSet(il, tComp, "Weapon");
                il.MarkLabel(lblNoRouge);
            }

            // comp.Index = weaponEntity.slot.Index
            if (tSlot != null && MemberType(tSlot, "Index") != null && MemberType(tComp, "Index") != null)
            {
                il.Emit(OpCodes.Ldarg_0);
                il.Emit(OpCodes.Ldarg_1);
                EmitGet(il, tWe, "slot");
                EmitGet(il, tSlot, "Index");
                EmitConvert(il, MemberType(tSlot, "Index"), MemberType(tComp, "Index"));
                EmitSet(il, tComp, "Index");
            }

            // data = weaponEntity.basicInfo.Data
            il.Emit(OpCodes.Ldarg_1);
            EmitGet(il, tWe, "basicInfo");
            EmitGet(il, tBi, "Data");
            il.Emit(OpCodes.Stloc, locData);

            // wname = data.WeaponName; comp.Name = wname
            il.Emit(OpCodes.Ldloc, locData);
            EmitGet(il, tData, "WeaponName");
            il.Emit(OpCodes.Stloc, locName);

            il.Emit(OpCodes.Ldarg_0);
            il.Emit(OpCodes.Ldloc, locName);
            EmitSet(il, tComp, "Name");

            EmitCopyDirect(il, tComp, "Level", tData, "Level", true, locData);

            // comp.WeaponInfo = (IEntitsWeaponInfo)ResolveWeaponInfoForComponent(comp, wname, weaponEntity.basicInfo.Info)
            if (tBi != null && MemberType(tBi, "Info") != null && MemberType(tComp, "WeaponInfo") != null)
            {
                Type wiType = MemberType(tComp, "WeaponInfo");
                il.Emit(OpCodes.Ldarg_0);
                if (mResolveWiForComp != null)
                {
                    il.Emit(OpCodes.Ldarg_0);
                    il.Emit(OpCodes.Ldloc, locName);
                    il.Emit(OpCodes.Ldarg_1);
                    EmitGet(il, tWe, "basicInfo");
                    EmitGet(il, tBi, "Info");
                    il.Emit(OpCodes.Call, mResolveWiForComp);
                    EmitConvert(il, typeof(object), wiType);
                }
                else
                {
                    il.Emit(OpCodes.Ldarg_1);
                    EmitGet(il, tWe, "basicInfo");
                    EmitGet(il, tBi, "Info");
                    EmitConvert(il, MemberType(tBi, "Info"), wiType);
                }
                EmitSet(il, tComp, "WeaponInfo");
            }

            // skin = WeaponSwapper.ResolveSkinForComponent(comp, wname, data.WeaponSkinName)
            // 同时写入 comp.SkinName 与 data.WeaponSkinName（确保屠魔素玉标记特效/地面/背枪/UI 同步生效）
            if (MemberType(tComp, "SkinName") != null && MemberType(tData, "WeaponSkinName") != null)
            {
                il.Emit(OpCodes.Ldarg_0);
                il.Emit(OpCodes.Ldloc, locName);
                il.Emit(OpCodes.Ldloc, locData);
                EmitGet(il, tData, "WeaponSkinName");
                il.Emit(OpCodes.Call, mResolveForComp);
                il.Emit(OpCodes.Stloc, locSkin);

                il.Emit(OpCodes.Ldarg_0);
                il.Emit(OpCodes.Ldloc, locSkin);
                EmitSet(il, tComp, "SkinName");

                il.Emit(OpCodes.Ldloc, locData);
                il.Emit(OpCodes.Ldloc, locSkin);
                EmitSet(il, tData, "WeaponSkinName");
            }

            EmitCopyDirect(il, tComp, "Worn", tData, "WeaponWorn", true, locData);
            EmitCopyDirect(il, tComp, "QuenchId", tData, "QuenchId", true, locData);

            il.MarkLabel(lblRet);
            il.Emit(OpCodes.Ret);
            return CompileDynamicMethod(dm, tComp, tWe);
        }

        // ------------------------------------------------------------------ 还原
        /// <summary>
        /// 把所有 detour 过的方法入口写回注入前的 12 字节，并停止后台改写。
        /// 对局结束 / 进程退出时调用：让 GameGuard 收尾扫描看到一个干净的可执行段。
        /// 返回还原成功的方法个数。
        /// </summary>
        public static int UninstallHooks()
        {
            int n = 0;
            _awpTrueSinceTick = 0;
            InspectPlayback.Reset();
            lock (_hookLock)
            {
                foreach (KeyValuePair<IntPtr, byte[]> kv in _hookOrig)
                {
                    try
                    {
                        uint oldProt;
                        if (!VirtualProtect(kv.Key, (UIntPtr)16, PAGE_EXECUTE_READWRITE, out oldProt))
                            continue;
                        Marshal.Copy(kv.Value, 0, kv.Key, kv.Value.Length);
                        uint dummy;
                        VirtualProtect(kv.Key, (UIntPtr)16, oldProt, out dummy);
                        n++;
                    }
                    catch (Exception ex)
                    {
                        Loader.Log("[skin] 还原 Hook 失败: " + ex.Message);
                    }
                }
                _hookOrig.Clear();
            }
            _hooksInstalled = false;
            _stopped = true;   // Loop() 下一轮直接返回，不再回写配置
            _localWeaponInfoClones = new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase);
            _appliedLocalClones.Clear();
            Loader.Log("[skin] 已还原 " + n + " 个方法入口（detour 全部撤销）");
            return n;
        }

        private static bool InstallJumpHookPtr(MethodInfo target, IntPtr dstPtr)
        {
            if (target == null || dstPtr == IntPtr.Zero || IntPtr.Size != 8) return false;
            try
            {
                RuntimeHelpers.PrepareMethod(target.MethodHandle);
                IntPtr srcPtr = target.MethodHandle.GetFunctionPointer();
                if (srcPtr == IntPtr.Zero) return false;

                byte[] jmp = new byte[12];
                jmp[0] = 0x48;
                jmp[1] = 0xB8;
                long addr = dstPtr.ToInt64();
                for (int i = 0; i < 8; i++)
                    jmp[2 + i] = (byte)((addr >> (i * 8)) & 0xFF);
                jmp[10] = 0xFF;
                jmp[11] = 0xE0;

                uint oldProt;
                if (!VirtualProtect(srcPtr, (UIntPtr)16, PAGE_EXECUTE_READWRITE, out oldProt))
                    return false;
                // 先留底再写：留底失败就不写，宁可没换肤也不要留下还原不了的字节
                byte[] orig = new byte[12];
                Marshal.Copy(srcPtr, orig, 0, orig.Length);
                lock (_hookLock)
                {
                    bool seen = false;
                    for (int i = 0; i < _hookOrig.Count; i++)
                        if (_hookOrig[i].Key == srcPtr) { seen = true; break; }
                    if (!seen) _hookOrig.Add(new KeyValuePair<IntPtr, byte[]>(srcPtr, orig));
                }
                Marshal.Copy(jmp, 0, srcPtr, jmp.Length);
                uint dummy;
                VirtualProtect(srcPtr, (UIntPtr)16, oldProt, out dummy);

                // 写回读一遍：确认跳转真的落在方法入口上（Mono 之后重 JIT 会把它盖掉）
                byte[] back = new byte[12];
                Marshal.Copy(srcPtr, back, 0, back.Length);
                string hex = BitConverter.ToString(back).Replace("-", "");
                string want = BitConverter.ToString(jmp).Replace("-", "");
                Loader.Log("[skin] Hook 已写入 " + target.DeclaringType.Name + "." + target.Name
                           + " 入口=" + hex + (hex == want ? "（校验一致）" : "（与预期不一致，可能被重新 JIT）"));
                return true;
            }
            catch (Exception ex)
            {
                Loader.Log("[skin] Hook 写入失败 " + target.Name + ": " + ex.Message);
                return false;
            }
        }

        // ------------------------------------------------------------------ 配置与映射
        private static bool ReloadConfigIfChanged()
        {
            if (!File.Exists(ConfigPath))
            {
                if (_rules.Count > 0)
                {
                    _rules.Clear();
                    _globalEnabled = false;
                    return true;
                }
                return false;
            }
            DateTime mt = File.GetLastWriteTimeUtc(ConfigPath);
            if (mt == _lastMtime) return false;
            _lastMtime = mt;
            string raw = File.ReadAllText(ConfigPath, Encoding.UTF8);
            bool en = true;
            Match mEn = Regex.Match(raw, "\"enabled\"\\s*:\\s*(true|false)", RegexOptions.IgnoreCase);
            if (mEn.Success) en = mEn.Groups[1].Value.Equals("true", StringComparison.OrdinalIgnoreCase);

            bool selfOnly = true;
            Match mSelf = Regex.Match(raw, "\"self_only\"\\s*:\\s*(true|false)", RegexOptions.IgnoreCase);
            if (mSelf.Success) selfOnly = mSelf.Groups[1].Value.Equals("true", StringComparison.OrdinalIgnoreCase);

            bool killFeedSim = true;
            Match mKf = Regex.Match(raw, "\"killfeed_sim\"\\s*:\\s*(true|false)", RegexOptions.IgnoreCase);
            if (mKf.Success) killFeedSim = mKf.Groups[1].Value.Equals("true", StringComparison.OrdinalIgnoreCase);

            List<SwapRule> parsed = new List<SwapRule>();
            int idx = raw.IndexOf("\"rules\"", StringComparison.OrdinalIgnoreCase);
            if (idx >= 0)
            {
                string tail = raw.Substring(idx);
                foreach (Match mObj in Regex.Matches(tail, "\\{[^{}]+\\}"))
                {
                    string obj = mObj.Value;
                    string src = ExtractStr(obj, "src");
                    string model = ExtractStr(obj, "model");
                    string skin = ExtractStr(obj, "skin");
                    string skinName = ExtractStr(obj, "skin_name");
                    bool rEn = true;
                    Match mREn = Regex.Match(obj, "\"enabled\"\\s*:\\s*(true|false)", RegexOptions.IgnoreCase);
                    if (mREn.Success) rEn = mREn.Groups[1].Value.Equals("true", StringComparison.OrdinalIgnoreCase);
                    if (!string.IsNullOrEmpty(src) && (!string.IsNullOrEmpty(model) || !string.IsNullOrEmpty(skin)))
                    {
                        parsed.Add(new SwapRule
                        {
                            Src = src.Trim(),
                            Model = (model ?? "").Trim(),
                            Skin = (skin ?? "").Trim(),
                            SkinName = (skinName ?? "").Trim(),
                            Enabled = rEn
                        });
                    }
                }
            }
            _globalEnabled = en;
            _selfOnly = selfOnly;
            _killFeedSim = killFeedSim;
            _rules = parsed;
            Loader.Log("[skin] 热加载配置: enabled=" + _globalEnabled +
                       " self_only=" + _selfOnly + " killfeed_sim=" + _killFeedSim +
                       " rules=" + _rules.Count);
            return true;
        }

        private static string ExtractStr(string jsonObj, string key)
        {
            Match m = Regex.Match(jsonObj, "\"" + key + "\"\\s*:\\s*\"((?:\\\\.|[^\"\\\\])*)\"");
            if (!m.Success) return "";
            return m.Groups[1].Value.Replace("\\\"", "\"").Replace("\\\\", "\\");
        }

        private static object GetSkinInfoFactory(out MethodInfo mGetSkin)
        {
            mGetSkin = null;
            try
            {
                Type t = FindType("Assets.Sources.Info.Weapon.WeaponSkinInfoFactory");
                if (t == null) return null;
                object inst = null;
                FieldInfo f = t.GetField("Instance", All);
                if (f != null) inst = f.GetValue(null);
                if (inst == null)
                {
                    PropertyInfo p = t.GetProperty("Instance", All);
                    if (p != null) inst = p.GetValue(null, null);
                }
                if (inst == null) return null;
                mGetSkin = inst.GetType().GetMethod("GetSkinInfo", new[] { typeof(string) });
                return inst;
            }
            catch { return null; }
        }

        private static bool HasSkinInfo(object factory, MethodInfo mGetSkin, string skinCode)
        {
            if (factory == null || mGetSkin == null || string.IsNullOrEmpty(skinCode)) return false;
            try { return mGetSkin.Invoke(factory, new object[] { skinCode }) != null; }
            catch { return false; }
        }

        private static string ResolveFactionSkin(object factory, MethodInfo mGetSkin, string skinCode, string factionSuffix)
        {
            if (string.IsNullOrEmpty(skinCode) || string.IsNullOrEmpty(factionSuffix)) return skinCode;
            string baseSkin = skinCode;
            if (baseSkin.EndsWith("_ct", StringComparison.OrdinalIgnoreCase))
                baseSkin = baseSkin.Substring(0, baseSkin.Length - 3);
            else if (baseSkin.EndsWith("_t", StringComparison.OrdinalIgnoreCase))
                baseSkin = baseSkin.Substring(0, baseSkin.Length - 2);

            string withSuffix = baseSkin + factionSuffix;
            if (factory != null && mGetSkin != null)
            {
                if (HasSkinInfo(factory, mGetSkin, withSuffix)) return withSuffix;
                if (HasSkinInfo(factory, mGetSkin, skinCode)) return skinCode;
                if (HasSkinInfo(factory, mGetSkin, baseSkin)) return baseSkin;
            }
            // 工厂尚未就绪时，已知双阵营皮肤直接补齐后缀
            if (baseSkin.IndexOf("awm_legend", StringComparison.OrdinalIgnoreCase) >= 0)
                return withSuffix;
            return skinCode;
        }

        private static void RebuildSkinMap(bool forceLog)
        {
            MethodInfo mGetSkin;
            object skinFactory = GetSkinInfoFactory(out mGetSkin);
            bool factoryReady = (skinFactory != null && mGetSkin != null);
            bool needRefresh = forceLog || (factoryReady && !_skinMapResolvedWithFactory);
            if (!needRefresh && (_activeSkins.Count > 0 || _activeModels.Count > 0)) return;

            Dictionary<string, string> next = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            foreach (SwapRule r in _rules)
            {
                if (!r.Enabled || string.IsNullOrEmpty(r.Skin)) continue;
                string src = r.Src;
                string skin = r.Skin;

                if (src.EndsWith("_ct", StringComparison.OrdinalIgnoreCase))
                {
                    string baseW = src.Substring(0, src.Length - 3);
                    next[src] = ResolveFactionSkin(skinFactory, mGetSkin, skin, "_ct");
                    if (!next.ContainsKey(baseW + "_t"))
                        next[baseW + "_t"] = ResolveFactionSkin(skinFactory, mGetSkin, skin, "_t");
                    if (!next.ContainsKey(baseW))
                        next[baseW] = skin;
                }
                else if (src.EndsWith("_t", StringComparison.OrdinalIgnoreCase))
                {
                    string baseW = src.Substring(0, src.Length - 2);
                    next[src] = ResolveFactionSkin(skinFactory, mGetSkin, skin, "_t");
                    if (!next.ContainsKey(baseW + "_ct"))
                        next[baseW + "_ct"] = ResolveFactionSkin(skinFactory, mGetSkin, skin, "_ct");
                    if (!next.ContainsKey(baseW))
                        next[baseW] = skin;
                }
                else
                {
                    string baseSkin = skin;
                    if (!string.IsNullOrEmpty(r.Model))
                    {
                        if (r.Model.EndsWith("_ct", StringComparison.OrdinalIgnoreCase))
                            baseSkin = ResolveFactionSkin(skinFactory, mGetSkin, skin, "_ct");
                        else if (r.Model.EndsWith("_t", StringComparison.OrdinalIgnoreCase))
                            baseSkin = ResolveFactionSkin(skinFactory, mGetSkin, skin, "_t");
                    }
                    next[src] = baseSkin;
                    // 若存在双阵营皮肤（如屠魔 awm_legend -> _ct / _t），自动同时绑定
                    string sCt = ResolveFactionSkin(skinFactory, mGetSkin, skin, "_ct");
                    string sT = ResolveFactionSkin(skinFactory, mGetSkin, skin, "_t");
                    if (!sCt.Equals(skin, StringComparison.OrdinalIgnoreCase) ||
                        src.IndexOf("awm_legend", StringComparison.OrdinalIgnoreCase) >= 0)
                    {
                        next[src + "_ct"] = sCt;
                        next[src + "_t"] = sT;
                    }
                }
            }

            _activeModels = BuildModelMap();

            _activeSkins = next;
            if (factoryReady) _skinMapResolvedWithFactory = true;

            if (needRefresh)
            {
                StringBuilder sb = new StringBuilder();
                foreach (KeyValuePair<string, string> kv in next)
                {
                    if (sb.Length > 0) sb.Append(", ");
                    sb.Append(kv.Key).Append("->").Append(kv.Value);
                }
                Loader.Log("[skin] 已更新皮肤绑定表(factory=" + factoryReady + ")，生效条目=" + next.Count + ": " + sb.ToString());
            }
        }

        // ------------------------------------------------------------------ 武器建模替换
        private static Type FindType(string fullName)
        {
            foreach (Assembly asm in AppDomain.CurrentDomain.GetAssemblies())
            {
                try
                {
                    Type t = asm.GetType(fullName, false);
                    if (t != null) return t;
                }
                catch { }
            }
            return null;
        }

        private static object GetWeaponInfoFactoryInstance()
        {
            if (_weaponInfoFactoryInst != null) return _weaponInfoFactoryInst;
            if (_weaponInfoFactoryType == null)
                _weaponInfoFactoryType = FindType("Assets.Sources.Info.Weapon.WeaponInfoFactory");
            if (_weaponInfoFactoryType == null) return null;
            FieldInfo f = _weaponInfoFactoryType.GetField("Instance", All);
            if (f != null) _weaponInfoFactoryInst = f.GetValue(null);
            if (_weaponInfoFactoryInst == null)
            {
                PropertyInfo p = _weaponInfoFactoryType.GetProperty("Instance", All);
                if (p != null) _weaponInfoFactoryInst = p.GetValue(null, null);
            }
            if (_weaponInfoFactoryInst != null)
                _getWeaponInfoMethod = _weaponInfoFactoryInst.GetType().GetMethod("GetWeaponInfo", new[] { typeof(string) });
            return _weaponInfoFactoryInst;
        }

        private static object CallGetWeaponInfo(object factory, string name)
        {
            if (factory == null || string.IsNullOrEmpty(name)) return null;
            MethodInfo m = _getWeaponInfoMethod ?? factory.GetType().GetMethod("GetWeaponInfo", new[] { typeof(string) });
            if (m == null) return null;
            try { return m.Invoke(factory, new object[] { name }); }
            catch { return null; }
        }

        private static string FactionBase(string weapon)
        {
            if (weapon.EndsWith("_ct", StringComparison.OrdinalIgnoreCase))
                return weapon.Substring(0, weapon.Length - 3);
            if (weapon.EndsWith("_t", StringComparison.OrdinalIgnoreCase))
                return weapon.Substring(0, weapon.Length - 2);
            return weapon;
        }

        private static Dictionary<string, string> BuildModelMap()
        {
            var map = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            // 基名规则覆盖双阵营；仅指定阵营时，补上基名和另一阵营。
            foreach (SwapRule r in _rules)
            {
                if (!r.Enabled || string.IsNullOrEmpty(r.Model) ||
                    r.Src.Equals(r.Model, StringComparison.OrdinalIgnoreCase)) continue;
                if (FactionBase(r.Src) != r.Src) continue;
                map[r.Src] = r.Model;
                map[r.Src + "_ct"] = r.Model;
                map[r.Src + "_t"] = r.Model;
            }
            foreach (SwapRule r in _rules)
            {
                if (!r.Enabled || string.IsNullOrEmpty(r.Model) ||
                    r.Src.Equals(r.Model, StringComparison.OrdinalIgnoreCase)) continue;
                string baseName = FactionBase(r.Src);
                if (baseName == r.Src) continue;
                if (!map.ContainsKey(baseName)) map[baseName] = r.Model;
                if (!map.ContainsKey(baseName + "_ct")) map[baseName + "_ct"] = r.Model;
                if (!map.ContainsKey(baseName + "_t")) map[baseName + "_t"] = r.Model;
                map[r.Src] = r.Model; // 显式阵营规则覆盖通用规则
            }
            return map;
        }

        private static bool AllModelsApplied(Dictionary<string, string> desired)
        {
            Dictionary<string, string> table = _selfOnly ? _appliedLocalClones : _appliedModels;
            foreach (var kv in desired)
            {
                string applied;
                if (!table.TryGetValue(kv.Key, out applied) ||
                    !applied.Equals(kv.Value, StringComparison.OrdinalIgnoreCase))
                    return false;
            }
            return true;
        }

        private static object CloneWeaponInfoWithVisuals(object srcInfo, object dstInfo, out int copied)
        {
            copied = 0;
            if (srcInfo == null || dstInfo == null || _memberwiseCloneMethod == null) return null;
            try
            {
                object clone = _memberwiseCloneMethod.Invoke(srcInfo, null);
                if (clone == null) return null;

                foreach (string fn in VisualFields)
                {
                    FieldInfo sfi = FindFieldInHierarchy(clone.GetType(), fn);
                    if (sfi == null) continue;
                    FieldInfo dfi = FindFieldInHierarchy(dstInfo.GetType(), fn);
                    if (dfi == null) continue;
                    try
                    {
                        sfi.SetValue(clone, dfi.GetValue(dstInfo));
                        copied++;
                    }
                    catch { }
                }
                return copied > 0 ? clone : null;
            }
            catch
            {
                return null;
            }
        }

        private static FieldInfo FindFieldInHierarchy(Type t, string name)
        {
            while (t != null && t != typeof(object))
            {
                FieldInfo f = t.GetField(name, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly);
                if (f != null) return f;
                t = t.BaseType;
            }
            return null;
        }

        private static void ApplyModelSwaps()
        {
            object factory = GetWeaponInfoFactoryInstance();
            if (factory == null) return;

            Dictionary<string, string> want = BuildModelMap();

            if (_selfOnly)
            {
                // 切到仅自己模式时，先还原先前可能写入过的全局 WeaponInfo，确保其他玩家不受影响
                if (_appliedModels.Count > 0)
                    RestoreAllModels();

                var nextClones = new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase);
                var prevClones = _localWeaponInfoClones;
                bool anyAttempt = false;
                bool anyCloneOk = false;

                foreach (var kv in want)
                {
                    string src = kv.Key;
                    string dst = kv.Value;

                    string curDst;
                    object existingClone;
                    if (_appliedLocalClones.TryGetValue(src, out curDst) &&
                        curDst.Equals(dst, StringComparison.OrdinalIgnoreCase) &&
                        prevClones != null && prevClones.TryGetValue(src, out existingClone) &&
                        existingClone != null)
                    {
                        nextClones[src] = existingClone;
                        anyCloneOk = true;
                        continue;
                    }

                    object srcInfo = CallGetWeaponInfo(factory, src);
                    object dstInfo = CallGetWeaponInfo(factory, dst);
                    if (srcInfo == null || dstInfo == null) continue;

                    anyAttempt = true;
                    int copied;
                    object cloned = CloneWeaponInfoWithVisuals(srcInfo, dstInfo, out copied);
                    if (cloned != null)
                    {
                        nextClones[src] = cloned;
                        _appliedLocalClones[src] = dst;
                        SyncShowGunAction(src, dst);
                        anyCloneOk = true;
                        Loader.Log("[skin] 本地玩家建模副本生效: " + src + " -> " + dst + " (字段=" + copied + "，仅自己可见)");
                    }
                }

                // 清理已移除规则的 ShowGun 动作与缓存记录
                foreach (string oldSrc in new List<string>(_appliedLocalClones.Keys))
                {
                    if (!want.ContainsKey(oldSrc))
                    {
                        RestoreShowGunAction(oldSrc);
                        _appliedLocalClones.Remove(oldSrc);
                    }
                }

                if (anyCloneOk || !anyAttempt)
                {
                    _localWeaponInfoClones = nextClones;
                    return;
                }

                Loader.Log("[skin] 本地 WeaponInfo 克隆不可用，优雅回退至全局建模替换");
            }
            else if (_localWeaponInfoClones.Count > 0 || _appliedLocalClones.Count > 0)
            {
                _localWeaponInfoClones = new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase);
                _appliedLocalClones.Clear();
            }

            List<string> toRestore = new List<string>();
            foreach (var kv in _appliedModels)
            {
                if (!want.ContainsKey(kv.Key))
                    toRestore.Add(kv.Key);
            }
            foreach (string src in toRestore)
                RestoreModel(factory, src);

            foreach (var kv in want)
            {
                string src = kv.Key;
                string dst = kv.Value;
                string cur;
                if (_appliedModels.TryGetValue(src, out cur) && cur.Equals(dst, StringComparison.OrdinalIgnoreCase))
                    continue;

                object srcInfo = CallGetWeaponInfo(factory, src);
                object dstInfo = CallGetWeaponInfo(factory, dst);
                if (srcInfo == null || dstInfo == null) continue;

                if (!_backups.ContainsKey(src))
                {
                    Dictionary<string, object> bak = new Dictionary<string, object>();
                    foreach (string fn in VisualFields)
                    {
                        FieldInfo fi = FindFieldInHierarchy(srcInfo.GetType(), fn);
                        if (fi != null) bak[fn] = fi.GetValue(srcInfo);
                    }
                    _backups[src] = bak;
                }

                Dictionary<string, object> dstBak;
                _backups.TryGetValue(dst, out dstBak);

                int copied = 0;
                foreach (string fn in VisualFields)
                {
                    FieldInfo sfi = FindFieldInHierarchy(srcInfo.GetType(), fn);
                    if (sfi == null) continue;
                    object val = null;
                    if (dstBak != null && dstBak.ContainsKey(fn))
                        val = dstBak[fn];
                    else
                    {
                        FieldInfo dfi = FindFieldInHierarchy(dstInfo.GetType(), fn);
                        if (dfi == null) continue;
                        val = dfi.GetValue(dstInfo);
                    }
                    try
                    {
                        sfi.SetValue(srcInfo, val);
                        copied++;
                    }
                    catch { }
                }
                if (copied > 0)
                {
                    SyncShowGunAction(src, dst);
                    _appliedModels[src] = dst;
                    Loader.Log("[skin] 建模替换生效: " + src + " -> " + dst + " (字段=" + copied + ")");
                }
            }
        }

        // 记录为原武器补挂的 ShowGunFrameExcludeAttackAction，还原时移除
        private static readonly Dictionary<string, KeyValuePair<object, object>> _addedShowGun =
            new Dictionary<string, KeyValuePair<object, object>>(StringComparer.OrdinalIgnoreCase);

        private static object GetPredicationConfig(string weaponName)
        {
            try
            {
                Type tFac = FindType("Assets.Sources.Info.Weapon.PredicationConfigFactory");
                if (tFac == null) return null;
                FieldInfo fInst = tFac.GetField("Instance", BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static);
                object inst = fInst != null ? fInst.GetValue(null) : null;
                if (inst == null) return null;
                MethodInfo mGet = tFac.GetMethod("GetPredicationConfig", new[] { typeof(string) });
                if (mGet == null) return null;
                return mGet.Invoke(inst, new object[] { weaponName });
            }
            catch { return null; }
        }

        private static object GetFrameActions(object predCfg)
        {
            if (predCfg == null) return null;
            try
            {
                MethodInfo mSys = predCfg.GetType().GetMethod("GetAttackPredicationSystem", All);
                object sys = mSys != null ? mSys.Invoke(predCfg, null) : null;
                if (sys == null) return null;
                FieldInfo fFrame = FindFieldInHierarchy(sys.GetType(), "FrameActions");
                return fFrame != null ? fFrame.GetValue(sys) : null;
            }
            catch { return null; }
        }

        private static bool HasShowGunAction(object frameActions)
        {
            System.Collections.IEnumerable seq = frameActions as System.Collections.IEnumerable;
            if (seq == null) return false;
            foreach (object item in seq)
            {
                if (item != null && item.GetType().Name.IndexOf("ShowGun", StringComparison.OrdinalIgnoreCase) >= 0)
                    return true;
            }
            return false;
        }

        /// <summary>
        /// 当目标建模武器支持 V 键检视（如蝶舞 butterfly_knife）而原武器没有挂 ShowGun 动作时，
        /// 仅在 FrameActions 末尾补挂 ShowGunFrameExcludeAttackAction（待机/移动按 V 播 44 号检视动画，
        /// 不改 LeftActions/RightActions 及任何攻击判定）。
        /// </summary>
        private static void SyncShowGunAction(string src, string dst)
        {
            try
            {
                if (_addedShowGun.ContainsKey(src)) return;
                object dstActions = GetFrameActions(GetPredicationConfig(dst));
                if (dstActions == null || !HasShowGunAction(dstActions)) return;
                object srcActions = GetFrameActions(GetPredicationConfig(src));
                if (srcActions == null || HasShowGunAction(srcActions)) return;

                Type tAction = FindType("weapon.predicateSystem.attackSystem.actions.ShowGunFrameExcludeAttackAction");
                if (tAction == null) return;
                object act = Activator.CreateInstance(tAction);
                if (act == null) return;

                MethodInfo mAdd = srcActions.GetType().GetMethod("Push") ?? srcActions.GetType().GetMethod("Add");
                if (mAdd == null) return;
                mAdd.Invoke(srcActions, new object[] { act });
                _addedShowGun[src] = new KeyValuePair<object, object>(srcActions, act);
            }
            catch { }
        }

        private static void RestoreShowGunAction(string src)
        {
            try
            {
                KeyValuePair<object, object> pair;
                if (!_addedShowGun.TryGetValue(src, out pair)) return;
                _addedShowGun.Remove(src);
                if (pair.Key != null && pair.Value != null)
                {
                    MethodInfo mRem = pair.Key.GetType().GetMethod("Remove");
                    if (mRem != null) mRem.Invoke(pair.Key, new object[] { pair.Value });
                }
            }
            catch { }
        }

        private static void RestoreModel(object factory, string src)
        {
            RestoreShowGunAction(src);
            Dictionary<string, object> bak;
            if (_backups.TryGetValue(src, out bak))
            {
                object srcInfo = CallGetWeaponInfo(factory, src);
                if (srcInfo != null)
                {
                    foreach (var kv in bak)
                    {
                        FieldInfo fi = FindFieldInHierarchy(srcInfo.GetType(), kv.Key);
                        if (fi != null)
                        {
                            try { fi.SetValue(srcInfo, kv.Value); } catch { }
                        }
                    }
                }
            }
            _appliedModels.Remove(src);
            Loader.Log("[skin] 已还原武器建模: " + src);
        }

        private static void RestoreAllModels()
        {
            object factory = GetWeaponInfoFactoryInstance();
            if (factory == null) return;
            foreach (string src in new List<string>(_appliedModels.Keys))
                RestoreModel(factory, src);
        }
    }
}
