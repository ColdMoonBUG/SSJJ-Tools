# -*- coding: utf-8 -*-
"""通用控件：卡片、指标块、状态徽章、后台任务、日志框、图标与页面骨架。"""
import threading

from PySide6.QtCore import QObject, QRect, QSize, Qt, Signal
from PySide6.QtGui import (QColor, QFont, QIcon, QLinearGradient, QPainter,
                           QPixmap, QTextCursor)
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPlainTextEdit,
                               QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from ui.theme import ACCENT, ACCENT2, TEXT2

GLYPH_FONT = "Segoe MDL2 Assets"


def glyph_pixmap(code, color=TEXT2, size=16):
    dpr = 2.0
    pm = QPixmap(int(size * dpr), int(size * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    f = QFont(GLYPH_FONT)
    f.setPixelSize(size)
    p.setFont(f)
    p.setPen(QColor(color))
    p.drawText(QRect(0, 0, int(size), int(size)), Qt.AlignCenter, code)
    p.end()
    return pm


def glyph_icon(code, color=TEXT2, size=16):
    return QIcon(glyph_pixmap(code, color, size))


def glyph_label(code, color=ACCENT, size=15):
    lab = QLabel()
    lab.setPixmap(glyph_pixmap(code, color, size))
    lab.setFixedSize(size, size)
    return lab


def icon_badge(code, color=ACCENT, box_size=28, glyph_size=14):
    """带微透光底座的图标徽章。"""
    box = QFrame()
    box.setObjectName("IconBadge")
    box.setFixedSize(box_size, box_size)
    lay = QHBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setAlignment(Qt.AlignCenter)
    lay.addWidget(glyph_label(code, color, glyph_size))
    return box


class Card(QFrame):
    def __init__(self, title=None, subtitle=None, glyph=None, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(20, 18, 20, 18)
        self._outer.setSpacing(12)
        self.header = None
        self._header_extra = None
        if title:
            row = QHBoxLayout()
            row.setSpacing(10)
            if glyph:
                row.addWidget(icon_badge(glyph, ACCENT, 28, 14))
            title_box = QVBoxLayout()
            title_box.setContentsMargins(0, 0, 0, 0)
            title_box.setSpacing(2)
            self.header = QLabel(title)
            self.header.setObjectName("CardTitle")
            title_box.addWidget(self.header)
            if subtitle:
                sub = QLabel(subtitle)
                sub.setObjectName("CardSub")
                sub.setWordWrap(True)
                title_box.addWidget(sub)
            row.addLayout(title_box, 1)
            self._header_row = row
            self._outer.addLayout(row)
        else:
            self._header_row = None
        self.content = QVBoxLayout()
        self.content.setSpacing(10)
        self.content.setAlignment(Qt.AlignTop)
        self._outer.addLayout(self.content, 1)

    def add(self, widget, stretch=0):
        self.content.addWidget(widget, stretch)
        return widget

    def add_layout(self, layout):
        self.content.addLayout(layout)

    def add_header_widget(self, widget):
        if self._header_row is not None:
            self._header_row.addWidget(widget, 0, Qt.AlignVCenter)

    def add_stretch(self):
        self.content.addStretch(1)


class StatTile(QFrame):
    def __init__(self, caption, value="--", glyph=None, accent=None, parent=None):
        super().__init__(parent)
        self.setObjectName("Tile")
        self._accent = accent or ACCENT
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 15, 16, 14)
        lay.setSpacing(8)
        head = QHBoxLayout()
        head.setSpacing(8)
        if glyph:
            head.addWidget(icon_badge(glyph, self._accent, 24, 12))
        self.cap = QLabel(caption)
        self.cap.setObjectName("StatCap")
        head.addWidget(self.cap)
        head.addStretch(1)
        lay.addLayout(head)
        self.value = QLabel(value)
        self.value.setObjectName("StatValue")
        lay.addWidget(self.value)

    def paintEvent(self, ev):
        super().paintEvent(ev)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        grad = QLinearGradient(16, 0, self.width() - 16, 0)
        c1 = QColor(self._accent)
        c1.setAlpha(180)
        c2 = QColor(ACCENT2)
        c2.setAlpha(30)
        grad.setColorAt(0.0, c1)
        grad.setColorAt(0.65, c2)
        grad.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(grad)
        p.drawRoundedRect(16, 1, max(20, self.width() - 32), 2, 1, 1)
        p.end()

    def set(self, text):
        self.value.setText(str(text))


class Badge(QLabel):
    def __init__(self, text="--", parent=None):
        super().__init__(text, parent)
        self.setObjectName("Badge")
        self.setAlignment(Qt.AlignCenter)
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)

    def state(self, s):
        self.setProperty("state", s)
        self.style().unpolish(self)
        self.style().polish(self)


class LogBox(QPlainTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setMaximumBlockCount(4000)
        self.setMinimumHeight(140)

    def line(self, text):
        self.appendPlainText(str(text))
        self._to_end()

    def set_text(self, text):
        self.setPlainText(text or "")
        self._to_end()

    def _to_end(self):
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.setTextCursor(cursor)

    def clear_all(self):
        self.clear()


class Worker(QObject):
    log = Signal(str)
    progress = Signal(int, str)
    done = Signal(object)

    def __init__(self, fn, parent=None, quiet=False):
        super().__init__(parent)
        self.fn = fn
        self.quiet = bool(quiet)
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        if not self.quiet:
            try:
                import diagnostics
                diagnostics.record("后台任务开始 page=%s task=%s" % (
                    type(self.parent()).__name__, getattr(self.fn, "__name__", "?")))
            except Exception:
                pass
        try:
            result = self.fn(self)
        except Exception as exc:  # noqa: BLE001
            try:
                import diagnostics
                import traceback
                diagnostics.record("后台任务异常 " + traceback.format_exc())
            except Exception:
                pass
            self.log.emit("[错误] %r" % (exc,))
            result = None
        if not self.quiet:
            try:
                import diagnostics
                diagnostics.record("后台任务结束 page=%s task=%s" % (
                    type(self.parent()).__name__, getattr(self.fn, "__name__", "?")))
            except Exception:
                pass
        self.done.emit(result)


class Page(QWidget):
    """页面基类：后台任务统一从这里起。"""

    TITLE = ""

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app
        self._workers = []

    def run(self, fn, on_done=None, log=None, progress=None, quiet=False):
        w = Worker(fn, self, quiet=quiet)
        if log is not None:
            w.log.connect(log)
        if progress is not None:
            w.progress.connect(progress)
        if on_done is not None:
            w.done.connect(on_done)

        def _cleanup(_res, _w=w):
            try:
                self._workers.remove(_w)
            except ValueError:
                pass
            _w.deleteLater()

        w.done.connect(_cleanup)
        self._workers.append(w)
        w.start()
        return w

    def on_show(self):
        pass

    def on_hide(self):
        pass


class ScrollPage(Page):
    """带滚动容器的页面：内容多时不出滚动条挤压布局。"""

    def __init__(self, app, parent=None):
        super().__init__(app, parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.body = QWidget()
        self.body.setObjectName("PageBody")
        self.root = QVBoxLayout(self.body)
        self.root.setContentsMargins(32, 24, 32, 28)
        self.root.setSpacing(16)
        self.scroll.setWidget(self.body)
        outer.addWidget(self.scroll)


def page_header(title, subtitle=None, extra=None):
    box = QWidget()
    lay = QVBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 2)
    lay.setSpacing(6)
    row = QHBoxLayout()
    row.setSpacing(12)
    bar = QFrame()
    bar.setObjectName("HeaderBar")
    bar.setFixedSize(4, 24)
    row.addWidget(bar, 0, Qt.AlignVCenter)
    lab = QLabel(title)
    lab.setObjectName("H1")
    row.addWidget(lab)
    if extra is not None:
        row.addWidget(extra, 0, Qt.AlignVCenter)
    row.addStretch(1)
    lay.addLayout(row)
    if subtitle:
        sub_row = QHBoxLayout()
        sub_row.setContentsMargins(16, 0, 0, 0)
        sub_row.addWidget(dim(subtitle))
        lay.addLayout(sub_row)
    return box


def title_row(title, extra=None):
    row = QHBoxLayout()
    lab = QLabel(title)
    lab.setObjectName("CardTitle")
    row.addWidget(lab)
    row.addStretch(1)
    if extra is not None:
        row.addWidget(extra)
    return row


def dim(text, small=False):
    lab = QLabel(text)
    lab.setObjectName("DimSmall" if small else "Dim")
    lab.setWordWrap(True)
    return lab


def dot(state="ok"):
    lab = QLabel("\u25cf")
    lab.setObjectName("Dot")
    lab.setProperty("state", state)
    return lab


def hline():
    line = QFrame()
    line.setObjectName("HLine")
    line.setFixedHeight(1)
    line.setStyleSheet("background:#1c283d; border:none;")
    return line
