# -*- coding: utf-8 -*-
import threading
import time

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import (QColor, QFont, QIcon, QLinearGradient, QPainter,
                           QPen, QPixmap)
from PySide6.QtWidgets import (QApplication, QButtonGroup, QFrame,
                               QGraphicsDropShadowEffect, QHBoxLayout, QLabel,
                               QPushButton, QSizeGrip, QStackedWidget, QVBoxLayout,
                               QWidget)

import backend
from ui.pages.accel import AccelPage
from ui.pages.appearance import AppearancePage
from ui.pages.fps import FpsPage
from ui.pages.home import HomePage
from ui.pages.oneclick import OneclickPage
from ui.pages.resources import ResourcesPage
from ui.pages.risk import RiskPage
from ui.pages.tools import ToolsPage
from ui.theme import ACCENT
from ui.widgets import glyph_icon, icon_badge

NAV = [("一键启动", "one", "\uE768"),
       ("换肤注入", "risk", "\uE7BA"),
       ("大厅帧率", "fps", "\uE7FC"),
       ("本地加速", "accel", "\uE896"),
       ("工具", "tools", "\uE713")]


def _draw_logo_pixmap(size=64):
    """绘制极简战术准星 Logo（高清 2x）。"""
    dpr = 2.0
    pm = QPixmap(int(size * dpr), int(size * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)

    grad = QLinearGradient(0, 0, size, size)
    grad.setColorAt(0.0, QColor("#0ea5e9"))
    grad.setColorAt(0.55, QColor("#3b82f6"))
    grad.setColorAt(1.0, QColor("#6366f1"))
    p.setBrush(grad)
    p.setPen(Qt.NoPen)
    r = size * 0.24
    p.drawRoundedRect(QRectF(0, 0, size, size), r, r)

    # 顶部柔和反光
    top_hl = QLinearGradient(0, 0, 0, size * 0.55)
    top_hl.setColorAt(0.0, QColor(255, 255, 255, 55))
    top_hl.setColorAt(1.0, QColor(255, 255, 255, 0))
    p.setBrush(top_hl)
    p.drawRoundedRect(QRectF(1, 1, size - 2, size * 0.5), r, r)

    # 战术准星几何
    cx, cy = size / 2.0, size / 2.0
    ring_r = size * 0.26
    pen = QPen(QColor("#ffffff"))
    pen.setWidthF(max(1.6, size * 0.068))
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawEllipse(QPointF(cx, cy), ring_r, ring_r)

    tick_in = ring_r * 0.58
    tick_out = ring_r * 1.42
    p.drawLine(QPointF(cx, cy - tick_out), QPointF(cx, cy - tick_in))
    p.drawLine(QPointF(cx, cy + tick_in), QPointF(cx, cy + tick_out))
    p.drawLine(QPointF(cx - tick_out, cy), QPointF(cx - tick_in, cy))
    p.drawLine(QPointF(cx + tick_in, cy), QPointF(cx + tick_out, cy))

    # 中心亮核
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#e0f2fe"))
    core_r = max(1.6, size * 0.065)
    p.drawEllipse(QPointF(cx, cy), core_r, core_r)
    p.end()
    return pm


class NavButton(QPushButton):
    def __init__(self, label, code, parent=None):
        super().__init__("  " + label, parent)
        self.setObjectName("Nav")
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(42)
        self.setIconSize(QSize(17, 17))
        self._on = glyph_icon(code, "#38bdf8", 17)
        self._off = glyph_icon(code, "#7c8da6", 17)
        self.setIcon(self._off)
        self.toggled.connect(lambda on: self.setIcon(self._on if on else self._off))
        self.setAccessibleName(label)


class TitleBar(QWidget):
    def __init__(self, window):
        super().__init__()
        self.setObjectName("TitleBar")
        self.setFixedHeight(50)
        self._win = window

        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 0, 12, 0)
        lay.setSpacing(10)

        mark = QLabel()
        mark.setPixmap(_draw_logo_pixmap(26))
        mark.setFixedSize(26, 26)
        lay.addWidget(mark)

        title = QLabel("生死狙击工具箱")
        title.setObjectName("AppTitle")
        lay.addWidget(title)

        ver = QLabel("v1.1.0")
        ver.setObjectName("VersionTag")
        ver.setFixedHeight(22)
        lay.addWidget(ver, 0, Qt.AlignVCenter)
        lay.addStretch(1)

        win_font = QFont("Segoe MDL2 Assets")
        win_font.setPixelSize(10)
        for glyph, tip, slot, name in (
                ("\uE921", "最小化", self._win.showMinimized, "WinBtn"),
                ("\uE922", "最大化", self._win.toggle_max, "WinBtn"),
                ("\uE8BB", "关闭", self._win.close, "WinClose")):
            b = QPushButton(glyph)
            b.setObjectName(name)
            b.setFont(win_font)
            b.setToolTip(tip)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(slot)
            lay.addWidget(b)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and not self._win.is_max:
            wh = self.window().windowHandle()
            if wh:
                wh.startSystemMove()

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._win.toggle_max()


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("生死狙击工具箱")
        self.setWindowIcon(QIcon(_draw_logo_pixmap(64)))
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.resize(1280, 820)
        self.setMinimumSize(1080, 720)
        self.is_max = False
        self._normal_geo = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(0)
        self._outer = outer

        self.shell = QFrame()
        self.shell.setObjectName("Shell")
        self._shadow = QGraphicsDropShadowEffect(self)
        self._shadow.setBlurRadius(46)
        self._shadow.setOffset(0, 12)
        self._shadow.setColor(QColor(0, 0, 0, 190))
        self.shell.setGraphicsEffect(self._shadow)
        outer.addWidget(self.shell)
        self.shell.installEventFilter(self)

        sh = QVBoxLayout(self.shell)
        sh.setContentsMargins(0, 0, 0, 0)
        sh.setSpacing(0)
        sh.addWidget(TitleBar(self))

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        sh.addLayout(body, 1)

        # ---------------- 侧边栏
        side = QWidget()
        side.setObjectName("Sidebar")
        side.setFixedWidth(220)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(14, 18, 14, 16)
        sl.setSpacing(5)

        sec = QLabel("功 能 导 航")
        sec.setObjectName("SideSection")
        sl.addWidget(sec)
        sl.addSpacing(4)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_buttons = {}
        for label, key, code in NAV:
            b = NavButton(label, code)
            b.clicked.connect(lambda _=False, k=key: self.goto(k))
            self.nav_group.addButton(b)
            self.nav_buttons[key] = b
            sl.addWidget(b)

        sl.addStretch(1)

        card = QFrame()
        card.setObjectName("SideCard")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(14, 13, 14, 13)
        cl.setSpacing(8)
        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(icon_badge("\uE9D9", ACCENT, 24, 12))
        t = QLabel("本地加速状态")
        t.setObjectName("SideTitle")
        head.addWidget(t)
        head.addStretch(1)
        cl.addLayout(head)

        self.svc_dot = QLabel("\u25cf")
        self.svc_dot.setObjectName("Dot")
        self.lb_svc = QLabel("检查中…")
        self.lb_svc.setObjectName("SideSub")
        r1 = QHBoxLayout()
        r1.setSpacing(6)
        r1.addWidget(self.svc_dot)
        r1.addWidget(self.lb_svc, 1)
        cl.addLayout(r1)

        self.lb_admin = QLabel("权限：检查中")
        self.lb_admin.setObjectName("SideSub")
        self.lb_admin.setWordWrap(True)
        cl.addWidget(self.lb_admin)

        self.lb_hosts = QLabel("hosts：检查中")
        self.lb_hosts.setObjectName("SideSub")
        self.lb_hosts.setWordWrap(True)
        cl.addWidget(self.lb_hosts)
        sl.addWidget(card)

        sl.addSpacing(6)
        foot = QLabel("本地资源优先 · 随时一键还原")
        foot.setObjectName("DimSmall")
        foot.setAlignment(Qt.AlignCenter)
        foot.setWordWrap(True)
        sl.addWidget(foot)
        body.addWidget(side)

        # ---------------- 内容区
        content = QWidget()
        cl2 = QVBoxLayout(content)
        cl2.setContentsMargins(0, 0, 0, 0)
        cl2.setSpacing(0)
        self.stack = QStackedWidget()
        self.pages = {
            "one": OneclickPage(self),
            "home": HomePage(self),
            "accel": AccelPage(self),
            "res": ResourcesPage(self),
            "fps": FpsPage(self),
            "look": AppearancePage(self),
            "risk": RiskPage(self),
            "tools": ToolsPage(self),
        }
        for _key, _page in self.pages.items():
            self.stack.addWidget(_page)
        cl2.addWidget(self.stack, 1)
        body.addWidget(content, 1)

        self.grip = QSizeGrip(self.shell)
        self.grip.setFixedSize(16, 16)
        self.grip.raise_()

        self._current = None
        self._workers = []
        self.goto("one")

        self.timer = QTimer(self)
        self.timer.setInterval(4000)
        self.timer.timeout.connect(self._poll)
        self.timer.start()
        self._poll()
        # 启动后自动恢复上次开启的加速服务、风险注入守护与武器皮肤配置
        QTimer.singleShot(300, self._auto_restore)

    def _auto_restore(self):
        def job(_w):
            return backend.restore_on_startup()

        def done(_logs):
            self._poll()
            cur = self.pages.get(self._current)
            if cur and hasattr(cur, "refresh"):
                try:
                    cur.refresh()
                except Exception:
                    pass

        self.run(job, on_done=done)

    # ------------------------------------------------------------ 窗口
    def toggle_max(self):
        if not self.is_max:
            self._normal_geo = self.geometry()
            self.is_max = True
            self._outer.setContentsMargins(0, 0, 0, 0)
            self.shell.setProperty("max", "1")
            self.shell.style().unpolish(self.shell)
            self.shell.style().polish(self.shell)
            self._shadow.setEnabled(False)
            self.grip.hide()
            self.setGeometry(self.screen().availableGeometry())
        else:
            self.is_max = False
            self._outer.setContentsMargins(12, 12, 12, 12)
            self.shell.setProperty("max", "0")
            self.shell.style().unpolish(self.shell)
            self.shell.style().polish(self.shell)
            self._shadow.setEnabled(True)
            self.grip.show()
            if self._normal_geo:
                self.setGeometry(self._normal_geo)

    def eventFilter(self, obj, ev):
        if obj is self.shell and ev.type() == QEvent.Resize:
            self.grip.move(max(0, self.shell.width() - 18),
                           max(0, self.shell.height() - 18))
            self.grip.raise_()
        return super().eventFilter(obj, ev)

    # ------------------------------------------------------------ 导航
    def goto(self, key):
        if key not in self.pages:
            return
        page = self.pages[key]
        _btn = self.nav_buttons.get(key)
        if self._current == key:
            if _btn:
                _btn.setChecked(True)
            return
        if self._current is not None:
            self.pages[self._current].on_hide()
        self._current = key
        if _btn:
            _btn.setChecked(True)
        self.stack.setCurrentWidget(page)
        page.on_show()

    def notify(self, text):
        pass

    # ------------------------------------------------------------ 全局状态
    def _poll(self):
        def job(_w):
            return {"running": backend.accel_running(),
                    "admin": backend.is_admin(),
                    "hosts": backend.hosts_marked()}

        def done(d):
            if not d:
                return
            running = d["running"]
            self.svc_dot.setProperty("state", "ok" if running else "bad")
            self.svc_dot.style().unpolish(self.svc_dot)
            self.svc_dot.style().polish(self.svc_dot)
            self.lb_svc.setText("运行中 · 80/443" if running else "未运行")
            self.lb_admin.setText("管理员模式" if d["admin"] else "普通权限（写 hosts 会失败）")
            self.lb_hosts.setText("hosts 已指向本机" if d["hosts"] else "hosts 未写入")

        self.run(lambda w: job(w), on_done=done)

    def run(self, fn, on_done=None):
        from ui.widgets import Worker
        w = Worker(fn, self)
        if on_done is not None:
            w.done.connect(on_done)
        self._workers.append(w)
        w.start()
        return w

    # ------------------------------------------------------------------ 退出
    def closeEvent(self, ev):
        """点右上角关闭 = 关软件 + 停掉所有后台（守护线程、加速守护、hosts）。

        收尾放到后台线程跑（停守护要 join、加速要等 0.4s、还可能扫一遍残留进程），
        窗口先藏起来，收完再真退；12 秒还没收完就强制退，绝不把窗口留在那儿骗人。
        """
        if getattr(self, "_closing", False):
            ev.accept()
            return
        self._closing = True
        self._shutdown_done = False
        self._shutdown_t0 = time.time()
        # hide() 之后 Qt 默认会立刻 quit（last window closed）→ aboutToQuit 的
        # 1.5 秒硬退看门人会在 shutdown_all 还没跑完时 os._exit，收尾被截断。
        # 这里先关掉「最后一个窗口关闭即退出」，收尾由 _poll_shutdown 决定。
        try:
            _app = QApplication.instance()
            if _app is not None:
                _app.setQuitOnLastWindowClosed(False)
        except Exception:
            pass
        self.hide()
        ev.accept()

        def work():
            try:
                backend.shutdown_all()
            except Exception:
                pass
            self._shutdown_done = True
            try:
                _a = QApplication.instance()
                if _a is not None:
                    _a.setProperty("ssjj_shutdown_done", True)
            except Exception:
                pass

        threading.Thread(target=work, name="ssjj-shutdown", daemon=True).start()
        self._close_timer = QTimer(self)
        self._close_timer.timeout.connect(self._poll_shutdown)
        self._close_timer.start(150)

    def _poll_shutdown(self):
        if self._shutdown_done or (time.time() - self._shutdown_t0) > 12.0:
            try:
                self._close_timer.stop()
            except Exception:
                pass
            app = QApplication.instance()
            if app is not None:
                app.quit()

    def _elevate(self):
        backend.elevate()
        self.close()
