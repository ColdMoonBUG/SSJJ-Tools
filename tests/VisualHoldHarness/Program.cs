using System;
using System.Collections.Generic;
using System.Reflection;
using SsjjRisk;

public sealed class WeaponDataProvider
{
    private WeaponEntity _currentWeapon;
    public void Set(WeaponEntity value) => _currentWeapon = value;
    public bool IsHoldWeapon(string requested) =>
        _currentWeapon != null && _currentWeapon.hasBasicInfo &&
        _currentWeapon.basicInfo.Data.WeaponName.Equals(requested);
}
public sealed class WeaponEntity
{
    public bool hasBasicInfo;
    public BasicInfo basicInfo = new BasicInfo();
}
public sealed class BasicInfo { public WeaponData Data = new WeaponData(); }
public sealed class WeaponData { public string WeaponName; }
public sealed class FakeGameObject { public string name => "ImageLeft"; }
public struct FakeActionValueData { public string StringValue; }
public interface IFakeBindingManager
{
    void RefreshImage(FakeGameObject obj, string path, bool active);
}
public sealed class FakeBindingManager : IFakeBindingManager
{
    public string LastPath;
    public void RefreshImage(FakeGameObject obj, string path, bool active) =>
        LastPath = path;
}
public sealed class FakeEditorUiBinding
{
    public void RefreshTexture(FakeGameObject obj, FakeActionValueData data,
                               IFakeBindingManager manager) =>
        manager.RefreshImage(obj, data.StringValue, false);
}

namespace SsjjRisk
{
    internal static class Loader { public static void Log(string value) { } }
    internal static class InspectPlayback
    {
        public static IntPtr Build(MethodInfo value) => IntPtr.Zero;
        public static void Reset() { }
        public static bool Guard;
        public static bool IsRaiseGuardActive() => Guard;
    }
    internal static class InspectProbe
    {
        public static void Start() { }
        public static void TraceScopeGetter(bool raw, bool guarded, bool visible) { }
    }
}

public static class Program
{
    private static void Check(bool condition, string message)
    {
        if (!condition) throw new Exception(message);
    }

    public static void Main()
    {
        var scopeType = typeof(WeaponSwapper);
        var pendingSince = scopeType.GetField("_scopePendingSince", BindingFlags.NonPublic | BindingFlags.Static);
        long ticks = System.Diagnostics.Stopwatch.Frequency;
        InspectPlayback.Guard = true;
        Check(!WeaponSwapper.FilterAwpCrossVisible(true), "new scope during raise starts hidden");
        pendingSince.SetValue(null, System.Diagnostics.Stopwatch.GetTimestamp() - ticks * 55 / 1000);
        Check(!WeaponSwapper.FilterAwpCrossVisible(true), "55ms switch pulse stays hidden");
        Check(!WeaponSwapper.FilterAwpCrossVisible(false), "short pulse ends hidden");
        Check(!WeaponSwapper.FilterAwpCrossVisible(true), "sustained scope starts pending");
        pendingSince.SetValue(null, System.Diagnostics.Stopwatch.GetTimestamp() - ticks * 71 / 1000);
        Check(WeaponSwapper.FilterAwpCrossVisible(true), "sustained scope shows during raise");
        Check(WeaponSwapper.FilterAwpCrossVisible(true), "admitted scope remains visible during raise");
        Check(!WeaponSwapper.FilterAwpCrossVisible(false), "scope closes immediately");
        Check(!WeaponSwapper.FilterAwpCrossVisible(true), "new guarded scope starts pending");
        InspectPlayback.Guard = false;
        Check(!WeaponSwapper.FilterAwpCrossVisible(true), "guard ending does not expose a short pulse");
        pendingSince.SetValue(null, System.Diagnostics.Stopwatch.GetTimestamp() - ticks * 71 / 1000);
        Check(WeaponSwapper.FilterAwpCrossVisible(true), "pending scope shows after filter window");
        Check(!WeaponSwapper.FilterAwpCrossVisible(false), "scope hides on false");
        Check(WeaponSwapper.FilterAwpCrossVisible(true), "normal scope visibility is immediate");
        InspectPlayback.Guard = true;
        Check(!WeaponSwapper.FilterAwpCrossVisible(true), "new raise hides previous weapon scope");
        Check(!WeaponSwapper.FilterAwpCrossVisible(false), "switch hides previous scope immediately");
        InspectPlayback.Guard = false;
        var type = typeof(WeaponSwapper);
        type.GetField("_activeModels", BindingFlags.NonPublic | BindingFlags.Static)
            .SetValue(null, new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
            {
                ["brick_knife_gold"] = "butterfly_knife"
            });
        var bundle = type.GetMethod("SoundBundle", BindingFlags.NonPublic | BindingFlags.Static);
        var asset = type.GetMethod("SoundAsset", BindingFlags.NonPublic | BindingFlags.Static);
        Check((string)bundle.Invoke(null, new object[] { "butterfly_knife", "_fire" }) ==
              "sound/weapon/butterfly_knife_fire2", "light attack bundle");
        Check(((string)asset.Invoke(null, new object[] { "butterfly_knife", "_fire" })).EndsWith(
              "butterfly_knife_fire2.mp3"), "light attack asset");
        Check((string)bundle.Invoke(null, new object[] { "butterfly_knife", "_sec_fire" }) ==
              "sound/weapon/butterfly_knife", "heavy attack bundle");
        Check(((string)asset.Invoke(null, new object[] { "butterfly_knife", "_sec_fire" })).EndsWith(
              "butterfly_knife_fire3.mp3"), "heavy attack asset");

        var build = type.GetMethod("CreateVisualHoldWeapon", BindingFlags.NonPublic | BindingFlags.Static);
        var method = typeof(WeaponDataProvider).GetMethod("IsHoldWeapon");
        var visualHold = (Func<WeaponDataProvider, string, bool>)build.Invoke(
            null, new object[] { method });
        Check(visualHold != null, "hold hook build");
        var provider = new WeaponDataProvider();
        Check(!visualHold(provider, "butterfly_knife"), "empty weapon");
        provider.Set(new WeaponEntity());
        Check(!visualHold(provider, "butterfly_knife"), "missing basic info");
        provider.Set(new WeaponEntity
        {
            hasBasicInfo = true,
            basicInfo = new BasicInfo { Data = new WeaponData { WeaponName = "brick_knife_gold" } }
        });
        Check(visualHold(provider, "butterfly_knife"), "mapped target UI");
        Check(visualHold(provider, "brick_knife_gold"), "source UI retains original match");
        Check(!visualHold(provider, "other_knife"), "unrelated weapon remains false");
        var uiBuilder = type.GetMethod("CreateButterflyUiTint",
            BindingFlags.NonPublic | BindingFlags.Static);
        var ui = (Action<FakeEditorUiBinding, FakeGameObject, FakeActionValueData,
            IFakeBindingManager>)uiBuilder.Invoke(null,
            new object[] { typeof(FakeEditorUiBinding).GetMethod("RefreshTexture") });
        Check(ui != null, "UI tint hook build");
        var binding = new FakeBindingManager();
        const string imagePath = "/weapon/butterfly_knife/ui/crossProgress1";
        ui(new FakeEditorUiBinding(), new FakeGameObject(),
           new FakeActionValueData { StringValue = imagePath }, binding);
        Check(binding.LastPath == imagePath, "UI must keep original image binding");
        Console.WriteLine("VisualHold: butterfly UI, original match, absent weapon, sound routes OK");
    }
}
