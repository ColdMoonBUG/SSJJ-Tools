using System;
using System.Collections.Generic;
using System.Reflection;
using System.Reflection.Emit;
using System.Runtime.CompilerServices;
using System.Runtime.InteropServices;
using System.Threading;

namespace SsjjRisk
{
    /// <summary>
    /// 纯客户端本地击杀播报与连杀通报模拟器（零上行网络包）。
    ///
    /// 通过 Inline Hook 拦截服务端下行包处理入口 KillPlayerInfoDataHandler.Handle(KillPlayerInfoData)，
    /// 仅在 KillPlayerId 等于本地玩家 ID 且命中换肤/换模规则时：
    ///   1. 本地改写下行对象 data.WeaponName / data.WeaponSkinName 与品质标（IsLegend/IsHero/Permanent），
    ///      驱动原生 KillMessageModel 自动加载目标皮肤的 ui_jy_backcolor / ui_jy_{skin} / ui_jy_{weapon}_name
    ///      并命中 ChampionWeaponAndSkillParser 播放 kill1~4 专属动画。
    ///   2. 当本机连杀数 CurKillNum >= 3 时，按 BonusWeaponKillInfoHandler 原生格式拼装 hononary1 XML，
    ///      直接入队本地 Contexts.sharedInstance.chat.chatJobHistroy.ContentReceived 显示（绝不向服务端发包）。
    ///   3. 退出/对局结束时由 Loader.RestoreEverything() 调用 Restore() 100% 还原方法入口 12 字节。
    /// </summary>
    internal static class KillFeedSimulator
    {
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool VirtualProtect(IntPtr addr, UIntPtr size, uint flNewProtect, out uint lpflOldProtect);

        private const uint PAGE_EXECUTE_READWRITE = 0x40;

        private static readonly BindingFlags All =
            BindingFlags.Public | BindingFlags.NonPublic |
            BindingFlags.Static | BindingFlags.Instance |
            BindingFlags.FlattenHierarchy;

        private static readonly object _hookLock = new object();
        private static readonly List<Delegate> _pinnedDelegates = new List<Delegate>();

        private static volatile bool _installStarted;
        private static volatile bool _installed;
        private static volatile bool _restored;

        private static IntPtr _srcPtr = IntPtr.Zero;
        private static byte[] _origBytes;
        private static byte[] _jmpBytes;
        private static Action<object, object> _invokeOriginal;

        // KillPlayerInfoData 成员缓存
        private static PropertyInfo _pKillPlayerId;
        private static PropertyInfo _pWeaponName;
        private static PropertyInfo _pWeaponSkinName;
        private static PropertyInfo _pIsLegend;
        private static PropertyInfo _pIsHero;
        private static PropertyInfo _pPermanent;
        private static PropertyInfo _pCurKillNum;
        private static PropertyInfo _pKillPlayerName;
        private static PropertyInfo _pBeKillPlayerName;

        // 本地聊天栏与本地化反射缓存
        private static bool _chatReflectionResolved;
        private static PropertyInfo _pContextsShared;
        private static PropertyInfo _pChatContext;
        private static PropertyInfo _pHasChatHistory;
        private static PropertyInfo _pChatHistory;
        private static FieldInfo _fContentReceived;
        private static MethodInfo _mLocalizationGet;
        private static MethodInfo _mGetWeaponCnName;
        private static PropertyInfo _pChampionInstance;
        private static MethodInfo _mChampionGetName;

        // 防止同一次连杀重复入队通报
        private static int _lastNotifiedKillerId = -1;
        private static int _lastNotifiedCurKillNum = -1;

        public static void Install()
        {
            if (_installed || _restored) return;
            if (TryInstallOnce()) return;

            if (_installStarted) return;
            _installStarted = true;
            try
            {
                Thread th = new Thread(InstallRetryLoop);
                th.IsBackground = true;
                th.Name = "SsjjKillFeedInit";
                th.Start();
            }
            catch (Exception ex)
            {
                Loader.Log("[killfeed] 启动延迟安装线程失败: " + ex.Message);
            }
        }

        private static void InstallRetryLoop()
        {
            try
            {
                for (int i = 0; i < 400; i++)
                {
                    if (_installed || _restored) return;
                    Thread.Sleep(300);
                    if (TryInstallOnce()) return;
                }
                if (!_installed && !_restored)
                    Loader.Log("[killfeed] 超时未找到 KillPlayerInfoDataHandler，本局跳过本地击杀播报模拟");
            }
            catch (Exception ex)
            {
                Loader.Log("[killfeed] 延迟安装线程异常: " + ex.Message);
            }
        }

        private static bool TryInstallOnce()
        {
            if (IntPtr.Size != 8) return false;
            lock (_hookLock)
            {
                if (_installed || _restored) return _installed;

                Type handlerType = FindType(
                    "Assets.Sources.Systems.PacketHandle.Handlers.KillPlayerInfoDataHandler",
                    "KillPlayerInfoDataHandler");
                if (handlerType == null) return false;

                MethodInfo handleMethod = null;
                foreach (MethodInfo m in handlerType.GetMethods(All))
                {
                    if (m.Name != "Handle" || m.IsStatic || m.IsAbstract) continue;
                    ParameterInfo[] ps = m.GetParameters();
                    if (ps.Length == 1 && ps[0].ParameterType.Name == "KillPlayerInfoData")
                    {
                        handleMethod = m;
                        break;
                    }
                }
                if (handleMethod == null) return false;

                Type dataType = handleMethod.GetParameters()[0].ParameterType;
                CacheKillDataMembers(dataType);

                try
                {
                    Action<object, object> origCaller = BuildOriginalCaller(handlerType, dataType, handleMethod);
                    IntPtr dstPtr = BuildHookEntry(handlerType, dataType);
                    if (origCaller == null || dstPtr == IntPtr.Zero) return false;

                    RuntimeHelpers.PrepareMethod(handleMethod.MethodHandle);
                    IntPtr srcPtr = handleMethod.MethodHandle.GetFunctionPointer();
                    if (srcPtr == IntPtr.Zero) return false;

                    byte[] orig = new byte[12];
                    byte[] jmp = BuildJmp12(dstPtr);

                    uint oldProt;
                    if (!VirtualProtect(srcPtr, (UIntPtr)16, PAGE_EXECUTE_READWRITE, out oldProt))
                        return false;
                    Marshal.Copy(srcPtr, orig, 0, orig.Length);
                    Marshal.Copy(jmp, 0, srcPtr, jmp.Length);
                    uint dummy;
                    VirtualProtect(srcPtr, (UIntPtr)16, oldProt, out dummy);

                    _srcPtr = srcPtr;
                    _origBytes = orig;
                    _jmpBytes = jmp;
                    _invokeOriginal = origCaller;
                    _installed = true;

                    Loader.Log("[killfeed] 已安装本地击杀播报模拟 Hook: " + handlerType.Name + ".Handle(" + dataType.Name + ")");
                    return true;
                }
                catch (Exception ex)
                {
                    Loader.Log("[killfeed] 安装 Hook 失败: " + ex.GetType().Name + " " + ex.Message);
                    return false;
                }
            }
        }

        private static byte[] BuildJmp12(IntPtr dstPtr)
        {
            byte[] jmp = new byte[12];
            jmp[0] = 0x48;
            jmp[1] = 0xB8;
            long addr = dstPtr.ToInt64();
            for (int i = 0; i < 8; i++)
                jmp[2 + i] = (byte)((addr >> (i * 8)) & 0xFF);
            jmp[10] = 0xFF;
            jmp[11] = 0xE0;
            return jmp;
        }

        private static Action<object, object> BuildOriginalCaller(Type handlerType, Type dataType, MethodInfo handleMethod)
        {
            DynamicMethod dm = new DynamicMethod(
                "SsjjKillFeedOrigCall", typeof(void), new[] { typeof(object), typeof(object) },
                typeof(KillFeedSimulator).Module, true);
            ILGenerator il = dm.GetILGenerator();
            il.Emit(OpCodes.Ldarg_0);
            il.Emit(OpCodes.Castclass, handlerType);
            il.Emit(OpCodes.Ldarg_1);
            il.Emit(OpCodes.Castclass, dataType);
            // 直接静态绑定原实现，避免经虚表重复分派
            il.Emit(OpCodes.Call, handleMethod);
            il.Emit(OpCodes.Ret);

            Action<object, object> del = (Action<object, object>)dm.CreateDelegate(typeof(Action<object, object>));
            _pinnedDelegates.Add(del);
            RuntimeHelpers.PrepareMethod(del.Method.MethodHandle);
            return del;
        }

        private static IntPtr BuildHookEntry(Type handlerType, Type dataType)
        {
            MethodInfo mOnHandle = typeof(KillFeedSimulator).GetMethod(
                nameof(OnHandle), BindingFlags.Public | BindingFlags.Static);
            if (mOnHandle == null) return IntPtr.Zero;

            DynamicMethod dm = new DynamicMethod(
                "SsjjKillFeedHookEntry", typeof(void), new[] { handlerType, dataType },
                typeof(KillFeedSimulator).Module, true);
            ILGenerator il = dm.GetILGenerator();
            il.Emit(OpCodes.Ldarg_0);
            il.Emit(OpCodes.Ldarg_1);
            il.Emit(OpCodes.Call, mOnHandle);
            il.Emit(OpCodes.Ret);

            Type delType = typeof(Action<,>).MakeGenericType(handlerType, dataType);
            Delegate del = dm.CreateDelegate(delType);
            _pinnedDelegates.Add(del);
            RuntimeHelpers.PrepareMethod(del.Method.MethodHandle);
            return del.Method.MethodHandle.GetFunctionPointer();
        }

        public static void OnHandle(object handler, object killData)
        {
            try
            {
                // 直播模式独立于换肤规则：无论是否开启换肤均先替换本机击杀/被击杀/助攻昵称
                StreamerMode.ApplyToKillData(killData, WeaponSwapper.GetLocalPlayerId());
                ProcessKillData(killData);
            }
            catch (Exception ex)
            {
                Loader.Log("[killfeed] 本地击杀处理异常: " + ex.Message);
            }

            // 临时恢复原 12 字节 → 调用原 Handle → 立即回写跳转，无指令重定位风险
            lock (_hookLock)
            {
                if (_srcPtr == IntPtr.Zero || _origBytes == null || _invokeOriginal == null)
                    return;

                WriteBytes(_srcPtr, _origBytes);
                try
                {
                    _invokeOriginal(handler, killData);
                }
                finally
                {
                    if (!_restored && _installed && _jmpBytes != null)
                        WriteBytes(_srcPtr, _jmpBytes);
                }
            }
        }

        private static void ProcessKillData(object data)
        {
            if (data == null || !WeaponSwapper.KillFeedSimEnabled) return;

            int localId = WeaponSwapper.GetLocalPlayerId();
            int killerId = GetInt(data, _pKillPlayerId);
            if (localId <= 0 || killerId != localId) return;

            string origWeapon = GetString(data, _pWeaponName);
            string origSkin = GetString(data, _pWeaponSkinName);
            if (string.IsNullOrEmpty(origWeapon)) return;

            string mappedWeapon = WeaponSwapper.ResolveModelWeapon(origWeapon);
            string mappedSkin = WeaponSwapper.ResolveSkin(mappedWeapon, origSkin);
            if (string.Equals(mappedSkin, origSkin, StringComparison.OrdinalIgnoreCase) &&
                !string.Equals(mappedWeapon, origWeapon, StringComparison.OrdinalIgnoreCase))
            {
                mappedSkin = WeaponSwapper.ResolveSkin(origWeapon, origSkin);
            }

            bool modelChanged = !string.IsNullOrEmpty(mappedWeapon) &&
                                !string.Equals(mappedWeapon, origWeapon, StringComparison.OrdinalIgnoreCase);
            bool skinChanged = !string.IsNullOrEmpty(mappedSkin) &&
                               !string.Equals(mappedSkin, origSkin, StringComparison.OrdinalIgnoreCase);
            if (!modelChanged && !skinChanged) return;

            if (modelChanged) SetString(data, _pWeaponName, mappedWeapon);
            if (skinChanged) SetString(data, _pWeaponSkinName, mappedSkin);

            // 按目标武器/皮肤品质置位，让 KillMessageModel 自动加载 legend/hero/permanent 标与皮肤专属图标
            string combined = ((mappedWeapon ?? "") + "|" + (mappedSkin ?? "")).ToLowerInvariant();
            if (combined.IndexOf("legend", StringComparison.Ordinal) >= 0)
            {
                SetBool(data, _pIsLegend, true);
                SetBool(data, _pPermanent, true);
            }
            else if (combined.IndexOf("hero", StringComparison.Ordinal) >= 0)
            {
                SetBool(data, _pIsHero, true);
                SetBool(data, _pPermanent, true);
            }
            else if (skinChanged)
            {
                SetBool(data, _pPermanent, true);
            }

            // 本地三杀及以上连杀聊天通报（纯本地入队，严禁发往服务端）
            int curKillNum = GetInt(data, _pCurKillNum);
            if (curKillNum < 3)
            {
                if (_lastNotifiedKillerId == killerId && curKillNum < _lastNotifiedCurKillNum)
                    _lastNotifiedCurKillNum = curKillNum;
                return;
            }
            if (_lastNotifiedKillerId == killerId && _lastNotifiedCurKillNum == curKillNum)
                return;

            _lastNotifiedKillerId = killerId;
            _lastNotifiedCurKillNum = curKillNum;
            TryEnqueueLocalChatBroadcast(data, mappedWeapon, mappedSkin, curKillNum);
        }

        private static void TryEnqueueLocalChatBroadcast(object data, string weaponName, string skinName, int count)
        {
            try
            {
                EnsureChatReflection();
                if (_pContextsShared == null || _pChatContext == null || _pChatHistory == null || _fContentReceived == null)
                    return;

                object contexts = _pContextsShared.GetValue(null, null);
                if (contexts == null) return;
                object chatCtx = _pChatContext.GetValue(contexts, null);
                if (chatCtx == null) return;
                if (_pHasChatHistory != null)
                {
                    object has = _pHasChatHistory.GetValue(chatCtx, null);
                    if (has is bool && !(bool)has) return;
                }
                object history = _pChatHistory.GetValue(chatCtx, null);
                if (history == null) return;
                Queue<string> queue = _fContentReceived.GetValue(history) as Queue<string>;
                if (queue == null) return;

                string killName = StreamerMode.ResolveDisplayName(GetString(data, _pKillPlayerName));
                string beKilledName = StreamerMode.ResolveDisplayName(GetString(data, _pBeKillPlayerName));
                string weaponCn = GetWeaponCnName(weaponName);
                string skinCn = WeaponSwapper.ResolveSkinDisplayName(skinName);
                if (string.IsNullOrEmpty(skinCn))
                    skinCn = GetChampionSkinCnName(skinName);
                if (string.IsNullOrEmpty(skinCn) && !string.IsNullOrEmpty(skinName))
                    skinCn = skinName;
                string displayWeapon = FormatWeaponAndSkin(weaponCn, skinCn);

                string tpl = GetLocalizedTemplate("USEWEAPON_KILL_PLAYER_NUMBER");
                string body;
                if (!string.IsNullOrEmpty(tpl))
                {
                    try
                    {
                        body = string.Format(tpl, killName, displayWeapon, beKilledName, count);
                    }
                    catch
                    {
                        body = killName + "使用" + displayWeapon + "击败了" + beKilledName + "（" + count + "连杀）";
                    }
                }
                else
                {
                    body = killName + "使用" + displayWeapon + "击败了" + beKilledName + "（" + count + "连杀）";
                }

                string xml = "<message type=\"hononary1\" ><body>" + body + "</body></message>";
                queue.Enqueue(xml);
            }
            catch (Exception ex)
            {
                Loader.Log("[killfeed] 本地连杀通报入队失败: " + ex.Message);
            }
        }

        private static string FormatWeaponAndSkin(string weaponCn, string skinCn)
        {
            string result = weaponCn + "武器";
            if (!string.IsNullOrEmpty(skinCn))
                result += "装备" + skinCn + "皮肤";
            return result;
        }

        private static void EnsureChatReflection()
        {
            if (_chatReflectionResolved) return;
            _chatReflectionResolved = true;
            try
            {
                Type tContexts = FindType("Contexts", "Contexts");
                if (tContexts != null)
                {
                    _pContextsShared = tContexts.GetProperty("sharedInstance", All);
                    _pChatContext = tContexts.GetProperty("chat", All);
                }
                if (_pChatContext != null)
                {
                    Type tChatCtx = _pChatContext.PropertyType;
                    _pHasChatHistory = tChatCtx.GetProperty("hasChatJobHistroy", All);
                    _pChatHistory = tChatCtx.GetProperty("chatJobHistroy", All);
                    if (_pChatHistory != null)
                        _fContentReceived = _pChatHistory.PropertyType.GetField("ContentReceived", All);
                }

                Type tLoc = FindType("I2.Loc.ScriptLocalization", "ScriptLocalization");
                if (tLoc != null)
                {
                    foreach (MethodInfo m in tLoc.GetMethods(All))
                    {
                        if (m.Name != "Get" || !m.IsStatic || m.ReturnType != typeof(string)) continue;
                        ParameterInfo[] ps = m.GetParameters();
                        if (ps.Length >= 1 && ps[0].ParameterType == typeof(string))
                        {
                            if (ps.Length == 1 || _mLocalizationGet == null)
                                _mLocalizationGet = m;
                            if (ps.Length == 1) break;
                        }
                    }
                }

                Type tLang = FindType("Assets.Sources.Utils.Ui.LanguageUtils", "LanguageUtils");
                if (tLang != null)
                    _mGetWeaponCnName = tLang.GetMethod("GetWeaponCnName", All, null, new[] { typeof(string) }, null);

                Type tChamp = FindType("Assets.Sources.Utils.Weapon.ChampionWeaponAndSkillParser", "ChampionWeaponAndSkillParser");
                if (tChamp != null)
                {
                    _pChampionInstance = tChamp.GetProperty("Instance", All);
                    _mChampionGetName = tChamp.GetMethod("GetChampionWeaponAndSkillfigName", All, null, new[] { typeof(string) }, null);
                }
            }
            catch { }
        }

        private static string GetLocalizedTemplate(string key)
        {
            if (_mLocalizationGet == null) return "";
            try
            {
                ParameterInfo[] ps = _mLocalizationGet.GetParameters();
                object[] args = new object[ps.Length];
                args[0] = key;
                for (int i = 1; i < ps.Length; i++)
                    args[i] = ps[i].HasDefaultValue ? ps[i].DefaultValue : (ps[i].ParameterType.IsValueType ? Activator.CreateInstance(ps[i].ParameterType) : null);
                return (_mLocalizationGet.Invoke(null, args) as string) ?? "";
            }
            catch { return ""; }
        }

        private static string GetWeaponCnName(string weaponName)
        {
            if (string.IsNullOrEmpty(weaponName)) return "";
            if (_mGetWeaponCnName != null)
            {
                try
                {
                    string cn = _mGetWeaponCnName.Invoke(null, new object[] { weaponName }) as string;
                    if (!string.IsNullOrEmpty(cn)) return cn;
                }
                catch { }
            }
            return weaponName;
        }

        private static string GetChampionSkinCnName(string skinName)
        {
            if (string.IsNullOrEmpty(skinName) || _pChampionInstance == null || _mChampionGetName == null)
                return "";
            try
            {
                object inst = _pChampionInstance.GetValue(null, null);
                if (inst == null) return "";
                return (_mChampionGetName.Invoke(inst, new object[] { skinName }) as string) ?? "";
            }
            catch { return ""; }
        }

        public static int Restore()
        {
            lock (_hookLock)
            {
                _restored = true;
                if (!_installed || _srcPtr == IntPtr.Zero || _origBytes == null)
                    return 0;

                bool ok = WriteBytes(_srcPtr, _origBytes);
                _installed = false;
                _origBytes = null;
                _jmpBytes = null;
                _srcPtr = IntPtr.Zero;
                if (ok)
                {
                    Loader.Log("[killfeed] 已还原 KillPlayerInfoDataHandler.Handle 首字节");
                    return 1;
                }
                return 0;
            }
        }

        private static bool WriteBytes(IntPtr addr, byte[] bytes)
        {
            if (addr == IntPtr.Zero || bytes == null || bytes.Length == 0) return false;
            try
            {
                uint oldProt;
                if (!VirtualProtect(addr, (UIntPtr)16, PAGE_EXECUTE_READWRITE, out oldProt))
                    return false;
                Marshal.Copy(bytes, 0, addr, bytes.Length);
                uint dummy;
                VirtualProtect(addr, (UIntPtr)16, oldProt, out dummy);
                return true;
            }
            catch
            {
                return false;
            }
        }

        private static void CacheKillDataMembers(Type t)
        {
            _pKillPlayerId = t.GetProperty("KillPlayerId", All);
            _pWeaponName = t.GetProperty("WeaponName", All);
            _pWeaponSkinName = t.GetProperty("WeaponSkinName", All);
            _pIsLegend = t.GetProperty("IsLegend", All);
            _pIsHero = t.GetProperty("IsHero", All);
            _pPermanent = t.GetProperty("Permanent", All);
            _pCurKillNum = t.GetProperty("CurKillNum", All);
            _pKillPlayerName = t.GetProperty("KillPlayerName", All);
            _pBeKillPlayerName = t.GetProperty("BeKillPlayerName", All);
        }

        private static int GetInt(object obj, PropertyInfo p)
        {
            if (obj == null || p == null) return 0;
            try { return Convert.ToInt32(p.GetValue(obj, null)); }
            catch { return 0; }
        }

        private static string GetString(object obj, PropertyInfo p)
        {
            if (obj == null || p == null) return "";
            try { return (p.GetValue(obj, null) as string) ?? ""; }
            catch { return ""; }
        }

        private static void SetString(object obj, PropertyInfo p, string value)
        {
            if (obj == null || p == null || !p.CanWrite) return;
            try { p.SetValue(obj, value, null); } catch { }
        }

        private static void SetBool(object obj, PropertyInfo p, bool value)
        {
            if (obj == null || p == null || !p.CanWrite) return;
            try { p.SetValue(obj, value, null); } catch { }
        }

        private static Type FindType(string fullName, string shortName)
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
            if (!string.IsNullOrEmpty(shortName))
            {
                foreach (Assembly asm in AppDomain.CurrentDomain.GetAssemblies())
                {
                    try
                    {
                        Type[] types = asm.GetTypes();
                        if (types == null) continue;
                        for (int i = 0; i < types.Length; i++)
                        {
                            if (types[i] != null && types[i].Name == shortName)
                                return types[i];
                        }
                    }
                    catch { }
                }
            }
            return null;
        }
    }
}
