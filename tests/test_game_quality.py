import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "toolbox"), str(ROOT)]

import backend


class FakeKey:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class GameQualityTest(unittest.TestCase):
    def setUp(self):
        self.key = FakeKey()
        self.values = {
            name + "_h123": (value, backend.winreg.REG_DWORD)
            for name, value in backend.GAME_QUALITY_PRESETS["极高"].items()
        }
        self.values["FrameLimit_h123"] = (144, backend.winreg.REG_DWORD)
        self.values["VSync_h123"] = (0, backend.winreg.REG_DWORD)
        self.values["QualityLevelVersion_h123"] = (999, backend.winreg.REG_DWORD)
        self.patches = [
            patch.object(backend, "_pref_subs", return_value=["Battle"]),
            patch.object(backend, "battle_running", return_value=False),
            patch.object(backend.winreg, "OpenKey", return_value=self.key),
            patch.object(backend, "_enum_names", side_effect=lambda _key: list(self.values)),
            patch.object(backend.winreg, "QueryValueEx",
                         side_effect=lambda _key, name: self.values[name]),
            patch.object(backend.winreg, "SetValueEx", side_effect=self._write),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.writes = []

    def _write(self, _key, name, _reserved, typ, value):
        self.writes.append((name, value))
        self.values[name] = (value, typ)

    def test_three_presets_round_trip_and_preserve_fps(self):
        for label, expected in backend.GAME_QUALITY_PRESETS.items():
            ok, msg = backend.game_quality_set(label)
            self.assertTrue(ok, msg)
            state = backend.game_quality_state()
            self.assertEqual(state["preset"], label)
            self.assertEqual(
                {field: self.values[field + "_h123"][0] for field in expected},
                expected,
            )
        self.assertEqual(self.values["FrameLimit_h123"][0], 144)
        self.assertEqual(self.values["VSync_h123"][0], 0)
        self.assertEqual(self.values["QualityLevelVersion_h123"][0], 999)

    def test_missing_and_wrong_type_rejected_before_write(self):
        del self.values["TextureQuality_h123"]
        self.assertIn("TextureQuality", backend.game_quality_state()["missing"])
        ok, msg = backend.game_quality_set("最低")
        self.assertFalse(ok)
        self.assertIn("TextureQuality", msg)
        self.assertEqual(self.writes, [])

        self.values["TextureQuality_h123"] = (0, backend.winreg.REG_SZ)
        self.assertIn("类型不符", backend.game_quality_state()["error"])
        ok, msg = backend.game_quality_set("最低")
        self.assertFalse(ok)
        self.assertIn("类型不符", msg)
        self.assertEqual(self.writes, [])

    def test_running_and_invalid_preset_rejected(self):
        with patch.object(backend, "battle_running", return_value=True):
            self.assertFalse(backend.game_quality_set("最低")[0])
        self.assertFalse(backend.game_quality_set("不存在")[0])
        self.assertEqual(self.writes, [])

    def test_write_failure_restores_old_values(self):
        original = dict(self.values)
        calls = 0

        def fail_once(key, name, reserved, typ, value):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError("test failure")
            self._write(key, name, reserved, typ, value)

        with patch.object(backend.winreg, "SetValueEx", side_effect=fail_once):
            ok, msg = backend.game_quality_set("最低")
        self.assertFalse(ok)
        self.assertIn("已恢复原值", msg)
        self.assertEqual(self.values, original)


class GameQualityUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_page_quality_state(self):
        from ui.pages.fps import FpsPage
        page = FpsPage(None)
        state = {"found": True, "missing": [], "error": None,
                 "running": False, "preset": "最低"}
        page._apply_quality_state(state)
        self.assertEqual(page.cmb_quality.currentData(), "最低")
        self.assertTrue(page.btn_quality.isEnabled())
        page._apply_quality_state({**state, "running": True})
        self.assertFalse(page.btn_quality.isEnabled())
        page._apply_quality_state({**state, "running": False,
                                   "missing": ["TextureQuality"]})
        self.assertFalse(page.btn_quality.isEnabled())
        self.assertIn("TextureQuality", page.lb_gquality.text())


if __name__ == "__main__":
    unittest.main()
