using System;
using System.Reflection;
using System.Threading;
using System.Diagnostics;
using SsjjRisk;

public class Contexts
{
    public static Contexts sharedInstance = new Contexts();
    public PlayerContext player = new PlayerContext();
}
public class PlayerContext { public Player myPlayerEntity = new Player(); }
public class Player
{
    public CurrentWeapon currentWeapon = new CurrentWeapon();
    public FirstPerson firstPersonUnityObjects = new FirstPerson();
}
public class CurrentWeapon { public Weapon WeaponInfo = new Weapon(); }
public class Weapon { public string Name = "brick_knife_gold"; }
public class FirstPerson { public IFakeAnimator WeaponAnimator; }

namespace Assets.Sources.Components.Interface.Info.AnimState
{
    public interface IAnimStateInfo
    {
        int StateId { get; }
        bool IsLoop { get; }
    }
}
public interface IFakeView { string AnimationName { get; } }
public class View : IFakeView, Assets.Sources.Components.Interface.Info.AnimState.IAnimStateInfo
{
    public View() { }
    public View(int state)
    {
        StateId = state;
        AnimationName = state == 170 ? "light-second" : "other";
    }
    public int StateId { get; set; }
    public bool IsLoop { get; set; }
    public string AnimationName { get; set; }
}
public interface IFakeWeaponInfo { int GetViewAnimDuration(int state); }
public class AnimInfo : IFakeWeaponInfo
{
    public int GetViewAnimDuration(int state) =>
        state == 44 ? 6033 : state == 20 ? 1600 : state == 31 ? 900 :
        state == 170 ? 1433 : state == 29 ? 0 : 800;
}
public interface IFakeAnimator
{
    float GetAnimDuration(int layer, string name);
    void PlayAtFixedTime(int layer, string name, float time, float speed,
                         bool loop, bool reverse, float transition, bool visible);
}
public class Animator : IFakeAnimator
{
    public string Name;
    public float Time;
    public float GetAnimDuration(int layer, string name) => 6.033f;
    public void PlayAtFixedTime(int layer, string name, float time, float speed,
                                bool loop, bool reverse, float transition, bool visible)
    {
        Name = name;
        Time = time;
    }
}
public class ViewSystem
{
    private void PlayHandAnimation(IFakeAnimator weapon, IFakeAnimator hand,
                                   IFakeView view, ref int currentTime,
                                   int remainTime, IFakeWeaponInfo info, float transition) { }
    public static MethodInfo Method() => typeof(ViewSystem).GetMethod("PlayHandAnimation",
        BindingFlags.Instance | BindingFlags.NonPublic);
}
namespace SsjjRisk
{
    internal static class Loader { public static void Log(string message) { } }
    internal static class WeaponSwapper
    {
        public static string ResolveModelWeapon(string name) =>
            name == "brick_knife_gold" ? "butterfly_knife" : name;
    }
}
public static class Program
{
    private static void Check(bool ok, string message)
    {
        if (!ok) throw new Exception(message);
    }

    public static void Main()
    {
        Delegate replacement = InspectPlayback.Create(ViewSystem.Method());
        Check(replacement != null, "method signature mismatch");
        var hand = new Animator();
        var weapon = new Animator();
        Contexts.sharedInstance.player.myPlayerEntity.firstPersonUnityObjects.WeaponAnimator = weapon;
        var info = new AnimInfo();
        var inspect = new View { StateId = 44, AnimationName = "showgun" };
        var idle = new View { StateId = 13, AnimationName = "idle", IsLoop = true };
        var attack = new View { StateId = 20, AnimationName = "attack" };
        int time = 0;
        void Frame(View view, int remaining)
        {
            object[] args = { new ViewSystem(), weapon, hand, view, time, remaining, info, 0.25f };
            replacement.DynamicInvoke(args);
            time = (int)args[4];
        }

        Frame(inspect, 2988);
        Check(hand.Name == "showgun" && hand.Time < 0.2f && time == 3045,
              "inspect must begin visually at zero while keeping game time");
        Thread.Sleep(35);
        Frame(idle, 800);
        Check(hand.Name == "showgun" && hand.Time > 0.02f && hand.Time < 0.3f,
              "idle snapshot must keep visual inspect");
        Frame(attack, 700);
        Check(hand.Name == "attack", "attack must interrupt visual inspect");
        Frame(idle, 700);
        Check(hand.Name == "idle", "inspect must stay cancelled");
        Frame(inspect, 2900);
        Check(hand.Name == "showgun" && hand.Time < 0.2f, "new V press must restart");
        Contexts.sharedInstance.player.myPlayerEntity.currentWeapon.WeaponInfo.Name = "other_knife";
        Thread.Sleep(110);
        Frame(idle, 500);
        Check(hand.Name == "idle", "weapon switch must interrupt visual inspect");
        Contexts.sharedInstance.player.myPlayerEntity.currentWeapon.WeaponInfo.Name = "brick_knife_gold";
        Frame(inspect, 2900);
        var clockField = typeof(InspectPlayback).GetField("_clock",
            BindingFlags.Static | BindingFlags.NonPublic);
        object clock = clockField.GetValue(null);
        clock.GetType().GetField("Started", BindingFlags.Instance | BindingFlags.NonPublic)
             .SetValue(clock, Stopwatch.GetTimestamp() - Stopwatch.Frequency * 7);
        Frame(idle, 500);
        Check(hand.Name == "idle", "finished visual inspect must return to game idle");
        Frame(attack, 1200);
        Check(hand.Name == "attack" && hand.Time < 0.1f && time == 400,
              "heavy attack must begin visually at zero while preserving game time");
        Thread.Sleep(40);
        Frame(attack, 1160);
        Check(hand.Time > 0.03f && hand.Time < 0.3f, "heavy attack must progress");
        Frame(idle, 500);
        Check(hand.Name == "idle", "attack cannot cover subsequent idle");
        var light = new View { StateId = 31, AnimationName = "light" };
        Frame(light, 550);
        Check(hand.Name == "light" && hand.Time < 0.1f, "light attack must start at zero");
        Frame(light, 250);
        Frame(light, 550);
        Check(hand.Time < 0.1f, "new attack of same state must restart on timer jump");
        var second = new View { StateId = 29, AnimationName = "missing-light-second" };
        Frame(second, 620);
        Check(hand.Name == "light-second" && hand.Time < 0.1f,
              "alternate light must use butterfly second stage 170");
        Thread.Sleep(40);
        Frame(second, 580);
        Check(hand.Name == "light-second" && hand.Time > 0.04f,
              "second stage must progress within source attack window");
        Frame(light, 550);
        Check(hand.Name == "light" && hand.Time < 0.1f,
              "alternating back to first stage must restart");
        Frame(second, 600);
        Check(hand.Name == "light-second" && hand.Time < 0.1f,
              "continuous light combo must alternate stage 31 / 170");
        InspectPlayback.Reset();
        Frame(idle, 500);
        Check(hand.Name == "idle", "reset must restore game animation");
        Console.WriteLine("InspectPlayback: inspect clock, heavy/light rescale, two-stage combo, attack/switch cancellation, reset OK");
    }
}
