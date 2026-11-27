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
            internal int VisualState;
            internal int LastRemain;
            internal bool Finished;
            internal object TargetInfo;
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

        private sealed class RaiseClock
        {
            internal object Animator;
            internal string Weapon;
            internal string Target;
            internal int State;
            internal int TargetDur;
            internal int OrigDur;
            internal int LastRemain;
            internal long Started;
            internal long LastWeaponCheck;
            internal object IdleView;
        }

        private static Clock _clock;
        private static AttackClock _attack;
        private static RaiseClock _raise;
        private static long _raiseGuardDeadline;
        private static object _raiseGuardAnimator;
        private static string _raiseGuardWeapon;
        private static int _raiseGuardState;
        private static int _raiseGuardRemain;
        private static object _standView;
        private static object _specialStandView;
        private static Type _standViewType;
        private static object _secondLightView;
        private static Type _secondLightType;
        private static Type _contextsType;
        private static readonly List<Delegate> Pinned = new List<Delegate>();

        internal static void Reset()
        {
            _clock = null;
            _attack = null;
            _raise = null;
            ClearRaiseGuard();
            _standView = null;
            _specialStandView = null;
            _standViewType = null;
            _secondLightView = null;
            _secondLightType = null;
        }

        internal static bool IsRaiseGuardActive()
        {
            long deadline = System.Threading.Interlocked.Read(ref _raiseGuardDeadline);
            return deadline > 0 && Stopwatch.GetTimestamp() < deadline;
        }

        private static void ClearRaiseGuard()
        {
            System.Threading.Interlocked.Exchange(ref _raiseGuardDeadline, 0);
            _raiseGuardAnimator = null;
            _raiseGuardWeapon = null;
            _raiseGuardRemain = 0;
        }

        private static void UpdateRaiseGuard(object animator, int state, int remain, long now)
        {
            string weapon = CurrentWeapon();
            bool restart = !ReferenceEquals(_raiseGuardAnimator, animator) ||
                _raiseGuardState != state ||
                !string.Equals(_raiseGuardWeapon, weapon, StringComparison.OrdinalIgnoreCase) ||
                remain > _raiseGuardRemain + 80;
            if (remain <= 0 || remain > 6000)
            {
                ClearRaiseGuard();
                return;
            }
            if (restart)
            {
                _raiseGuardAnimator = animator;
                _raiseGuardWeapon = weapon;
                _raiseGuardState = state;
                System.Threading.Interlocked.Exchange(ref _raiseGuardDeadline,
                    now + remain * Stopwatch.Frequency / 1000);
            }
            _raiseGuardRemain = remain;
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

        private static void EnsureContextsType()
        {
            if (_contextsType != null) return;
            foreach (Assembly assembly in AppDomain.CurrentDomain.GetAssemblies())
            {
                _contextsType = assembly.GetType("Contexts", false);
                if (_contextsType != null) break;
            }
        }

        private static string CurrentWeapon()
        {
            EnsureContextsType();
            object contexts = Read(_contextsType, "sharedInstance");
            object player = Read(Read(contexts, "player"), "myPlayerEntity");
            return Read(Read(Read(player, "currentWeapon"), "WeaponInfo"), "Name") as string;
        }

        private static bool IsLocalAnimator(object animator)
        {
            EnsureContextsType();
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

        private static bool IsRaiseState(int state)
        {
            // TORSO_RAISE = 16, TORSO_SPECIAL_RAISE = 47, TORSO_RAISE1 = 202
            return state == 16 || state == 47 || state == 202;
        }

        private static object GetIdleView(object sampleView, int raiseState)
        {
            if (sampleView == null) return null;
            Type vt = sampleView.GetType();
            if (_standViewType != vt)
            {
                _standView = null;
                _specialStandView = null;
                _standViewType = vt;
            }
            bool special = (raiseState == 47);
            object cached = special ? _specialStandView : _standView;
            if (cached != null) return cached;
            int idleId = special ? 39 : 13; // TORSO_SPECIAL_STAND = 39, TORSO_STAND = 13
            try
            {
                object created = Activator.CreateInstance(vt, new object[] { idleId });
                if (created != null)
                {
                    if (special) _specialStandView = created;
                    else _standView = created;
                    return created;
                }
            }
            catch { }
            return null;
        }

        private static object TargetAnimationInfo(string target)
        {
            if (string.IsNullOrEmpty(target)) return null;
            try
            {
                foreach (Assembly assembly in AppDomain.CurrentDomain.GetAssemblies())
                {
                    Type factoryType = assembly.GetType("Assets.Sources.Info.WeaponAnim.WeaponAnimInfoFactory", false);
                    if (factoryType == null) continue;
                    object cache = Read(Read(factoryType, "Instance"), "_weaponInfoCache");
                    if (cache == null) return null;
                    MethodInfo get = cache.GetType().GetMethod("GetOrNull", All, null,
                        new[] { typeof(string) }, null);
                    return get == null ? null : get.Invoke(cache, new object[] { target });
                }
            }
            catch { }
            return null;
        }

        private static int AnimationValue(object info, string method, int state, int fallback)
        {
            if (info == null) return fallback;
            MethodInfo get = info.GetType().GetMethod(method, All, null, new[] { typeof(int) }, null);
            return get == null ? fallback : Convert.ToInt32(get.Invoke(info, new object[] { state }));
        }

        private static int TargetDuration(object animator, object view, object info, int state)
        {
            int duration = AnimationValue(info, "GetViewAnimDuration", state, 0);
            if (duration > 0 && duration <= 120000) return duration;
            string name = Read(view, "AnimationName") as string;
            if (animator == null || string.IsNullOrEmpty(name)) return 0;
            MethodInfo get = animator.GetType().GetMethod("GetAnimDuration", All, null,
                new[] { typeof(int), typeof(string) }, null);
            if (get == null) return 0;
            double seconds = Convert.ToDouble(get.Invoke(animator, new object[] { 0, name }));
            return seconds > 0 && seconds <= 120 ? (int)(seconds * 1000) : 0;
        }

        public static object Adjust(object animator, object view, int state, int remain,
                                    int duration, object animInfo,
                                    ref int visualTime, ref float visualSpeed)
        {
            try
            {
                long now = Stopwatch.GetTimestamp();
                if (IsRaiseState(state))
                {
                    if (!IsLocalAnimator(animator)) return view;
                    UpdateRaiseGuard(animator, state, remain, now);
                    _clock = null;
                    _attack = null;
                    RaiseClock raise = _raise;
                    bool restart = raise == null ||
                        !ReferenceEquals(raise.Animator, animator) ||
                        raise.State != state || remain > raise.LastRemain + 80;
                    if (restart)
                    {
                        string name = CurrentWeapon();
                        string target = WeaponSwapper.ResolveModelWeapon(name);
                        if (!string.IsNullOrEmpty(name) && !string.IsNullOrEmpty(target) &&
                            !string.Equals(target, name, StringComparison.OrdinalIgnoreCase) &&
                            duration >= 100 && duration <= 6000 &&
                            remain >= 100 && Math.Abs(remain - duration) > 10)
                        {
                            raise = new RaiseClock
                            {
                                Animator = animator, Weapon = name, Target = target,
                                State = state, TargetDur = duration, OrigDur = remain,
                                LastRemain = remain, Started = now, LastWeaponCheck = now,
                                IdleView = GetIdleView(view, state)
                            };
                            _raise = raise;
                            Loader.Log("[skin] 切枪动画原速方案B: " + name + "->" + target +
                                       " 动作=" + state + " 原窗口=" + remain + "ms 目标动画=" + duration +
                                       "ms (1.0x原速播完切待机)");
                        }
                        else
                        {
                            _raise = null;
                            return view;
                        }
                    }
                    if (now - raise.LastWeaponCheck >= Stopwatch.Frequency / 10)
                    {
                        if (!string.Equals(CurrentWeapon(), raise.Weapon,
                                           StringComparison.OrdinalIgnoreCase))
                        {
                            _raise = null;
                            return view;
                        }
                        raise.LastWeaponCheck = now;
                    }
                    if (remain > raise.OrigDur)
                        raise.OrigDur = remain;
                    long elapsedMs = (now - raise.Started) * 1000 / Stopwatch.Frequency;
                    if (elapsedMs < 0) elapsedMs = 0;
                    raise.LastRemain = remain;
                    if (elapsedMs < raise.TargetDur)
                    {
                        // 阶段 1：严格 1.0x 原速从第 0 帧流畅播完目标建模切枪动画
                        visualTime = (int)elapsedMs;
                        visualSpeed = 1f;
                        return view;
                    }
                    // 阶段 2：目标掏枪已播完，逻辑冷却剩余时间内平滑切入待机姿态（不改 NextAttackTimer）
                    if (raise.IdleView != null)
                    {
                        visualTime = (int)(elapsedMs - raise.TargetDur);
                        visualSpeed = 1f;
                        return raise.IdleView;
                    }
                    visualTime = raise.TargetDur;
                    visualSpeed = 0f;
                    return view;
                }
                if (!IsLocalAnimator(animator)) return view;
                ClearRaiseGuard();
                _raise = null;
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
                    if (clock != null && clock.Finished)
                    {
                        if (remain <= clock.LastRemain + 80)
                        {
                            clock.LastRemain = remain;
                            return view;
                        }
                        clock = null;
                        _clock = null;
                    }
                    if (clock == null || clock.LastState != 44)
                    {
                        string name = CurrentWeapon();
                        string target = WeaponSwapper.ResolveModelWeapon(name);
                        if (string.IsNullOrEmpty(name) || string.IsNullOrEmpty(target) ||
                            string.Equals(name, target, StringComparison.OrdinalIgnoreCase))
                        {
                            _clock = null;
                            return view;
                        }
                        object targetInfo = TargetAnimationInfo(target);
                        int targetDuration = TargetDuration(animator, view, targetInfo, 44);
                        if (targetDuration <= 0)
                        {
                            _clock = null;
                            return view;
                        }
                        clock = new Clock
                        {
                            Animator = animator, InspectView = view, Weapon = name,
                            Duration = targetDuration, LastState = 44, VisualState = 44, LastRemain = remain,
                            TargetInfo = targetInfo, Started = now, LastWeaponCheck = now
                        };
                        _clock = clock;
                        Loader.Log("[inspect] 本地检视开始: " + name + " 目标=" + target +
                                   " 时长=" + targetDuration + "ms");
                    }
                }
                else if (clock == null || (state != 13 && state != 14))
                {
                    if (clock != null) _clock = null;
                    return view;
                }

                long elapsed = (now - clock.Started) * 1000 / Stopwatch.Frequency;
                // Follow the target's declared sequence, but stop loops or invalid metadata.
                int transitions = 0;
                while (elapsed >= clock.Duration && transitions++ < 16)
                {
                    int next = AnimationValue(clock.TargetInfo, "ViewGetNextAnim", clock.VisualState, -1);
                    if (next < 0 || next == clock.VisualState || next == 13 || next == 14 || next == 39)
                    {
                        if (state == 44) { clock.Finished = true; clock.LastRemain = remain; }
                        else _clock = null;
                        return view;
                    }
                    object nextView;
                    try { nextView = Activator.CreateInstance(clock.InspectView.GetType(), new object[] { next }); }
                    catch { _clock = null; return view; }
                    int nextDuration = TargetDuration(animator, nextView, clock.TargetInfo, next);
                    if (nextDuration <= 0) { _clock = null; return view; }
                    elapsed -= clock.Duration;
                    clock.Started += clock.Duration * Stopwatch.Frequency / 1000;
                    clock.InspectView = nextView;
                    clock.VisualState = next;
                    clock.Duration = nextDuration;
                }
                if (elapsed < 0 || elapsed >= clock.Duration)
                {
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
                clock.LastRemain = remain;
                visualTime = (int)elapsed;
                return clock.InspectView;
            }
            catch
            {
                _clock = null;
                _attack = null;
                _raise = null;
                ClearRaiseGuard();
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
            LocalBuilder visualSpeed = il.DeclareLocal(typeof(float));
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
            il.Emit(OpCodes.Ldc_R4, 1f);
            il.Emit(OpCodes.Stloc, visualSpeed);
            il.Emit(OpCodes.Ldarg_1);
            il.Emit(OpCodes.Ldarg_3);
            il.Emit(OpCodes.Ldarg_3);
            il.Emit(OpCodes.Castclass, stateType);
            il.Emit(OpCodes.Callvirt, state.GetGetMethod());
            il.Emit(OpCodes.Ldarg_S, (byte)5);
            il.Emit(OpCodes.Ldloc, duration);
            il.Emit(OpCodes.Ldarg_S, (byte)6);
            il.Emit(OpCodes.Ldloca_S, visualTime);
            il.Emit(OpCodes.Ldloca_S, visualSpeed);
            il.Emit(OpCodes.Call, typeof(InspectPlayback).GetMethod("Adjust"));
            il.Emit(OpCodes.Castclass, view);
            il.Emit(OpCodes.Stloc, activeView);
            il.Emit(OpCodes.Ldloc, activeView);
            il.Emit(OpCodes.Castclass, stateType);
            il.Emit(OpCodes.Callvirt, loop.GetGetMethod());
            il.Emit(OpCodes.Stloc, isLoop);
            EmitPlay(il, 2, activeView, visualTime, visualSpeed, isLoop, name.GetGetMethod(), play);
            EmitPlay(il, 1, activeView, visualTime, visualSpeed, isLoop, name.GetGetMethod(), play);
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
                                     LocalBuilder time, LocalBuilder speed, LocalBuilder loop,
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
            il.Emit(OpCodes.Ldloc, speed);
            il.Emit(OpCodes.Ldloc, loop);
            il.Emit(OpCodes.Ldc_I4_0);
            il.Emit(OpCodes.Ldarg_S, (byte)7);
            il.Emit(OpCodes.Ldc_I4_1);
            il.Emit(OpCodes.Callvirt, play);
        }
    }
}
