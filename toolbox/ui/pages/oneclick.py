# -*- coding: utf-8 -*-
"""一键启动页：小白只需要「选武器 → 选皮肤 → 点一下」。

武器/皮肤的选择方式跟「武器皮肤」页一致：分类 + 搜索 + 下拉，不用记英文代号。
换肤只走注入：规则写进 skin_swap.json，战斗端里的载荷冷加载一次改材质，
不在进程里留常驻线程，退出时把改过的代码首字节还原。
"""
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox,
                               QGridLayout, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QPushButton, QSpinBox, QTableWidget,
                               QTableWidgetItem, QVBoxLayout)

import backend
import oneclick
import quick_login
import skin_pack
from ui.widgets import Badge, Card, LogBox, ScrollPage, dim, page_header


class OneclickPage(ScrollPage):
    TITLE = "一键启动"

    @staticmethod
    def _dbg(text):
        try:
            import os
            base = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "ssjj-toolbox")
            os.makedirs(base, exist_ok=True)
            with open(os.path.join(base, "ui_oneclick.log"), "a", encoding="utf-8") as f:
                f.write(text + "\n")
        except Exception:
            pass

    def __init__(self, app):
        super().__init__(app)
        self._busy = False
        self._catalog = {"weapons": [], "skins": []}
        self._skin_names = {}      # 皮肤包 code -> 中文名
        self._skins = []           # 当前武器的 [(短名, 显示名)]
        self._rules = []           # 与「武器皮肤」页共用的规则表

        self.root.addWidget(page_header(
            "一键启动",
            "选武器 → 选要借用的皮肤 → 点一下。换肤走注入：规则写进配置，战斗端进场时应用一次，退出自动还原。"))

        # ------------------------------------------------ 状态徽章
        line = QHBoxLayout()
        line.setSpacing(8)
        self.badge_mode = Badge("注入换肤")
        self.badge_applied = Badge("还没换过")
        self.badge_admin = Badge("权限：--")
        line.addWidget(self.badge_mode)
        line.addWidget(self.badge_applied)
        line.addWidget(self.badge_admin)
        line.addStretch(1)
        holder = Card()
        holder.add_layout(line)
        self.root.addWidget(holder)

        # ------------------------------------------------ 外观（跟「武器皮肤」页同一套选择方式）
        look = Card("③ 选外观", "皮肤包里的贴图直接换到武器上：左边那把枪不需要拥有皮肤，也能换色。"
                "靠注入在战斗端里改材质，冷加载只应用一次。", glyph="\uE790")
        self.badge_look = Badge("")
        self.badge_rule = Badge("")
        look.add_header_widget(self.badge_look)
        look.add_header_widget(self.badge_rule)

        # 第 1 行：分类 + 搜索 + 武器
        r1 = QHBoxLayout()
        r1.setSpacing(8)
        lb1 = QLabel("武器")
        lb1.setFixedWidth(56)
        lb1.setObjectName("Dim")
        r1.addWidget(lb1)
        self.cmb_cat = QComboBox()
        self.cmb_cat.setFixedWidth(110)
        self.cmb_cat.currentIndexChanged.connect(self._refill_weapons)
        r1.addWidget(self.cmb_cat)
        self.ed_weapon_q = QLineEdit()
        self.ed_weapon_q.setPlaceholderText("搜武器（中文名或代号，如 AK47 / ak47）")
        self.ed_weapon_q.setFixedWidth(230)
        self.ed_weapon_q.textChanged.connect(self._refill_weapons)
        r1.addWidget(self.ed_weapon_q)
        self.cmb_weapon = QComboBox()
        self.cmb_weapon.currentIndexChanged.connect(self._weapon_changed)
        r1.addWidget(self.cmb_weapon, 1)
        look.add_layout(r1)

        # 第 2 行：分类 + 搜索 + 目标建模（与原武器 1:1 对齐，不换建模就留「保持原建模」）
        r2 = QHBoxLayout()
        r2.setSpacing(8)
        lb2 = QLabel("建模")
        lb2.setFixedWidth(56)
        lb2.setObjectName("Dim")
        r2.addWidget(lb2)
        self.cmb_model_cat = QComboBox()
        self.cmb_model_cat.setFixedWidth(110)
        self.cmb_model_cat.currentIndexChanged.connect(self._refill_models)
        r2.addWidget(self.cmb_model_cat)
        self.ed_model_q = QLineEdit()
        self.ed_model_q.setPlaceholderText("搜建模（中文名或代号，留空可选不改建模）")
        self.ed_model_q.setFixedWidth(230)
        self.ed_model_q.textChanged.connect(self._refill_models)
        r2.addWidget(self.ed_model_q)
        self.cmb_model = QComboBox()
        self.cmb_model.addItem("保持原建模（只换皮肤）", "")
        self.cmb_model.currentIndexChanged.connect(self._model_changed)
        r2.addWidget(self.cmb_model, 1)
        look.add_layout(r2)

        # 第 3 行：皮肤筛选 + 皮肤 + 加入规则
        r3 = QHBoxLayout()
        r3.setSpacing(8)
        lb3 = QLabel("皮肤")
        lb3.setFixedWidth(56)
        lb3.setObjectName("Dim")
        r3.addWidget(lb3)
        self.ed_skin_q = QLineEdit()
        self.ed_skin_q.setPlaceholderText("筛选皮肤（留空显示当前武器全部官方涂装）")
        self.ed_skin_q.setFixedWidth(348)
        self.ed_skin_q.textChanged.connect(self._refill_skins)
        r3.addWidget(self.ed_skin_q)
        self.cmb_skin = QComboBox()
        r3.addWidget(self.cmb_skin, 1)
        self.btn_add_rule = QPushButton("加入规则")
        self.btn_add_rule.setToolTip("把当前选的武器 + 建模/皮肤存成一条规则，不用启动游戏")
        self.btn_add_rule.setMinimumHeight(34)
        r3.addWidget(self.btn_add_rule)
        look.add_layout(r3)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.lb_look = dim("", small=True)
        self.lb_look.setWordWrap(True)
        row.addWidget(self.lb_look, 1)
        # 还原按钮统一放到下面的「当前规则」卡里，这里不再重复放一个
        look.add_layout(row)
        self.root.addWidget(look)

        # ------------------------------------------------ 当前规则（与「武器皮肤」页同一份）
        rules_card = Card("④ 当前规则", "这边选了什么就存什么，双击一行可直接回到那套皮肤。",
                          glyph="\uE8FD")
        self.badge_rules = Badge("")
        rules_card.add_header_widget(self.badge_rules)
        self.tbl = QTableWidget(0, 4)
        self.tbl.setHorizontalHeaderLabels(["方式", "武器", "皮肤", "建模"])
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tbl.setAlternatingRowColors(True)
        self.tbl.setMinimumHeight(140)
        hh = self.tbl.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Fixed)
        self.tbl.setColumnWidth(0, 96)
        for c in (1, 2, 3):
            hh.setSectionResizeMode(c, QHeaderView.Stretch)
        self.tbl.cellDoubleClicked.connect(self._pick_row)
        rules_card.add(self.tbl)
        rrow = QHBoxLayout()
        rrow.setSpacing(8)
        self.btn_del_rule = QPushButton("删除选中规则")
        self.btn_reset_files = QPushButton("全部还原外观")
        self.btn_clear_rules = QPushButton("清空全部规则")
        self.btn_clear_rules.setObjectName("Danger")
        rrow.addWidget(self.btn_del_rule)
        rrow.addWidget(self.btn_reset_files)
        rrow.addWidget(self.btn_clear_rules)
        rrow.addStretch(1)
        rules_card.add_layout(rrow)
        self.root.addWidget(rules_card)

        # ------------------------------------------------ 账号
        acc = Card("① 账号配置（可选）",
                   "官方启动后请在游戏窗口登录并选区；这里的账号不会传给启动器。",
                   glyph="\uE77B")
        g2 = QGridLayout()
        g2.setHorizontalSpacing(14)
        g2.setVerticalSpacing(10)
        self.ed_user = QLineEdit()
        self.ed_user.setPlaceholderText("4399 账号")
        self.ed_pwd = QLineEdit()
        self.ed_pwd.setEchoMode(QLineEdit.Password)
        self.ed_pwd.setPlaceholderText("密码")
        self.cmb_server = QComboBox()
        self.cmb_server.setMinimumWidth(220)
        self.chk_remember = QCheckBox("记住账号和密码")
        self.chk_remember.setChecked(True)
        for i, (k, w) in enumerate((("账号", self.ed_user), ("密码", self.ed_pwd), ("区服", self.cmb_server))):
            lab = QLabel(k)
            lab.setObjectName("Dim")
            g2.addWidget(lab, i, 0)
            g2.addWidget(w, i, 1)
        g2.addWidget(self.chk_remember, 3, 1)
        g2.setColumnStretch(1, 1)
        acc.add_layout(g2)
        self.root.addWidget(acc)

        # ------------------------------------------------ 启动
        run = Card("② 一键启动", "写外观 → 开注入 → 官方启动器；在游戏窗口登录。", glyph="\uE768")
        row2 = QHBoxLayout()
        row2.setSpacing(10)
        self.btn_go = QPushButton("一键启动")
        self.btn_go.setObjectName("Primary")
        self.btn_go.setMinimumHeight(46)
        self.btn_plain = QPushButton("只打开官方启动器")
        row2.addWidget(self.btn_go, 2)
        row2.addWidget(self.btn_plain, 1)
        run.add_layout(row2)

        self.log = LogBox()
        self.log.setFixedHeight(180)
        run.add(self.log)
        self.root.addWidget(run)

        # ------------------------------------------------ 高级开关
        adv = Card("⑤ 高级开关（默认已调好，不懂别动）",
                   "冷加载＝进场应用一次、进程里不留线程；热加载＝每 500ms 重读配置，"
                   "对局里改规则立刻生效，但进程里多一个常驻线程。", glyph="\uE713")
        ar0 = QHBoxLayout()
        ar0.setSpacing(10)
        self.cmb_method = QComboBox()
        self.cmb_method.addItem("加载方式：代理 DLL（version.dll · 游戏启动时自加载，最隐蔽）", "proxy")
        self.cmb_method.addItem("加载方式：远程线程注入（会被 GameGuard 当场杀，仅调试用）", "thread")
        self.cmb_method.setMinimumWidth(430)
        self.cmb_method.setToolTip(
            "代理 DLL：往 battle\\*_64\\ 放一个 version.dll，战斗端加载器会先加载它，"
            "载荷在进程内自己进 mono。全程不 OpenProcess / 不建远程线程 / 不留匿名可执行页。")
        self.btn_proxy_deploy = QPushButton("部署代理")
        self.btn_proxy_remove = QPushButton("移除代理")
        self.lb_proxy = dim("", small=True)
        ar0.addWidget(self.cmb_method)
        ar0.addWidget(self.btn_proxy_deploy)
        ar0.addWidget(self.btn_proxy_remove)
        ar0.addWidget(self.lb_proxy, 1)
        adv.add_layout(ar0)

        ar1 = QHBoxLayout()
        ar1.setSpacing(12)
        self.chk_cold = QCheckBox("冷加载（推荐）")
        self.chk_cold.setToolTip("勾上：进对局时应用一次，之后线程退出。取消：热加载，每 500ms 重读配置。")
        self.cmb_ace = QComboBox()
        self.cmb_ace.addItem("ACE：none（完全不碰 ACE，最保守 · 默认）", "none")
        self.cmb_ace.addItem("ACE：teardown（只关退出路径，保留登录上报）", "teardown")
        self.cmb_ace.addItem("ACE：full（连登录/上报一起关，会被风控）", "full")
        self.cmb_ace.setMinimumWidth(330)
        self.chk_auto = QCheckBox("权限不足时自动弹 UAC")
        ar1.addWidget(self.chk_cold)
        ar1.addWidget(self.cmb_ace)
        ar1.addWidget(self.chk_auto)
        ar1.addStretch(1)
        adv.add_layout(ar1)

        ar1b = QHBoxLayout()
        ar1b.setSpacing(10)
        self.chk_exit = QCheckBox("退出前先还原补丁（推荐）")
        self.chk_exit.setToolTip(
            "勾上：拦截退出流程，先把改过的代码字节写回原样，再等一会儿让 GameGuard"
            "（npggNT64）跑完收尾扫描才放行 —— 就是治「退出偶发崩溃」的那个开关。")
        self.spin_exit = QSpinBox()
        self.spin_exit.setRange(0, 10)
        self.spin_exit.setSingleStep(1)
        self.spin_exit.setValue(2)
        self.spin_exit.setSuffix(" 秒")
        self.spin_exit.setMinimumWidth(80)
        self.spin_exit.setToolTip("还原后等多久再退出。默认 2 秒；觉得退出慢可以调到 0。")
        ar1b.addWidget(self.chk_exit)
        ar1b.addWidget(QLabel("等待"))
        ar1b.addWidget(self.spin_exit)
        ar1b.addStretch(1)
        adv.add_layout(ar1b)
        ar2 = QHBoxLayout()
        ar2.setSpacing(8)
        self.btn_inj_on = QPushButton("启动注入守护")
        self.btn_inj_off = QPushButton("停止注入守护")
        self.lb_adv = dim("", small=True)
        ar2.addWidget(self.btn_inj_on)
        ar2.addWidget(self.btn_inj_off)
        ar2.addWidget(self.lb_adv, 1)
        adv.add_layout(ar2)
        self.root.addWidget(adv)
        self.root.addStretch(1)

        # 顺序：启动游戏 → 选外观 → 当前规则 → 高级（原来的构建顺序是 外观在最前）
        _tail = self.root.takeAt(self.root.count() - 1)
        for _w in (acc, run, look, rules_card, adv):
            self.root.removeWidget(_w)
        for _w in (acc, run, look, rules_card, adv):
            self.root.addWidget(_w)
        if _tail is not None:
            self.root.addItem(_tail)

        # ------------------------------------------------ 连接
        self.btn_go.clicked.connect(lambda: self._go(swap=True))
        self.btn_plain.clicked.connect(lambda: self._go(swap=False))
        self.btn_add_rule.clicked.connect(self._add_rule)
        self.btn_del_rule.clicked.connect(self._del_rule)
        self.btn_reset_files.clicked.connect(self._restore)
        self.btn_clear_rules.clicked.connect(self._clear_rules)
        self.chk_cold.toggled.connect(self._cold_toggled)
        self.cmb_ace.currentIndexChanged.connect(self._ace_changed)
        self.chk_auto.toggled.connect(self._auto_toggled)
        self.chk_exit.toggled.connect(self._exit_toggled)
        self.spin_exit.valueChanged.connect(self._exit_delay_changed)
        self.btn_inj_on.clicked.connect(self._inj_on)
        self.btn_inj_off.clicked.connect(self._inj_off)
        self.cmb_method.currentIndexChanged.connect(self._method_changed)
        self.btn_proxy_deploy.clicked.connect(self._proxy_deploy)
        self.btn_proxy_remove.clicked.connect(self._proxy_remove)

        for name, _sid in quick_login.SERVERS:
            self.cmb_server.addItem(name)
        self.refresh()
        self._load_profile()

    # ------------------------------------------------------------ 数据
    def refresh(self):
        def job(*_a):
            try:
                return {"cat": backend.skin_catalog(), "st": oneclick.status(),
                        "admin": backend.is_admin()}
            except Exception:
                import traceback
                self._dbg("refresh FAIL\n" + traceback.format_exc())
                return None
        self.run(job, on_done=self._apply)

    def _load_profile(self):
        try:
            prof = quick_login.load_profile() or {}
        except Exception:
            prof = {}
        self.ed_user.setText(prof.get("username") or "")
        if prof.get("password"):
            self.ed_pwd.setText(prof.get("password") or "")
        srv = prof.get("server") or ""
        if srv:
            idx = self.cmb_server.findText(srv)
            if idx >= 0:
                self.cmb_server.setCurrentIndex(idx)

    def _apply(self, d):
        if not d:
            self._dbg("apply: empty result")
            return
        st = d.get("st") or {}
        applied = st.get("applied") or []
        total = st.get("total") or 0
        if total:
            self.badge_applied.setText("规则 %d 条（注入）%s" % (
                total, " · 守护已注入 %d" % len(st.get("injected") or [])
                if st.get("injected") else ""))
            self.badge_applied.state("ok")
        elif applied:
            self.badge_applied.setText("已替换 %d 把" % len(applied))
            self.badge_applied.state("ok")
        else:
            self.badge_applied.setText("还没换过")
            self.badge_applied.state("")
        self._rules = st.get("rules") or []
        self.badge_admin.setText("权限：管理员" if d.get("admin") else "权限：普通用户")
        self.badge_admin.state("ok" if d.get("admin") else "warn")

        cat = d.get("cat") or {}
        self._catalog = cat
        self._skin_names = {}
        for s in cat.get("skins") or []:
            code = (s.get("code") or "")
            if code.startswith("skin_"):
                self._skin_names[code] = s.get("name") or code

        # 分类下拉（原武器 + 目标建模共用同一套分类列表）
        cats = []
        for w in cat.get("weapons") or []:
            c = w.get("cat") or ""
            if c and c not in cats:
                cats.append(c)
        for cmb in (self.cmb_cat, self.cmb_model_cat):
            cur_cat = cmb.currentText()
            cmb.blockSignals(True)
            cmb.clear()
            cmb.addItem("全部分类")
            for c in cats:
                cmb.addItem(c)
            if cur_cat:
                idx = cmb.findText(cur_cat)
                if idx >= 0:
                    cmb.setCurrentIndex(idx)
            cmb.blockSignals(False)
        self._refill_weapons()
        # 默认挑一把「已有规则」或「有官方皮肤」的枪，免得刚打开就落到无涂装武器上
        if not self._catalog_skins_for(self.cmb_weapon.currentData() or ""):
            picked = -1
            if self._rules:
                idx = self.cmb_weapon.findData(self._rules[0].get("src") or "")
                if idx >= 0:
                    picked = idx
            if picked < 0:
                for code in ("ak47", "m4a1", "awm_legend", "awm", "deagle_g"):
                    idx = self.cmb_weapon.findData(code)
                    if idx >= 0 and self._catalog_skins_for(code):
                        picked = idx
                        break
            if picked < 0:
                for i in range(min(self.cmb_weapon.count(), 200)):
                    if self._catalog_skins_for(self.cmb_weapon.itemData(i) or ""):
                        picked = i
                        break
            if picked >= 0:
                self.cmb_weapon.setCurrentIndex(picked)
        self._refill_models()
        self._weapon_changed()
        self._render_rules()
        # 高级开关同步
        for _w, _v in ((self.chk_cold, st.get("cold_swap", True)),
                       (self.chk_auto, st.get("auto_elevate", False)),
        (self.chk_exit, st.get("exit_hold", True))):
            _w.blockSignals(True)
            _w.setChecked(bool(_v))
            _w.blockSignals(False)
        _ace = st.get("ace_mode") or "none"
        _i = self.cmb_ace.findData(_ace)
        if _i >= 0:
            self.cmb_ace.blockSignals(True)
            self.cmb_ace.setCurrentIndex(_i)
            self.cmb_ace.blockSignals(False)
        try:
            _delay = int(round(float(st.get("exit_delay", 1.5) or 0)))
        except Exception:
            _delay = 2
        self.spin_exit.blockSignals(True)
        self.spin_exit.setValue(max(0, min(10, _delay)))
        self.spin_exit.blockSignals(False)
        self.lb_adv.setText("注入守护：%s%s" % (
            "运行中" if st.get("inject_on") else "未启动",
            " · 已注入 %d" % len(st.get("injected") or []) if st.get("injected") else ""))
        _m = st.get("method") or "proxy"
        _mi = self.cmb_method.findData(_m)
        if _mi >= 0:
            self.cmb_method.blockSignals(True)
            self.cmb_method.setCurrentIndex(_mi)
            self.cmb_method.blockSignals(False)
        _pn = st.get("proxy_installed") or 0
        self.lb_proxy.setText(
            ("代理已部署 %d 个战斗端目录（新开一局自动生效）" % _pn) if _pn
            else "代理未部署 —— 点「部署代理」写进 battle\\*_64\\")
        self.btn_inj_on.setEnabled(_m != "proxy")
        self._dbg("apply: weapons=%d cur=%s skins=%d skin0=%s" % (
            self.cmb_weapon.count(), self.cmb_weapon.currentData(),
            self.cmb_skin.count(), self.cmb_skin.itemData(0)))

    # ------------------------------------------------------------ 选择器
    def _refill_models(self, *_):
        """建模目标：支持分类 + 关键词搜索，与原武器选择器 1:1 一致。"""
        cat = self.cmb_model_cat.currentText()
        q = (self.ed_model_q.text() or "").strip().lower()
        cur = self.cmb_model.currentData()
        self.cmb_model.blockSignals(True)
        self.cmb_model.clear()
        self.cmb_model.addItem("保持原建模（只换皮肤）", "")
        for w in self._catalog.get("weapons") or []:
            code = (w.get("code") or "")
            if not code:
                continue
            if cat and cat != "全部分类" and (w.get("cat") or "") != cat:
                continue
            name = w.get("name") or code
            hay = ("%s %s %s" % (name, code, w.get("search") or "")).lower()
            if q and q not in hay:
                continue
            self.cmb_model.addItem("%s（%s）" % (name, code), code)
        if cur:
            i = self.cmb_model.findData(cur)
            if i >= 0:
                self.cmb_model.setCurrentIndex(i)
        elif q and self.cmb_model.count() > 1:
            # 输入了搜索词时自动选中第一把匹配的建模武器，免得还要手动下拉
            self.cmb_model.setCurrentIndex(1)
        self.cmb_model.blockSignals(False)
        self._model_changed()

    def _model_changed(self, *_):
        """切换目标建模时，同步把皮肤列表切到目标武器（未改建模则用原武器）。"""
        eff = (self.cmb_model.currentData() or "") or (self.cmb_weapon.currentData() or "")
        self._skins = self._catalog_skins_for(eff) if eff else []
        self._refill_skins()

    def _refill_weapons(self, *_):
        cat = self.cmb_cat.currentText()
        q = (self.ed_weapon_q.text() or "").strip().lower()
        cur = self.cmb_weapon.currentData()
        self.cmb_weapon.blockSignals(True)
        self.cmb_weapon.clear()
        for w in self._catalog.get("weapons") or []:
            code = (w.get("code") or "")
            if not code:
                continue
            if cat and cat != "全部分类" and (w.get("cat") or "") != cat:
                continue
            name = w.get("name") or code
            hay = ("%s %s %s" % (name, code, w.get("search") or "")).lower()
            if q and q not in hay:
                continue
            self.cmb_weapon.addItem("%s（%s）" % (name, code), code)
        self.cmb_weapon.blockSignals(False)
        if cur:
            idx = self.cmb_weapon.findData(cur)
            if idx >= 0:
                self.cmb_weapon.setCurrentIndex(idx)
        self._weapon_changed()

    def _catalog_skins_for(self, code):
        """从官方全量目录查该武器绑定的皮肤（注入路线无需受限于本地 Preload 缓存）。"""
        if not code:
            return []
        low = code.lower()
        out = []
        seen = set()
        for s in self._catalog.get("skins") or []:
            ws = [str(x).lower() for x in (s.get("weapons") or [])]
            if low in ws:
                sc = s.get("code") or ""
                if sc and sc not in seen:
                    seen.add(sc)
                    out.append((sc, s.get("name") or sc))
        # 兜底补充本地缓存里发现的皮肤
        try:
            for short, _full in skin_pack.available_skins(code):
                full = "skin_%s_%s" % (low, short)
                if full not in seen:
                    seen.add(full)
                    out.append((full, self._skin_names.get(full) or short))
        except Exception:
            pass
        return out

    def _weapon_changed(self, *_):
        code = self.cmb_weapon.currentData() or ""
        # 先同步已有规则里的目标建模，再按有效武器（目标建模优先）刷皮肤列表
        rule = oneclick.rule_of(code, self._rules) if code else None
        want_model = (rule.get("model") or "").lower() if rule else ""
        if want_model:
            midx = self.cmb_model.findData(want_model)
            if midx < 0:
                if self.cmb_model_cat.currentIndex() != 0:
                    self.cmb_model_cat.setCurrentIndex(0)
                if self.ed_model_q.text():
                    self.ed_model_q.clear()
                midx = self.cmb_model.findData(want_model)
            if midx >= 0 and midx != self.cmb_model.currentIndex():
                self.cmb_model.blockSignals(True)
                self.cmb_model.setCurrentIndex(midx)
                self.cmb_model.blockSignals(False)
        eff = (self.cmb_model.currentData() or "") or code
        self._skins = self._catalog_skins_for(eff) if eff else []
        self._refill_skins()
        self._sync_from_rule()

    def _sync_from_rule(self):
        """把「武器皮肤」页里已有的这条规则读回来：选中它、标出模式。"""
        code = self.cmb_weapon.currentData() or ""
        rule = oneclick.rule_of(code, self._rules) if code else None
        base = self.lb_look.text().split("  ·  已有规则")[0]
        if not rule:
            self.lb_look.setText(base)
            self.badge_rule.setText("")
            return
        skin_val = rule.get("skin") or ""
        if skin_val:
            idx = self.cmb_skin.findData(skin_val)
            if idx < 0:
                short = oneclick.skin_short(code, skin_val)
                idx = self.cmb_skin.findData(short)
            if idx >= 0:
                self.cmb_skin.setCurrentIndex(idx)
        else:
            idx = self.cmb_skin.findData("")
            if idx >= 0:
                self.cmb_skin.setCurrentIndex(idx)
        has_model = bool(rule.get("model"))
        midx = self.cmb_model.findData((rule.get("model") or "").lower())
        if midx >= 0 and midx != self.cmb_model.currentIndex():
            self.cmb_model.blockSignals(True)
            self.cmb_model.setCurrentIndex(midx)
            self.cmb_model.blockSignals(False)
        self.badge_rule.setText("已有规则：注入")
        self.badge_rule.state("ok")
        self.lb_look.setText(base + "  ·  已有规则（注入%s）" % (
            "，含建模" if has_model else ""))

    def _refill_skins(self, *_):
        code = self.cmb_weapon.currentData() or ""
        q = (self.ed_skin_q.text() or "").strip().lower()
        cur = self.cmb_skin.currentData()
        self.cmb_skin.blockSignals(True)
        self.cmb_skin.clear()
        for full, name in self._skins:
            if q and q not in ("%s %s" % (name, full)).lower():
                continue
            self.cmb_skin.addItem("%s（%s）" % (name, full), full)
        self.cmb_skin.addItem("保持原皮肤（仅换建模时可选）", "")
        self.cmb_skin.blockSignals(False)
        if cur is not None:
            idx = self.cmb_skin.findData(cur)
            if idx >= 0:
                self.cmb_skin.setCurrentIndex(idx)

        if self._skins:
            self.lb_look.setText("这把枪共有 %d 款官方涂装可选（走注入换肤，无需提前拥有皮肤）。" % len(self._skins))
            self.badge_look.setText("可换")
            self.badge_look.state("ok")
        else:
            self.lb_look.setText(
                "这把枪暂无专属涂装，仍可通过下方「建模」切换成其他武器外形。"
                if code else "请先选择武器。")
            self.badge_look.setText("仅换建模" if code else "")
            self.badge_look.state("warn" if code else "")

    def _cold_toggled(self, on):
        try:
            backend.risk_inject_option("cold_swap", bool(on))
            self.log.line("[OK] 已切换为%s加载（下一局生效）" % ("冷" if on else "热"))
        except Exception as exc:
            self.log.line("[!] 切换失败：%r" % (exc,))

    def _ace_changed(self, _idx):
        mode = self.cmb_ace.currentData() or "none"
        try:
            backend.risk_inject_option("ace_mode", mode)
            self.log.line("[OK] ACE 策略 = %s" % mode)
        except Exception as exc:
            self.log.line("[!] 切换失败：%r" % (exc,))

    def _auto_toggled(self, on):
        try:
            backend.risk_inject_option("auto_elevate", bool(on))
        except Exception:
            pass

    def _exit_toggled(self, on):
        try:
            backend.risk_inject_option("exit_hold", bool(on))
            self.log.line("[OK] 退出保护 %s（下一局生效）" % (
                "已开：退出前先还原补丁" if on else "已关"))
        except Exception as exc:
            self.log.line("[!] 切换失败：%r" % (exc,))

    def _exit_delay_changed(self, val):
        try:
            backend.risk_inject_option("exit_delay", float(val))
        except Exception:
            pass

    def _method_changed(self, _idx):
        mode = self.cmb_method.currentData() or "proxy"
        try:
            backend.risk_inject_option("method", mode)
            if mode == "proxy":
                self.log.line("[OK] 加载方式 = 代理 DLL（version.dll）")
            else:
                self.log.line("[!] 加载方式 = 远程线程注入：GameGuard 会在 CreateRemoteThread "
                              "那一刻用 0x80000004 杀掉战斗端，只在没有反作弊的环境里用")
            self.refresh()
        except Exception as exc:
            self.log.line("[!] 切换失败：%r" % (exc,))

    def _proxy_deploy(self):
        if self._busy:
            return
        self._set_busy(True)
        lines = []

        def job(*_a):
            return backend.risk_proxy_install(emit=lines.append)

        def done(res):
            self._set_busy(False)
            ok, msg = res if isinstance(res, tuple) else (False, str(res))
            for ln in lines:
                self.log.line(str(ln))
            self.log.line(("[OK] " if ok else "[!] ") + str(msg))
            self.refresh()

        self.run(job, on_done=done)

    def _proxy_remove(self):
        if self._busy:
            return
        self._set_busy(True)

        def job(*_a):
            return backend.risk_proxy_uninstall()

        def done(res):
            self._set_busy(False)
            ok, msg = res if isinstance(res, tuple) else (False, str(res))
            self.log.line(("[OK] " if ok else "[!] ") + str(msg))
            self.refresh()

        self.run(job, on_done=done)

    def _inj_on(self):
        if self._busy:
            return
        self._set_busy(True)

        def job(*_a):
            return backend.risk_inject_start()

        def done(res):
            self._set_busy(False)
            ok, msg = res if isinstance(res, tuple) else (False, str(res))
            self.log.line(("[OK] " if ok else "[!] ") + str(msg))
            self.refresh()

        self.run(job, on_done=done)

    def _inj_off(self):
        if self._busy:
            return
        self._set_busy(True)

        def job(*_a):
            return backend.risk_inject_stop()

        def done(res):
            self._set_busy(False)
            self.log.line("[OK] 注入守护已停止")
            self.refresh()

        self.run(job, on_done=done)

    # ------------------------------------------------------------ 当前规则表
    def _render_rules(self):
        rules = list(self._rules or [])
        self.tbl.setRowCount(len(rules))
        for i, r in enumerate(rules):
            mode = oneclick.MODE_LABELS.get(r.get("mode") or "", "注入")
            st = mode if r.get("enabled", True) else (mode + "·暂停")
            self.tbl.setItem(i, 0, QTableWidgetItem(st))
            self.tbl.setItem(i, 1, QTableWidgetItem("%s（%s）" % (
                r.get("src_name") or r.get("src") or "", r.get("src") or "")))
            skin = r.get("skin_name") or oneclick.skin_short(r.get("src") or "", r.get("skin"))
            self.tbl.setItem(i, 2, QTableWidgetItem(skin or "保持原皮肤"))
            self.tbl.setItem(i, 3, QTableWidgetItem(r.get("model_name") or r.get("model") or "保持原建模"))
        n_static = sum(1 for r in rules if (r.get("mode") or "") == "static")
        if rules:
            if n_static:
                self.badge_rules.setText("共 %d 条 · 注入 %d · 旧的静态 %d" % (
                    len(rules), len(rules) - n_static, n_static))
            else:
                self.badge_rules.setText("共 %d 条 · 全部注入" % len(rules))
            self.badge_rules.state("ok")
        else:
            self.badge_rules.setText("还没有规则")
            self.badge_rules.state("")

    def _rule_at(self, row):
        rules = self._rules or []
        if 0 <= row < len(rules):
            return rules[row]
        return None

    def _pick_row(self, row, _col=0):
        r = self._rule_at(row)
        if not r:
            return
        # 先清搜索词并切回全部分类，避免目标武器/建模被当前筛选隐藏
        if self.cmb_cat.currentIndex() != 0:
            self.cmb_cat.setCurrentIndex(0)
        if self.ed_weapon_q.text():
            self.ed_weapon_q.clear()
        if self.cmb_model_cat.currentIndex() != 0:
            self.cmb_model_cat.setCurrentIndex(0)
        if self.ed_model_q.text():
            self.ed_model_q.clear()
        idx = self.cmb_weapon.findData(r.get("src") or "")
        if idx >= 0:
            self.cmb_weapon.setCurrentIndex(idx)
            self._weapon_changed()

    def _add_rule(self):
        """把当前选的武器+皮肤/建模登记成一条规则（不启动游戏）。"""
        if self._busy:
            return
        weapon = self.cmb_weapon.currentData() or ""
        skin = self.cmb_skin.currentData() or ""
        model = self.cmb_model.currentData() or ""
        if not weapon or (not skin and not model):
            self.log.line("[!] 请先选武器，并至少选择一款皮肤或目标建模")
            return
        wname = self.cmb_weapon.currentText().split("（")[0]
        sname = self.cmb_skin.currentText().split("（")[0] if skin else ""
        mname = self.cmb_model.currentText().split("（")[0] if model else ""
        self._set_busy(True)

        def job(w):
            ok, msg = oneclick.upsert_rule(weapon, skin, model, weapon_name=wname,
                                           skin_name=sname, model_name=mname,
                                           mode=oneclick.MODE_INJECT)
            if ok:
                msg = "已添加注入规则（注入守护开启后才会在对局生效）"
            return ok, msg

        def done(res):
            self._set_busy(False)
            ok, msg = res if isinstance(res, tuple) else (False, "未知结果")
            self.log.line(("[OK] " if ok else "[!] ") + str(msg))
            self.refresh()

        self.run(job, on_done=done, log=self._emit)

    def _del_rule(self):
        row = self.tbl.currentRow()
        r = self._rule_at(row)
        if not r:
            self.log.line("[!] 先在表里选一行")
            return
        src = r.get("src") or ""
        static = (r.get("mode") or "") == "static"
        self._set_busy(True)

        def job(w):
            if static:
                try:
                    skin_pack.restore_weapon(src)
                except Exception as exc:
                    w.log.emit("[!] 还原贴图失败：%r" % (exc,))
            return oneclick.drop_rule(src)

        def done(res):
            self._set_busy(False)
            ok, msg = res if isinstance(res, tuple) else (bool(res), str(res))
            if ok:
                self.log.line("[OK] 已删除 %s 的规则%s" % (
                    src, "，本地贴图已还原" if static else "（战斗端下一局生效）"))
            else:
                self.log.line("[!] 删除失败：%s" % msg)
            self.refresh()

        self.run(job, on_done=done, log=self._emit)

    def _clear_rules(self):
        if self._busy:
            return
        self._set_busy(True)

        def job(w):
            return oneclick.clear_all_rules(restore_files=True, emit=w.log.emit)

        def done(res):
            self._set_busy(False)
            ok, msg = res if isinstance(res, tuple) else (False, "未知结果")
            self.log.line(("[OK] " if ok else "[!] ") + str(msg))
            self.refresh()

        self.run(job, on_done=done, log=self._emit)

    # ------------------------------------------------------------ 动作
    def _set_busy(self, busy):
        self._busy = busy
        for b in (self.btn_go, self.btn_plain, self.btn_add_rule,
                  self.btn_del_rule, self.btn_reset_files, self.btn_clear_rules,
                  self.btn_inj_on, self.btn_inj_off,
                  self.btn_proxy_deploy, self.btn_proxy_remove):
            b.setEnabled(not busy)

    def _emit(self, text):
        self.log.line(text)

    def _go(self, swap=True):
        if self._busy:
            return
        user = self.ed_user.text().strip()
        pwd = self.ed_pwd.text()
        server = self.cmb_server.currentText()
        weapon = self.cmb_weapon.currentData() or ""
        skin = self.cmb_skin.currentData() or ""
        model = self.cmb_model.currentData() or ""
        wname = self.cmb_weapon.currentText().split("（")[0]
        sname = self.cmb_skin.currentText().split("（")[0] if skin else ""
        mname = self.cmb_model.currentText().split("（")[0] if model else ""
        mode = oneclick.MODE_INJECT
        has_sel = bool(weapon and (skin or model))
        if swap and not has_sel and not self._rules:
            self.log.line("[!] 请先选武器并选择一款皮肤或目标建模（或点「只启动游戏」）")
            return
        if user and pwd and self.chk_remember.isChecked():
            try:
                quick_login.save_profile(user, pwd, server)
            except Exception as exc:
                self.log.line("[!] 保存账号失败：%r" % (exc,))

        self._set_busy(True)
        self.badge_mode.setText("进行中…")

        def job(w):
            if not swap:
                ok, msg = quick_login.launch_official(emit=w.log.emit)
                return ok, msg
            return oneclick.launch(mode, weapon, skin, user, pwd, server,
                                   model=model, emit=w.log.emit,
                                   weapon_name=wname, skin_name=sname, model_name=mname)

        def done(res):
            self._set_busy(False)
            ok, msg = res if isinstance(res, tuple) else (False, "未知结果")
            self.log.line(("[OK] " if ok else "[!] ") + str(msg))
            self.badge_mode.state("ok" if ok else "bad")
            self.badge_mode.setText("完成" if ok else "失败")
            self.refresh()

        self.run(job, on_done=done, log=self._emit)

    def _restore(self):
        if self._busy:
            return
        self._set_busy(True)

        def job(w):
            return oneclick.restore(emit=w.log.emit)

        def done(res):
            self._set_busy(False)
            ok, msg = res if isinstance(res, tuple) else (False, "未知结果")
            self.log.line(("[OK] " if ok else "[!] ") + str(msg))
            self.refresh()

        self.run(job, on_done=done, log=self._emit)
