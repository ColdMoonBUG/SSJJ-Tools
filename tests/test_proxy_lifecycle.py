import hashlib
import inspect
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "toolbox"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from risk import guard, proxy


def _is_truthy_result(res):
    if isinstance(res, tuple):
        return bool(res[0])
    return bool(res)


class ProxyLifecycleTest(unittest.TestCase):
    def _make_battle_dir(self, root: Path, name: str) -> Path:
        d = root / "battle" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SSJJ_BattleClient_Unity.exe").write_bytes(b"unity-stub")
        return d

    def test_hide_in_dir_renames_proxy_dll_and_handles_slot_fallback(self):
        dll_name = getattr(proxy, "NAME", "hid.dll")
        alt_name = getattr(proxy, "RENAMED", ".hid.dat")
        alt_prefix = alt_name[:-4] if alt_name.endswith(".dat") else ".hid"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            b3 = self._make_battle_dir(root, "3_64")
            src = root / ("src_" + dll_name)
            payload = b"proxy-dll-payload-v120"
            src.write_bytes(payload)
            (b3 / dll_name).write_bytes(payload)

            with patch.object(proxy, "dll_source", return_value=str(src)):
                res = proxy.hide_in_dir(str(b3))
                self.assertTrue(_is_truthy_result(res), res)
                self.assertFalse((b3 / dll_name).exists())
                self.assertEqual((b3 / alt_name).read_bytes(), payload)

                # Slot fallback: existing alt occupies slot 0 while a new proxy dll is hidden.
                new_payload = b"proxy-dll-payload-v120-second"
                (b3 / dll_name).write_bytes(new_payload)
                res2 = proxy.hide_in_dir(str(b3))
                self.assertTrue(_is_truthy_result(res2), res2)
                self.assertFalse((b3 / dll_name).exists())
                alt_contents = [
                    p.read_bytes()
                    for p in b3.iterdir()
                    if p.is_file() and p.name.startswith(alt_prefix) and p.name.endswith(".dat")
                ]
                self.assertIn(new_payload, alt_contents)

                # Idempotent when proxy dll is already absent.
                res3 = proxy.hide_in_dir(str(b3))
                self.assertTrue(_is_truthy_result(res3), res3)

    def test_heal_skips_active_dir_and_restores_after_exit(self):
        dll_name = getattr(proxy, "NAME", "hid.dll")
        alt_name = getattr(proxy, "RENAMED", ".hid.dat")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            b3 = self._make_battle_dir(root, "3_64")
            b5 = self._make_battle_dir(root, "5_64")
            src = root / ("src_" + dll_name)
            payload = b"proxy-dll-payload-v120"
            src.write_bytes(payload)
            # Both directories start hidden (.hid.dat / .ver.dat) as in an ongoing/previous match.
            (b3 / alt_name).write_bytes(payload)
            (b5 / alt_name).write_bytes(payload)
            state_file = root / "proxy_state.json"
            log_file = root / "proxy.log"
            guard_log = root / "risk_mode.log"

            # 1) Direct proxy.heal check if skip/exclude/active parameter is supported.
            sig = inspect.signature(proxy.heal)
            skip_kw = next(
                (k for k in ("skip_dirs", "exclude", "exclude_dirs", "active_dirs") if k in sig.parameters),
                None,
            )
            if skip_kw is not None:
                with patch.object(proxy, "battle_dirs", return_value=[str(b3), str(b5)]), \
                     patch.object(proxy, "dll_source", return_value=str(src)), \
                     patch.object(proxy, "STATE", str(state_file)), \
                     patch.object(proxy, "LOG", str(log_file)):
                    proxy.heal(**{skip_kw: {str(b3)}})
                self.assertFalse((b3 / dll_name).exists())
                self.assertTrue((b3 / alt_name).exists())
                self.assertEqual((b5 / dll_name).read_bytes(), payload)
                # Re-hide b5 before guard._watch lifecycle check below.
                (b5 / dll_name).replace(b5 / alt_name)

            # 2) guard._watch() lifecycle check: runtime active process in b3, then exit.
            exe_3 = str(b3 / "SSJJ_BattleClient_Unity.exe")
            pid = 999123
            polls = [0]

            def fake_settings(key, default=None):
                return {
                    "inject.targets": ["SSJJ_BattleClient_Unity.exe"],
                    "inject.enabled": True,
                    "inject.method": "proxy",
                }.get(key, default)

            def fake_wait(_t):
                polls[0] += 1
                guard._stop.set()
                return True

            active_patches = [
                patch.object(proxy, "battle_dirs", return_value=[str(b3), str(b5)]),
                patch.object(proxy, "dll_source", return_value=str(src)),
                patch.object(proxy, "STATE", str(state_file)),
                patch.object(proxy, "LOG", str(log_file)),
                patch.object(guard, "LOG_PATH", str(guard_log)),
                patch.object(guard, "log"),
                patch.object(guard.config, "get", side_effect=fake_settings),
                patch.object(guard._stop, "wait", side_effect=fake_wait),
            ]
            # Provide active process path via whichever helper guard/injector/proxy exposes.
            for mod, attr, val in (
                (guard, "_active_battle_dirs", lambda *a, **kw: {str(b3)}),
                (guard, "active_battle_dirs", lambda *a, **kw: {str(b3)}),
                (proxy, "active_battle_dirs", lambda *a, **kw: {str(b3)}),
                (proxy, "running_battle_dirs", lambda *a, **kw: {str(b3)}),
                (guard.injector, "process_exe_path", lambda p: exe_3 if p == pid else None),
            ):
                if hasattr(mod, attr):
                    active_patches.append(patch.object(mod, attr, side_effect=val))

            orig_done = set(guard._done_pids)
            try:
                # Phase A: battle client PID alive in battle/3_64.
                guard._stop.clear()
                guard._done_pids.clear()
                proc_list = [{"pid": pid, "exe": "SSJJ_BattleClient_Unity.exe", "path": exe_3}]
                with patch.object(guard.injector, "list_processes", return_value=proc_list):
                    for cm in active_patches:
                        cm.__enter__()
                    try:
                        guard._watch()
                    finally:
                        for cm in reversed(active_patches):
                            cm.__exit__(None, None, None)

                # Active directory 3_64 MUST NOT receive proxy dll while game process is running.
                self.assertFalse((b3 / dll_name).exists())
                self.assertTrue((b3 / alt_name).exists())

                # Phase B: battle client exited -> next poll restores 3_64/<dll_name>.
                exit_patches = [
                    patch.object(proxy, "battle_dirs", return_value=[str(b3), str(b5)]),
                    patch.object(proxy, "dll_source", return_value=str(src)),
                    patch.object(proxy, "STATE", str(state_file)),
                    patch.object(proxy, "LOG", str(log_file)),
                    patch.object(guard, "LOG_PATH", str(guard_log)),
                    patch.object(guard, "log"),
                    patch.object(guard.config, "get", side_effect=fake_settings),
                    patch.object(guard._stop, "wait", side_effect=fake_wait),
                    patch.object(guard.injector, "list_processes", return_value=[]),
                ]
                for mod, attr in (
                    (guard, "_active_battle_dirs"),
                    (guard, "active_battle_dirs"),
                    (proxy, "active_battle_dirs"),
                    (proxy, "running_battle_dirs"),
                    (guard.injector, "process_exe_path"),
                ):
                    if hasattr(mod, attr):
                        val = (lambda *a, **kw: set()) if "dirs" in attr else (lambda p: None)
                        exit_patches.append(patch.object(mod, attr, side_effect=val))

                guard._stop.clear()
                for cm in exit_patches:
                    cm.__enter__()
                try:
                    guard._watch()
                finally:
                    for cm in reversed(exit_patches):
                        cm.__exit__(None, None, None)

                self.assertEqual((b3 / dll_name).read_bytes(), payload)
                self.assertFalse((b3 / alt_name).exists())
                self.assertEqual((b5 / dll_name).read_bytes(), payload)
            finally:
                guard._stop.clear()
                guard._done_pids.clear()
                guard._done_pids.update(orig_done)

    def test_uninstall_cleans_bak_files_and_keeps_unknown_dll(self):
        dll_name = getattr(proxy, "NAME", "hid.dll")
        alt_name = getattr(proxy, "RENAMED", ".hid.dat")
        alt_prefix = alt_name[:-4] if alt_name.endswith(".dat") else ".hid"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            b3 = self._make_battle_dir(root, "3_64")
            b5 = self._make_battle_dir(root, "5_64")
            src = root / ("src_" + dll_name)
            payload = b"proxy-dll-payload-v120"
            src.write_bytes(payload)
            src_md5 = hashlib.md5(payload).hexdigest()

            (b3 / dll_name).write_bytes(payload)
            (b3 / alt_name).write_bytes(payload)
            (b3 / (alt_prefix + "1.dat")).write_bytes(payload)
            (b3 / (dll_name + ".bak-20261127-100000")).write_bytes(b"old-bak-1")
            (b3 / (dll_name + ".bak-legacy")).write_bytes(b"old-bak-2")

            # Legacy version.dll residuals should also be cleaned when migrated to hid.dll.
            if dll_name.lower() == "hid.dll":
                (b3 / ".ver.dat").write_bytes(payload)
                (b3 / "version.dll.bak-20261125-130049").write_bytes(b"old-ver-bak")

            # Unknown third-party dll in 5_64 should stay; backup .bak-* should still be cleaned.
            (b5 / dll_name).write_bytes(b"unknown-third-party-dll")
            (b5 / (dll_name + ".bak-20261127-100000")).write_bytes(b"old-bak-3")

            state = {"md5": src_md5, "installed": {str(b3): {"md5": src_md5}}}
            state_file = root / "proxy_state.json"
            state_file.write_text(__import__("json").dumps(state), encoding="utf-8")

            with patch.object(proxy, "battle_dirs", return_value=[str(b3), str(b5)]), \
                 patch.object(proxy, "dll_source", return_value=str(src)), \
                 patch.object(proxy, "STATE", str(state_file)):
                ok, msg = proxy.uninstall()

            self.assertTrue(ok, msg)
            self.assertFalse((b3 / dll_name).exists())
            self.assertFalse((b3 / alt_name).exists())
            self.assertFalse((b3 / (alt_prefix + "1.dat")).exists())
            self.assertEqual(list(b3.glob(dll_name + ".bak-*")), [])
            self.assertEqual(list(b5.glob(dll_name + ".bak-*")), [])
            if dll_name.lower() == "hid.dll":
                self.assertFalse((b3 / ".ver.dat").exists())
                self.assertEqual(list(b3.glob("version.dll.bak-*")), [])
            self.assertEqual((b5 / dll_name).read_bytes(), b"unknown-third-party-dll")

    def test_uninstall_fallback_when_proxy_state_missing(self):
        dll_name = getattr(proxy, "NAME", "hid.dll")
        alt_name = getattr(proxy, "RENAMED", ".hid.dat")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            b3 = self._make_battle_dir(root, "3_64")
            b5 = self._make_battle_dir(root, "5_64")
            src = root / ("src_" + dll_name)
            payload = b"proxy-dll-payload-v120"
            src.write_bytes(payload)

            # Even without proxy_state.json, uninstall() removes matching proxy DLLs, alts, and .bak-*.
            missing_state = root / "missing" / "proxy_state.json"
            (b3 / dll_name).write_bytes(payload)
            (b3 / alt_name).write_bytes(payload)
            (b3 / (dll_name + ".bak-20261127-100000")).write_bytes(b"stale-bak")
            (b5 / dll_name).write_bytes(b"unrelated-third-party")

            with patch.object(proxy, "battle_dirs", return_value=[str(b3), str(b5)]), \
                 patch.object(proxy, "dll_source", return_value=str(src)), \
                 patch.object(proxy, "STATE", str(missing_state)):
                ok, msg = proxy.uninstall()

            self.assertTrue(ok, msg)
            self.assertFalse((b3 / dll_name).exists())
            self.assertFalse((b3 / alt_name).exists())
            self.assertEqual(list(b3.glob(dll_name + ".bak-*")), [])
            self.assertEqual((b5 / dll_name).read_bytes(), b"unrelated-third-party")


if __name__ == "__main__":
    unittest.main()
