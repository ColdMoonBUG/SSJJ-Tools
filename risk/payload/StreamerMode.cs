using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
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
    /// 战斗端纯本地「直播模式」隐私遮蔽引擎（零上行包、零常驻线程、500ms 热更新、退出 100% 还原）。
    ///
    /// 通过 3 个纯下行/本地 Hook + KillFeedSimulator 协同，将本机显示的自己昵称替换为自定义假名，
    /// 绝不修改数字 EntityId/CharacterId，也绝不篡改发往服务端的任何网络包：
    ///   1. PlayerEntityUtility.PostInitBasicInfo(PlayerEntity)：
    ///      仅当 entity.isMyPlayer 时改写 basicInfo.Current.PlayerName，覆盖 HUD / 小地图 / 观战卡 /
    ///      语音条 / Tab 自己行高亮及全部 5 个 UiIEventCondition Getter。
    ///   2. AllKillInfoDataHandler.Handle(AllKillInfoData)：
    ///      仅当 item.Id == localId 且 PlayerNameSpecified 时改写 OneKillInfoData.PlayerName，
    ///      并同步拷贝 battleRoom.playerInfo.VipLevels[realName] -> VipLevels[fakeName] 保住 VIP 图标。
    ///   3. ChatJobSystem.ParseChatXml(string xml, out ChatHistroyData data)：
    ///      下行聊天 XML 与连杀/系统通报渲染前替换真实昵称（绝不触碰 SendChatInfo 上行包）。
    /// </summary>
    internal static class StreamerMode
    {
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool VirtualProtect(IntPtr addr, UIntPtr size, uint flNewProtect, out uint lpflOldProtect);

        private const uint PAGE_EXECUTE_READWRITE = 0x40;
        private const int ConfigCheckIntervalMs = 500;

        private static readonly BindingFlags All =
            BindingFlags.Public | BindingFlags.NonPublic |
            BindingFlags.Static | BindingFlags.Instance |
            BindingFlags.FlattenHierarchy;

        private static readonly string ConfigPath = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "ssjj-toolbox", "streamer_mode.json");

        // 泛型委托供 DynamicMethod 绑定带 out 参数的 ParseChatXml，避免创建动态程序集
        private delegate bool ParseChatXmlDel<TThis, TOut>(TThis self, string xml, out TOut chatdata);

        // 配置状态（按 500ms 节流热加载，零常驻线程）
        private static readonly object _cfgLock = new object();
        private static int _lastCheckTick;
        private static DateTime _lastMtime = DateTime.MinValue;
        private static bool _fileExisted;
        private static volatile bool _enabled;
        private static volatile string _fakeName = "";
        private static volatile string _realLocalName = "";
        private static volatile string _lastSyncedVipFakeName = "";

        // Hook 管理与原始 12 字节备份
        private static readonly object _installLock = new object();
        private static readonly List<Delegate> _pinnedDelegates = new List<Delegate>();
        private static readonly List<KeyValuePair<IntPtr, byte[]>> _hookOrig =
            new List<KeyValuePair<IntPtr, byte[]>>();

        private static volatile bool _installStarted;
        private static volatile bool _restored;

        // Hook 1: PlayerEntityUtility.PostInitBasicInfo(PlayerEntity)
        private static readonly object _postInitLock = new object();
        private static volatile bool _postInitHooked;
        private static IntPtr _postInitSrc = IntPtr.Zero;
        private static byte[] _postInitOrig;
        private static byte[] _postInitJmp;
        private static Action<object> _origPostInit;

        // Hook 2: AllKillInfoDataHandler.Handle(AllKillInfoData)
        private static readonly object _allKillLock = new object();
        private static volatile bool _allKillHooked;
        private static IntPtr _allKillSrc = IntPtr.Zero;
        private static byte[] _allKillOrig;
        private static byte[] _allKillJmp;
        private static Action<object, object> _origAllKill;

        // Hook 3: ChatJobSystem.ParseChatXml(string, out ChatHistroyData)
        private static readonly object _chatXmlLock = new object();
        private static volatile bool _chatXmlHooked;
        private static IntPtr _chatXmlSrc = IntPtr.Zero;
        private static byte[] _chatXmlOrig;
        private static byte[] _chatXmlJmp;

        // PlayerEntity / BasicInfo / PlayerEntityData 反射缓存
        private static bool _playerMembersResolved;
        private static PropertyInfo _pIsMyPlayer;
        private static PropertyInfo _pBasicInfo;
        private static FieldInfo _fBasicCurrent;
        private static PropertyInfo _pBasicCurrent;
        private static PropertyInfo _pEntityPlayerName;
        private static PropertyInfo _pEntityPlayerNameSpec;

        // AllKillInfoData / OneKillInfoData 反射缓存
        private static bool _allKillMembersResolved;
        private static PropertyInfo _pAllKillList;
        private static PropertyInfo _pOneKillId;
        private static PropertyInfo _pOneKillName;
        private static PropertyInfo _pOneKillNameSpec;

        // KillPlayerInfoData 反射缓存（供 ApplyToKillData 使用）
        private static Type _cachedKillDataType;
        private static PropertyInfo _pKillPlayerId;
        private static PropertyInfo _pBeKillPlayerId;
        private static PropertyInfo _pKillPlayerName;
        private static PropertyInfo _pBeKillPlayerName;
        private static PropertyInfo _pConnectPlayerName;

        // Contexts 反射缓存（实时切换开关 & 退出还原内存昵称 + VipLevels 同步）
        private static bool _contextsResolved;
        private static PropertyInfo _pContextsShared;
        private static PropertyInfo _pPlayerCtx;
        private static PropertyInfo _pMyPlayerEntity;
        private static PropertyInfo _pBattleRoomCtx;
        private static PropertyInfo _pPlayerInfo;
        private static FieldInfo _fPlayerInfoAll;
        private static FieldInfo _fPlayerInfoVipLevels;

        public static bool IsActive
        {
            get
            {
                RefreshConfigIfDue();
                return !_restored && _enabled && !string.IsNullOrEmpty(_fakeName);
            }
        }

        public static void Install()
        {
            if (_restored) return;
            RefreshConfigForce();
            if (TryInstallAll()) return;

            if (_installStarted) return;
            _installStarted = true;
            try
            {
                Thread th = new Thread(InstallRetryLoop);
                th.IsBackground = true;
                th.Name = "SsjjStreamerModeInit";
                th.Start();
            }
            catch (Exception ex)
            {
                Loader.Log("[streamer] 启动延迟安装线程失败: " + ex.Message);
            }
        }

        private static void InstallRetryLoop()
        {
            try
            {
                for (int i = 0; i < 400; i++)
                {
                    if (_restored) return;
                    Thread.Sleep(300);
                    if (TryInstallAll()) return;
                }
                if (!_restored)
                {
                    Loader.Log("[streamer] 延迟安装结束（postInit=" + _postInitHooked +
                               " allKill=" + _allKillHooked + " chatXml=" + _chatXmlHooked + "）");
                }
            }
            catch (Exception ex)
            {
                Loader.Log("[streamer] 延迟安装线程异常: " + ex.Message);
            }
        }

        private static bool TryInstallAll()
        {
            if (IntPtr.Size != 8 || _restored) return false;
            lock (_installLock)
            {
                if (_restored) return true;
                if (!_postInitHooked) TryInstallPostInitHook();
                if (!_allKillHooked) TryInstallAllKillHook();
                if (!_chatXmlHooked) TryInstallChatXmlHook();
                return _postInitHooked && _allKillHooked && _chatXmlHooked;
            }
        }

        // ------------------------------------------------------------------ Hook 1: PlayerEntityUtility.PostInitBasicInfo
        private static void TryInstallPostInitHook()
        {
            try
            {
                Type utilType = FindType("Assets.Sources.Utils.ComponentInit.PlayerEntityUtility", "PlayerEntityUtility");
                if (utilType == null) return;

                MethodInfo mPost = null;
                foreach (MethodInfo m in utilType.GetMethods(All))
                {
                    if (m.Name != "PostInitBasicInfo" || !m.IsStatic || m.IsAbstract) continue;
                    ParameterInfo[] ps = m.GetParameters();
                    if (ps.Length == 1 && ps[0].ParameterType.Name == "PlayerEntity")
                    {
                        mPost = m;
                        break;
                    }
                }
                if (mPost == null) return;

                Type playerType = mPost.GetParameters()[0].ParameterType;
                ResolvePlayerMembers(playerType);

                // 构建原方法调用器
                DynamicMethod dmOrig = new DynamicMethod(
                    "SsjjSmOrigPostInit", typeof(void), new[] { typeof(object) },
                    typeof(StreamerMode).Module, true);
                ILGenerator ilO = dmOrig.GetILGenerator();
                ilO.Emit(OpCodes.Ldarg_0);
                ilO.Emit(OpCodes.Castclass, playerType);
                ilO.Emit(OpCodes.Call, mPost);
                ilO.Emit(OpCodes.Ret);
                Action<object> origDel = (Action<object>)dmOrig.CreateDelegate(typeof(Action<object>));
                _pinnedDelegates.Add(origDel);
                RuntimeHelpers.PrepareMethod(origDel.Method.MethodHandle);

                // 构建 Hook 入口
                MethodInfo mOnPost = typeof(StreamerMode).GetMethod(
                    nameof(OnPostInitBasicInfo), BindingFlags.Public | BindingFlags.Static);
                if (mOnPost == null) return;

                DynamicMethod dmEntry = new DynamicMethod(
                    "SsjjSmHookPostInit", typeof(void), new[] { playerType },
                    typeof(StreamerMode).Module, true);
                ILGenerator ilE = dmEntry.GetILGenerator();
                ilE.Emit(OpCodes.Ldarg_0);
                ilE.Emit(OpCodes.Call, mOnPost);
                ilE.Emit(OpCodes.Ret);
                Type entryDelType = typeof(Action<>).MakeGenericType(playerType);
                Delegate entryDel = dmEntry.CreateDelegate(entryDelType);
                _pinnedDelegates.Add(entryDel);
                RuntimeHelpers.PrepareMethod(entryDel.Method.MethodHandle);
                IntPtr dstPtr = entryDel.Method.MethodHandle.GetFunctionPointer();

                IntPtr srcPtr;
                byte[] orig;
                byte[] jmp;
                if (!InstallJump12(mPost, dstPtr, out srcPtr, out orig, out jmp)) return;

                _postInitSrc = srcPtr;
                _postInitOrig = orig;
                _postInitJmp = jmp;
                _origPostInit = origDel;
                _postInitHooked = true;
                Loader.Log("[streamer] 已安装 Hook: PlayerEntityUtility.PostInitBasicInfo");
            }
            catch (Exception ex)
            {
                Loader.Log("[streamer] 安装 PostInitBasicInfo Hook 失败: " + ex.Message);
            }
        }

        public static void OnPostInitBasicInfo(object entity)
        {
            lock (_postInitLock)
            {
                if (_postInitSrc != IntPtr.Zero && _postInitOrig != null && _origPostInit != null)
                {
                    WriteBytes(_postInitSrc, _postInitOrig);
                    try
                    {
                        _origPostInit(entity);
                    }
                    finally
                    {
                        if (!_restored && _postInitHooked && _postInitJmp != null)
                            WriteBytes(_postInitSrc, _postInitJmp);
                    }
                }
            }

            try
            {
                ProcessLocalPlayerEntity(entity);
            }
            catch { }
        }

        private static void ProcessLocalPlayerEntity(object entity)
        {
            if (entity == null || _restored) return;
            if (_pIsMyPlayer == null || _pBasicInfo == null) return;

            object isMyObj = _pIsMyPlayer.GetValue(entity, null);
            if (!(isMyObj is bool) || !(bool)isMyObj) return;

            RefreshConfigIfDue();

            object basicInfo = _pBasicInfo.GetValue(entity, null);
            if (basicInfo == null) return;
            object current = GetBasicCurrent(basicInfo);
            if (current == null) return;

            EnsureEntityDataMembers(current.GetType());
            if (_pEntityPlayerName == null) return;

            string curName = GetString(current, _pEntityPlayerName);
            string fake = _fakeName;
            bool active = _enabled && !string.IsNullOrEmpty(fake);

            // 首次见到本机玩家真实昵称时缓存
            if (!string.IsNullOrEmpty(curName) && string.IsNullOrEmpty(_realLocalName))
            {
                if (!active || !string.Equals(curName, fake, StringComparison.Ordinal))
                    _realLocalName = curName;
            }

            if (active)
            {
                if (!string.Equals(curName, fake, StringComparison.Ordinal))
                {
                    SetString(current, _pEntityPlayerName, fake);
                }
                EnsureVipLevelSynced(fake);
            }
            else if (!string.IsNullOrEmpty(_realLocalName) &&
                     !string.Equals(curName, _realLocalName, StringComparison.Ordinal))
            {
                SetString(current, _pEntityPlayerName, _realLocalName);
            }
        }

        // ------------------------------------------------------------------ Hook 2: AllKillInfoDataHandler.Handle
        private static void TryInstallAllKillHook()
        {
            try
            {
                Type handlerType = FindType(
                    "Assets.Sources.Systems.PacketHandle.Handlers.AllKillInfoDataHandler",
                    "AllKillInfoDataHandler");
                if (handlerType == null) return;

                MethodInfo mHandle = null;
                foreach (MethodInfo m in handlerType.GetMethods(All))
                {
                    if (m.Name != "Handle" || m.IsStatic || m.IsAbstract) continue;
                    ParameterInfo[] ps = m.GetParameters();
                    if (ps.Length == 1 && ps[0].ParameterType.Name == "AllKillInfoData")
                    {
                        mHandle = m;
                        break;
                    }
                }
                if (mHandle == null) return;

                Type dataType = mHandle.GetParameters()[0].ParameterType;
                ResolveAllKillMembers(dataType);

                // 原方法调用器
                DynamicMethod dmOrig = new DynamicMethod(
                    "SsjjSmOrigAllKill", typeof(void), new[] { typeof(object), typeof(object) },
                    typeof(StreamerMode).Module, true);
                ILGenerator ilO = dmOrig.GetILGenerator();
                ilO.Emit(OpCodes.Ldarg_0);
                ilO.Emit(OpCodes.Castclass, handlerType);
                ilO.Emit(OpCodes.Ldarg_1);
                ilO.Emit(OpCodes.Castclass, dataType);
                ilO.Emit(OpCodes.Call, mHandle);
                ilO.Emit(OpCodes.Ret);
                Action<object, object> origDel = (Action<object, object>)dmOrig.CreateDelegate(typeof(Action<object, object>));
                _pinnedDelegates.Add(origDel);
                RuntimeHelpers.PrepareMethod(origDel.Method.MethodHandle);

                // Hook 入口
                MethodInfo mOnHandle = typeof(StreamerMode).GetMethod(
                    nameof(OnAllKillInfoHandle), BindingFlags.Public | BindingFlags.Static);
                if (mOnHandle == null) return;

                DynamicMethod dmEntry = new DynamicMethod(
                    "SsjjSmHookAllKill", typeof(void), new[] { handlerType, dataType },
                    typeof(StreamerMode).Module, true);
                ILGenerator ilE = dmEntry.GetILGenerator();
                ilE.Emit(OpCodes.Ldarg_0);
                ilE.Emit(OpCodes.Ldarg_1);
                ilE.Emit(OpCodes.Call, mOnHandle);
                ilE.Emit(OpCodes.Ret);
                Type entryDelType = typeof(Action<,>).MakeGenericType(handlerType, dataType);
                Delegate entryDel = dmEntry.CreateDelegate(entryDelType);
                _pinnedDelegates.Add(entryDel);
                RuntimeHelpers.PrepareMethod(entryDel.Method.MethodHandle);
                IntPtr dstPtr = entryDel.Method.MethodHandle.GetFunctionPointer();

                IntPtr srcPtr;
                byte[] orig;
                byte[] jmp;
                if (!InstallJump12(mHandle, dstPtr, out srcPtr, out orig, out jmp)) return;

                _allKillSrc = srcPtr;
                _allKillOrig = orig;
                _allKillJmp = jmp;
                _origAllKill = origDel;
                _allKillHooked = true;
                Loader.Log("[streamer] 已安装 Hook: AllKillInfoDataHandler.Handle");
            }
            catch (Exception ex)
            {
                Loader.Log("[streamer] 安装 AllKillInfoDataHandler Hook 失败: " + ex.Message);
            }
        }

        public static void OnAllKillInfoHandle(object handler, object data)
        {
            try
            {
                ProcessAllKillInfoData(data);
            }
            catch (Exception ex)
            {
                Loader.Log("[streamer] 处理 AllKillInfoData 异常: " + ex.Message);
            }

            lock (_allKillLock)
            {
                if (_allKillSrc == IntPtr.Zero || _allKillOrig == null || _origAllKill == null)
                    return;

                WriteBytes(_allKillSrc, _allKillOrig);
                try
                {
                    _origAllKill(handler, data);
                }
                finally
                {
                    if (!_restored && _allKillHooked && _allKillJmp != null)
                        WriteBytes(_allKillSrc, _allKillJmp);
                }
            }
        }

        private static void ProcessAllKillInfoData(object data)
        {
            if (data == null || _restored) return;
            RefreshConfigIfDue();

            if (!_enabled || string.IsNullOrEmpty(_fakeName)) return;
            if (_pAllKillList == null) return;

            int localId = WeaponSwapper.GetLocalPlayerId();
            if (localId <= 0) return;

            IList list = _pAllKillList.GetValue(data, null) as IList;
            if (list == null || list.Count == 0) return;

            string fake = _fakeName;
            for (int i = 0; i < list.Count; i++)
            {
                object item = list[i];
                if (item == null) continue;
                EnsureOneKillMembers(item.GetType());
                if (_pOneKillId == null || _pOneKillName == null) return;

                int id = GetInt(item, _pOneKillId);
                if (id != localId) continue;

                bool nameSpec = _pOneKillNameSpec == null || GetBool(item, _pOneKillNameSpec);
                if (nameSpec)
                {
                    string origName = GetString(item, _pOneKillName);
                    if (string.IsNullOrEmpty(_realLocalName) &&
                        !string.IsNullOrEmpty(origName) &&
                        !string.Equals(origName, fake, StringComparison.Ordinal))
                    {
                        _realLocalName = origName;
                    }
                    SetString(item, _pOneKillName, fake);
                }
                EnsureVipLevelSynced(fake);
                break;
            }
        }

        // ------------------------------------------------------------------ Hook 3: ChatJobSystem.ParseChatXml
        private static void TryInstallChatXmlHook()
        {
            try
            {
                Type chatSysType = FindType("Assets.Sources.Modules.Ui.Chat.ChatJobSystem", "ChatJobSystem");
                if (chatSysType == null) return;

                MethodInfo mParse = null;
                foreach (MethodInfo m in chatSysType.GetMethods(All))
                {
                    if (m.Name != "ParseChatXml" || m.IsStatic || m.IsAbstract) continue;
                    if (m.ReturnType != typeof(bool)) continue;
                    ParameterInfo[] ps = m.GetParameters();
                    if (ps.Length == 2 &&
                        ps[0].ParameterType == typeof(string) &&
                        ps[1].ParameterType.IsByRef &&
                        ps[1].ParameterType.GetElementType().Name == "ChatHistroyData")
                    {
                        mParse = m;
                        break;
                    }
                }
                if (mParse == null) return;

                Type outByRefType = mParse.GetParameters()[1].ParameterType;
                Type outElemType = outByRefType.GetElementType();

                MethodInfo mRewrite = typeof(StreamerMode).GetMethod(
                    nameof(RewriteChatXml), BindingFlags.Public | BindingFlags.Static);
                MethodInfo mBefore = typeof(StreamerMode).GetMethod(
                    nameof(BeforeParseChatXml), BindingFlags.Public | BindingFlags.Static);
                MethodInfo mAfter = typeof(StreamerMode).GetMethod(
                    nameof(AfterParseChatXml), BindingFlags.Public | BindingFlags.Static);
                if (mRewrite == null || mBefore == null || mAfter == null) return;

                // 生成零装箱 DynamicMethod，直接透传 out ChatHistroyData 引用参数
                DynamicMethod dm = new DynamicMethod(
                    "SsjjSmHookParseChatXml",
                    typeof(bool),
                    new[] { chatSysType, typeof(string), outByRefType },
                    typeof(StreamerMode).Module,
                    true);

                ILGenerator il = dm.GetILGenerator();
                LocalBuilder locXml = il.DeclareLocal(typeof(string));
                LocalBuilder locRet = il.DeclareLocal(typeof(bool));

                // locXml = RewriteChatXml(xml)
                il.Emit(OpCodes.Ldarg_1);
                il.Emit(OpCodes.Call, mRewrite);
                il.Emit(OpCodes.Stloc, locXml);

                // BeforeParseChatXml() —— 加锁并临时还原原始 12 字节
                il.Emit(OpCodes.Call, mBefore);

                il.BeginExceptionBlock();
                il.Emit(OpCodes.Ldarg_0);
                il.Emit(OpCodes.Ldloc, locXml);
                il.Emit(OpCodes.Ldarg_2);
                il.Emit(OpCodes.Call, mParse);
                il.Emit(OpCodes.Stloc, locRet);
                il.BeginFinallyBlock();
                // AfterParseChatXml() —— 回写跳转并释放锁
                il.Emit(OpCodes.Call, mAfter);
                il.EndExceptionBlock();

                il.Emit(OpCodes.Ldloc, locRet);
                il.Emit(OpCodes.Ret);

                Type delType = typeof(ParseChatXmlDel<,>).MakeGenericType(chatSysType, outElemType);
                Delegate del = dm.CreateDelegate(delType);
                _pinnedDelegates.Add(del);
                RuntimeHelpers.PrepareMethod(del.Method.MethodHandle);
                IntPtr dstPtr = del.Method.MethodHandle.GetFunctionPointer();

                IntPtr srcPtr;
                byte[] orig;
                byte[] jmp;
                if (!InstallJump12(mParse, dstPtr, out srcPtr, out orig, out jmp)) return;

                _chatXmlSrc = srcPtr;
                _chatXmlOrig = orig;
                _chatXmlJmp = jmp;
                _chatXmlHooked = true;
                Loader.Log("[streamer] 已安装 Hook: ChatJobSystem.ParseChatXml(string, out ChatHistroyData)");
            }
            catch (Exception ex)
            {
                Loader.Log("[streamer] 安装 ParseChatXml Hook 失败: " + ex.Message);
            }
        }

        public static string RewriteChatXml(string xml)
        {
            try
            {
                if (string.IsNullOrEmpty(xml) || _restored) return xml;
                RefreshConfigIfDue();
                if (!_enabled) return xml;
                string fake = _fakeName;
                string real = _realLocalName;
                if (string.IsNullOrEmpty(fake) || string.IsNullOrEmpty(real)) return xml;
                if (string.Equals(real, fake, StringComparison.Ordinal)) return xml;
                if (xml.IndexOf(real, StringComparison.Ordinal) >= 0)
                    return xml.Replace(real, fake);
            }
            catch { }
            return xml;
        }

        public static void BeforeParseChatXml()
        {
            Monitor.Enter(_chatXmlLock);
            if (_chatXmlSrc != IntPtr.Zero && _chatXmlOrig != null)
                WriteBytes(_chatXmlSrc, _chatXmlOrig);
        }

        public static void AfterParseChatXml()
        {
            try
            {
                if (!_restored && _chatXmlHooked && _chatXmlSrc != IntPtr.Zero && _chatXmlJmp != null)
                    WriteBytes(_chatXmlSrc, _chatXmlJmp);
            }
            finally
            {
                Monitor.Exit(_chatXmlLock);
            }
        }

        // ------------------------------------------------------------------ 供 KillFeedSimulator 协同调用
        /// <summary>
        /// 改写 KillPlayerInfoData 中归属本机玩家的昵称（击杀者 / 被击杀者 / 助攻连线者）。
        /// 无论换肤开关是否开启，只要直播模式启用即生效。
        /// </summary>
        public static void ApplyToKillData(object killData, int localId)
        {
            try
            {
                if (killData == null || _restored) return;
                RefreshConfigIfDue();
                if (!_enabled) return;

                string fake = _fakeName;
                if (string.IsNullOrEmpty(fake)) return;

                EnsureKillDataMembers(killData.GetType());

                if (localId > 0)
                {
                    int killerId = GetInt(killData, _pKillPlayerId);
                    if (killerId == localId && _pKillPlayerName != null)
                    {
                        string orig = GetString(killData, _pKillPlayerName);
                        if (string.IsNullOrEmpty(_realLocalName) &&
                            !string.IsNullOrEmpty(orig) &&
                            !string.Equals(orig, fake, StringComparison.Ordinal))
                        {
                            _realLocalName = orig;
                        }
                        SetString(killData, _pKillPlayerName, fake);
                    }

                    int beKillerId = GetInt(killData, _pBeKillPlayerId);
                    if (beKillerId == localId && _pBeKillPlayerName != null)
                    {
                        string orig = GetString(killData, _pBeKillPlayerName);
                        if (string.IsNullOrEmpty(_realLocalName) &&
                            !string.IsNullOrEmpty(orig) &&
                            !string.Equals(orig, fake, StringComparison.Ordinal))
                        {
                            _realLocalName = orig;
                        }
                        SetString(killData, _pBeKillPlayerName, fake);
                    }
                }

                string real = _realLocalName;
                if (!string.IsNullOrEmpty(real) && _pConnectPlayerName != null)
                {
                    string conn = GetString(killData, _pConnectPlayerName);
                    if (!string.IsNullOrEmpty(conn) && string.Equals(conn, real, StringComparison.Ordinal))
                        SetString(killData, _pConnectPlayerName, fake);
                }
            }
            catch { }
        }

        /// <summary>供 KillFeedSimulator 生成本地三杀通报时映射显示昵称。</summary>
        public static string ResolveDisplayName(string origName)
        {
            try
            {
                if (string.IsNullOrEmpty(origName) || _restored) return origName;
                RefreshConfigIfDue();
                if (!_enabled) return origName;
                string fake = _fakeName;
                if (string.IsNullOrEmpty(fake)) return origName;
                string real = _realLocalName;
                if (!string.IsNullOrEmpty(real) && string.Equals(origName, real, StringComparison.Ordinal))
                    return fake;
            }
            catch { }
            return origName;
        }

        // ------------------------------------------------------------------ 500ms 节流热加载配置（零常驻线程）
        private static void RefreshConfigIfDue()
        {
            int now = Environment.TickCount;
            int elapsed = unchecked(now - _lastCheckTick);
            if (elapsed >= 0 && elapsed < ConfigCheckIntervalMs) return;
            RefreshConfigCore(now);
        }

        private static void RefreshConfigForce()
        {
            RefreshConfigCore(Environment.TickCount);
        }

        private static void RefreshConfigCore(int nowTick)
        {
            bool needApply = false;
            bool needRestore = false;
            string applyTarget = "";

            lock (_cfgLock)
            {
                _lastCheckTick = nowTick;
                try
                {
                    bool exists = File.Exists(ConfigPath);
                    if (!exists)
                    {
                        if (_fileExisted || _enabled)
                        {
                            bool wasActive = _enabled && !string.IsNullOrEmpty(_fakeName);
                            _fileExisted = false;
                            _lastMtime = DateTime.MinValue;
                            _enabled = false;
                            _fakeName = "";
                            if (wasActive) needRestore = true;
                        }
                    }
                    else
                    {
                        DateTime mt = File.GetLastWriteTimeUtc(ConfigPath);
                        if (_fileExisted && mt == _lastMtime)
                            return;

                        _fileExisted = true;
                        _lastMtime = mt;

                        string raw = File.ReadAllText(ConfigPath, Encoding.UTF8);
                        bool newEnabled = ExtractJsonBool(raw, "enabled", false);
                        string newFake = (ExtractJsonString(raw, "fake_name") ?? "").Trim();

                        bool prevActive = _enabled && !string.IsNullOrEmpty(_fakeName);
                        string prevFake = _fakeName;
                        bool nextActive = newEnabled && !string.IsNullOrEmpty(newFake);

                        _enabled = newEnabled;
                        _fakeName = newFake;

                        if (nextActive && (!prevActive || !string.Equals(prevFake, newFake, StringComparison.Ordinal)))
                        {
                            needApply = true;
                            applyTarget = newFake;
                            Loader.Log("[streamer] 配置生效: enabled=true fake_name=" + newFake);
                        }
                        else if (!nextActive && prevActive)
                        {
                            needRestore = true;
                            Loader.Log("[streamer] 配置关闭: enabled=" + newEnabled + "，立即还原本地昵称");
                        }
                    }
                }
                catch { }
            }

            if (needApply)
                ApplyInMemoryNames(applyTarget);
            else if (needRestore)
                RestoreInMemoryNames();
        }

        private static void ApplyInMemoryNames(string fakeName)
        {
            if (string.IsNullOrEmpty(fakeName)) return;
            try
            {
                EnsureContextsReflection();

                // 1. 立即同步 myPlayerEntity.basicInfo.Current.PlayerName
                object myPlayer = GetMyPlayerEntity();
                if (myPlayer != null)
                {
                    ResolvePlayerMembers(myPlayer.GetType());
                    if (_pBasicInfo != null)
                    {
                        object basic = _pBasicInfo.GetValue(myPlayer, null);
                        object cur = GetBasicCurrent(basic);
                        if (cur != null)
                        {
                            EnsureEntityDataMembers(cur.GetType());
                            if (_pEntityPlayerName != null)
                            {
                                string orig = GetString(cur, _pEntityPlayerName);
                                if (string.IsNullOrEmpty(_realLocalName) &&
                                    !string.IsNullOrEmpty(orig) &&
                                    !string.Equals(orig, fakeName, StringComparison.Ordinal))
                                {
                                    _realLocalName = orig;
                                }
                                SetString(cur, _pEntityPlayerName, fakeName);
                            }
                        }
                    }
                }

                // 2. 立即同步 battleRoom.playerInfo.All 中本机项 + VipLevels
                UpdateBattleRoomScoreName(fakeName);
                EnsureVipLevelSynced(fakeName);
            }
            catch { }
        }

        private static void RestoreInMemoryNames()
        {
            string real = _realLocalName;
            if (string.IsNullOrEmpty(real)) return;
            try
            {
                EnsureContextsReflection();

                object myPlayer = GetMyPlayerEntity();
                if (myPlayer != null)
                {
                    ResolvePlayerMembers(myPlayer.GetType());
                    if (_pBasicInfo != null)
                    {
                        object basic = _pBasicInfo.GetValue(myPlayer, null);
                        object cur = GetBasicCurrent(basic);
                        if (cur != null)
                        {
                            EnsureEntityDataMembers(cur.GetType());
                            if (_pEntityPlayerName != null)
                                SetString(cur, _pEntityPlayerName, real);
                        }
                    }
                }

                UpdateBattleRoomScoreName(real);
                CleanupSyncedVipLevel();
            }
            catch { }
        }

        private static void UpdateBattleRoomScoreName(string targetName)
        {
            if (string.IsNullOrEmpty(targetName)) return;
            object playerInfo = GetBattleRoomPlayerInfo();
            if (playerInfo == null || _fPlayerInfoAll == null) return;

            IList all = _fPlayerInfoAll.GetValue(playerInfo) as IList;
            if (all == null || all.Count == 0) return;

            int localId = WeaponSwapper.GetLocalPlayerId();
            if (localId <= 0) return;

            for (int i = 0; i < all.Count; i++)
            {
                object item = all[i];
                if (item == null) continue;
                EnsureOneKillMembers(item.GetType());
                if (_pOneKillId == null || _pOneKillName == null) return;
                if (GetInt(item, _pOneKillId) == localId)
                {
                    SetString(item, _pOneKillName, targetName);
                    break;
                }
            }
        }

        private static void EnsureVipLevelSynced(string fakeName)
        {
            if (string.IsNullOrEmpty(fakeName)) return;
            string real = _realLocalName;
            if (string.IsNullOrEmpty(real) || string.Equals(real, fakeName, StringComparison.Ordinal)) return;
            if (string.Equals(_lastSyncedVipFakeName, fakeName, StringComparison.Ordinal)) return;

            try
            {
                EnsureContextsReflection();
                object playerInfo = GetBattleRoomPlayerInfo();
                if (playerInfo == null || _fPlayerInfoVipLevels == null) return;
                IDictionary<string, int> vips = _fPlayerInfoVipLevels.GetValue(playerInfo) as IDictionary<string, int>;
                if (vips == null) return;

                int lv;
                if (vips.TryGetValue(real, out lv))
                {
                    vips[fakeName] = lv;
                    _lastSyncedVipFakeName = fakeName;
                }
            }
            catch { }
        }

        private static void CleanupSyncedVipLevel()
        {
            string synced = _lastSyncedVipFakeName;
            _lastSyncedVipFakeName = "";
            if (string.IsNullOrEmpty(synced) || string.Equals(synced, _realLocalName, StringComparison.Ordinal))
                return;
            try
            {
                EnsureContextsReflection();
                object playerInfo = GetBattleRoomPlayerInfo();
                if (playerInfo == null || _fPlayerInfoVipLevels == null) return;
                IDictionary<string, int> vips = _fPlayerInfoVipLevels.GetValue(playerInfo) as IDictionary<string, int>;
                if (vips != null) vips.Remove(synced);
            }
            catch { }
        }

        // ------------------------------------------------------------------ 退出 100% 还原
        public static int Restore()
        {
            _restored = true;
            RestoreInMemoryNames();

            int count = 0;
            lock (_installLock)
            {
                lock (_postInitLock)
                {
                    _postInitHooked = false;
                    _postInitSrc = IntPtr.Zero;
                    _postInitOrig = null;
                    _postInitJmp = null;
                }
                lock (_allKillLock)
                {
                    _allKillHooked = false;
                    _allKillSrc = IntPtr.Zero;
                    _allKillOrig = null;
                    _allKillJmp = null;
                }
                lock (_chatXmlLock)
                {
                    _chatXmlHooked = false;
                    _chatXmlSrc = IntPtr.Zero;
                    _chatXmlOrig = null;
                    _chatXmlJmp = null;
                }

                for (int i = 0; i < _hookOrig.Count; i++)
                {
                    if (WriteBytes(_hookOrig[i].Key, _hookOrig[i].Value))
                        count++;
                }
                _hookOrig.Clear();
            }

            if (count > 0)
                Loader.Log("[streamer] 已还原 " + count + " 个直播模式 Hook 入口字节并恢复本机昵称");
            return count;
        }

        // ------------------------------------------------------------------ 底层 12 字节 Inline Hook
        private static bool InstallJump12(MethodInfo target, IntPtr dstPtr,
            out IntPtr srcPtr, out byte[] orig, out byte[] jmp)
        {
            srcPtr = IntPtr.Zero;
            orig = null;
            jmp = null;
            if (target == null || dstPtr == IntPtr.Zero || IntPtr.Size != 8) return false;

            RuntimeHelpers.PrepareMethod(target.MethodHandle);
            srcPtr = target.MethodHandle.GetFunctionPointer();
            if (srcPtr == IntPtr.Zero) return false;

            jmp = BuildJmp12(dstPtr);
            orig = new byte[12];

            uint oldProt;
            if (!VirtualProtect(srcPtr, (UIntPtr)16, PAGE_EXECUTE_READWRITE, out oldProt))
                return false;
            Marshal.Copy(srcPtr, orig, 0, orig.Length);

            bool seen = false;
            for (int i = 0; i < _hookOrig.Count; i++)
            {
                if (_hookOrig[i].Key == srcPtr) { seen = true; break; }
            }
            if (!seen)
                _hookOrig.Add(new KeyValuePair<IntPtr, byte[]>(srcPtr, orig));

            Marshal.Copy(jmp, 0, srcPtr, jmp.Length);
            uint dummy;
            VirtualProtect(srcPtr, (UIntPtr)16, oldProt, out dummy);
            return true;
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

        // ------------------------------------------------------------------ 反射缓存与安全读写
        private static void ResolvePlayerMembers(Type playerType)
        {
            if (_playerMembersResolved || playerType == null) return;
            _playerMembersResolved = true;
            _pIsMyPlayer = playerType.GetProperty("isMyPlayer", All);
            _pBasicInfo = playerType.GetProperty("basicInfo", All);
            if (_pBasicInfo != null)
            {
                Type tBasic = _pBasicInfo.PropertyType;
                _fBasicCurrent = tBasic.GetField("Current", All);
                if (_fBasicCurrent == null)
                    _pBasicCurrent = tBasic.GetProperty("Current", All);
            }
        }

        private static object GetBasicCurrent(object basicInfo)
        {
            if (basicInfo == null) return null;
            if (_fBasicCurrent != null) return _fBasicCurrent.GetValue(basicInfo);
            if (_pBasicCurrent != null) return _pBasicCurrent.GetValue(basicInfo, null);
            return null;
        }

        private static void EnsureEntityDataMembers(Type t)
        {
            if (_pEntityPlayerName != null || t == null) return;
            _pEntityPlayerName = t.GetProperty("PlayerName", All);
            _pEntityPlayerNameSpec = t.GetProperty("PlayerNameSpecified", All);
        }

        private static void ResolveAllKillMembers(Type allKillType)
        {
            if (_allKillMembersResolved || allKillType == null) return;
            _allKillMembersResolved = true;
            _pAllKillList = allKillType.GetProperty("List", All);
        }

        private static void EnsureOneKillMembers(Type oneKillType)
        {
            if (_pOneKillId != null || oneKillType == null) return;
            _pOneKillId = oneKillType.GetProperty("Id", All);
            _pOneKillName = oneKillType.GetProperty("PlayerName", All);
            _pOneKillNameSpec = oneKillType.GetProperty("PlayerNameSpecified", All);
        }

        private static void EnsureKillDataMembers(Type t)
        {
            if (_cachedKillDataType == t || t == null) return;
            _cachedKillDataType = t;
            _pKillPlayerId = t.GetProperty("KillPlayerId", All);
            _pBeKillPlayerId = t.GetProperty("BeKillPlayerId", All);
            _pKillPlayerName = t.GetProperty("KillPlayerName", All);
            _pBeKillPlayerName = t.GetProperty("BeKillPlayerName", All);
            _pConnectPlayerName = t.GetProperty("ConnectPlayerName", All);
        }

        private static void EnsureContextsReflection()
        {
            if (_contextsResolved) return;
            _contextsResolved = true;
            try
            {
                Type tCtx = FindType("Contexts", "Contexts");
                if (tCtx == null) return;
                _pContextsShared = tCtx.GetProperty("sharedInstance", All);
                _pPlayerCtx = tCtx.GetProperty("player", All);
                if (_pPlayerCtx != null)
                    _pMyPlayerEntity = _pPlayerCtx.PropertyType.GetProperty("myPlayerEntity", All);

                _pBattleRoomCtx = tCtx.GetProperty("battleRoom", All);
                if (_pBattleRoomCtx != null)
                {
                    _pPlayerInfo = _pBattleRoomCtx.PropertyType.GetProperty("playerInfo", All);
                    if (_pPlayerInfo != null)
                    {
                        _fPlayerInfoAll = _pPlayerInfo.PropertyType.GetField("All", All);
                        _fPlayerInfoVipLevels = _pPlayerInfo.PropertyType.GetField("VipLevels", All);
                    }
                }
            }
            catch { }
        }

        private static object GetMyPlayerEntity()
        {
            if (_pContextsShared == null || _pPlayerCtx == null || _pMyPlayerEntity == null) return null;
            object ctx = _pContextsShared.GetValue(null, null);
            if (ctx == null) return null;
            object pCtx = _pPlayerCtx.GetValue(ctx, null);
            if (pCtx == null) return null;
            return _pMyPlayerEntity.GetValue(pCtx, null);
        }

        private static object GetBattleRoomPlayerInfo()
        {
            if (_pContextsShared == null || _pBattleRoomCtx == null || _pPlayerInfo == null) return null;
            object ctx = _pContextsShared.GetValue(null, null);
            if (ctx == null) return null;
            object brCtx = _pBattleRoomCtx.GetValue(ctx, null);
            if (brCtx == null) return null;
            return _pPlayerInfo.GetValue(brCtx, null);
        }

        private static int GetInt(object obj, PropertyInfo p)
        {
            if (obj == null || p == null) return 0;
            try { return Convert.ToInt32(p.GetValue(obj, null)); }
            catch { return 0; }
        }

        private static bool GetBool(object obj, PropertyInfo p)
        {
            if (obj == null || p == null) return false;
            try
            {
                object v = p.GetValue(obj, null);
                return v is bool && (bool)v;
            }
            catch { return false; }
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

        // ------------------------------------------------------------------ 轻量零依赖 JSON 解析（支持中文/Unicode 转义）
        private static bool ExtractJsonBool(string json, string key, bool defaultValue)
        {
            if (string.IsNullOrEmpty(json)) return defaultValue;
            Match m = Regex.Match(json, "\"" + Regex.Escape(key) + "\"\\s*:\\s*(true|false)", RegexOptions.IgnoreCase);
            if (!m.Success) return defaultValue;
            return m.Groups[1].Value.Equals("true", StringComparison.OrdinalIgnoreCase);
        }

        private static string ExtractJsonString(string json, string key)
        {
            if (string.IsNullOrEmpty(json)) return "";
            Match m = Regex.Match(json, "\"" + Regex.Escape(key) + "\"\\s*:\\s*\"((?:\\\\.|[^\"\\\\])*)\"");
            if (!m.Success) return "";
            return UnescapeJsonString(m.Groups[1].Value);
        }

        private static string UnescapeJsonString(string s)
        {
            if (string.IsNullOrEmpty(s) || s.IndexOf('\\') < 0) return s ?? "";
            StringBuilder sb = new StringBuilder(s.Length);
            for (int i = 0; i < s.Length; i++)
            {
                char c = s[i];
                if (c != '\\' || i + 1 >= s.Length)
                {
                    sb.Append(c);
                    continue;
                }
                char next = s[++i];
                switch (next)
                {
                    case '"': sb.Append('"'); break;
                    case '\\': sb.Append('\\'); break;
                    case '/': sb.Append('/'); break;
                    case 'b': sb.Append('\b'); break;
                    case 'f': sb.Append('\f'); break;
                    case 'n': sb.Append('\n'); break;
                    case 'r': sb.Append('\r'); break;
                    case 't': sb.Append('\t'); break;
                    case 'u':
                        if (i + 4 < s.Length)
                        {
                            string hex = s.Substring(i + 1, 4);
                            int code;
                            if (int.TryParse(hex, NumberStyles.HexNumber, CultureInfo.InvariantCulture, out code))
                            {
                                sb.Append((char)code);
                                i += 4;
                                break;
                            }
                        }
                        sb.Append("\\u");
                        break;
                    default:
                        sb.Append(next);
                        break;
                }
            }
            return sb.ToString();
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
