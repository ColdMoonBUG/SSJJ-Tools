# -*- coding: utf-8 -*-
"""诊断文件保存位置选择。"""
import datetime
from pathlib import Path

from PySide6.QtCore import QStandardPaths
from PySide6.QtWidgets import QFileDialog


def choose_diagnostic_path(parent):
    desktop = QStandardPaths.writableLocation(QStandardPaths.DesktopLocation)
    folder = Path(desktop) if desktop and Path(desktop).is_dir() else Path.home()
    name = "SSJJ诊断-%s.txt" % datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path, _filter = QFileDialog.getSaveFileName(
        parent, "保存诊断日志", str(folder / name), "文本文件 (*.txt)")
    return path
