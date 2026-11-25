# -*- coding: utf-8 -*-
"""风险模式页：总开关、战斗端内存注入与过检测、HTTPS 全量本地化。"""
from PySide6.QtWidgets import (QCheckBox, QComboBox, QGridLayout, QHBoxLayout, QLabel,
                               QPushButton, QVBoxLayout)

import backend
from ui.widgets import Badge, Card, LogBox, ScrollPage, dim, page_header


def _short(text, head=30, tail=8):
    text = text or ""
    if len(text) <= head + tail + 1:
        return text
    return text[:head] + "…" + text[-tail:]


def _fmt_mb(n):
    try:
        n = float(n)
    except Exception:
        return "0 MB"
    if n >= 1048576 * 1024:
        return "%.2f GB" % (n / 1073741824.0)
    return "%.1f MB" % (n / 1048576.0)


class RiskPage(ScrollPage):
    TITLE = "风险"

    def __init__(self, app):
        super().__init__(app)
        self._busy = False
        self._st = {}

        self.root.addWidget(page_header(
            "换肤与注入",
            "换肤只需要「只开注入守护」：冷加载、进程内不留常驻线程。本地加速与资源缓存默认全关，想用再在下面单独打开。"))

        # ------------------------------------------------ 总开关 + 注入
        cols = QHBoxLayout()
        cols.setSpacing(14)

        main = Card("总开关（换肤 + 检测降级）", "开一次就好：注入守护 + 检测降级 + 皮肤/建模替换引擎。不含本地加速。", glyph="\uE7BA")
        self.badge_main = Badge("未开启")
        main.add_header_widget(self.badge_main)

        g = QGridLayout()
        g.setHorizontalSpacing(16)
        g.setVerticalSpacing(9)
        self.lb_mode = QLabel("--")
        self.lb_guard = QLabel("--")
        self.lb_proc = QLabel("--")
        self.lb_hit = QLabel("--")
        for i, (k, v) in enumerate((("模式", self.lb_mode),
                                    ("注入守护", self.lb_guard),
                                    ("战斗端进程", self.lb_proc),
                                    ("本地命中率", self.lb_hit))):
            lab = QLabel(k)
            lab.setObjectName("Dim")
            g.addWidget(lab, i, 0)
            g.addWidget(v, i, 1)
        g.setColumnStretch(1, 1)
        main.add_layout(g)

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addStretch(1)
        self.btn_on = QPushButton("开启风险模式")
        self.btn_on.setObjectName("Primary")
        self.btn_off = QPushButton("全部关闭并还原")
        self.btn_off.setObjectName("Danger")
        row.addWidget(self.btn_on)
        row.addWidget(self.btn_off)
        main.add_layout(row)

        self.lb_main_msg = dim("", small=True)
        self.lb_main_msg.setWordWrap(True)
        main.add(self.lb_main_msg)
        cols.addWidget(main, 1)

        inj = Card("注入与过检测", "内存注入，不落盘、不建窗口，同时驱动武器皮肤与建模替换。",
                   glyph="\uE945")
        self.badge_inj = Badge("检查中")
        inj.add_header_widget(self.badge_inj)

        g2 = QGridLayout()
        g2.setHorizontalSpacing(16)
        g2.setVerticalSpacing(9)
        self.lb_payload = QLabel("--")
        self.lb_pids = QLabel("--")
        self.lb_admin = QLabel("--")
        self.lb_admin.setWordWrap(True)
        self.lb_loader = QLabel("--")
        self.lb_last = QLabel("--")
        self.lb_last.setWordWrap(True)
        for i, (k, v) in enumerate((("载荷", self.lb_payload),
                                    ("已注入 pid", self.lb_pids),
                                    ("权限", self.lb_admin),
                                    ("加载方式", self.lb_loader),
                                    ("最近事件", self.lb_last))):
            lab = QLabel(k)
            lab.setObjectName("Dim")
            g2.addWidget(lab, i, 0)
            g2.addWidget(v, i, 1)
        g2.setColumnStretch(1, 1)
        inj.add_layout(g2)

        # 只开注入：不碰总开关、不碰证书、不碰大厅，单给一条路
        row1 = QHBoxLayout()
        row1.setSpacing(8)
        self.btn_inj_on = QPushButton("只开注入守护")
        self.btn_inj_on.setObjectName("Primary")
        self.btn_inj_off = QPushButton("停止注入守护")
        self.btn_elevate = QPushButton("以管理员重启")
        row1.addWidget(self.btn_inj_on)
        row1.addWidget(self.btn_inj_off)
        row1.addStretch(1)
        row1.addWidget(self.btn_elevate)
        inj.add_layout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(8)
        self.chk_cold = QCheckBox("冷加载（规则进场只应用一次，不每 500ms 重建）")
        self.chk_cold.setChecked(True)
        self.btn_build = QPushButton("重新编译载荷")
        self.btn_inject = QPushButton("立即注入一次")
        row2.addWidget(self.chk_cold)
        row2.addStretch(1)
        row2.addWidget(self.btn_build)
        row2.addWidget(self.btn_inject)
        inj.add_layout(row2)

        row3 = QHBoxLayout()
        row3.setSpacing(8)
        lab_ace = QLabel("ACE 策略")
        lab_ace.setObjectName("Dim")
        self.cmb_ace = QComboBox()
        self.cmb_ace.addItem("none · 完全不碰 ACE（默认，客户端痕迹最少）", "none")
        self.cmb_ace.addItem("teardown · 只拦退出清理，保留登录/上报", "teardown")
        self.cmb_ace.addItem("full · 连上报一起打掉（会被服务端风控）", "full")
        row3.addWidget(lab_ace)
        row3.addWidget(self.cmb_ace, 1)
        inj.add_layout(row3)

        self.lb_inj_msg = dim("", small=True)
        self.lb_inj_msg.setWordWrap(True)
        inj.add(self.lb_inj_msg)
        inj.add(dim("注入对象是战斗端 SSJJ_BattleClient_Unity.exe；守护会等 mono 运行时加载完自动注入。"
                    "冷加载下，改动外观规则需要重进一局才生效。", small=True))
        cols.addWidget(inj, 1)
        self.root.addLayout(cols)

        # 本地加速与根证书已统一收口到左侧「本地加速」页，这里只聚焦换肤注入与载荷。

        # ------------------------------------------------ 日志
        logc = Card("风险模式日志", None, glyph="\uE9D9")
        self.log = LogBox()
        self.log.setFixedHeight(190)
        logc.add(self.log)
        row6 = QHBoxLayout()
        row6.setSpacing(8)
        row6.addStretch(1)
        self.btn_log = QPushButton("刷新日志")
        row6.addWidget(self.btn_log)
        logc.add_layout(row6)
        self.root.addWidget(logc)

        notes = Card("技术说明")
        for text in (
            "注入链：mono_image_open_from_data_with_name → assembly_load_from_full → "
            "runtime_invoke，程序集仅存在于目标进程内存，PEB 模块链不可见。",
            "托管侧核心：停掉 RuntimeCheatGuard 巡检线程、将 AceClientManager 开关打回 false，"
            "并启动武器皮肤/建模实时替换守护线程。",
            "武器替换只修改客户端纯视觉建模、动画、特效、音效与皮肤名，不改伤害/射速/后坐力等任何战斗数值。",
        ):
            lab = QLabel("·  " + text)
            lab.setObjectName("Dim")
            lab.setWordWrap(True)
            notes.add(lab)
        self.root.addWidget(notes)
        self.root.addStretch(1)

        self.btn_on.clicked.connect(self._enable)
        self.btn_off.clicked.connect(self._disable)
        self.btn_build.clicked.connect(self._build)
        self.btn_inject.clicked.connect(self._inject)
        self.btn_inj_on.clicked.connect(self._inj_on)
        self.btn_inj_off.clicked.connect(self._inj_off)
        self.btn_elevate.clicked.connect(self._elevate)
        self.chk_cold.toggled.connect(self._cold_toggled)
        self.cmb_ace.currentIndexChanged.connect(self._ace_changed)
        self.btn_log.clicked.connect(self._refresh_log)

    # ------------------------------------------------------------ 状态
    def on_show(self):
        self.refresh()

    def _set_busy(self, busy):
        self._busy = busy
        for b in (self.btn_on, self.btn_off, self.btn_build, self.btn_inject,
                  self.btn_inj_on, self.btn_inj_off, self.btn_elevate):
            b.setEnabled(not busy)

    def refresh(self):
        def job(_w):
            return {"st": backend.risk_status(),
                    "inj": backend.risk_inject_state(),
                    "ecs": backend.ecs_cache_state()}

        self.run(job, on_done=self._apply)

    def _apply(self, d):
        if not d:
            return
        st = d.get("st") or {}
        self._st = st
        if not st.get("ready"):
            self.badge_main.setText("risk 包不可用")
            self.badge_main.state("bad")
            self.lb_main_msg.setText(st.get("error") or "")
            return

        on = bool(st.get("enabled"))
        self.badge_main.setText("已开启" if on else "未开启")
        self.badge_main.state("ok" if on else "")
        self.lb_mode.setText("风险模式" if on else "普通模式")
        self.lb_guard.setText("运行中" if st.get("thread_alive") else "停止")
        procs = st.get("processes") or []
        if procs:
            self.lb_proc.setText("、".join("pid %d" % p["pid"] for p in procs))
        else:
            self.lb_proc.setText("未运行")
        loc = st.get("localized") or {}
        self.lb_hit.setText("%.1f%%（命中 %d / 回源 %d）" % (loc.get("rate", 0.0),
                                                          loc.get("hit", 0),
                                                          loc.get("miss", 0)))

        # 注入卡
        pay = st.get("payload") or {}
        if pay.get("built"):
            self.badge_inj.setText("载荷就绪")
            self.badge_inj.state("ok")
            self.lb_payload.setText("%d 字节 · %s" % (pay.get("size", 0),
                                                      _short(pay.get("path") or "", 34, 12)))
        else:
            self.badge_inj.setText("载荷未编译")
            self.badge_inj.state("warn")
            self.lb_payload.setText("点「重新编译载荷」")
        pids = st.get("injected_pids") or []
        self.lb_pids.setText("、".join(str(p) for p in pids) if pids else "无")
        inj = d.get("inj") or {}
        admin = bool(inj.get("admin", (st.get("mitm") or {}).get("admin")))
        if admin:
            self.lb_admin.setText("管理员（SeDebugPrivilege 已开）" if inj.get("debug_privilege")
                                  else "管理员")
        else:
            self.lb_admin.setText("普通权限 —— 注入时会请求提权，或点右侧「以管理员重启」")
        self.btn_elevate.setEnabled(not self._busy and not admin)
        cold = bool(inj.get("cold_swap", True))
        if self.chk_cold.isChecked() != cold:
            self.chk_cold.blockSignals(True)
            self.chk_cold.setChecked(cold)
            self.chk_cold.blockSignals(False)
        mode = inj.get("ace_mode") or "none"
        idx = max(0, self.cmb_ace.findData(mode))
        if self.cmb_ace.currentIndex() != idx:
            self.cmb_ace.blockSignals(True)
            self.cmb_ace.setCurrentIndex(idx)
            self.cmb_ace.blockSignals(False)
        self.lb_loader.setText("%s · ACE %s · 守护%s" % (
            "冷加载" if cold else "热加载",
            mode,
            "运行中" if inj.get("thread_alive") else "停止"))
        last = (st.get("last") or {}).get("text") or ""
        self.lb_last.setText(_short(last, 42, 12) or "--")
        self.lb_last.setToolTip(last)

        self._refresh_log()

    def _refresh_log(self):
        def job(_w):
            return backend.risk_log(120)

        def done(lines):
            self.log.set_text("\n".join(lines or []) or "（暂无日志）")

        self.run(job, on_done=done)

    # ------------------------------------------------------------ 动作
    def _enable(self):
        if self._busy:
            return
        self._set_busy(True)
        self.badge_main.setText("开启中…")
        self.badge_main.state("warn")

        def job(_w):
            return backend.risk_enable()

        def done(res):
            self._set_busy(False)
            ok, msgs = res if res else (False, ["没有输出"])
            self.lb_main_msg.setText("\n".join((msgs or [])[-6:]))
            self.refresh()

        self.run(job, on_done=done)

    def _disable(self):
        if self._busy:
            return
        self._set_busy(True)
        self.badge_main.setText("还原中…")
        self.badge_main.state("warn")

        def job(_w):
            return backend.risk_disable()

        def done(res):
            self._set_busy(False)
            ok, msgs = res if res else (False, ["没有输出"])
            self.lb_main_msg.setText("\n".join((msgs or [])[-6:]))
            self.refresh()

        self.run(job, on_done=done)

    def _build(self):
        if self._busy:
            return
        self._set_busy(True)
        self.badge_inj.setText("编译中…")
        self.badge_inj.state("warn")

        def job(_w):
            lines, _path = backend.risk_build_payload(force=True)
            return lines

        def done(lines):
            self._set_busy(False)
            self.lb_inj_msg.setText("\n".join((lines or [])[-5:]))
            self.refresh()

        self.run(job, on_done=done)

    def _inject(self):
        if self._busy:
            return
        self._set_busy(True)
        self.badge_inj.setText("注入中…")
        self.badge_inj.state("warn")

        def job(_w):
            return backend.risk_inject_now()

        def done(res):
            self._set_busy(False)
            ok, detail = res if res else (False, "没有输出")
            self.lb_inj_msg.setText(("成功" if ok else "未成功") + "：" +
                                    _short(str(detail), 120, 20))
            self.refresh()

        self.run(job, on_done=done)

    # ------------------------------------------------ 只开注入 / 提权
    def _inj_on(self):
        if self._busy:
            return
        self._set_busy(True)
        self.badge_inj.setText("启动中…")
        self.badge_inj.state("warn")

        def job(_w):
            return backend.risk_inject_start(elevate=False)

        def done(res):
            self._set_busy(False)
            ok, msg = res if res else (False, "没有输出")
            self.lb_inj_msg.setText(str(msg))
            self.refresh()

        self.run(job, on_done=done)

    def _inj_off(self):
        if self._busy:
            return
        self._set_busy(True)

        def job(_w):
            return (backend.risk_inject_stop(), "注入守护已停止")

        def done(res):
            self._set_busy(False)
            ok, msg = res if res else (False, "没有输出")
            self.lb_inj_msg.setText(str(msg))
            self.refresh()

        self.run(job, on_done=done)

    def _elevate(self):
        if self._busy:
            return
        self._set_busy(True)

        def job(_w):
            # 会弹一次 UAC；先把当前在跑的战斗端也一起注入了，省得重启后再点
            return backend.risk_elevate(reinject=True)

        def done(res):
            self._set_busy(False)
            ok, msg = res if res else (False, "没有输出")
            self.lb_inj_msg.setText(("已请求提权：" if ok else "提权未完成：") + str(msg))
            self.refresh()

        self.run(job, on_done=done)

    def _cold_toggled(self, checked):
        if self._busy:
            return
        try:
            backend.risk_inject_option('cold_swap', bool(checked))
        except Exception as e:
            self.lb_inj_msg.setText("保存失败：%r" % (e,))
            return
        self.lb_inj_msg.setText("已切换为%s：%s" % (
            "冷加载" if checked else "热加载",
            "规则进场应用一次，改配置要重进一局" if checked
            else "每 500ms 重建（会掉帧，也可能被服务端行为检测看见）"))

    def _ace_changed(self, idx):
        if self._busy:
            return
        mode = self.cmb_ace.itemData(idx) or 'none'
        try:
            backend.risk_inject_option('ace_mode', mode)
        except Exception as e:
            self.lb_inj_msg.setText("保存失败：%r" % (e,))
            return
        self.lb_inj_msg.setText(
            "ACE 策略=%s（下一次注入生效）" % mode)

    def _mitm(self, action):
        if self._busy:
            return
        self._set_busy(True)
        self.badge_net.setText("处理中…")
        self.badge_net.state("warn")

        def job(_w):
            return backend.risk_mitm(action)

        def done(res):
            self._set_busy(False)
            ok, lines = res if res else (False, ["没有输出"])
            self.lb_net_msg.setText("\n".join([str(x) for x in (lines or [])][-6:]))
            self.refresh()

        self.run(job, on_done=done)
