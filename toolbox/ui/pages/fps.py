# -*- coding: utf-8 -*-
from PySide6.QtWidgets import (QCheckBox, QComboBox, QGridLayout, QHBoxLayout,
                               QLabel, QPushButton, QSpinBox, QVBoxLayout)

import backend
from ui.widgets import Badge, Card, ScrollPage, dim, page_header

OPTIONS = [24, 30, 45, 60, 90, 120]
GAME_PRESETS = (60, 120, 144, 200, 240, 305, 400)


def _short(text, head=22, tail=6):
    text = text or ""
    if len(text) <= head + tail + 1:
        return text
    return text[:head] + "…" + text[-tail:]


class FpsPage(ScrollPage):
    TITLE = "帧率"

    def __init__(self, app):
        super().__init__(app)
        self._busy_hall = False
        self._busy_game = False
        self._game = {}

        self.root.addWidget(page_header(
            "帧率",
            "大厅（AIR/SWF）与战斗端（Unity）分开设置，互不影响。改战斗端无需插件。"))

        cols = QHBoxLayout()
        cols.setSpacing(14)

        # ------------------------------------------------ 大厅帧率
        hall = Card("大厅帧率", "只改大厅主 SWF 的帧率，不改任何游戏程序文件。",
                    glyph="\uE7FC")
        self.badge_hall = Badge("检查中")
        hall.add_header_widget(self.badge_hall)

        g = QGridLayout()
        g.setHorizontalSpacing(16)
        g.setVerticalSpacing(9)
        self.lb_name = QLabel("--")
        self.lb_name.setObjectName("DimSmall")
        self.lb_official = QLabel("--")
        self.lb_override = QLabel("--")
        self.lb_renamed = QLabel("--")
        for i, (k, v) in enumerate((("大厅主 SWF", self.lb_name),
                                    ("官方帧率", self.lb_official),
                                    ("当前覆盖", self.lb_override),
                                    ("文件名改写", self.lb_renamed))):
            lab = QLabel(k)
            lab.setObjectName("Dim")
            g.addWidget(lab, i, 0)
            g.addWidget(v, i, 1)
        g.setColumnStretch(1, 1)
        hall.add_layout(g)

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(QLabel("目标帧率"))
        self.combo = QComboBox()
        for v in OPTIONS:
            self.combo.addItem("%d fps" % v, v)
        self.combo.setCurrentIndex(OPTIONS.index(60))
        self.combo.setMinimumWidth(96)
        row.addWidget(self.combo)
        row.addStretch(1)
        self.btn_apply = QPushButton("应用")
        self.btn_apply.setObjectName("Primary")
        self.btn_off = QPushButton("关闭覆盖")
        self.btn_off.setObjectName("Danger")
        row.addWidget(self.btn_apply)
        row.addWidget(self.btn_off)
        hall.add_layout(row)

        row2 = QHBoxLayout()
        row2.setSpacing(8)
        self.chk_rename = QCheckBox("同时改写 paras.dat 文件名（推荐）")
        self.chk_rename.setChecked(True)
        self.chk_rename.setToolTip("客户端会拿文件名里的 md5 校验内容，改名后一次校验通过。")
        row2.addWidget(self.chk_rename)
        row2.addStretch(1)
        self.btn_restart = QPushButton("重启加速服务")
        row2.addWidget(self.btn_restart)
        hall.add_layout(row2)

        self.lb_msg = dim("", small=True)
        hall.add(self.lb_msg)
        cols.addWidget(hall, 1)

        # ------------------------------------------------ 游戏内帧率
        game = Card("游戏内帧率", "直接写战斗端的 Unity 设置（注册表），不改游戏文件。",
                    glyph="\uE768")
        self.badge_game = Badge("检查中")
        game.add_header_widget(self.badge_game)

        g2 = QGridLayout()
        g2.setHorizontalSpacing(16)
        g2.setVerticalSpacing(9)
        self.lb_gkey = QLabel("--")
        self.lb_gkey.setObjectName("DimSmall")
        self.lb_gkey.setWordWrap(True)
        self.lb_glimit = QLabel("--")
        self.lb_gvsync = QLabel("--")
        self.lb_grun = QLabel("--")
        for i, (k, v) in enumerate((("设置位置", self.lb_gkey),
                                    ("当前上限", self.lb_glimit),
                                    ("垂直同步", self.lb_gvsync),
                                    ("客户端状态", self.lb_grun))):
            lab = QLabel(k)
            lab.setObjectName("Dim")
            g2.addWidget(lab, i, 0)
            g2.addWidget(v, i, 1)
        g2.setColumnStretch(1, 1)
        game.add_layout(g2)

        row3 = QHBoxLayout()
        row3.setSpacing(8)
        row3.addWidget(QLabel("帧率上限"))
        self.spin_limit = QSpinBox()
        self.spin_limit.setRange(30, 500)
        self.spin_limit.setSingleStep(5)
        self.spin_limit.setValue(60)
        self.spin_limit.setSuffix(" fps")
        self.spin_limit.setMinimumWidth(110)
        row3.addWidget(self.spin_limit)
        self.chk_vsync = QCheckBox("关闭垂直同步")
        row3.addWidget(self.chk_vsync)
        row3.addStretch(1)
        self.btn_gapply = QPushButton("写入设置")
        self.btn_gapply.setObjectName("Primary")
        row3.addWidget(self.btn_gapply)
        game.add_layout(row3)

        preset_row = QHBoxLayout()
        preset_row.setSpacing(6)
        lb_pre = QLabel("常用：")
        lb_pre.setObjectName("DimSmall")
        preset_row.addWidget(lb_pre)
        for v in GAME_PRESETS:
            b = QPushButton(str(v))
            b.setObjectName("Chip")
            b.setFixedHeight(28)
            b.setToolTip("设为 %d fps" % v)
            b.clicked.connect(lambda _=False, x=v: self.spin_limit.setValue(x))
            preset_row.addWidget(b)
        preset_row.addStretch(1)
        game.add_layout(preset_row)

        self.lb_gmsg = dim("", small=True)
        game.add(self.lb_gmsg)
        game.add(dim("写入后需启动战斗端生效；客户端退出时可能回写设置，先改再进游戏。"
                     "大厅帧率与这里互不影响。", small=True))
        cols.addWidget(game, 1)
        self.root.addLayout(cols)


        # ------------------------------------------------ 大厅动效（缓动曲线）
        anim = Card("大厅动效（缓动曲线）",
                    "大厅转场/弹窗用 GreenSock 的 Back 过冲常数，改小就不回弹、更顺。"
                    "只换 CDN 下发的 SWF，不动磁盘上的客户端文件。", glyph="\uE790")
        self.badge_anim = Badge("检查中")
        anim.add_header_widget(self.badge_anim)
        ar = QHBoxLayout()
        ar.setSpacing(8)
        lb_e = QLabel("缓动风格")
        lb_e.setObjectName("Dim")
        ar.addWidget(lb_e)
        self.cmb_ease = QComboBox()
        self.cmb_ease.setMinimumWidth(150)
        try:
            for _name, _val in backend.risk_lobby_presets():
                self.cmb_ease.addItem("%s（%s）" % (_name, _val), _name)
        except Exception:
            pass
        _i = self.cmb_ease.findData("柔和")
        if _i >= 0:
            self.cmb_ease.setCurrentIndex(_i)
        ar.addWidget(self.cmb_ease)
        self.chk_anim_fps = QCheckBox("同时套用上面的帧率")
        self.chk_anim_fps.setChecked(True)
        self.chk_anim_fps.setToolTip("一次把「大厅帧率 + 缓动」都写进去；只想要缓动就取消勾选。")
        ar.addWidget(self.chk_anim_fps)
        ar.addStretch(1)
        self.btn_anim = QPushButton("应用动效")
        self.btn_anim.setObjectName("Primary")
        self.btn_anim_off = QPushButton("还原官方")
        ar.addWidget(self.btn_anim)
        ar.addWidget(self.btn_anim_off)
        anim.add_layout(ar)
        self.lb_anim = dim("", small=True)
        self.lb_anim.setWordWrap(True)
        anim.add(self.lb_anim)
        self.root.addWidget(anim)

        notes = Card("实测结论")
        for text in (
            "大厅 24fps（官方默认）空闲 CPU 约 1.4%；60fps 约 24.6%，涨 18 倍，视觉差别却很小。",
            "真正影响手感的是战斗端帧率；在大厅把帧率调高基本只增加功耗。",
            "战斗端上限由 Unity PlayerPrefs 的 FrameLimit 控制，本页直接写注册表，无需插件。",
            "大厅改 SWF 内容会触发 md5 校验，所以应用时会同时改写 paras.dat 里的文件名。",
        ):
            lab = QLabel("·  " + text)
            lab.setObjectName("Dim")
            lab.setWordWrap(True)
            notes.add(lab)
        self.root.addWidget(notes)
        self.root.addStretch(1)

        self.btn_apply.clicked.connect(self._apply_hall)
        self.btn_off.clicked.connect(self._off_hall)
        self.btn_restart.clicked.connect(self._restart)
        self.btn_gapply.clicked.connect(self._apply_game)
        self.btn_anim.clicked.connect(self._apply_anim)
        self.btn_anim_off.clicked.connect(self._off_anim)


    # ------------------------------------------------------------ 大厅动效
    def _apply_anim(self):
        ease = self.cmb_ease.currentData() or "柔和"
        fps = int(self.combo.currentData() or 0) if self.chk_anim_fps.isChecked() else 0
        self.btn_anim.setEnabled(False)
        self.lb_anim.setText("正在生成大厅变体…")

        def job(*_a):
            return backend.risk_lobby_apply(ease, fps, False)

        def done(res):
            self.btn_anim.setEnabled(True)
            ok, msgs = res if isinstance(res, tuple) else (False, [str(res)])
            text = "\n".join(str(m) for m in (msgs or []))
            self.lb_anim.setText(("[OK] " if ok else "[!] ") + (text or ("已应用" if ok else "失败")))
            self.refresh()

        self.run(job, on_done=done)

    def _off_anim(self):
        self.btn_anim_off.setEnabled(False)

        def job(*_a):
            return backend.risk_lobby_restore()

        def done(res):
            self.btn_anim_off.setEnabled(True)
            ok, msgs = res if isinstance(res, tuple) else (False, [str(res)])
            self.lb_anim.setText("[OK] " + "\n".join(str(m) for m in (msgs or []) or ["已还原官方大厅"]))
            self.refresh()

        self.run(job, on_done=done)

    # ------------------------------------------------------------ 状态
    def on_show(self):
        self.refresh()

    def refresh(self):
        def job(_w):
            return {"hall": backend.fps_state(), "game": backend.game_fps_state(),
                    "anim": backend.risk_lobby_inspect()}

        self.run(job, on_done=self._apply)

    def _apply(self, d):
        if not d:
            return
        st = d.get("hall") or {}
        name = st.get("name") or ""
        self.lb_name.setText(_short(name) if name else "（paras.dat 里没有）")
        self.lb_name.setToolTip(name)
        self.lb_official.setText(("%d fps" % st["official"]) if st.get("official") else
                                 "读不到（先启动一次加速服务）")
        if st.get("override"):
            self.lb_override.setText("%d fps" % st["override"])
            self.badge_hall.setText("已覆盖")
            self.badge_hall.state("ok")
            if st["override"] in OPTIONS:
                self.combo.setCurrentIndex(OPTIONS.index(st["override"]))
        else:
            official = st.get("official")
            self.lb_override.setText("未启用（官方 %s）" % (("%d fps" % official) if official else "默认"))
            self.badge_hall.setText("官方设置")
            self.badge_hall.state("")
        self.lb_renamed.setText("已改写，一次校验通过" if st.get("renamed") else "未改写")

        self._apply_game_state(d.get("game") or {})

    def _apply_game_state(self, st):
        self._game = st or {}
        found = st.get("found")
        limit = st.get("limit")
        vsync = st.get("vsync")
        running = st.get("running")
        self.lb_gkey.setText(_short(st.get("key") or "未找到", 26, 10))
        self.lb_gkey.setToolTip(st.get("key") or "")
        self.lb_glimit.setText(("%d fps" % limit) if limit else "未生成")
        self.lb_gvsync.setText("--" if vsync is None else ("关闭" if vsync is False else "开启"))
        self.lb_grun.setText("运行中（退出后再改）" if running else "已关闭")
        if found:
            self.badge_game.setText("已接入")
            self.badge_game.state("ok")
            if limit and 30 <= limit <= 500:
                self.spin_limit.setValue(limit)
            self.chk_vsync.setChecked(vsync is False)
        else:
            self.badge_game.setText("未生成设置")
            self.badge_game.state("warn")
        self.btn_gapply.setEnabled(bool(found) and not running and not self._busy_game)
        self.spin_limit.setEnabled(bool(found))

    # ------------------------------------------------------------ 大厅动作
    def _set_hall_busy(self, busy):
        self._busy_hall = busy
        for b in (self.btn_apply, self.btn_off, self.btn_restart):
            b.setEnabled(not busy)

    def _apply_hall(self):
        if self._busy_hall:
            return
        fps = self.combo.currentData()
        rename = self.chk_rename.isChecked()
        self._set_hall_busy(True)
        self.badge_hall.setText("正在生成…")
        self.badge_hall.state("warn")

        def job(_w):
            return backend.fps_set(fps, rename)

        def done(lines):
            self._set_hall_busy(False)
            self.lb_msg.setText("\n".join((lines or [])[-4:]) or "没有输出")
            self.refresh()

        self.run(job, on_done=done)

    def _restart(self):
        if self._busy_hall:
            return
        self._set_hall_busy(True)
        self.badge_hall.setText("重启中…")
        self.badge_hall.state("warn")

        def job(_w):
            return backend.accel_restart()

        def done(_r):
            self._set_hall_busy(False)
            self.refresh()

        self.run(job, on_done=done)

    def _off_hall(self):
        if self._busy_hall:
            return
        self._set_hall_busy(True)

        def job(_w):
            return backend.fps_off()

        def done(lines):
            self._set_hall_busy(False)
            self.lb_msg.setText("\n".join((lines or [])[-3:]) or "已关闭覆盖")
            self.refresh()

        self.run(job, on_done=done)

    # ------------------------------------------------------------ 游戏内动作
    def _apply_game(self):
        if self._busy_game:
            return
        limit = self.spin_limit.value()
        vsync_off = self.chk_vsync.isChecked()
        self._busy_game = True
        self.btn_gapply.setEnabled(False)

        def job(_w):
            return backend.game_fps_set(limit=limit, vsync_off=vsync_off)

        def done(res):
            self._busy_game = False
            ok, msg = res if res else (False, "写入失败")
            self.lb_gmsg.setText(msg)
            self.refresh()

        self.run(job, on_done=done)
