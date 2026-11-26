import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "toolbox"), str(ROOT)]

from PySide6.QtWidgets import QApplication
import backend
import updater
from ui.pages.tools import ToolsPage


class SettingsPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_settings_export_uses_selected_destination(self):
        with tempfile.TemporaryDirectory() as folder:
            dest = str(Path(folder) / "chosen.txt")
            with patch("ui.pages.tools.choose_diagnostic_path", return_value=dest), \
                 patch("backend.export_diagnostics") as export:
                def write_file(path):
                    Path(path).write_text("diagnostic data", encoding="utf-8")
                    return path
                export.side_effect = write_file
                page = ToolsPage(None)
                page.btn_export.click()
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and (
                        not export.called or not page.btn_export.isEnabled()):
                    self.app.processEvents()
                    time.sleep(0.01)
                self.app.processEvents()
                export.assert_called_once_with(dest)
                self.assertEqual(Path(dest).read_text(encoding="utf-8"), "diagnostic data")
                self.assertTrue(page.btn_export.isEnabled())
                self.assertIn(dest, page.lb_export_msg.text())

    def test_updater_version_and_exe_asset(self):
        self.assertTrue(updater.is_newer("v1.1.11", "1.1.10"))
        self.assertFalse(updater.is_newer("1.1.10", "1.1.10"))
        self.assertFalse(updater.is_newer("1.1.9", "1.1.10"))
        picked = updater._pick_exe_asset([
            {"name": "notes.zip", "size": 99999999, "browser_download_url": "https://x/a.zip"},
            {"name": "生死狙击工具箱-1.1.11.exe", "size": 54000000, "browser_download_url": "https://x/a.exe"},
        ])
        self.assertIsNotNone(picked)
        self.assertEqual(picked["name"], "生死狙击工具箱-1.1.11.exe")

    def test_tools_page_has_progress_bar_cancel_and_no_invalid_on_log(self):
        tools_src = (ROOT / "toolbox" / "ui" / "pages" / "tools.py").read_text(encoding="utf-8")
        self.assertNotIn("on_log=", tools_src)
        page = ToolsPage(None)
        self.assertTrue(hasattr(page, "prog_upd"))
        self.assertTrue(hasattr(page, "btn_cancel_upd"))
        self.assertFalse(page.prog_upd.isVisible())
        self.assertFalse(page.btn_cancel_upd.isVisible())
        page.apply_update_info(None)
        self.assertEqual(page.badge_upd.text(), "检查失败")
        self.assertFalse(page.btn_pull_upd.isEnabled())

    def test_autostart_permanently_disabled(self):
        ok, _msg = backend.autostart(True)
        self.assertFalse(ok)
        self.assertFalse(backend.load_app_settings().get("accel_autostart"))


if __name__ == "__main__":
    unittest.main()
