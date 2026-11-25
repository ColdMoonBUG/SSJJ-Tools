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
        self.assertTrue(updater.is_newer("v1.1.9", "1.1.8"))
        self.assertFalse(updater.is_newer("1.1.8", "1.1.8"))
        self.assertFalse(updater.is_newer("1.1.7", "1.1.8"))
        picked = updater._pick_exe_asset([
            {"name": "notes.zip", "size": 99999999, "browser_download_url": "https://x/a.zip"},
            {"name": "生死狙击工具箱-1.1.9.exe", "size": 54000000, "browser_download_url": "https://x/a.exe"},
        ])
        self.assertIsNotNone(picked)
        self.assertEqual(picked["name"], "生死狙击工具箱-1.1.9.exe")

    def test_autostart_permanently_disabled(self):
        ok, _msg = backend.autostart(True)
        self.assertFalse(ok)
        self.assertFalse(backend.load_app_settings().get("accel_autostart"))


if __name__ == "__main__":
    unittest.main()
