# -*- coding: utf-8 -*-
"""武器皮肤与建模替换 + 加载背景图配置页。"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox,
                               QFileDialog, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout)

import backend
from ui.widgets import Badge, Card, ScrollPage, dim, page_header

CATS = ["全部分类", "步枪", "狙击枪", "手枪", "近战", "冲锋枪", "机枪", "霰弹枪", "投掷", "特殊"]


class AppearancePage(ScrollPage):
    TITLE = "武器皮肤"

    def __init__(self, app):
        super().__init__(app)
        self._catalog = {"weapons": [], "skins": []}
        self._rules_data = {"enabled": True, "rules": []}
        self._busy = False
        self._mod_state = None

        self.badge = Badge("就绪")
        self.root.addWidget(page_header(
            "武器皮肤与建模替换",
            "基于 D:\\Ccode\\sjsf 官方全量武器与皮肤库：支持任意武器 3D 建模替换与皮肤涂装覆盖，"
            "纯客户端视觉生效，不修改伤害/射速等战斗数值。",
            extra=self.badge))

        # ------------------------------------------------ 顶部：总开关与目录信息
        bar_card = Card("替换引擎状态", "开启「风险」页内存注入守护后，本页规则会在战斗端自动热加载生效。", glyph="\uE790")
        top_row = QHBoxLayout()
        top_row.setSpacing(12)
        self.chk_global = QCheckBox("启用武器皮肤 / 建模替换")
        self.chk_global.setChecked(True)
        self.chk_global.toggled.connect(self._on_global_toggle)
        top_row.addWidget(self.chk_global)

        self.lb_cat_info = QLabel("目录加载中…")
        self.lb_cat_info.setObjectName("DimSmall")
        top_row.addWidget(self.lb_cat_info, 1)

        self.btn_sync = QPushButton("同步 sjsf 武器库")
        self.btn_sync.setCursor(Qt.PointingHandCursor)
        self.btn_sync.clicked.connect(self._sync_catalog)
        top_row.addWidget(self.btn_sync)

        self.btn_goto_risk = QPushButton("前往开启注入守护")
        self.btn_goto_risk.setCursor(Qt.PointingHandCursor)
        self.btn_goto_risk.clicked.connect(lambda: self.app.goto("risk"))
        top_row.addWidget(self.btn_goto_risk)
        bar_card.add_layout(top_row)
        self.root.addWidget(bar_card)

        # ------------------------------------------------ 添加替换规则
        add_card = Card("添加替换规则", "先选你背包里的原武器，再选择要替换成的目标武器建模和/或武器皮肤。", glyph="\uE7FC")

        # 第 1 行：分类 + 搜索源武器
        r1 = QHBoxLayout()
        r1.setSpacing(8)
        lb1 = QLabel("1. 原武器")
        lb1.setFixedWidth(72)
        lb1.setObjectName("Dim")
        r1.addWidget(lb1)

        self.combo_cat = QComboBox()
        for c in CATS:
            self.combo_cat.addItem(c)
        self.combo_cat.setFixedWidth(105)
        self.combo_cat.currentIndexChanged.connect(self._refill_src)
        r1.addWidget(self.combo_cat)

        self.ed_src_q = QLineEdit()
        self.ed_src_q.setPlaceholderText("搜索原武器（中文名或英文代号，如 M4A1 / ak47）")
        self.ed_src_q.setFixedWidth(250)
        self.ed_src_q.textChanged.connect(self._refill_src)
        r1.addWidget(self.ed_src_q)

        self.combo_src = QComboBox()
        self.combo_src.currentIndexChanged.connect(self._refill_skins)
        r1.addWidget(self.combo_src, 1)
        add_card.add_layout(r1)

        # 第 2 行：目标武器建模（可选）
        r2 = QHBoxLayout()
        r2.setSpacing(8)
        lb2 = QLabel("2. 目标建模")
        lb2.setFixedWidth(72)
        lb2.setObjectName("Dim")
        r2.addWidget(lb2)

        self.ed_dst_q = QLineEdit()
        self.ed_dst_q.setPlaceholderText("搜索目标武器建模（留空则可选不改建模，如 传说 / 英雄 / 命运）")
        self.ed_dst_q.setFixedWidth(363)
        self.ed_dst_q.textChanged.connect(self._refill_dst)
        r2.addWidget(self.ed_dst_q)

        self.combo_dst = QComboBox()
        self.combo_dst.currentIndexChanged.connect(self._refill_skins)
        r2.addWidget(self.combo_dst, 1)
        add_card.add_layout(r2)

        # 第 3 行：目标武器皮肤（可选）
        r3 = QHBoxLayout()
        r3.setSpacing(8)
        lb3 = QLabel("3. 目标皮肤")
        lb3.setFixedWidth(72)
        lb3.setObjectName("Dim")
        r3.addWidget(lb3)

        self.ed_skin_q = QLineEdit()
        self.ed_skin_q.setPlaceholderText("筛选当前武器专属皮肤（皮肤与武器严格绑定）")
        self.ed_skin_q.setFixedWidth(363)
        self.ed_skin_q.textChanged.connect(self._refill_skins)
        r3.addWidget(self.ed_skin_q)

        self.combo_skin = QComboBox()
        r3.addWidget(self.combo_skin, 1)
        add_card.add_layout(r3)

        # 第 4 行：添加按钮 + 提示
        r4 = QHBoxLayout()
        r4.setSpacing(10)
        self.btn_add = QPushButton("添加替换规则并校验缓存")
        self.btn_add.setObjectName("Primary")
        self.btn_add.setMinimumHeight(38)
        self.btn_add.setCursor(Qt.PointingHandCursor)
        self.btn_add.clicked.connect(self._add_rule)
        r4.addWidget(self.btn_add)

        self.lb_op_msg = QLabel("支持「只换建模」、「只换皮肤」或「建模+皮肤同时替换」，进局切枪即时生效。")
        self.lb_op_msg.setObjectName("DimSmall")
        self.lb_op_msg.setWordWrap(True)
        r4.addWidget(self.lb_op_msg, 1)
        add_card.add_layout(r4)
        self.root.addWidget(add_card)

        # ------------------------------------------------ 已配置规则表
        list_card = Card("当前替换规则", "实时热同步至战斗端，支持随时启用、禁用或一键还原。", glyph="\uE8FD")
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["状态", "原武器", "目标武器建模", "目标皮肤涂装", "内部映射"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setMinimumHeight(210)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Fixed)
        self.table.setColumnWidth(0, 100)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        hh.setSectionResizeMode(4, QHeaderView.Stretch)
        list_card.add(self.table)

        act_row = QHBoxLayout()
        act_row.setSpacing(8)
        self.btn_toggle = QPushButton("启用 / 禁用选中")
        self.btn_del = QPushButton("删除选中规则")
        self.btn_clear = QPushButton("清空并还原所有武器")
        self.btn_clear.setObjectName("Danger")
        self.btn_toggle.clicked.connect(self._toggle_selected)
        self.btn_del.clicked.connect(self._delete_selected)
        self.btn_clear.clicked.connect(self._clear_all)
        for b in (self.btn_toggle, self.btn_del, self.btn_clear):
            b.setCursor(Qt.PointingHandCursor)
            act_row.addWidget(b)
        act_row.addStretch(1)
        list_card.add_layout(act_row)
        self.root.addWidget(list_card)

        # ------------------------------------------------ 加载背景图（精简保留）
        bg_card = Card("自定义加载背景图（SingleSkinMod）", "可选：为已部署 SingleSkinMod 的战斗端更换加载画面背景图。", glyph="\uE91B")
        bg_row = QHBoxLayout()
        bg_row.setSpacing(10)
        self.lb_bg_info = QLabel("未检测到 SingleSkinMod 目录（不影响上方武器皮肤替换功能）")
        self.lb_bg_info.setObjectName("DimSmall")
        bg_row.addWidget(self.lb_bg_info, 1)
        self.btn_bg_pick = QPushButton("选择背景图…")
        self.btn_bg_pick.clicked.connect(self._pick_bg)
        bg_row.addWidget(self.btn_bg_pick)
        bg_card.add_layout(bg_row)
        self.root.addWidget(bg_card)
        self.root.addStretch(1)

    # ------------------------------------------------------------ 生命周期
    def on_show(self):
        self.refresh()

    def refresh(self):
        def job(_w):
            return {
                "cat": backend.skin_catalog(),
                "rules": backend.skin_load_rules(),
                "mod": backend.mod_state(),
            }

        self.run(job, on_done=self._apply_data)

    def _apply_data(self, d):
        if not d:
            return
        self._catalog = d.get("cat") or {"weapons": [], "skins": []}
        self._rules_data = d.get("rules") or {"enabled": True, "rules": []}
        self._mod_state = d.get("mod") or {}

        ws = self._catalog.get("weapons") or []
        sks = self._catalog.get("skins") or []
        self.lb_cat_info.setText("已加载官方库：%d 把武器 · %d 款武器皮肤" % (len(ws), len(sks)))

        self.chk_global.blockSignals(True)
        self.chk_global.setChecked(bool(self._rules_data.get("enabled", True)))
        self.chk_global.blockSignals(False)

        self._refill_src()
        self._refill_dst()
        self._refill_skins()
        self._render_table()

        rules = self._rules_data.get("rules") or []
        active_n = sum(1 for r in rules if r.get("enabled", True))
        if self._rules_data.get("enabled", True) and active_n:
            self.badge.setText("生效中 · %d 条" % active_n)
            self.badge.state("ok")
        else:
            self.badge.setText("待配置" if not rules else "已暂停")
            self.badge.state("warn" if rules else "")

        if self._mod_state.get("dir"):
            self.lb_bg_info.setText("已检测到客户端：%s" % self._mod_state["dir"])
        else:
            self.lb_bg_info.setText("未检测到 SingleSkinMod 目录（不影响上方武器皮肤替换功能）")

    # ------------------------------------------------------------ 下拉筛选
    def _refill_src(self):
        cat = self.combo_cat.currentText()
        q = self.ed_src_q.text().strip().lower()
        cur = self.combo_src.currentData()
        self.combo_src.blockSignals(True)
        self.combo_src.clear()
        for w in (self._catalog.get("weapons") or []):
            if cat != "全部分类" and w.get("cat") != cat:
                continue
            hay = w.get("search") or (w["name"] + " " + w["code"]).lower()
            if q and q not in hay:
                continue
            self.combo_src.addItem("[%s] %s  (%s)" % (w.get("cat", ""), w["name"], w["code"]), w)
        if cur:
            for i in range(self.combo_src.count()):
                if (self.combo_src.itemData(i) or {}).get("code") == cur.get("code"):
                    self.combo_src.setCurrentIndex(i)
                    break
        self.combo_src.blockSignals(False)
        self._refill_skins()

    def _refill_dst(self):
        q = self.ed_dst_q.text().strip().lower()
        cur = self.combo_dst.currentData()
        self.combo_dst.blockSignals(True)
        self.combo_dst.clear()
        self.combo_dst.addItem("（不改建模 · 仅替换皮肤）", None)
        for w in (self._catalog.get("weapons") or []):
            hay = w.get("search") or (w["name"] + " " + w["code"]).lower()
            if q and q not in hay:
                continue
            self.combo_dst.addItem("[%s] %s  (%s)" % (w.get("cat", ""), w["name"], w["code"]), w)
        if cur:
            for i in range(1, self.combo_dst.count()):
                if (self.combo_dst.itemData(i) or {}).get("code") == cur.get("code"):
                    self.combo_dst.setCurrentIndex(i)
                    break
        self.combo_dst.blockSignals(False)
        self._refill_skins()

    def _refill_skins(self):
        q = self.ed_skin_q.text().strip().lower()
        target_w = self.combo_dst.currentData() or self.combo_src.currentData()
        target_code = (target_w or {}).get("code", "").lower()

        self.combo_skin.clear()
        matched = []
        if target_code:
            for s in (self._catalog.get("skins") or []):
                ws = [x.lower() for x in (s.get("weapons") or [])]
                if target_code not in ws:
                    continue
                if q and q not in s["name"].lower() and q not in s["code"].lower() and q not in s.get("desc", "").lower():
                    continue
                matched.append(s)

        if matched:
            self.combo_skin.addItem("（不改皮肤 · 保持原皮肤）", None)
            for s in matched:
                label = "%s  (%s)" % (s["name"], s["code"])
                self.combo_skin.addItem(label, s)
        else:
            self.combo_skin.addItem("（当前武器暂无专属皮肤，可改上方目标建模）", None)

    # ------------------------------------------------------------ 规则表格
    def _render_table(self):
        rules = self._rules_data.get("rules") or []
        self.table.setRowCount(len(rules))
        for i, r in enumerate(rules):
            is_static = (r.get("mode") or "") == "static"
            st = ("启用" if r.get("enabled", True) else "暂停") + ("·静态" if is_static else "·注入")
            self.table.setItem(i, 0, QTableWidgetItem(st))
            self.table.setItem(i, 1, QTableWidgetItem("%s (%s)" % (r.get("src_name", ""), r.get("src", ""))))
            m_txt = ("%s (%s)" % (r.get("model_name", ""), r.get("model", ""))) if r.get("model") else "保持原建模"
            s_txt = ("%s (%s)" % (r.get("skin_name", ""), r.get("skin", ""))) if r.get("skin") else "保持原皮肤"
            self.table.setItem(i, 2, QTableWidgetItem(m_txt))
            self.table.setItem(i, 3, QTableWidgetItem(s_txt))
            mp = "%s → %s%s" % (
                r.get("src", ""),
                r.get("model", "") or "(原模)",
                (" + " + r.get("skin", "")) if r.get("skin") else "",
            )
            self.table.setItem(i, 4, QTableWidgetItem(mp))

    def _save_and_refresh(self, msg_override=None):
        ok, msg = backend.skin_save_rules(self._rules_data)
        self.lb_op_msg.setText(msg_override or msg)
        self._render_table()
        rules = self._rules_data.get("rules") or []
        active_n = sum(1 for r in rules if r.get("enabled", True))
        if self._rules_data.get("enabled", True) and active_n:
            self.badge.setText("生效中 · %d 条" % active_n)
            self.badge.state("ok")
        else:
            self.badge.setText("待配置" if not rules else "已暂停")
            self.badge.state("warn" if rules else "")

    def _on_global_toggle(self, checked):
        self._rules_data["enabled"] = bool(checked)
        self._save_and_refresh("已%s武器皮肤/建模替换" % ("启用" if checked else "暂停"))

    def _add_rule(self):
        if self._busy:
            return
        src_w = self.combo_src.currentData()
        dst_w = self.combo_dst.currentData()
        skin_o = self.combo_skin.currentData()
        if not src_w:
            self.lb_op_msg.setText("请先选择一把原武器")
            return
        if not dst_w and not skin_o:
            self.lb_op_msg.setText("请至少选择「目标武器建模」或「目标皮肤」其中一项")
            return

        new_rule = {
            "src": src_w["code"],
            "src_name": src_w["name"],
            "model": dst_w["code"] if dst_w else "",
            "model_name": dst_w["name"] if dst_w else "",
            "skin": skin_o["code"] if skin_o else "",
            "skin_name": skin_o["name"] if skin_o else "",
            "enabled": True,
        }
        old_same = [r for r in (self._rules_data.get("rules") or [])
                    if r.get("src", "").lower() == new_rule["src"].lower()]
        rules = [r for r in (self._rules_data.get("rules") or [])
                 if r.get("src", "").lower() != new_rule["src"].lower()]
        rules.append(new_rule)
        # 同一把枪从「静态」改成「注入」时，先把本地贴图还原，避免两套叠加
        self._restore_static_rules(old_same)
        self._rules_data["rules"] = rules
        self._rules_data["enabled"] = True
        self.chk_global.blockSignals(True)
        self.chk_global.setChecked(True)
        self.chk_global.blockSignals(False)
        self._save_and_refresh("已保存规则，正在校验目标武器/皮肤本地缓存…")

        codes = [c for c in (new_rule["model"], new_rule["skin"]) if c]
        if not codes:
            return
        self._busy = True
        self.btn_add.setEnabled(False)

        def job(w):
            return backend.skin_ensure_bundles(codes, emit=w.log.emit)

        def done(res):
            self._busy = False
            self.btn_add.setEnabled(True)
            if res:
                self.lb_op_msg.setText("规则已生效 · " + res[1])

        self.run(job, on_done=done)

    def _toggle_selected(self):
        row = self.table.currentRow()
        rules = self._rules_data.get("rules") or []
        if 0 <= row < len(rules):
            rules[row]["enabled"] = not bool(rules[row].get("enabled", True))
            off = rules[row] if not rules[row]["enabled"] else None
            self._save_and_refresh()
            if off:
                n = self._restore_static_rules([off])
                if n:
                    self.lb_op_msg.setText(self.lb_op_msg.text() + "；已把该武器的本地外观还原")

    def _delete_selected(self):
        row = self.table.currentRow()
        rules = self._rules_data.get("rules") or []
        if 0 <= row < len(rules):
            gone = rules.pop(row)
            self._save_and_refresh("已删除选中规则，战斗端将自动还原该武器")
            if self._restore_static_rules([gone]):
                self.lb_op_msg.setText("已删除规则，并把该武器的本地贴图还原成官方原样")

    def _clear_all(self):
        gone = list(self._rules_data.get("rules") or [])
        self._rules_data["rules"] = []
        self._save_and_refresh("已清空所有替换规则，战斗端将自动还原全部武器原貌")
        if self._restore_static_rules(gone):
            self.lb_op_msg.setText("已清空规则，导出的本地武器包也全部还原成官方原样")

    # ------------------------------------------------------------ 静态规则联动
    def _restore_static_rules(self, rules):
        """删掉/暂停静态规则时，把被替换的本地武器包一起还原（静态换肤没有运行时在跑）。"""
        names = [r.get("src") for r in (rules or [])
                 if (r.get("mode") or "") == "static" and r.get("src")]
        if not names:
            return 0
        try:
            import skin_pack
        except Exception:
            return 0
        n = 0
        for w in names:
            try:
                if skin_pack.restore_weapon(w):
                    n += 1
            except Exception:
                pass
        return n

    def _sync_catalog(self):
        if self._busy:
            return
        self._busy = True
        self.btn_sync.setEnabled(False)
        self.lb_op_msg.setText("正在从 D:\\Ccode\\sjsf 同步最新武器与皮肤库…")

        def job(_w):
            return backend.skin_rebuild_catalog()

        def done(res):
            self._busy = False
            self.btn_sync.setEnabled(True)
            if res:
                self.lb_op_msg.setText(res[1])
            self.refresh()

        self.run(job, on_done=done)

    def _pick_bg(self):
        mod_dir = (self._mod_state or {}).get("mod_dir")
        if not mod_dir:
            self.lb_bg_info.setText("未检测到已部署 SingleSkinMod 的战斗端目录")
            return
        src, _ = QFileDialog.getOpenFileName(
            self, "选择加载背景图", "", "图片 (*.png *.jpg *.jpeg *.webp)")
        if not src:
            return
        ok, res = backend.set_background(mod_dir, src, fit=0, enable=True)
        self.lb_bg_info.setText(("已更新背景图：" + res) if ok else res)
