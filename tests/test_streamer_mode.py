# -*- coding: utf-8 -*-
"""直播模式离线单元测试（不污染真实 %LOCALAPPDATA%）。"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLBOX_DIR = REPO_ROOT / "toolbox"
if str(TOOLBOX_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLBOX_DIR))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import backend
import lobby_overlay


class StreamerConfigTest(unittest.TestCase):
    def test_load_defaults_and_atomic_save(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"LOCALAPPDATA": tmpdir}):
                cfg_path = backend.streamer_config_path()
                self.assertEqual(cfg_path, Path(tmpdir) / "ssjj-toolbox" / "streamer_mode.json")
                self.assertFalse(cfg_path.exists())

                # 默认值
                defaults = backend.streamer_load_config()
                self.assertFalse(defaults["enabled"])
                self.assertEqual(defaults["fake_name"], "神秘玩家")
                self.assertTrue(defaults["lobby_overlay"])

                # 原子写入 + 再次读取
                ok, saved = backend.streamer_save_config(True, "主播小号", False)
                self.assertTrue(ok)
                self.assertEqual(saved, {
                    "enabled": True,
                    "fake_name": "主播小号",
                    "lobby_overlay": False,
                })
                self.assertTrue(cfg_path.is_file())
                # 临时文件应已被 os.replace 替换，无残留
                self.assertFalse(cfg_path.with_suffix(".json.tmp").exists())
                # 确认 UTF-8 无 BOM 且非 ASCII 直写
                raw_bytes = cfg_path.read_bytes()
                self.assertFalse(raw_bytes.startswith(b"\xef\xbb\xbf"))
                raw_json = json.loads(raw_bytes.decode("utf-8"))
                self.assertEqual(raw_json["fake_name"], "主播小号")

                reloaded = backend.streamer_load_config()
                self.assertEqual(reloaded, saved)

                # 空昵称回落默认值
                ok2, saved2 = backend.streamer_save_config(False, "   ", True)
                self.assertTrue(ok2)
                self.assertEqual(saved2["fake_name"], "神秘玩家")
                self.assertFalse(saved2["enabled"])
                self.assertTrue(saved2["lobby_overlay"])

                # 损坏 JSON 安全回落默认值
                cfg_path.write_text("{broken json", encoding="utf-8")
                corrupted = backend.streamer_load_config()
                self.assertEqual(corrupted, defaults)


class LobbyOverlayMathTest(unittest.TestCase):
    def test_compute_overlay_rect_base_and_scaled(self):
        # 基准 1024x650 窗口
        x, y, w, h = lobby_overlay.compute_overlay_rect((100, 200, 1024, 650))
        self.assertEqual((x, y, w, h), (140, 204, 248, 26))

        # 2x 放大窗口（2048x1300）
        x2, y2, w2, h2 = lobby_overlay.compute_overlay_rect((0, 0, 2048, 1300))
        self.assertEqual((x2, y2, w2, h2), (80, 8, 496, 52))

        # 极小窗口保护下限（宽 >= 140，高 >= 22）
        _x3, _y3, w3, h3 = lobby_overlay.compute_overlay_rect((10, 20, 320, 200))
        self.assertGreaterEqual(w3, 140)
        self.assertGreaterEqual(h3, 22)


class OneclickStreamerUiTest(unittest.TestCase):
    def test_oneclick_page_contains_streamer_controls(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        from ui.pages.oneclick import OneclickPage

        app = QApplication.instance() or QApplication([])

        class _DummyApp:
            def __init__(self):
                self.synced = []

            def sync_streamer_overlay(self, cfg=None):
                self.synced.append(cfg)

            def run(self, fn, on_done=None, quiet=False):
                return None

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"LOCALAPPDATA": tmpdir}):
                dummy = _DummyApp()
                with patch.object(OneclickPage, "refresh", return_value=None), \
                     patch.object(OneclickPage, "_load_profile", return_value=None):
                    page = OneclickPage(dummy)

                # 验证 4 个核心控件存在
                self.assertTrue(hasattr(page, "chk_streamer_mode"))
                self.assertTrue(hasattr(page, "ed_streamer_name"))
                self.assertTrue(hasattr(page, "chk_streamer_lobby"))
                self.assertTrue(hasattr(page, "btn_streamer_apply"))

                # 回填配置并触发保存联动
                page._sync_streamer_ui({
                    "enabled": False,
                    "fake_name": "测试昵称",
                    "lobby_overlay": True,
                })
                self.assertEqual(page.ed_streamer_name.text(), "测试昵称")
                self.assertEqual(page.badge_streamer.text(), "未开启")

                page.ed_streamer_name.setText("实时主播ID")
                with patch.object(page, "_streamer_lobby_experiment") as prepare_lobby:
                    page.chk_streamer_mode.setChecked(True)
                    prepare_lobby.assert_called_once_with(True)
                self.assertEqual(page.badge_streamer.text(), "已开启")
                self.assertTrue(dummy.synced)
                self.assertTrue(dummy.synced[-1]["enabled"])
                self.assertEqual(dummy.synced[-1]["fake_name"], "实时主播ID")

                saved = backend.streamer_load_config()
                self.assertTrue(saved["enabled"])
                self.assertEqual(saved["fake_name"], "实时主播ID")
                page.deleteLater()
                app.processEvents()


class StreamerPayloadSourceTest(unittest.TestCase):
    def test_streamer_mode_cs_key_symbols(self):
        cs_path = REPO_ROOT / "risk" / "payload" / "StreamerMode.cs"
        self.assertTrue(cs_path.is_file(), "缺少 C# 载荷源码: %s" % cs_path)
        src = cs_path.read_text(encoding="utf-8")
        for symbol in (
            "PostInitBasicInfo",
            "AllKillInfoDataHandler",
            "ParseChatXml",
            "Restore",
        ):
            self.assertIn(symbol, src, "StreamerMode.cs 缺少关键符号: %s" % symbol)


if __name__ == "__main__":
    unittest.main()
