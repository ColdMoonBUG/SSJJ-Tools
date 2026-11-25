# -*- coding: utf-8 -*-
"""现代战术深色主题：深海黑曜石底 + 电光青/靛蓝渐变，细腻分层与高可读性排版。"""

ACCENT = "#38bdf8"
ACCENT2 = "#6366f1"
OK = "#22d3a7"
WARN = "#fbbf24"
BAD = "#fb7185"
TEXT = "#f1f5f9"
TEXT2 = "#94a3b8"
TEXT3 = "#64748b"

QSS = """
* {
    font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
    font-size: 13px;
    color: #f1f5f9;
}

QFrame#Shell {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                stop:0 #0b101b, stop:0.5 #0a0e17, stop:1 #0d111e);
    border: 1px solid #1e293b;
    border-radius: 16px;
}
QFrame#Shell[max="1"] { border-radius: 0; border: none; }
QDialog { background: #0f1624; }
QMessageBox { background: #0f1624; }

/* ---------------- 标题栏 ---------------- */
QWidget#TitleBar {
    background: rgba(13, 19, 31, 0.85);
    border-bottom: 1px solid #172033;
    border-top-left-radius: 16px;
    border-top-right-radius: 16px;
}
QLabel#AppTitle {
    font-size: 13.5px;
    font-weight: 700;
    color: #f8fafc;
    letter-spacing: 0.5px;
}
QLabel#VersionTag {
    color: #38bdf8;
    font-size: 10.5px;
    font-weight: 700;
    background: rgba(56, 189, 248, 0.12);
    border: 1px solid rgba(56, 189, 248, 0.28);
    border-radius: 9px;
    padding: 1px 8px;
}
QPushButton#WinBtn {
    background: transparent;
    border: none;
    border-radius: 7px;
    color: #94a3b8;
    font-family: "Segoe MDL2 Assets";
    font-size: 10px;
    min-width: 38px; max-width: 38px;
    min-height: 28px; max-height: 28px;
}
QPushButton#WinBtn:hover { background: #1e293b; color: #f8fafc; }
QPushButton#WinClose {
    background: transparent;
    border: none;
    border-radius: 7px;
    color: #94a3b8;
    font-family: "Segoe MDL2 Assets";
    font-size: 10px;
    min-width: 38px; max-width: 38px;
    min-height: 28px; max-height: 28px;
}
QPushButton#WinClose:hover { background: #e11d48; color: #ffffff; }

/* ---------------- 侧边栏 ---------------- */
QWidget#Sidebar {
    background: #0c121e;
    border-right: 1px solid #172033;
    border-bottom-left-radius: 16px;
}
QLabel#SideSection {
    color: #475569;
    font-size: 11px;
    font-weight: 700;
    padding: 2px 12px;
    letter-spacing: 2px;
}
QPushButton#Nav {
    text-align: left;
    padding: 0 14px;
    border: 1px solid transparent;
    border-left: 3px solid transparent;
    border-radius: 10px;
    background: transparent;
    color: #94a3b8;
    font-size: 13.5px;
    font-weight: 500;
}
QPushButton#Nav:hover {
    background: rgba(30, 41, 59, 0.65);
    color: #f1f5f9;
}
QPushButton#Nav:checked {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 rgba(56, 189, 248, 0.16),
                stop:1 rgba(99, 102, 241, 0.08));
    border: 1px solid rgba(56, 189, 248, 0.24);
    border-left: 3px solid #38bdf8;
    color: #ffffff;
    font-weight: 700;
}

QFrame#SideCard {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #111a2c, stop:1 #0e1524);
    border: 1px solid #1e2b42;
    border-radius: 13px;
}
QLabel#Dot { color: #64748b; font-size: 12px; }
QLabel#Dot[state="ok"] { color: #22d3a7; }
QLabel#Dot[state="bad"] { color: #fb7185; }
QLabel#SideTitle { font-size: 12.5px; font-weight: 700; color: #e2e8f0; }
QLabel#SideSub { font-size: 11.5px; color: #8b9bb4; }

/* ---------------- 卡片与指标块 ---------------- */
QFrame#Card {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #131b2b, stop:1 #101624);
    border: 1px solid #1e293d;
    border-radius: 14px;
}
QFrame#Card:hover {
    border-color: #273652;
}
QFrame#Tile {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #131c2e, stop:1 #0f1624);
    border: 1px solid #1d2a40;
    border-radius: 13px;
}
QFrame#Tile:hover {
    border-color: #2d4266;
}
QFrame#IconBadge {
    background: rgba(56, 189, 248, 0.12);
    border: 1px solid rgba(56, 189, 248, 0.22);
    border-radius: 8px;
}
QFrame#HeaderBar {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #38bdf8, stop:1 #6366f1);
    border-radius: 2px;
}

QLabel#CardTitle { font-size: 14.5px; font-weight: 700; color: #f8fafc; }
QLabel#CardSub { font-size: 12px; color: #7c8da6; }
QLabel#H1 { font-size: 23px; font-weight: 800; color: #ffffff; }
QLabel#H2 { font-size: 17px; font-weight: 700; color: #ffffff; }
QLabel#Dim { color: #94a3b8; }
QLabel#DimSmall { color: #64748b; font-size: 11.5px; }
QLabel#StatValue { font-size: 22px; font-weight: 800; color: #f8fafc; }
QLabel#StatCap { font-size: 11.5px; font-weight: 600; color: #7c8da6; }
QLabel { background: transparent; }

/* ---------------- 状态徽章 ---------------- */
QLabel#Badge {
    border-radius: 10px;
    padding: 3px 10px;
    font-size: 11.5px;
    font-weight: 600;
    background: #162033;
    color: #94a3b8;
    border: 1px solid #24324a;
}
QLabel#Badge[state="ok"] {
    background: rgba(34, 211, 167, 0.12);
    color: #34e0b5;
    border: 1px solid rgba(34, 211, 167, 0.30);
}
QLabel#Badge[state="bad"] {
    background: rgba(251, 113, 133, 0.12);
    color: #fda4af;
    border: 1px solid rgba(251, 113, 133, 0.30);
}
QLabel#Badge[state="warn"] {
    background: rgba(251, 191, 36, 0.12);
    color: #fcd34d;
    border: 1px solid rgba(251, 191, 36, 0.30);
}

/* ---------------- Hero 横幅 ---------------- */
QFrame#Hero {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                stop:0 #111c33, stop:0.55 #131c30, stop:1 #1a1838);
    border: 1px solid #26395e;
    border-radius: 18px;
}
QLabel#Eyebrow {
    color: #38bdf8;
    font-size: 11px;
    font-weight: 800;
    letter-spacing: 1.5px;
}
QLabel#HeroTitle {
    font-size: 26px;
    font-weight: 800;
    color: #ffffff;
}

/* ---------------- 按钮 ---------------- */
QPushButton {
    background: #172133;
    border: 1px solid #26344d;
    border-radius: 10px;
    padding: 7px 16px;
    color: #e2e8f0;
    font-weight: 500;
}
QPushButton:hover {
    background: #1e2b42;
    border-color: #3b5075;
    color: #ffffff;
}
QPushButton:pressed {
    background: #131b2a;
}
QPushButton:disabled {
    color: #475569;
    border-color: #1b2436;
    background: #101622;
}
QPushButton#Primary {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 #0ea5e9, stop:0.5 #3b82f6, stop:1 #6366f1);
    border: 1px solid rgba(125, 211, 252, 0.45);
    font-weight: 700;
    color: #ffffff;
}
QPushButton#Primary:hover {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 #38bdf8, stop:0.5 #60a5fa, stop:1 #818cf8);
    border-color: rgba(186, 230, 253, 0.70);
}
QPushButton#Primary:pressed {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 #0284c7, stop:1 #4f46e5);
}
QPushButton#Primary:disabled {
    background: #1e293b;
    border-color: #293548;
    color: #64748b;
}
QPushButton#Danger {
    color: #fda4af;
    border: 1px solid rgba(251, 113, 133, 0.35);
    background: rgba(251, 113, 133, 0.08);
    font-weight: 600;
}
QPushButton#Danger:hover {
    background: rgba(244, 63, 94, 0.20);
    border-color: rgba(251, 113, 133, 0.60);
    color: #ffe4e6;
}
QPushButton#Ghost {
    background: rgba(255, 255, 255, 0.02);
    border: 1px solid #1b263b;
    border-radius: 10px;
    text-align: left;
    padding: 8px 12px;
    color: #cbd5e1;
}
QPushButton#Ghost:hover {
    background: rgba(56, 189, 248, 0.09);
    color: #ffffff;
    border-color: rgba(56, 189, 248, 0.32);
}
QPushButton#Chip {
    background: rgba(15, 23, 42, 0.75);
    border: 1px solid #24324a;
    border-radius: 8px;
    padding: 2px 9px;
    font-size: 12px;
    font-weight: 600;
    color: #cbd5e1;
}
QPushButton#Chip:hover {
    background: rgba(56, 189, 248, 0.16);
    border-color: rgba(56, 189, 248, 0.45);
    color: #ffffff;
}

/* ---------------- 快捷入口条目 / 提示编号 ---------------- */
QFrame#QuickRow {
    background: rgba(15, 23, 42, 0.55);
    border: 1px solid #1c283d;
    border-radius: 11px;
}
QFrame#QuickRow:hover {
    background: rgba(56, 189, 248, 0.10);
    border-color: rgba(56, 189, 248, 0.35);
}
QLabel#TipNum {
    background: rgba(56, 189, 248, 0.14);
    border: 1px solid rgba(56, 189, 248, 0.28);
    border-radius: 10px;
    color: #38bdf8;
    font-size: 11px;
    font-weight: 800;
}

/* ---------------- 输入框与下拉 ---------------- */
QLineEdit, QComboBox, QSpinBox {
    background: #0b111d;
    border: 1px solid #24324a;
    border-radius: 9px;
    padding: 6px 11px;
    min-height: 20px;
    selection-background-color: #2563eb;
}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover {
    border-color: #354a6e;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus {
    border-color: #38bdf8;
    background: #0d1424;
}
QComboBox {
    padding-right: 26px;
}
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 26px;
    border: none;
    border-left: 1px solid #1c283d;
    border-top-right-radius: 9px;
    border-bottom-right-radius: 9px;
    background: rgba(30, 41, 59, 0.45);
}
QComboBox::down-arrow {
    width: 0px;
    height: 0px;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid #94a3b8;
}
QComboBox QAbstractItemView {
    background: #0f172a;
    border: 1px solid #293854;
    border-radius: 8px;
    selection-background-color: rgba(56, 189, 248, 0.22);
    selection-color: #ffffff;
    outline: none;
    padding: 4px;
}
QSpinBox::up-button, QSpinBox::down-button {
    width: 20px;
    border: none;
    background: transparent;
}
QSpinBox::up-arrow {
    width: 0px; height: 0px;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-bottom: 5px solid #94a3b8;
}
QSpinBox::down-arrow {
    width: 0px; height: 0px;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid #94a3b8;
}

/* ---------------- 复选框 ---------------- */
QCheckBox { spacing: 8px; color: #cbd5e1; }
QCheckBox::indicator {
    width: 16px; height: 16px;
    border-radius: 5px;
    border: 1px solid #334155;
    background: #0b111d;
}
QCheckBox::indicator:hover { border-color: #38bdf8; }
QCheckBox::indicator:checked {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                stop:0 #0ea5e9, stop:1 #6366f1);
    border-color: #38bdf8;
}

/* ---------------- 日志与列表 ---------------- */
QPlainTextEdit, QTextEdit {
    background: #080c14;
    border: 1px solid #192438;
    border-radius: 11px;
    padding: 8px;
    font-family: "Cascadia Mono", "Consolas", "Microsoft YaHei UI";
    font-size: 12px;
    color: #94a3b8;
    selection-background-color: #1e3a5f;
}

QListWidget {
    background: #0b111d;
    border: 1px solid #1c283d;
    border-radius: 10px;
    padding: 5px;
    outline: none;
}
QListWidget::item {
    padding: 6px 10px;
    border-radius: 7px;
    color: #cbd5e1;
}
QListWidget::item:selected {
    background: rgba(56, 189, 248, 0.18);
    color: #ffffff;
}
QListWidget::item:hover { background: #152033; }

/* ---------------- 进度条 ---------------- */
QProgressBar {
    border: none;
    background: #162033;
    border-radius: 5px;
    min-height: 9px;
    max-height: 9px;
    text-align: center;
    color: transparent;
}
QProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 #0ea5e9, stop:0.6 #38bdf8, stop:1 #818cf8);
    border-radius: 5px;
}

/* ---------------- 滚动条 ---------------- */
QScrollArea { border: none; background: transparent; }
QScrollArea > QWidget > QWidget { background: transparent; }
QWidget#PageBody { background: transparent; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 4px 2px; }
QScrollBar::handle:vertical {
    background: #24324a;
    border-radius: 4px;
    min-height: 36px;
}
QScrollBar::handle:vertical:hover { background: #385075; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QScrollBar:horizontal { background: transparent; height: 8px; margin: 2px 4px; }
QScrollBar::handle:horizontal {
    background: #24324a;
    border-radius: 4px;
    min-width: 36px;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: transparent; }

/* ---------------- 表格 ---------------- */
QTableWidget {
    background: #0b111d;
    border: 1px solid #1c283d;
    border-radius: 11px;
    gridline-color: #162033;
    selection-background-color: rgba(56, 189, 248, 0.16);
    alternate-background-color: #0e1524;
}
QHeaderView::section {
    background: #101829;
    border: none;
    border-bottom: 1px solid #1e2b42;
    padding: 8px 10px;
    color: #7c8da6;
    font-size: 11.5px;
    font-weight: 700;
}
QTableWidget::item { padding: 6px 10px; border: none; }

QToolTip {
    background: #131c2e;
    color: #f1f5f9;
    border: 1px solid #2d4266;
    padding: 6px 10px;
    border-radius: 8px;
}
QSizeGrip { background: transparent; width: 14px; height: 14px; }
"""


def apply_theme(app):
    app.setStyle("Fusion")
    app.setStyleSheet(QSS)
