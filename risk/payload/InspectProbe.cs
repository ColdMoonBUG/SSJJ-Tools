using System;
using System.IO;
using System.Reflection;
using System.Threading;

namespace SsjjRisk
{
    // 显式放置 risk_inspect_probe.txt 才启用。只读采样，不修改游戏状态。
    internal static class InspectProbe
    {
        private const BindingFlags Flags = BindingFlags.Public | BindingFlags.NonPublic |
                                           BindingFlags.Instance | BindingFlags.Static;
        private static int _started;
        private static Type _contextsType;
        private static Type _animFactoryType;
        private static Type _crossLoaderType;
        private static readonly string LogFile = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "ssjj-toolbox", "risk_inspect.log");

        private static void Log(string message)
        {
            try
            {
                Directory.CreateDirectory(Path.GetDirectoryName(LogFile));
                File.AppendAllText(LogFile, DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss.fff") +
                    "  " + message + Environment.NewLine);
            }
            catch { }
        }

        internal static void Start()
        {
            try
            {
                string dir = Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                    "ssjj-toolbox");
                if (!File.Exists(Path.Combine(dir, "risk_inspect_probe.txt"))) return;
                if (Interlocked.Exchange(ref _started, 1) != 0) return;
                Thread thread = new Thread(Run);
                thread.IsBackground = true;
                thread.Name = "SsjjInspectProbe";
                thread.Start();
            }
            catch (Exception ex) { Log("[inspect] 启动失败: " + ex.Message); }
        }

        private static Type FindType(string name)
        {
            foreach (Assembly assembly in AppDomain.CurrentDomain.GetAssemblies())
            {
                try
                {
                    Type type = assembly.GetType(name, false);
                    if (type != null) return type;
                }
                catch { }
            }
            return null;
        }

        private static object Read(object obj, string name)
        {
            if (obj == null) return null;
            Type type = obj as Type ?? obj.GetType();
            object receiver = obj is Type ? null : obj;
            PropertyInfo property = type.GetProperty(name, Flags);
            if (property != null) return property.GetValue(receiver, null);
            FieldInfo field = type.GetField(name, Flags);
            return field == null ? null : field.GetValue(receiver);
        }

        private static object CacheEntry(string name)
        {
            Type type = _animFactoryType ??
                (_animFactoryType = FindType("Assets.Sources.Info.WeaponAnim.WeaponAnimInfoFactory"));
            object factory = Read(type, "Instance");
            object cache = Read(factory, "_weaponInfoCache");
            if (cache == null) return null;
            MethodInfo get = cache.GetType().GetMethod("GetOrNull", Flags, null,
                                                       new[] { typeof(string) }, null);
            return get == null ? null : get.Invoke(cache, new object[] { name });
        }

        private static string AnimDetails(object anim)
        {
            if (anim == null) return "未缓存";
            MethodInfo duration = anim.GetType().GetMethod("GetViewAnimDuration",
                Flags, null, new[] { typeof(int) }, null);
            MethodInfo next = anim.GetType().GetMethod("ViewGetNextAnim",
                Flags, null, new[] { typeof(int) }, null);
            return "44时长=" + (duration == null ? "?" : duration.Invoke(anim, new object[] { 44 })) +
                   " 下一动作=" + (next == null ? "?" : next.Invoke(anim, new object[] { 44 }));
        }

        private static string CrossDetails(string name)
        {
            Type type = _crossLoaderType ??
                (_crossLoaderType = FindType("Assets.Sources.CsvLoader.WeaponCrossLoader"));
            object table = Read(Read(type, "Instance"), "Table");
            MethodInfo find = table == null ? null : table.GetType().GetMethod(
                "Find_Name", Flags, null, new[] { typeof(string) }, null);
            object row = find == null ? null : find.Invoke(table, new object[] { name });
            return row == null ? "无配置" :
                "SpecialCross=" + Read(row, "SpecialCross") +
                " SniperCross=" + Read(row, "SniperCross") +
                " SpecialModel=" + Read(row, "SpecialModel");
        }

        private static string UiDetails(string name)
        {
            Type type = FindType("Assets.Sources.Info.Weapon.WeaponInfoFactory");
            object factory = Read(type, "Instance");
            MethodInfo get = factory == null ? null :
                factory.GetType().GetMethod("GetWeaponInfo", Flags, null,
                                            new[] { typeof(string) }, null);
            object info = get == null ? null : get.Invoke(factory, new object[] { name });
            if (info == null) return "武器信息未就绪";
            return "bundle=" + (Read(info, "AssetBundleName") ?? "?") +
                " layout=" + (Read(info, "WeaponLayoutAssetName") ?? "?") +
                " logic=" + (Read(info, "WeaponLogicAssetName") ?? "?");
        }

        private static void LogUiProviderMethods()
        {
            int found = 0;
            foreach (Assembly assembly in AppDomain.CurrentDomain.GetAssemblies())
            {
                Type[] types;
                try { types = assembly.GetTypes(); }
                catch (ReflectionTypeLoadException ex) { types = ex.Types; }
                catch { continue; }
                foreach (Type type in types)
                {
                    if (type == null || type.FullName == null ||
                        (type.FullName.IndexOf("Provider", StringComparison.OrdinalIgnoreCase) < 0 &&
                         type.FullName.IndexOf("RuleUiPlayback", StringComparison.OrdinalIgnoreCase) < 0))
                        continue;
                    foreach (MethodInfo method in type.GetMethods(Flags))
                    {
                        if (method.Name != "IsHoldWeapon" && method.Name != "GetPower" &&
                            method.Name != "GetWeaponName") continue;
                        Log("[inspect] 专属UI入口 " + type.FullName + "." + method);
                        if (++found >= 12) return;
                    }
                }
            }
            if (found == 0) Log("[inspect] 未找到专属UI入口");
        }

        private static void Run()
        {
            string lastWeapon = "";
            int lastState = -1;
            int lastBucket = -1;
            bool uiMethodsLogged = false;
            DateTime deadline = DateTime.UtcNow.AddMinutes(3);
            Log("[inspect] 采样开始，最多三分钟；仅记录蝶舞映射的动作变化");
            while (DateTime.UtcNow < deadline)
            {
                try
                {
                    Type contextsType = _contextsType ?? (_contextsType = FindType("Contexts"));
                    object contexts = Read(contextsType, "sharedInstance");
                    object player = Read(Read(contexts, "player"), "myPlayerEntity");
                    object weapon = Read(Read(Read(player, "currentWeapon"), "WeaponInfo"), "Name");
                    string name = weapon as string;
                    if (!string.IsNullOrEmpty(name) &&
                        string.Equals(WeaponSwapper.ResolveModelWeapon(name), "butterfly_knife",
                                      StringComparison.OrdinalIgnoreCase))
                    {
                        object view = Read(player, "viewAnim");
                        object info = Read(view, "ViewAnimInfo");
                        int state = Convert.ToInt32(Read(info, "StateId") ?? -1);
                        int remain = Convert.ToInt32(Read(view, "RemainTime") ?? -1);
                        int bucket = remain / 500;
                        if (name != lastWeapon || state != lastState ||
                            (state == 44 && bucket != lastBucket))
                        {
                            object snapshot = Read(Read(player, "basicInfo"), "Current");
                            bool enteringInspect = state == 44 && (name != lastWeapon || lastState != 44);
                            Log("[inspect] 武器=" + name + " 本地动作=" + state +
                                " 剩余=" + remain +
                                " 快照动作=" + (Read(snapshot, "ViewAnim") ?? "?") +
                                " 快照剩余=" + (Read(snapshot, "ViewAnimationTimer") ?? "?") +
                                " 缓存源={" + AnimDetails(CacheEntry(name)) + "}" +
                                " 缓存蝶舞={" + AnimDetails(CacheEntry("butterfly_knife")) + "}");
                            if (enteringInspect)
                            {
                                object current = Read(Read(contexts, "weapon"), "currentWeaponEntity");
                                object hasPower = Read(current, "hasPower");
                                object power = hasPower is bool && (bool)hasPower ?
                                    Read(current, "power") : null;
                                Log("[inspect] 准星状态 武器=" + name +
                                    " hasPower=" + (hasPower ?? "?") +
                                    " Power=" + (Read(power, "Power") ?? "?") +
                                    " Power2=" + (Read(power, "Power2") ?? "?"));
                                Log("[inspect] 准星配置 源={" + CrossDetails(name) +
                                    "} 蝶舞={" + CrossDetails("butterfly_knife") + "}");
                                Log("[inspect] 专属UI 源={" + UiDetails(name) +
                                    "} 蝶舞={" + UiDetails("butterfly_knife") + "}");
                                if (!uiMethodsLogged)
                                {
                                    uiMethodsLogged = true;
                                    LogUiProviderMethods();
                                }
                            }
                            lastWeapon = name;
                            lastState = state;
                            lastBucket = bucket;
                        }
                    }
                    else
                    {
                        lastWeapon = "";
                        lastState = -1;
                        lastBucket = -1;
                    }
                }
                catch (Exception ex)
                {
                    // 反射对象可能在换场景时销毁；限量记录，不阻断采样。
                    if (lastState != -2)
                        Log("[inspect] 采样暂不可用: " + ex.GetType().Name + " " + ex.Message);
                    lastState = -2;
                }
                Thread.Sleep(100);
            }
            Log("[inspect] 采样结束");
        }
    }
}
