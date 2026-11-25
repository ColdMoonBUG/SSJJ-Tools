# -*- coding: utf-8 -*-
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QCheckBox, QGridLayout, QHBoxLayout, QLabel,
                               QPushButton, QVBoxLayout)

import backend
from ui.widgets import Badge, Card, LogBox, Page, StatTile, dim, page_header


class AccelPage(Page):
    TITLE = "加速服务"

    def __init__(self, app):
        super().__init__(app)
        self._busy = False
        self._refreshing = False

        root = QVBoxLayout(self)
        root.setContentsMargins(32, 24, 32, 26)
        root.setSpacing(14)

        self.badge = Badge("检查中")
        root.addWidget(page_header(
            "本地加速（可选 · 默认关闭）",
            "统一总开关：一键开启自动处理 hosts、代理守护与可选的 HTTPS 根证书；一键关闭彻底还原。对换肤无帮助，不需要可保持关闭。",
            extra=self.badge))

        tiles = QHBoxLayout()
        tiles.setSpacing(12)
        self.tile_hit = StatTile("本地命中", "0", "\uE7FC")
        self.tile_rate = StatTile("命中率", "--")
        self.tile_miss = StatTile("回源", "0", "\uE774")
        self.tile_https = StatTile("HTTPS 透传/命中", "0", "\uE8A7")
        for t in (self.tile_hit, self.tile_rate, self.tile_miss, self.tile_https):
            tiles.addWidget(t)
        root.addLayout(tiles)

        info = Card("统一开关与状态", "不用分开装证书、改配置、启服务，这里一个按钮全部搞定")
        g = QGridLayout()
        g.setHorizontalSpacing(20)
        g.setVerticalSpacing(9)
        self.lb_hosts = QLabel("--")
        self.lb_ca = QLabel("--")
        self.lb_up = QLabel("--")
        self.lb_lib = QLabel("--")
        self.lb_save = QLabel("--")
        self.lb_bytes = QLabel("--")
        rows = (("hosts", self.lb_hosts), ("HTTPS 证书", self.lb_ca),
                ("回源节点", self.lb_up), ("资源库", self.lb_lib),
                ("运行期新存", self.lb_save), ("总流量", self.lb_bytes))
        for i, (k, v) in enumerate(rows):
            lab = dim(k)
            lab.setObjectName("Dim")
            g.addWidget(lab, i // 2, (i % 2) * 2)
            g.addWidget(v, i // 2, (i % 2) * 2 + 1)
        g.setColumnStretch(1, 1)
        g.setColumnStretch(3, 1)
        info.add_layout(g)

        opt_row = QHBoxLayout()
        opt_row.setSpacing(10)
        self.chk_https = QCheckBox("同时加速 HTTPS 资源（开启时自动安装本地根证书，不需要可不勾）")
        self.chk_https.setChecked(False)
        opt_row.addWidget(self.chk_https)
        opt_row.addStretch(1)
        info.add_layout(opt_row)

        btns = QHBoxLayout()
        btns.setSpacing(8)
        self.btn_start = QPushButton("一键开启本地加速")
        self.btn_start.setObjectName("Primary")
        self.btn_stop = QPushButton("一键关闭并还原")
        self.btn_stop.setObjectName("Danger")
        self.btn_del_ca = QPushButton("卸载根证书")
        self.btn_refresh = QPushButton("刷新")
        for b in (self.btn_start, self.btn_stop, self.btn_del_ca, self.btn_refresh):
            btns.addWidget(b)
        btns.addStretch(1)
        self.btn_start.clicked.connect(self._start)
        self.btn_stop.clicked.connect(self._stop)
        self.btn_del_ca.clicked.connect(self._uninstall_ca)
        self.btn_refresh.clicked.connect(self.refresh)
        info.add_layout(btns)
        root.addWidget(info)

        log_card = Card("运行日志")
        self.chk_auto = QCheckBox("自动刷新")
        self.chk_auto.setChecked(True)
        log_card.add_header_widget(self.chk_auto)
        open_btn = QPushButton("打开日志")
        open_btn.clicked.connect(lambda: backend.open_path(backend.C.LOG))
        log_card.add_header_widget(open_btn)
        self.log_view = LogBox()
        log_card.add(self.log_view, 1)
        root.addWidget(log_card, 1)

        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self._tick)

    # ------------------------------------------------------------ 状态
    def on_show(self):
        self.timer.start()
        self.refresh()
        self._load_log()

    def on_hide(self):
        self.timer.stop()

    def _tick(self):
        if self.chk_auto.isChecked() and self.isVisible():
            self.refresh()
            self._load_log()

    def _load_log(self):
        self.log_view.set_text(backend.log_tail(200))

    def refresh(self):
        if self._refreshing:
            return
        self._refreshing = True

        def job(_w):
            running = backend.accel_running()
            stats = backend.accel_stats() if running else None
            _rows, n, b = backend.cache_stats(ttl=30)
            return {"running": running, "stats": stats or {}, "n": n, "b": b,
                    "up": backend.upstream_info(), "hosts": backend.hosts_marked(),
                    "ca": backend.accel_ca_status()}

        self.run(job, on_done=self._apply)

    def _apply(self, d):
        self._refreshing = False
        if not d:
            return
        running = d["running"]
        ca = d.get("ca") or {}
        ca_ok = bool(ca.get("ca_installed"))
        mitm_on = bool(ca.get("mitm_enabled"))
        self.badge.setText(("运行中（含 HTTPS）" if mitm_on else "运行中") if running else "已关闭")
        self.badge.state("ok" if running else "")
        self.lb_hosts.setText("127.0.0.1 cdn.4399ssjj.com" if d["hosts"] else "未写入（直连官方）")
        self.lb_ca.setText("已安装（HTTPS 加速%s）" % ("开" if mitm_on else "关") if ca_ok else "未安装（默认无需安装）")
        up = d.get("up") or {}
        ip = up.get("ip") or "自动解析"
        rate = up.get("rate")
        self.lb_up.setText("%s%s" % (ip, (" · %.0f KB/s" % (rate / 1024)) if rate else ""))
        self.lb_lib.setText("%s / %d 个文件" % (backend.human(d["b"]), d["n"]))

        s = d.get("stats") or {}
        hit, miss = s.get("hit", 0), s.get("miss", 0)
        self.tile_hit.set(str(hit))
        self.tile_miss.set(str(miss))
        self.tile_rate.set(("%.1f%%" % (100.0 * hit / (hit + miss))) if hit + miss else "--")
        self.tile_https.set(str(s.get("https", 0) + s.get("https_mitm", 0)))
        self.lb_save.setText("%d 个 · %.1f MB" % (s.get("save", 0), s.get("saved_mb", 0.0)))
        self.lb_bytes.setText("%.1f MB" % (s.get("bytes", 0) / 1048576.0))

        self.btn_start.setEnabled(not self._busy)
        self.btn_stop.setEnabled(not self._busy and (running or bool(d.get("hosts")) or mitm_on))
        self.btn_del_ca.setEnabled(not self._busy and ca_ok)

    # ------------------------------------------------------------ 动作
    def _busy_op(self, fn, name):
        if self._busy:
            return
        self._busy = True
        for b in (self.btn_start, self.btn_stop, self.btn_del_ca):
            b.setEnabled(False)

        def job(_w):
            return fn()

        def done(res):
            self._busy = False
            if res:
                self.log_view.line("[*] " + str(res[1]))
            self.refresh()
            self._load_log()

        self.run(job, on_done=done)
        self.log_view.line("… %s" % name)

    def _start(self):
        with_https = self.chk_https.isChecked()
        self._busy_op(lambda: backend.accel_unified_enable(with_https=with_https),
                      "一键开启本地加速" + ("（含 HTTPS 证书）" if with_https else ""))

    def _stop(self):
        self._busy_op(backend.accel_unified_disable, "一键关闭并还原")

    def _uninstall_ca(self):
        self._busy_op(backend.accel_uninstall_ca, "卸载根证书")
