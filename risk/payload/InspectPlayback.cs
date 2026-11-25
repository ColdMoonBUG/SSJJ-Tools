using System;
using System.Diagnostics;
using System.Reflection;
using System.Reflection.Emit;
using System.Runtime.CompilerServices;
using System.Collections.Generic;

namespace SsjjRisk
{
    // 只改变第一人称动画器的显示时间，不改 ViewAnimComponent 或服务端快照。
    internal static class InspectPlayback
    {
        private const BindingFlags All = BindingFlags.Public | BindingFlags.NonPublic |
                                         BindingFlags.Instance | BindingFlags.Static;
        private sealed class Clock
        {
            internal object Animator;
            internal object InspectView;
            internal string Weapon;
            internal int Duration;
            internal int LastState;
            internal long Started;
            internal long LastWeaponCheck;
        }

        private sealed class AttackClock
        {
            internal object Animator;
            internal string Weapon;
            internal int State;
            internal int Duration;
            internal int ServerWindow;
            internal int LastRemain;
            internal long Started;
            internal long LastWeaponCheck;
            internal object VisualView;
        }

        private static Clock _clock;
        private static AttackClock _attack;
        private static object _secondLightView;
        private static Type _secondLightType;
        private static Type _contextsType;
        private static readonly List<Delegate> Pinned = new List<Delegate>();

        internal static void Reset()
        {
            _clock = null;
            _attack = null;
            _secondLightView = null;
            _secondLightType = null;
        }

        private static object Read(object value, string name)
        {
            if (value == null) return null;
            Type type = value as Type ?? value.GetType();
            object receiver = value is Type ? null : value;
            PropertyInfo prop = type.GetProperty(name, All);
            if (prop != null) return prop.GetValue(receiver, null);
            FieldInfo field = type.GetField(name, All);
            return field == null ? null : field.GetValue(receiver);
        }

        private static string CurrentWeapon()
        {
            if (_contextsType == null)
                foreach (Assembly assembly in AppDomain.CurrentDomain.GetAssemblies())
                {
                    _contextsType = assembly.GetType("Contexts", false);
                    if (_contextsType != null) break;
                }
            object contexts = Read(_contextsType, "sharedInstance");
            object player = Read(Read(contexts, "player"), "myPlayerEntity");
            return Read(Read(Read(player, "currentWeapon"), "WeaponInfo"), "Name") as string;
        }

        private static bool IsLocalAnimator(object animator)
        {
            object contexts = Read(_contextsType, "sharedInstance");
            object player = Read(Read(contexts, "player"), "myPlayerEntity");
            object firstPerson = Read(player, "firstPersonUnityObjects");
            return ReferenceEquals(animator, Read(firstPerson, "WeaponAnimator"));
        }

        // 动画器调用前独立修正局部播放位置；攻击、切枪等状态仍由游戏自己控制。
        private static bool IsKnifeAttack(int state)
        {
            return state == 20 || state == 29 || state == 30 || state == 31;
        }

        public static object Adjust(object animator, object view, int state, int remain,
                                    int duration, object animInfo,
                                    ref int visualTime)
        {
            try
            {
                long now = Stopwatch.GetTimestamp();
                if (IsKnifeAttack(state))
                {
                    if (!IsLocalAnimator(animator)) return view;
                    _clock = null;
                    AttackClock attack = _attack;
                    bool restart = attack == null ||
                        !ReferenceEquals(attack.Animator, animator) ||
                        attack.State != state || remain > attack.LastRemain + 120;
                    if (restart)
                    {
                        string name = CurrentWeapon();
                        object activeView = view;
                        if (state == 29 &&
                            string.Equals(WeaponSwapper.ResolveModelWeapon(name),
                                          "butterfly_knife", StringComparison.OrdinalIgnoreCase) &&
                            !string.Equals(name, "butterfly_knife", StringComparison.OrdinalIgnoreCase))
                        {
                            MethodInfo getDuration = animInfo.GetType().GetMethod(
                                "GetViewAnimDuration", new[] { typeof(int) });
                            int secondDuration = getDuration == null ? 0 :
                                Convert.ToInt32(getDuration.Invoke(animInfo, new object[] { 170 }));
                            if (secondDuration > 0)
                            {
                                if (_secondLightView == null || _secondLightType != view.GetType())
                                {
                                    _secondLightView = Activator.CreateInstance(view.GetType(),
                                        new object[] { 170 });
                                    _secondLightType = view.GetType();
                                }
                                activeView = _secondLightView;
                                duration = secondDuration;
                            }
                        }
                        if (string.Equals(WeaponSwapper.ResolveModelWeapon(name),
                                          "butterfly_knife", StringComparison.OrdinalIgnoreCase) &&
                            !string.Equals(name, "butterfly_knife", StringComparison.OrdinalIgnoreCase) &&
                            duration >= 200 && duration <= 4000 && remain >= 120 &&
                            Math.Abs(duration - remain) > 120)
                        {
                            attack = new AttackClock
                            {
                                Animator = animator, Weapon = name, State = state,
                                Duration = duration, ServerWindow = remain,
                                LastRemain = remain, Started = now, LastWeaponCheck = now,
                                VisualView = activeView
                            };
                            _attack = attack;
                            Loader.Log("[inspect] 本地攻击动画对齐: " + name +
                                       " 动作=" + state + (activeView != view ? " 画面=170" : "") +
                                       " 动画=" + duration +
                                       "ms 原刀窗口=" + remain + "ms");
                        }
                        else
                        {
                            _attack = null;
                            return view;
                        }
                    }
                    if (now - attack.LastWeaponCheck >= Stopwatch.Frequency / 10)
                    {
                        if (!string.Equals(CurrentWeapon(), attack.Weapon,
                                           StringComparison.OrdinalIgnoreCase))
                        {
                            _attack = null;
                            return view;
                        }
                        attack.LastWeaponCheck = now;
                    }
                    long attackElapsed = (now - attack.Started) * 1000 / Stopwatch.Frequency;
                    visualTime = (int)Math.Max(0, Math.Min(attack.Duration,
                        attackElapsed * attack.Duration / attack.ServerWindow));
                    attack.LastRemain = remain;
                    return attack.VisualView ?? view;
                }
                // 攻击动画不能在待机或换枪后继续覆盖画面。
                _attack = null;
                Clock clock = _clock;
                if (clock != null && !ReferenceEquals(clock.Animator, animator))
                    clock = null;

                if (state == 44)
                {
                    if (clock == null || clock.LastState != 44)
                    {
                        string name = CurrentWeapon();
                        if (!IsLocalAnimator(animator) ||
                            !string.Equals(WeaponSwapper.ResolveModelWeapon(name),
                                           "butterfly_knife", StringComparison.OrdinalIgnoreCase) ||
                            string.Equals(name, "butterfly_knife", StringComparison.OrdinalIgnoreCase) ||
                            duration < 4000 || duration > 12000)
                        {
                            _clock = null;
                            return view;
                        }
                        clock = new Clock
                        {
                            Animator = animator, InspectView = view, Weapon = name,
                            Duration = duration, LastState = 44,
                            Started = now, LastWeaponCheck = now
                        };
                        _clock = clock;
                        Loader.Log("[inspect] 本地长检视开始: " + name +
                                   " 目标=butterfly_knife 时长=" + duration + "ms");
                    }
                }
                else if (clock == null || (state != 13 && state != 14))
                {
                    if (clock != null) _clock = null;
                    return view;
                }

                long elapsed = (now - clock.Started) * 1000 / Stopwatch.Frequency;
                if (elapsed < 0 || elapsed >= clock.Duration)
                {
                    if (elapsed >= clock.Duration)
                        Loader.Log("[inspect] 本地长检视播放完成");
                    _clock = null;
                    return view;
                }
                // 武器模型可能复用同一个动画器；低频核对真实武器以便立即终止。
                if (now - clock.LastWeaponCheck >= Stopwatch.Frequency / 10)
                {
                    if (!string.Equals(CurrentWeapon(), clock.Weapon,
                                       StringComparison.OrdinalIgnoreCase))
                    {
                        _clock = null;
                        return view;
                    }
                    clock.LastWeaponCheck = now;
                }
                clock.LastState = state;
                visualTime = (int)elapsed;
                return clock.InspectView;
            }
            catch
            {
                _clock = null;
                _attack = null;
                return view;
            }
        }

        private static PropertyInfo Property(Type type, string name)
        {
            PropertyInfo property = type.GetProperty(name);
            if (property != null) return property;
            foreach (Type iface in type.GetInterfaces())
            {
                property = iface.GetProperty(name);
                if (property != null) return property;
            }
            return null;
        }

        internal static Delegate Create(MethodInfo original)
        {
            ParameterInfo[] ps = original.GetParameters();
            if (original.IsStatic || original.ReturnType != typeof(void) || ps.Length != 7 ||
                ps[3].ParameterType != typeof(int).MakeByRefType() ||
                ps[4].ParameterType != typeof(int) || ps[6].ParameterType != typeof(float))
                return null;

            Type animator = ps[0].ParameterType;
            Type view = ps[2].ParameterType;
            Type info = ps[5].ParameterType;
            // IViewAnimStateInfo 与 IAnimStateInfo 是两个独立接口，原方法也显式 cast。
            Type stateType = original.DeclaringType.Assembly.GetType(
                "Assets.Sources.Components.Interface.Info.AnimState.IAnimStateInfo", false);
            if (stateType == null)
                foreach (Assembly candidateAssembly in AppDomain.CurrentDomain.GetAssemblies())
                {
                    stateType = candidateAssembly.GetType(
                        "Assets.Sources.Components.Interface.Info.AnimState.IAnimStateInfo", false);
                    if (stateType != null) break;
                }
            if (stateType == null) return null;
            PropertyInfo loop = Property(stateType, "IsLoop");
            PropertyInfo state = Property(stateType, "StateId");
            PropertyInfo name = Property(view, "AnimationName");
            MethodInfo getDuration = info.GetMethod("GetViewAnimDuration", new[] { typeof(int) });
            MethodInfo getAnimDuration = animator.GetMethod("GetAnimDuration", new[] { typeof(int), typeof(string) });
            MethodInfo play = animator.GetMethod("PlayAtFixedTime",
                new[] { typeof(int), typeof(string), typeof(float), typeof(float),
                        typeof(bool), typeof(bool), typeof(float), typeof(bool) });
            if (loop == null || state == null || name == null || getDuration == null ||
                getAnimDuration == null || play == null ||
                loop.PropertyType != typeof(bool) || state.PropertyType != typeof(int) ||
                name.PropertyType != typeof(string) || getAnimDuration.ReturnType != typeof(float))
                return null;

            Type[] args = new Type[8];
            args[0] = original.DeclaringType;
            for (int i = 0; i < ps.Length; i++) args[i + 1] = ps[i].ParameterType;
            DynamicMethod dm = new DynamicMethod("SsjjVisualInspectPlayback",
                typeof(void), args, typeof(InspectPlayback).Module, true);
            ILGenerator il = dm.GetILGenerator();
            LocalBuilder activeView = il.DeclareLocal(view);
            LocalBuilder isLoop = il.DeclareLocal(typeof(bool));
            LocalBuilder duration = il.DeclareLocal(typeof(int));
            LocalBuilder visualTime = il.DeclareLocal(typeof(int));
            Label done = il.DefineLabel();
            Label computed = il.DefineLabel();
            Label nonzero = il.DefineLabel();

            il.Emit(OpCodes.Ldarg_3);
            il.Emit(OpCodes.Brfalse, done);
            il.Emit(OpCodes.Ldarg_3);
            il.Emit(OpCodes.Stloc, activeView);
            il.Emit(OpCodes.Ldarg_3);
            il.Emit(OpCodes.Castclass, stateType);
            il.Emit(OpCodes.Callvirt, loop.GetGetMethod());
            il.Emit(OpCodes.Stloc, isLoop);
            il.Emit(OpCodes.Ldloc, isLoop);
            il.Emit(OpCodes.Brtrue, computed);

            il.Emit(OpCodes.Ldarg_S, (byte)6);
            il.Emit(OpCodes.Ldarg_3);
            il.Emit(OpCodes.Castclass, stateType);
            il.Emit(OpCodes.Callvirt, state.GetGetMethod());
            il.Emit(OpCodes.Callvirt, getDuration);
            il.Emit(OpCodes.Stloc, duration);
            il.Emit(OpCodes.Ldloc, duration);
            il.Emit(OpCodes.Brtrue, nonzero);
            il.Emit(OpCodes.Ldarg_2);
            il.Emit(OpCodes.Ldc_I4_0);
            il.Emit(OpCodes.Ldarg_3);
            il.Emit(OpCodes.Callvirt, name.GetGetMethod());
            il.Emit(OpCodes.Callvirt, getAnimDuration);
            il.Emit(OpCodes.Ldc_R4, 1000f);
            il.Emit(OpCodes.Mul);
            il.Emit(OpCodes.Conv_I4);
            il.Emit(OpCodes.Stloc, duration);
            il.MarkLabel(nonzero);
            il.Emit(OpCodes.Ldarg_S, (byte)4);
            il.Emit(OpCodes.Ldloc, duration);
            il.Emit(OpCodes.Ldarg_S, (byte)5);
            il.Emit(OpCodes.Sub);
            il.Emit(OpCodes.Stind_I4);
            il.MarkLabel(computed);

            il.Emit(OpCodes.Ldarg_S, (byte)4);
            il.Emit(OpCodes.Ldind_I4);
            il.Emit(OpCodes.Stloc, visualTime);
            il.Emit(OpCodes.Ldarg_1);
            il.Emit(OpCodes.Ldarg_3);
            il.Emit(OpCodes.Ldarg_3);
            il.Emit(OpCodes.Castclass, stateType);
            il.Emit(OpCodes.Callvirt, state.GetGetMethod());
            il.Emit(OpCodes.Ldarg_S, (byte)5);
            il.Emit(OpCodes.Ldloc, duration);
            il.Emit(OpCodes.Ldarg_S, (byte)6);
            il.Emit(OpCodes.Ldloca_S, visualTime);
            il.Emit(OpCodes.Call, typeof(InspectPlayback).GetMethod("Adjust"));
            il.Emit(OpCodes.Castclass, view);
            il.Emit(OpCodes.Stloc, activeView);
            il.Emit(OpCodes.Ldloc, activeView);
            il.Emit(OpCodes.Castclass, stateType);
            il.Emit(OpCodes.Callvirt, loop.GetGetMethod());
            il.Emit(OpCodes.Stloc, isLoop);
            EmitPlay(il, 2, activeView, visualTime, isLoop, name.GetGetMethod(), play);
            EmitPlay(il, 1, activeView, visualTime, isLoop, name.GetGetMethod(), play);
            il.MarkLabel(done);
            il.Emit(OpCodes.Ret);

            // Action<> 不支持 ref int；建立与实例方法 ABI 完全一致的委托。
#if NET8_0_OR_GREATER
            AssemblyBuilder assembly = AssemblyBuilder.DefineDynamicAssembly(
                new AssemblyName("SsjjInspectDelegate"), AssemblyBuilderAccess.Run);
#else
            AssemblyBuilder assembly = AppDomain.CurrentDomain.DefineDynamicAssembly(
                new AssemblyName("SsjjInspectDelegate"), AssemblyBuilderAccess.Run);
#endif
            ModuleBuilder module = assembly.DefineDynamicModule("InspectDelegate");
            TypeBuilder type = module.DefineType("InspectPlaybackDelegate",
                TypeAttributes.Public | TypeAttributes.Sealed | TypeAttributes.Class,
                typeof(MulticastDelegate));
            ConstructorBuilder ctor = type.DefineConstructor(MethodAttributes.Public |
                MethodAttributes.HideBySig | MethodAttributes.RTSpecialName,
                CallingConventions.Standard, new[] { typeof(object), typeof(IntPtr) });
            ctor.SetImplementationFlags(MethodImplAttributes.Runtime | MethodImplAttributes.Managed);
            MethodBuilder invoke = type.DefineMethod("Invoke", MethodAttributes.Public |
                MethodAttributes.HideBySig | MethodAttributes.NewSlot |
                MethodAttributes.Virtual, typeof(void), args);
            invoke.SetImplementationFlags(MethodImplAttributes.Runtime | MethodImplAttributes.Managed);
            Delegate del = dm.CreateDelegate(type.CreateType());
            Pinned.Add(del);
            return del;
        }

        internal static IntPtr Build(MethodInfo original)
        {
            Delegate del = Create(original);
            if (del == null) return IntPtr.Zero;
            RuntimeHelpers.PrepareMethod(del.Method.MethodHandle);
            return del.Method.MethodHandle.GetFunctionPointer();
        }

        private static void EmitPlay(ILGenerator il, byte arg, LocalBuilder view,
                                     LocalBuilder time, LocalBuilder loop,
                                     MethodInfo getName, MethodInfo play)
        {
            il.Emit(OpCodes.Ldarg_S, arg);
            il.Emit(OpCodes.Ldc_I4_0);
            il.Emit(OpCodes.Ldloc, view);
            il.Emit(OpCodes.Callvirt, getName);
            il.Emit(OpCodes.Ldloc, time);
            il.Emit(OpCodes.Conv_R4);
            il.Emit(OpCodes.Ldc_R4, 0.001f);
            il.Emit(OpCodes.Mul);
            il.Emit(OpCodes.Ldc_R4, 1f);
            il.Emit(OpCodes.Ldloc, loop);
            il.Emit(OpCodes.Ldc_I4_0);
            il.Emit(OpCodes.Ldarg_S, (byte)7);
            il.Emit(OpCodes.Ldc_I4_1);
            il.Emit(OpCodes.Callvirt, play);
        }
    }
}
