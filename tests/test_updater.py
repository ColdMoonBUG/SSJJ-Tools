# -*- coding: utf-8 -*-
import hashlib
import inspect
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "toolbox"), str(ROOT)]

import updater


class UpdaterOfflineTest(unittest.TestCase):
    def test_build_mirror_urls_contains_mirrors_and_direct_fallback(self):
        raw = "https://github.com/ColdMoonBUG/SSJJ-Tools/releases/download/v1.1.9/app.exe"
        self.assertTrue(hasattr(updater, "build_mirror_urls"), "updater 应提供 build_mirror_urls()")
        entries = updater.build_mirror_urls(raw)
        self.assertGreaterEqual(len(entries), 2)
        urls = [e[1] if isinstance(e, (tuple, list)) else str(e) for e in entries]
        self.assertIn(raw, urls)
        self.assertEqual(urls[-1], raw, "直连 GitHub 应作为最后兜底线路")
        self.assertTrue(any(u != raw and raw in u for u in urls[:-1]), "应包含加速镜像线路")

    def test_extract_sha256_from_release_notes(self):
        digest = "a" * 64
        body = "## 更新内容\n- 修复进度条\n\nSHA256: `%s`\n" % digest
        fake_rel = {
            "tag_name": "v9.9.9",
            "name": "v9.9.9",
            "body": body,
            "html_url": "https://github.com/ColdMoonBUG/SSJJ-Tools/releases/tag/v9.9.9",
            "assets": [{
                "name": "生死狙击工具箱-9.9.9.exe",
                "size": 50 * 1024 * 1024,
                "browser_download_url": "https://github.com/ColdMoonBUG/SSJJ-Tools/releases/download/v9.9.9/生死狙击工具箱-9.9.9.exe",
            }],
        }
        fn = getattr(updater, "extract_sha256", None) or getattr(updater, "_extract_sha256", None)
        if callable(fn):
            self.assertEqual(fn(body).lower(), digest)
        with patch.object(updater, "_http_get_json", return_value=fake_rel):
            info = updater.check_update()
        self.assertTrue(info.get("ok"))
        self.assertEqual(str(info.get("asset_sha256") or "").lower(), digest)

    def test_download_update_rejects_corrupt_small_and_non_pe_files(self):
        with tempfile.TemporaryDirectory() as folder:
            target_dir = Path(folder)

            def write_small(_url, dest_path, *_args, **_kw):
                Path(dest_path).write_bytes(b"MZ" + b"0" * 100)

            with patch.object(updater, "_writable_target_dir", return_value=target_dir), \
                 patch.object(updater, "probe_mirrors", side_effect=lambda c, **_kw: list(c)), \
                 patch.object(updater, "_download_via_curl", side_effect=write_small):
                ok, msg, saved, bat = updater.download_update(
                    "https://github.com/ColdMoonBUG/SSJJ-Tools/releases/download/v1.1.9/test.exe",
                    "test.exe",
                    expected_size=2 * 1024 * 1024,
                )
            self.assertFalse(ok)
            self.assertEqual(saved, "")
            self.assertEqual(bat, "")
            self.assertFalse((target_dir / "test.exe.downloading").exists())

            def write_non_pe(_url, dest_path, *_args, **_kw):
                Path(dest_path).write_bytes(b"<html>error</html>" + b"x" * (1024 * 1024 + 16))

            with patch.object(updater, "_writable_target_dir", return_value=target_dir), \
                 patch.object(updater, "probe_mirrors", side_effect=lambda c, **_kw: list(c)), \
                 patch.object(updater, "_download_via_curl", side_effect=write_non_pe):
                ok, msg, _saved, _bat = updater.download_update(
                    "https://github.com/ColdMoonBUG/SSJJ-Tools/releases/download/v1.1.9/test.exe",
                    "test.exe",
                    expected_size=1024 * 1024 + 34,
                )
            self.assertFalse(ok)
            self.assertFalse((target_dir / "test.exe.downloading").exists())

    def test_download_update_rejects_sha256_mismatch(self):
        payload = b"MZ" + b"\x00" * (1024 * 1024 + 64)
        wrong_hash = "0" * 64
        self.assertNotEqual(hashlib.sha256(payload).hexdigest(), wrong_hash)
        params = inspect.signature(updater.download_update).parameters
        self.assertIn("expected_sha256", params, "download_update 应支持 expected_sha256 校验")

        with tempfile.TemporaryDirectory() as folder:
            target_dir = Path(folder)

            def write_pe(_url, dest_path, *_args, **_kw):
                Path(dest_path).write_bytes(payload)

            with patch.object(updater, "_writable_target_dir", return_value=target_dir), \
                 patch.object(updater, "probe_mirrors", side_effect=lambda c, **_kw: list(c)), \
                 patch.object(updater, "_download_via_curl", side_effect=write_pe):
                ok, msg, saved, _bat = updater.download_update(
                    "https://github.com/ColdMoonBUG/SSJJ-Tools/releases/download/v1.1.9/test.exe",
                    "test.exe",
                    expected_size=len(payload),
                    expected_sha256=wrong_hash,
                )
            self.assertFalse(ok)
            self.assertEqual(saved, "")
            self.assertTrue("sha256" in msg.lower() or "校验" in msg)
            self.assertFalse((target_dir / "test.exe").exists())

    def test_download_update_honors_cancel_event(self):
        params = inspect.signature(updater.download_update).parameters
        self.assertIn("cancel_event", params, "download_update 应支持 cancel_event 取消")
        ev = threading.Event()
        ev.set()

        with tempfile.TemporaryDirectory() as folder:
            target_dir = Path(folder)
            with patch.object(updater, "_writable_target_dir", return_value=target_dir):
                ok, msg, saved, _bat = updater.download_update(
                    "https://github.com/ColdMoonBUG/SSJJ-Tools/releases/download/v1.1.9/test.exe",
                    "test.exe",
                    expected_size=2 * 1024 * 1024,
                    cancel_event=ev,
                )
            self.assertFalse(ok)
            self.assertEqual(saved, "")
            self.assertIn("取消", msg)

    def test_launch_update_script_strips_pyi_env(self):
        with tempfile.TemporaryDirectory() as folder:
            ps1 = Path(folder) / "ssjj_update_test.ps1"
            ps1.write_text("# test\n", encoding="utf-8-sig")
            captured = {}

            def fake_popen(cmd, **kwargs):
                captured["cmd"] = cmd
                captured["env"] = kwargs.get("env")
                return object()

            fake_env = {
                "PATH": "C:\\Windows\\System32",
                "_PYI_APPLICATION_HOME_DIR": "C:\\Temp\\_MEI123",
                "_PYI_ARCHIVE_FILE": "C:\\Temp\\old.exe",
                "_MEIPASS2": "C:\\Temp\\_MEI123",
                "TEMP": "C:\\Temp",
            }
            with patch.dict("os.environ", fake_env, clear=True), \
                 patch("subprocess.Popen", side_effect=fake_popen):
                ok = updater.launch_update_script(str(ps1))

            self.assertTrue(ok)
            env = captured.get("env")
            self.assertIsInstance(env, dict, "launch_update_script 应显式传入清洗后的 env")
            for k in env:
                self.assertFalse(k.upper().startswith("_PYI_"), "不应残留 _PYI_* 环境变量: %s" % k)
                self.assertFalse(k.upper().startswith("_MEIPASS"), "不应残留 _MEIPASS* 环境变量: %s" % k)
            self.assertEqual(env.get("PATH"), "C:\\Windows\\System32")

    def test_write_replace_script_has_dual_pid_wait_log_and_explorer_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            staged = Path(folder) / "app-new.exe"
            final = Path(folder) / "app-new.exe"
            ps1_path = updater._write_replace_script(staged, final, same_as_running=False)
            try:
                content = Path(ps1_path).read_text(encoding="utf-8-sig")
            finally:
                Path(ps1_path).unlink(missing_ok=True)
            self.assertIn("update_launch.log", content)
            self.assertIn("explorer", content.lower())
            self.assertTrue(
                ("parentPid" in content or "ppid" in content.lower() or "pids" in content.lower() or "Wait" in content),
                "替换脚本应包含双 PID 等待逻辑",
            )

    def test_quick_login_launch_official_prefers_launcher_and_falls_back_to_microclient(self):
        import quick_login
        with tempfile.TemporaryDirectory() as folder:
            gdir = Path(folder)
            mc_dir = gdir / "MicroClient64"
            mc_dir.mkdir(parents=True)
            mc_exe = mc_dir / "MicroClient.exe"
            mc_exe.write_bytes(b"MZ")
            wd_exe = gdir / "WDlauncher.exe"
            wd_exe.write_bytes(b"MZ")
            calls = []

            def fake_shell_exec(hwnd, op, file_path, params, work_dir, show):
                calls.append((op, file_path, params, work_dir))
                return 42

            with patch.object(quick_login.P, "GAME_DIR", str(gdir)), \
                 patch.object(quick_login, "active_official_lobby", return_value=False), \
                 patch.object(quick_login.ctypes.windll.shell32, "ShellExecuteW", side_effect=fake_shell_exec), \
                 patch.object(quick_login, "_log_launch"), \
                 patch.object(quick_login, "_watch_launch"):
                ok1, msg1 = quick_login.launch_official()
                wd_exe.unlink()
                ok2, msg2 = quick_login.launch_official()

            self.assertTrue(ok1, msg1)
            self.assertTrue(ok2, msg2)
            self.assertEqual(len(calls), 2)
            self.assertTrue(calls[0][1].lower().endswith("wdlauncher.exe"))
            self.assertIsNone(calls[0][2])
            self.assertEqual(Path(calls[0][3]).resolve(), gdir.resolve())
            self.assertTrue(calls[1][1].lower().endswith("microclient.exe"))
            self.assertIsNone(calls[1][2])
            self.assertEqual(Path(calls[1][3]).resolve(), gdir.resolve())


if __name__ == "__main__":
    unittest.main()
