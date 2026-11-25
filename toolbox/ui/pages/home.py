# -*- coding: utf-8 -*-
from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QRadialGradient
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFrame, QHBoxLayout,
                               QLabel, QLineEdit, QPushButton, QVBoxLayout)

import backend
from ui.theme import ACCENT, ACCENT2, OK
from ui.widgets import (Badge, Card, ScrollPage, StatTile, dim, glyph_icon,
                        glyph_label, icon_badge, page_header)

TILES = (
    ("加速服务", "运行中", "\uE768", OK),
    ("本次命中率", "--", "\uE9D9", ACCENT),
    ("回源节点", "--", "\uE774", ACCENT2),
    ("本地资源库（大厅+战斗）", "--", "\uE896", ACCENT),
)

QUICK = (
    ("武器皮肤与建模替换（重点功能）", "look", "1274 把官方武器建模 + 475 款涂装自由替换，支持双阵营自动适配", "\uE790"),
    ("大厅与战斗帧率调校", "fps", "大厅 SWF 与战斗端 Unity 帧率上限分开设置", "\uE7FC"),
    ("本地资源库（效果一般，不推荐下载）", "res", "可选功能：本地缓存提速有限且占空间，一般无需下载", "\uE896"),
)


class HeroFrame(QFrame):
    """带柔和电光径向光晕与极细战术纹理的首页 Hero 横幅。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Hero")

    def paintEvent(self, ev):
        super().paintEvent(ev)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(1, 1, self.width() - 2, self.height() - 2), 17, 17)
        p.setClipPath(clip)

        g1 = QRadialGradient(QPointF(self.width() * 0.88, self.height() * 0.15),
                             max(240, self.width() * 0.42))
        g1.setColorAt(0.0, QColor(56, 189, 248, 42))
        g1.setColorAt(0.55, QColor(99, 102, 241, 18))
        g1.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.fillRect(self.rect(), g1)

        cx, cy = self.width() - 90, self.height() * 0.52
        pen = QPen(QColor(56, 189, 248, 22))
        pen.setWidthF(1.2)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        for rad in (52, 86, 120):
            p.drawEllipse(QPointF(cx, cy), rad, rad)
        p.drawLine(QPointF(cx - 135, cy), QPointF(cx + 135, cy))
        p.drawLine(QPointF(cx, cy - 135), QPointF(cx, cy + 135))
        p.end()


class QuickRow(QFrame):
    """带图标、标题、说明与箭头的快捷入口行。"""

    def __init__(self, title, desc, glyph, on_click, parent=None):
        super().__init__(parent)
        self.setObjectName("QuickRow")
        self.setCursor(Qt.PointingHandCursor)
        self._on_click = on_click

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(11)
        lay.addWidget(icon_badge(glyph, ACCENT, 28, 13))

        mid = QVBoxLayout()
        mid.setContentsMargins(0, 0, 0, 0)
        mid.setSpacing(2)
        lb_t = QLabel(title)
        lb_t.setStyleSheet("font-size: 13px; font-weight: 600; color: #f1f5f9;")
        lb_d = QLabel(desc)
        lb_d.setObjectName("DimSmall")
        mid.addWidget(lb_t)
        mid.addWidget(lb_d)
        lay.addLayout(mid, 1)

        lay.addWidget(glyph_label("\uE72A", "#64748b", 12))

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton and self._on_click:
            self._on_click()
        super().mousePressEvent(ev)


class GuideStepRow(QFrame):
    """带步骤编号、状态徽章与直达按钮的新手引导行。"""

    def __init__(self, step_num, title, desc, btn_text, on_click, parent=None):
        super().__init__(parent)
        self.setObjectName("QuickRow")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 9, 12, 9)
        lay.setSpacing(10)

        num = QLabel(str(step_num))
        num.setObjectName("TipNum")
        num.setAlignment(Qt.AlignCenter)
        num.setFixedSize(22, 22)
        lay.addWidget(num, 0, Qt.AlignVCenter)

        mid = QVBoxLayout()
        mid.setContentsMargins(0, 0, 0, 0)
        mid.setSpacing(2)
        lb_t = QLabel(title)
        lb_t.setStyleSheet("font-size: 13px; font-weight: 600; color: #f1f5f9;")
        lb_d = QLabel(desc)
        lb_d.setObjectName("DimSmall")
        lb_d.setWordWrap(True)
        mid.addWidget(lb_t)
        mid.addWidget(lb_d)
        lay.addLayout(mid, 1)

        self.badge = Badge("检查中")
        lay.addWidget(self.badge)

        self.btn = QPushButton(btn_text)
        self.btn.setObjectName("Chip")
        self.btn.setCursor(Qt.PointingHandCursor)
        self.btn.setFixedHeight(28)
        self.btn.clicked.connect(on_click)
        lay.addWidget(self.btn)

    def set_done(self, done, done_text="已完成", todo_text="待完成"):
        self.badge.setText(done_text if done else todo_text)
        self.badge.state("ok" if done else "warn")


class HomePage(ScrollPage):
    TITLE = "首页"

    def __init__(self, app):
        super().__init__(app)
        self._busy = False
        self._refreshing = False

        self.root.addWidget(page_header(
            "总览", "三步搞定：开换肤注入 → 选武器皮肤 → 一键直登进游戏。本地缓存与加速默认关闭，需要时再开。"))

        hero = HeroFrame()
        hl = QVBoxLayout(hero)
        hl.setContentsMargins(26, 18, 26, 18)
        hl.setSpacing(10)

        top_row = QHBoxLayout()
        top_row.setSpacing(10)
        eyebrow = QLabel("SSJJ TOOLBOX · 换肤优先")
        eyebrow.setObjectName("Eyebrow")
        top_row.addWidget(eyebrow)
        top_row.addStretch(1)
        self.badge_svc = Badge("本地加速：关闭")
        self.badge_hosts = Badge("换肤注入：检查中")
        self.badge_admin = Badge("权限：检查中")
        for b in (self.badge_svc, self.badge_hosts, self.badge_admin):
            top_row.addWidget(b)
        hl.addLayout(top_row)

        title = QLabel("选好皮肤，一键进游戏")
        title.setObjectName("HeroTitle")
        hl.addWidget(title)

        # ---------------- 一键登录账号区服条
        prof = backend.quick_login_load()
        form = QHBoxLayout()
        form.setSpacing(10)

        self.ed_user = QLineEdit(prof.get("username", ""))
        self.ed_user.setPlaceholderText("4399 账号")
        self.ed_user.setMinimumHeight(36)
        self.ed_user.setMinimumWidth(165)
        form.addWidget(self.ed_user, 2)

        self.ed_pwd = QLineEdit(prof.get("password", ""))
        self.ed_pwd.setPlaceholderText("登录密码")
        self.ed_pwd.setEchoMode(QLineEdit.Password)
        self.ed_pwd.setMinimumHeight(36)
        self.ed_pwd.setMinimumWidth(165)
        form.addWidget(self.ed_pwd, 2)

        self.btn_eye = QPushButton("显示")
        self.btn_eye.setObjectName("Chip")
        self.btn_eye.setCheckable(True)
        self.btn_eye.setFixedHeight(36)
        self.btn_eye.setCursor(Qt.PointingHandCursor)
        self.btn_eye.toggled.connect(self._toggle_pwd)
        form.addWidget(self.btn_eye)

        self.combo_srv = QComboBox()
        self.combo_srv.setMinimumHeight(36)
        self.combo_srv.setMinimumWidth(135)
        for name, sid in backend.quick_login_servers():
            self.combo_srv.addItem(name, sid)
        idx = self.combo_srv.findText(prof.get("server", "联通二区"))
        self.combo_srv.setCurrentIndex(idx if idx >= 0 else 4)
        form.addWidget(self.combo_srv, 1)

        self.chk_remember = QCheckBox("记住密码")
        self.chk_remember.setChecked(prof.get("remember", True))
        form.addWidget(self.chk_remember)

        self.btn_save = QPushButton("保存配置")
        self.btn_save.setMinimumHeight(36)
        self.btn_save.setCursor(Qt.PointingHandCursor)
        self.btn_save.clicked.connect(self._save_profile)
        form.addWidget(self.btn_save)
        hl.addLayout(form)

        # ---------------- 主操作按钮组
        actions = QHBoxLayout()
        actions.setSpacing(10)
        self.btn_quick = QPushButton("  官方启动并开启换肤")
        self.btn_quick.setObjectName("Primary")
        self.btn_quick.setIcon(glyph_icon("\uE768", "#ffffff", 14))
        self.btn_quick.setIconSize(QSize(14, 14))
        self.btn_quick.setCursor(Qt.PointingHandCursor)
        self.btn_quick.setMinimumHeight(42)
        self.btn_quick.setMinimumWidth(190)
        self.btn_quick.clicked.connect(self._quick_login_play)
        actions.addWidget(self.btn_quick)

        self.btn_look = QPushButton("去配武器皮肤")
        self.btn_look.clicked.connect(lambda: self.app.goto("look"))
        self.btn_play = QPushButton("普通启动游戏")
        self.btn_svc = QPushButton("开启本地加速（默认关闭）")
        self.btn_stop = QPushButton("停止加速并还原 hosts")
        self.btn_stop.setObjectName("Danger")
        self.btn_play.clicked.connect(self._start_and_play)
        self.btn_svc.clicked.connect(self._start_only)
        self.btn_stop.clicked.connect(self._stop)
        for b in (self.btn_look, self.btn_play, self.btn_svc, self.btn_stop):
            b.setMinimumHeight(42)
            b.setCursor(Qt.PointingHandCursor)
            actions.addWidget(b)
        actions.addStretch(1)
        hl.addLayout(actions)

        self.lb_hero_status = QLabel("")
        self.lb_hero_status.setStyleSheet("font-size: 12px; color: #38bdf8; padding-top: 2px;")
        self.lb_hero_status.setWordWrap(True)
        hl.addWidget(self.lb_hero_status)
        self.root.addWidget(hero)

        # ---------------- 状态指标卡
        tiles = QHBoxLayout()
        tiles.setSpacing(12)
        self.tile_svc = StatTile(*TILES[0])
        self.tile_hit = StatTile(*TILES[1])
        self.tile_up = StatTile(*TILES[2])
        self.tile_lib = StatTile(*TILES[3])
        for t in (self.tile_svc, self.tile_hit, self.tile_up, self.tile_lib):
            tiles.addWidget(t)
        self.root.addLayout(tiles)

        # ---------------- 新手引导 + 使用流程说明
        bottom = QHBoxLayout()
        bottom.setSpacing(14)

        guide = Card("新手引导 · 3 步换肤 + 1 个可选", "1→2→3 按顺序做；第 4 步只在想用本地加速时才需要", glyph="\uE73A")
        self.step_risk = GuideStepRow(
            1, "开启换肤注入（只需点一次）",
            "在「风险」页点「只开注入守护」，不用开总开关；之后每次开软件自动生效",
            "去开启注入", lambda: self.app.goto("risk"),
        )
        self.step_skin = GuideStepRow(
            2, "选武器皮肤 / 建模替换",
            "搜索武器并挑涂装，1274 把武器 + 双阵营自动适配，一条规则警匪通用",
            "去换皮肤", lambda: self.app.goto("look"),
        )
        self.step_play = GuideStepRow(
            3, "从官方入口进游戏",
            "点「官方启动并开启换肤」，再在游戏窗口登录并选区",
            "立即进游戏", self._quick_login_play,
        )
        self.step_ca = GuideStepRow(
            4, "（可选）本地加速 / 根证书",
            "默认关闭。想加速大厅资源时再去「风险」页装根证书开总开关，占磁盘较大",
            "去设置", lambda: self.app.goto("risk"),
        )
        for row in (self.step_risk, self.step_skin, self.step_play, self.step_ca):
            guide.add(row)
        bottom.addWidget(guide, 3)

        tips = Card("怎么用 · 常见问题", "只看这几条就够", glyph="\uE946")
        tip_items = (
            "换肤只需两步：「风险」页点「只开注入守护」→「武器皮肤」页选枪和涂装，然后正常进游戏即可。",
            "注入是冷加载：规则进场时应用一次就完事，进程里不留常驻线程，也不会周期性刷新。",
            "改完皮肤规则要重新进一局才生效（不做热更新，这是刻意选的更安全做法）。",
            "本地加速与资源缓存默认全关：只影响下载速度、占磁盘大，对换肤没有帮助；要用再去「风险」页开。",
            "账号密码用 Windows DPAPI 加密存在本机，只在登录时用；「记住密码」可随时取消。",
        )
        for idx, text in enumerate(tip_items, 1):
            r = QHBoxLayout()
            r.setSpacing(10)
            num = QLabel(str(idx))
            num.setObjectName("TipNum")
            num.setAlignment(Qt.AlignCenter)
            num.setFixedSize(20, 20)
            r.addWidget(num, 0, Qt.AlignTop)
            line = QLabel(text)
            line.setObjectName("Dim")
            line.setWordWrap(True)
            r.addWidget(line, 1)
            tips.add_layout(r)
        tips.add_stretch()
        bottom.addWidget(tips, 2)
        self.root.addLayout(bottom)
        self.root.addStretch(1)

        self.status = QLabel("")
        self.status.setObjectName("DimSmall")
        self.root.addWidget(self.status)

        self.timer = QTimer(self)
        self.timer.setInterval(3000)
        self.timer.timeout.connect(self.refresh)

    # ------------------------------------------------------------ 状态
    def on_show(self):
        self.timer.start()
        self.refresh()

    def on_hide(self):
        self.timer.stop()

    def refresh(self):
        if self._refreshing:
            return
        self._refreshing = True

        def job(_w):
            running = backend.accel_running()
            stats = backend.accel_stats() if running else None
            _rows, n, b = backend.cache_stats(ttl=60)
            _bd, bn, bb = backend.battle_cache_stats()
            try:
                rst = backend.risk_status() or {}
            except Exception:
                rst = {}
            try:
                scfg = backend.skin_load_rules() or {}
                skin_cnt = len([r for r in (scfg.get("rules") or []) if r.get("enabled", True)])
            except Exception:
                skin_cnt = 0
            prof = backend.quick_login_load() or {}
            try:
                inj = backend.risk_inject_state() or {}
            except Exception:
                inj = {}
            return {"running": running, "stats": stats, "n": n, "b": b,
                    "bn": bn, "bb": bb,
                    "up": backend.upstream_info(), "hosts": backend.hosts_marked(),
                    "admin": backend.is_admin(), "game": backend.game_installed(),
                    "ca_ok": bool((rst.get("mitm") or {}).get("ca_installed")),
                    "risk_on": bool(rst.get("enabled")),
                    "inj_on": bool(inj.get("enabled")),
                    "injected": list(inj.get("injected") or []),
                    "skin_cnt": skin_cnt,
                    "has_acc": bool(prof.get("username"))}

        self.run(job, on_done=self._apply_refresh)

    def _apply_refresh(self, d):
        self._refreshing = False
        if not d:
            return
        running = d["running"]
        inj_on = bool(d.get("inj_on"))
        self.badge_svc.setText("本地加速：运行中" if running else "本地加速：关闭")
        self.badge_svc.state("ok" if running else "")
        self.badge_hosts.setText(
            "换肤注入：运行中 · 已注入 %d" % len(d.get("injected") or []) if inj_on and d.get("injected")
            else ("换肤注入：运行中" if inj_on else "换肤注入：未开启"))
        self.badge_hosts.state("ok" if inj_on else "warn")
        self.badge_admin.setText("权限：管理员" if d["admin"] else "权限：普通用户")
        self.badge_admin.state("ok" if d["admin"] else "warn")

        self.tile_svc.set("运行中" if running else "已停止")
        stats = d.get("stats") or {}
        hit, miss = stats.get("hit", 0), stats.get("miss", 0)
        if hit + miss:
            self.tile_hit.set("%.1f%%" % (100.0 * hit / (hit + miss)))
        else:
            self.tile_hit.set("--")
        up = d.get("up") or {}
        self.tile_up.set(up.get("ip") or "自动")
        total_b = (d.get("b") or 0) + (d.get("bb") or 0)
        total_n = (d.get("n") or 0) + (d.get("bn") or 0)
        self.tile_lib.set("%s / %d" % (backend.human(total_b), total_n))
        self.tile_lib.setToolTip(
            "大厅库：%s（%d 个文件）\n战斗库：%s（%d 个 AssetBundle 包）"
            % (backend.human(d.get("b") or 0), d.get("n") or 0,
               backend.human(d.get("bb") or 0), d.get("bn") or 0))

        self.step_risk.set_done(inj_on, "已开启", "未开启")
        sc = int(d.get("skin_cnt") or 0)
        self.step_skin.set_done(sc > 0, "已配 %d 条" % sc, "未配置")
        self.step_play.set_done(bool(d.get("has_acc")), "已存账号", "待填账号")
        self.step_ca.set_done(bool(d.get("ca_ok")), "已装证书", "未启用（可选）")

        if not self._busy:
            if not d["game"]:
                self.status.setText("警告：没找到游戏启动器 %s" % backend.GAME_LAUNCHER)
            elif not inj_on:
                self.status.setText("换肤还没开启：去左侧「风险」页点「只开注入守护」即可（不需要开总开关）。")
            elif not d["admin"]:
                self.status.setText("提示：当前不是管理员，注入会失败，请右键用管理员身份运行。")

    # ------------------------------------------------------------ 动作
    def _toggle_pwd(self, checked):
        self.ed_pwd.setEchoMode(QLineEdit.Normal if checked else QLineEdit.Password)
        self.btn_eye.setText("隐藏" if checked else "显示")

    def _set_status(self, text, is_err=False):
        self.status.setText(text)
        self.lb_hero_status.setText(text)
        self.lb_hero_status.setStyleSheet(
            "font-size: 12px; color: %s; padding-top: 2px;"
            % ("#f87171" if is_err else "#38bdf8")
        )

    def _save_profile(self):
        ok, msg = backend.quick_login_save(
            self.ed_user.text().strip(),
            self.ed_pwd.text(),
            self.combo_srv.currentText(),
            remember=self.chk_remember.isChecked(),
        )
        self._set_status(msg, is_err=not ok)

    def _set_busy(self, busy):
        self._busy = busy
        for b in (self.btn_quick, self.btn_play, self.btn_svc, self.btn_stop, self.btn_save):
            b.setEnabled(not busy)

    def _quick_login_play(self):
        if self._busy:
            return
        self._set_busy(True)
        self._set_status("正在开启换肤并打开官方启动器…")

        def job(w):
            return backend.quick_login_launch(
                "", "", "", with_accel=False, emit=w.log.emit)

        def done(res):
            self._set_busy(False)
            if res:
                self._set_status(res[1], is_err=not res[0])
            else:
                self._set_status("一键登录异常，请重试", is_err=True)
            self.refresh()

        self.run(job, on_done=done, log=self._log_line)

    def _start_and_play(self):
        if self._busy:
            return
        self._set_busy(True)
        self.status.setText("正在启动游戏…")

        def job(w):
            ok, msg = backend.launch_game()
            w.log.emit(msg)
            return ok

        def done(ok):
            self._set_busy(False)
            self.status.setText("已拉起官方游戏启动器" if ok else "启动失败，请确认游戏已安装")
            self.refresh()

        self.run(job, on_done=done, log=self._log_line)

    def _start_only(self):
        if self._busy:
            return
        self._set_busy(True)

        def job(_w):
            return backend.accel_start()

        def done(res):
            self._set_busy(False)
            self.status.setText(res[1] if res else "启动失败")
            self.refresh()

        self.run(job, on_done=done)

    def _stop(self):
        if self._busy:
            return
        self._set_busy(True)

        def job(_w):
            return backend.accel_stop()

        def done(res):
            self._set_busy(False)
            self.status.setText(res[1] if res else "已停止")
            self.refresh()

        self.run(job, on_done=done)

    def _log_line(self, s):
        self._set_status(s)
