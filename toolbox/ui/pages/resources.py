# -*- coding: utf-8 -*-
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QDialogButtonBox,
                               QGridLayout, QHBoxLayout, QHeaderView, QLabel,
                               QListWidget, QListWidgetItem, QProgressBar,
                               QPushButton, QSpinBox, QTableWidget,
                               QTableWidgetItem, QVBoxLayout)

import backend
from ui.widgets import Card, LogBox, ScrollPage, StatTile, dim, page_header

CATEGORIES = ["maps/", "weaponskin/", "weapon/", "player/", "lobbyweaponskin/",
              "lobbyweapon/", "ui/", "playeranim/", "effect/", "sound/"]


class CategoryDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("选择要预下载的分类")
        self.resize(360, 440)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(12)
        lay.addWidget(dim("可多选。第一次用推荐 maps/ 和 weapon/，体积最大的也是它们。"))
        self.list = QListWidget()
        for c in CATEGORIES:
            item = QListWidgetItem("  " + c)
            item.setData(Qt.UserRole, c)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            self.list.addItem(item)
        lay.addWidget(self.list, 1)
        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        lay.addWidget(box)

    def selected(self):
        out = []
        for i in range(self.list.count()):
            item = self.list.item(i)
            if item.checkState() == Qt.Checked:
                out.append(item.data(Qt.UserRole))
        return out


class ResourcesPage(ScrollPage):
    TITLE = "资源库"

    def __init__(self, app):
        super().__init__(app)
        self._busy = False
        self._refreshing = False

        self.root.addWidget(page_header(
            "资源库（可选 · 默认关闭）",
            "换肤用不到这里。本地缓存与加速全部默认关，只有你手动开启才会走；开着会占不少磁盘，对换肤没有任何帮助。"))

        top = QHBoxLayout()
        top.setSpacing(14)

        lobby = Card("大厅资源", "客户端真实请求过的资源，本地已有多少", glyph="\uE896")
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        self.tile_req = StatTile("请求过", "--")
        self.tile_have = StatTile("已缓存", "--")
        self.tile_miss = StatTile("缺失", "--")
        self.tile_404 = StatTile("已知 404", "--")
        for i, t in enumerate((self.tile_req, self.tile_have,
                               self.tile_miss, self.tile_404)):
            grid.addWidget(t, i // 2, i % 2)
        lobby.add_layout(grid)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.btn_cov = QPushButton("刷新覆盖率")
        self.btn_cover = QPushButton("补齐缺失")
        self.btn_prewarm = QPushButton("清单预热")
        self.btn_prewarm.setObjectName("Primary")
        for b in (self.btn_cov, self.btn_cover, self.btn_prewarm):
            row.addWidget(b)
        row.addStretch(1)
        lobby.add_layout(row)
        lobby.add(dim("「清单预热」下载启动必需资源；「补齐缺失」扫上局日志，"
                      "把客户端真实请求过、清单里没有的资源补下来。", small=True))
        top.addWidget(lobby, 1)

        battle = Card("战斗资源", "AssetBundle 预热，进图与换武器秒开", glyph="\uE7FC")
        self.lb_bdir = QLabel("--")
        self.lb_bdir.setObjectName("DimSmall")
        self.lb_bdir.setWordWrap(True)
        self.lb_bcache = QLabel("--")
        self.lb_bmanifest = QLabel("--")
        battle.add(self.lb_bdir)
        battle.add(self.lb_bcache)
        battle.add(self.lb_bmanifest)
        row2 = QHBoxLayout()
        row2.setSpacing(8)
        self.spin_jobs = QSpinBox()
        self.spin_jobs.setRange(2, 32)
        self.spin_jobs.setValue(8)
        self.spin_jobs.setPrefix("并发 ")
        row2.addWidget(self.spin_jobs)
        self.btn_ball = QPushButton("预下载全部")
        self.btn_ball.setObjectName("Primary")
        self.btn_bcat = QPushButton("按分类…")
        row2.addWidget(self.btn_ball)
        row2.addWidget(self.btn_bcat)
        row2.addStretch(1)
        battle.add_layout(row2)
        battle.add(dim("全量约 17 GB。下完进图、换武器全部秒开；已在缓存里的包自动跳过，"
                       "中断后再点会续跑。", small=True))
        top.addWidget(battle, 1)
        self.root.addLayout(top)

        table_card = Card("本地库分布")
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["目录", "文件数", "大小"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setShowGrid(False)
        self.table.setFixedHeight(196)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        table_card.add(self.table)
        self.lb_total = QLabel("--")
        self.lb_total.setObjectName("DimSmall")
        table_card.add(self.lb_total)
        self.root.addWidget(table_card)

        prog_row = QHBoxLayout()
        prog_row.setSpacing(10)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        prog_row.addWidget(self.progress, 1)
        self.lb_prog = QLabel("空闲")
        self.lb_prog.setObjectName("DimSmall")
        prog_row.addWidget(self.lb_prog)
        self.root.addLayout(prog_row)

        log_card = Card("任务输出")
        clear_btn = QPushButton("清空")
        clear_btn.clicked.connect(lambda: self.log_view.clear_all())
        log_card.add_header_widget(clear_btn)
        self.log_view = LogBox()
        self.log_view.setFixedHeight(190)
        log_card.add(self.log_view)
        self.root.addWidget(log_card)
        self.root.addStretch(1)

        self.btn_cov.clicked.connect(self.refresh)
        self.btn_cover.clicked.connect(self._do_cover)
        self.btn_prewarm.clicked.connect(self._do_prewarm)
        self.btn_ball.clicked.connect(lambda: self._do_battle(["all"]))
        self.btn_bcat.clicked.connect(self._choose_battle)

    # ------------------------------------------------------------ 状态
    def on_show(self):
        self.refresh()

    def refresh(self):
        if self._refreshing:
            return
        self._refreshing = True

        def job(_w):
            cov = backend.coverage()
            rows, n, b = backend.cache_stats()
            bdir, bn, bb = backend.battle_cache_stats()
            try:
                total, size, _src = backend.battle_manifest_counts()
            except Exception:
                total, size = -1, 0
            return {"cov": cov, "rows": rows, "n": n, "b": b,
                    "bdir": bdir, "bn": bn, "bb": bb, "btotal": total, "bsize": size}

        self.run(job, on_done=self._apply)

    def _apply(self, d):
        self._refreshing = False
        if not d:
            return
        cov = d["cov"]
        self.tile_req.set(str(cov["requested"]))
        self.tile_have.set("%d（%.1f%%）" % (cov["have"], cov["pct"]))
        self.tile_miss.set(str(cov["missing"]))
        self.tile_404.set(str(cov["known404"]))

        self.lb_bdir.setText("缓存目录：" + d["bdir"])
        self.lb_bcache.setText("已缓存 %d 个包 / %s" % (d["bn"], backend.human(d["bb"])))
        if d["btotal"] >= 0:
            self.lb_bmanifest.setText("清单 %d 个包 / %s" % (d["btotal"], backend.human(d["bsize"])))
        else:
            self.lb_bmanifest.setText("清单读取失败（检查网络或 _manifest_cache.xml）")

        rows = d["rows"]
        self.table.setRowCount(len(rows))
        for i, (name, cnt, size) in enumerate(rows):
            self.table.setItem(i, 0, QTableWidgetItem(name))
            it1 = QTableWidgetItem(str(cnt))
            it1.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(i, 1, it1)
            it2 = QTableWidgetItem(backend.human(size))
            it2.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(i, 2, it2)
        self.lb_total.setText("合计 %d 个文件 / %s" % (d["n"], backend.human(d["b"])))

    # ------------------------------------------------------------ 任务
    def _set_busy(self, busy, label="空闲"):
        self._busy = busy
        for b in (self.btn_cov, self.btn_cover, self.btn_prewarm, self.btn_ball, self.btn_bcat):
            b.setEnabled(not busy)
        self.lb_prog.setText(label)
        if not busy:
            self.progress.setValue(0)

    def _run_task(self, fn, label):
        if self._busy:
            return
        self._set_busy(True, label)
        self.log_view.line("")
        self.log_view.line("==== %s ====" % label)

        def _log(s):
            self.log_view.line(s)

        def _prog(pct, text):
            self.progress.setValue(pct)
            self.lb_prog.setText(text)

        def job(w):
            return fn(w)

        def done(_r):
            self._set_busy(False, "完成")
            self.refresh()

        self.run(job, on_done=done, log=_log, progress=_prog)

    def _do_prewarm(self):
        jobs = self.spin_jobs.value()
        self._run_task(lambda w: backend.prewarm(jobs, emit=w.log.emit), "清单预热")

    def _do_cover(self):
        self._run_task(lambda w: backend.cover(emit=w.log.emit), "补齐上局缺失")

    def _do_battle(self, patterns):
        jobs = self.spin_jobs.value()

        def job(w):
            return backend.battle_preload(patterns, jobs, emit=w.log.emit,
                                          progress=lambda p, t: w.progress.emit(p, t))

        self._run_task(job, "战斗资源预下载 " + " ".join(patterns))

    def _choose_battle(self):
        dlg = CategoryDialog(self)
        if dlg.exec() == QDialog.Accepted:
            sel = dlg.selected()
            if sel:
                self._do_battle(sel)
