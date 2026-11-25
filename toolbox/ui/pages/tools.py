# -*- coding: utf-8 -*-
from PySide6.QtWidgets import (QGridLayout, QHBoxLayout, QLabel, QPushButton,
                               QVBoxLayout)

import backend
from ui.widgets import Badge, Card, ScrollPage, dim, page_header

VERSION = "1.1.0"


class ToolsPage(ScrollPage):
    TITLE = "工具"

    def __init__(self, app):
        super().__init__(app)
        self._busy = False

        self.root.addWidget(page_header(
            "工具与设置", "系统集成和常用入口。所有写系统位置的操作都需要管理员权限。"))

        row = QHBoxLayout()
        row.setSpacing(14)

        svc = Card("服务", "守护进程与开机自启", glyph="\uE768")
        self.badge_svc = Badge("检查中")
        svc.add_header_widget(self.badge_svc)
        r1 = QHBoxLayout()
        r1.setSpacing(8)
        b_start = QPushButton("启动加速服务")
        b_start.clicked.connect(self._start)
        b_stop = QPushButton("停止并还原 hosts")
        b_stop.setObjectName("Danger")
        b_stop.clicked.connect(self._stop)
        b_restart = QPushButton("重启服务")
        b_restart.clicked.connect(self._restart)
        for b in (b_start, b_stop, b_restart):
            r1.addWidget(b)
        r1.addStretch(1)
        svc.add_layout(r1)
        self.badge_auto = Badge("开机自启：检查中")
        svc.add(self.badge_auto)
        r2 = QHBoxLayout()
        r2.setSpacing(8)
        b_auto_on = QPushButton("启用开机自启")
        b_auto_on.clicked.connect(lambda: self._auto(True))
        b_auto_off = QPushButton("取消开机自启")
        b_auto_off.clicked.connect(lambda: self._auto(False))
        r2.addWidget(b_auto_on)
        r2.addWidget(b_auto_off)
        r2.addStretch(1)
        svc.add_layout(r2)
        svc.add(dim("开机自启用登录计划任务（最高权限），登录后静默拉起加速服务，不弹 UAC。",
                    small=True))
        row.addWidget(svc, 1)

        hostcard = Card("hosts 与权限", "只维护带标记的那一行", glyph="\uE774")
        self.badge_hosts = Badge("hosts：检查中")
        hostcard.add_header_widget(self.badge_hosts)
        r3 = QHBoxLayout()
        r3.setSpacing(8)
        b_h_on = QPushButton("写入 hosts 标记")
        b_h_on.clicked.connect(lambda: self._hosts(True))
        b_h_off = QPushButton("移除 hosts 标记")
        b_h_off.clicked.connect(lambda: self._hosts(False))
        r3.addWidget(b_h_on)
        r3.addWidget(b_h_off)
        r3.addStretch(1)
        hostcard.add_layout(r3)
        self.badge_admin = Badge("权限：检查中")
        hostcard.add(self.badge_admin)
        self.btn_elevate = QPushButton("以管理员身份重启工具箱")
        self.btn_elevate.clicked.connect(self._elevate)
        hostcard.add(self.btn_elevate)
        hostcard.add(dim("hosts 只加一行：127.0.0.1 cdn.4399ssjj.com # ssjj-local-cdn，"
                         "其余行不动。", small=True))
        row.addWidget(hostcard, 1)
        self.root.addLayout(row)

        dirs = Card("目录", "一键直达")
        r4 = QHBoxLayout()
        r4.setSpacing(8)
        for label, path in (
            ("游戏目录", backend.P.GAME_DIR),
            ("大厅资源库", backend.P.CACHE_DIR),
            ("战斗资源缓存", backend.battle_preload_dir()),
            ("运行日志", backend.C.LOG),
        ):
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, p=path: backend.open_path(p))
            r4.addWidget(b)
        r4.addStretch(1)
        dirs.add_layout(r4)
        self.lb_data = QLabel("数据目录：%s" % backend.DATA)
        self.lb_data.setObjectName("DimSmall")
        dirs.add(self.lb_data)
        self.root.addWidget(dirs)

        about = Card("关于")
        g = QGridLayout()
        g.setHorizontalSpacing(24)
        g.setVerticalSpacing(8)
        for i, (k, v) in enumerate((
            ("名称", "生死狙击工具箱"),
            ("版本", VERSION),
            ("打包模式", "单文件 exe" if backend.IS_FROZEN else "源码运行"),
            ("说明", "本地资源加速与外观配置整合。不注入游戏进程，不含对战辅助功能。"),
        )):
            lab = QLabel(k)
            lab.setObjectName("Dim")
            g.addWidget(lab, i, 0)
            val = QLabel(v)
            val.setWordWrap(True)
            g.addWidget(val, i, 1)
        g.setColumnStretch(1, 1)
        about.add_layout(g)
        self.root.addWidget(about)
        self.root.addStretch(1)

    # ------------------------------------------------------------ 状态
    def on_show(self):
        self.refresh()

    def refresh(self):
        def job(_w):
            return {"running": backend.accel_running(),
                    "hosts": backend.hosts_marked(),
                    "auto": backend.autostart_state(),
                    "admin": backend.is_admin()}

        self.run(job, on_done=self._apply)

    def _apply(self, d):
        if not d:
            return
        self.badge_svc.setText("服务：运行中" if d["running"] else "服务：未运行")
        self.badge_svc.state("ok" if d["running"] else "bad")
        self.badge_auto.setText("开机自启：已启用" if d["auto"] else "开机自启：未启用")
        self.badge_auto.state("ok" if d["auto"] else "")
        self.badge_hosts.setText("hosts：已写入" if d["hosts"] else "hosts：未写入")
        self.badge_hosts.state("ok" if d["hosts"] else "warn")
        self.badge_admin.setText("权限：管理员" if d["admin"] else "权限：普通用户")
        self.badge_admin.state("ok" if d["admin"] else "warn")
        self.btn_elevate.setEnabled(not d["admin"])

    # ------------------------------------------------------------ 动作
    def _op(self, fn):
        if self._busy:
            return
        self._busy = True

        def done(_r):
            self._busy = False
            self.refresh()

        self.run(lambda _w: fn(), on_done=done)

    def _start(self):
        self._op(backend.accel_start)

    def _stop(self):
        self._op(backend.accel_stop)

    def _restart(self):
        self._op(backend.accel_restart)

    def _auto(self, add):
        ok, msg = backend.autostart(add)
        self.refresh()

    def _hosts(self, add):
        def fn():
            backend.C.hosts_set(add)
            return add

        self._op(fn)

    def _elevate(self):
        backend.elevate()
        self.app.close()
