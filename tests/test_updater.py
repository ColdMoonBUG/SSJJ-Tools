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


if __name__ == "__main__":
    unittest.main()
