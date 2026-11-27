# -*- coding: utf-8 -*-
import inspect
import os
import subprocess
import threading
import time

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QCheckBox, QGridLayout, QHBoxLayout, QLabel,
                               QMessageBox, QProgressBar, QPushButton)

import backend
from ui.diagnostic_export import choose_diagnostic_path
from ui.widgets import Badge, Card, ScrollPage, dim, page_header

RELEASES_URL = "https://github.com/ColdMoonBUG/SSJJ-Tools/releases"


def _fmt_speed(bps):
    if bps <= 0:
        return ""
    if bps >= 1048576.0:
        return "%.2f MB/s" % (bps / 1048576.0)
    return "%.0f KB/s" % (bps / 1024.0)


def _fmt_eta(seconds):
    if seconds <= 0 or seconds > 36000:
        return ""
    sec = int(seconds)
    if sec >= 60:
        return "剩余 %d分%02d秒" % (sec // 60, sec % 60)
    return "剩余 %d秒" % sec


class ToolsPage(ScrollPage):
    TITLE = "设置"

    def __init__(self, app):
        super().__init__(app)
        self._busy = False
        self._latest_info = None
        self._upd_cancel = None
        self._saved_upd_path = ""

        self.root.addWidget(page_header(
            "设置", "软件更新、诊断日志导出与常用目录。服务与 hosts 已全部整合至「本地加速」页。"))

        # ---------------- 软件更新
        upd = Card("软件更新", "从 GitHub Releases（ColdMoonBUG/SSJJ-Tools）拉取最新便携 exe", glyph="\uE895")
        self.badge_upd = Badge("当前 v%s" % backend.app_version())
        upd.add_header_widget(self.badge_upd)

        st = backend.load_app_settings()
        self.chk_auto_upd = QCheckBox("启动时自动检测新版本")
        self.chk_auto_upd.setChecked(bool(st.get("auto_check_update", True)))
        self.chk_auto_upd.toggled.connect(self._toggle_auto_update)
        upd.add(self.chk_auto_upd)

        r_upd = QHBoxLayout()
        r_upd.setSpacing(8)
        self.btn_check_upd = QPushButton("检查更新")
        self.btn_check_upd.clicked.connect(self._check_update)
        self.btn_pull_upd = QPushButton("立即拉取更新")
        self.btn_pull_upd.setObjectName("Primary")
        self.btn_pull_upd.setEnabled(False)
        self.btn_pull_upd.clicked.connect(self._pull_update)
        self.btn_cancel_upd = QPushButton("取消下载")
        self.btn_cancel_upd.setVisible(False)
        self.btn_cancel_upd.clicked.connect(self._cancel_update)
        self.btn_open_upd_dir = QPushButton("打开所在文件夹")
        self.btn_open_upd_dir.setVisible(False)
        self.btn_open_upd_dir.clicked.connect(lambda: self._open_update_dir())
        self.btn_open_rel = QPushButton("打开发布页")
        self.btn_open_rel.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(RELEASES_URL)))
        r_upd.addWidget(self.btn_check_upd)
        r_upd.addWidget(self.btn_pull_upd)
        r_upd.addWidget(self.btn_cancel_upd)
        r_upd.addWidget(self.btn_open_upd_dir)
        r_upd.addWidget(self.btn_open_rel)
        r_upd.addStretch(1)
        upd.add_layout(r_upd)

        self.prog_upd = QProgressBar()
        self.prog_upd.setRange(0, 100)
        self.prog_upd.setValue(0)
        self.prog_upd.setTextVisible(True)
        self.prog_upd.setFixedHeight(16)
        self.prog_upd.setStyleSheet(
            "QProgressBar {"
            "  background: #0f172a; border: 1px solid #1e293b; border-radius: 8px;"
            "  text-align: center; font-size: 11px; color: #f8fafc;"
            "}"
            "QProgressBar::chunk {"
            "  background: qlineargradient(x1:0, y1:0, x2:1, y2:0,"
            "    stop:0 #0ea5e9, stop:0.5 #38bdf8, stop:1 #6366f1);"
            "  border-radius: 7px;"
            "}"
        )
        self.prog_upd.setVisible(False)
        upd.add(self.prog_upd)

        self.lb_upd_msg = dim("点击「检查更新」查看 GitHub 最新版本；仅拉取 .exe 便携包。", small=True)
        self.lb_upd_msg.setWordWrap(True)
        self.lb_upd_msg.setTextInteractionFlags(Qt.TextSelectableByMouse)
        upd.add(self.lb_upd_msg)
        self.root.addWidget(upd)

        # ---------------- 诊断日志导出
        diag = Card("诊断日志", "遇到进游戏卡住或闪退时，导出脱敏日志发到反馈群排查", glyph="\uE9F9")
        r_diag = QHBoxLayout()
        r_diag.setSpacing(8)
        self.btn_export = QPushButton("导出本次诊断日志")
        self.btn_export.setToolTip("选择保存位置，导出游戏、工具箱与 Windows 崩溃日志（自动脱敏）")
        self.btn_export.clicked.connect(self._export_diagnostics)
        r_diag.addWidget(self.btn_export)
        r_diag.addStretch(1)
        diag.add_layout(r_diag)

        self.lb_export_msg = dim("导出文件不含账号密码等私密信息，可放心发给开发者排查。", small=True)
        self.lb_export_msg.setWordWrap(True)
        self.lb_export_msg.setTextInteractionFlags(Qt.TextSelectableByMouse)
        diag.add(self.lb_export_msg)
        self.root.addWidget(diag)

        # ---------------- 常用目录
        dirs = Card("目录", "一键直达", glyph="\uE8B7")
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
        self.lb_data.setTextInteractionFlags(Qt.TextSelectableByMouse)
        dirs.add(self.lb_data)
        self.root.addWidget(dirs)

        # ---------------- 关于
        about = Card("关于", glyph="\uE946")
        g = QGridLayout()
        g.setHorizontalSpacing(24)
        g.setVerticalSpacing(8)
        for i, (k, v) in enumerate((
            ("名称", "生死狙击工具箱"),
            ("版本", "v%s" % backend.app_version()),
            ("打包模式", "单文件便携 exe" if backend.IS_FROZEN else "源码运行"),
            ("BUG 反馈群", "QQ群：193808764"),
            ("更新仓库", "https://github.com/ColdMoonBUG/SSJJ-Tools"),
        )):
            lab = QLabel(k)
            lab.setObjectName("Dim")
            g.addWidget(lab, i, 0)
            val = QLabel(v)
            val.setWordWrap(True)
            val.setTextInteractionFlags(Qt.TextSelectableByMouse)
            g.addWidget(val, i, 1)
        g.setColumnStretch(1, 1)
        about.add_layout(g)
        self.root.addWidget(about)
        self.root.addStretch(1)

    # ------------------------------------------------------------ 状态
    def on_show(self):
        self.refresh()

    def refresh(self):
        st = backend.load_app_settings()
        self.chk_auto_upd.blockSignals(True)
        self.chk_auto_upd.setChecked(bool(st.get("auto_check_update", True)))
        self.chk_auto_upd.blockSignals(False)

    # ------------------------------------------------------------ 更新
    def _toggle_auto_update(self, checked):
        backend.save_app_setting("auto_check_update", bool(checked))

    def apply_update_info(self, info):
        if not isinstance(info, dict):
            self.badge_upd.setText("检查失败")
            self.badge_upd.state("bad")
            self.lb_upd_msg.setText("检查更新异常，请检查网络后重试，或点击「打开发布页」手动查看。")
            self.btn_pull_upd.setEnabled(False)
            return
        self._latest_info = info
        if not info.get("ok"):
            self.badge_upd.setText("检查失败")
            self.badge_upd.state("bad")
            self.lb_upd_msg.setText(str(info.get("error") or "无法连接 GitHub"))
            self.btn_pull_upd.setEnabled(False)
            return
        if info.get("has_update"):
            latest = info.get("latest") or info.get("tag")
            asset = info.get("asset_name") or ""
            sz_mb = (info.get("asset_size") or 0) / 1048576.0
            self.badge_upd.setText("发现新版本 v%s" % latest)
            self.badge_upd.state("ok")
            if info.get("download_url"):
                self.btn_pull_upd.setEnabled(True)
                note = (info.get("notes") or "").strip().splitlines()
                brief = (" | " + note[0][:80]) if note else ""
                self.lb_upd_msg.setText("可更新至 v%s（%s，%.1f MB）%s" % (latest, asset, sz_mb, brief))
            else:
                self.btn_pull_upd.setEnabled(False)
                self.lb_upd_msg.setText("发现新版本 v%s，但该 Release 未附带 .exe，请点「打开发布页」查看。" % latest)
        else:
            self.badge_upd.setText("已是最新 v%s" % backend.app_version())
            self.badge_upd.state("ok")
            msg = info.get("error") or ("当前已是最新版本（v%s）" % backend.app_version())
            self.lb_upd_msg.setText(msg)
            self.btn_pull_upd.setEnabled(False)

    def _check_update(self):
        if self._busy:
            return
        self._busy = True
        self.btn_check_upd.setEnabled(False)
        self.badge_upd.setText("检查中…")
        self.badge_upd.state("")
        self.lb_upd_msg.setText("正在连接 GitHub 获取最新版本信息…")

        def job(_w):
            return backend.check_update()

        def done(info):
            self._busy = False
            self.btn_check_upd.setEnabled(True)
            self.apply_update_info(info)

        self.run(job, on_done=done)

    def _open_update_dir(self, path=None):
        target = str(path or self._saved_upd_path or "").strip()
        if not target:
            return
        norm = os.path.normpath(target)
        try:
            if os.path.isfile(norm):
                subprocess.Popen(["explorer.exe", "/select,%s" % norm])
                return
            folder = norm if os.path.isdir(norm) else os.path.dirname(norm)
            if folder:
                backend.open_path(folder)
        except Exception:
            folder = os.path.dirname(norm)
            if folder:
                backend.open_path(folder)

    def _cancel_update(self):
        if self._upd_cancel is not None:
            self._upd_cancel.set()
        self.btn_cancel_upd.setEnabled(False)
        self.badge_upd.setText("取消中…")
        self.badge_upd.state("")
        self.lb_upd_msg.setText("正在取消下载，请稍候…")

    def _on_upd_progress(self, pct, detail):
        if pct < 0:
            if self.prog_upd.minimum() != 0 or self.prog_upd.maximum() != 0:
                self.prog_upd.setRange(0, 0)
        else:
            if self.prog_upd.minimum() != 0 or self.prog_upd.maximum() != 100:
                self.prog_upd.setRange(0, 100)
            val = max(0, min(100, int(pct)))
            if self.prog_upd.value() != val:
                self.prog_upd.setValue(val)
        if detail and self.lb_upd_msg.text() != detail:
            self.lb_upd_msg.setText(detail)

    def _pull_update(self):
        info = self._latest_info or {}
        url = info.get("download_url")
        asset = info.get("asset_name")
        size = info.get("asset_size") or 0
        expected_sha256 = str(info.get("asset_sha256") or "").strip()
        if not url:
            self.lb_upd_msg.setText("请先点击「检查更新」获取下载地址。")
            return
        if self._busy:
            return
        self._busy = True
        self._upd_cancel = threading.Event()
        self._saved_upd_path = ""
        cancel_ev = self._upd_cancel
        self.btn_check_upd.setEnabled(False)
        self.btn_pull_upd.setEnabled(False)
        self.btn_open_upd_dir.setVisible(False)
        self.btn_cancel_upd.setEnabled(True)
        self.btn_cancel_upd.setVisible(True)
        self.prog_upd.setRange(0, 0)
        self.prog_upd.setVisible(True)
        self.badge_upd.setText("下载中…")
        self.badge_upd.state("")
        self.lb_upd_msg.setText("正在测速选择最快加速线路，准备下载 %s…" % (asset or "新版本"))

        def job(w):
            state = {
                "mirror": "",
                "last_emit": 0.0,
                "last_t": time.monotonic(),
                "last_got": 0,
                "speed": 0.0,
            }

            def on_status(payload):
                now = time.monotonic()
                if isinstance(payload, dict):
                    stage = str(payload.get("stage") or "downloading").strip().lower()
                    mirror = str(payload.get("mirror") or state["mirror"] or "").strip()
                    if mirror:
                        state["mirror"] = mirror
                    if stage == "probing":
                        w.progress.emit(-1, "正在测速选择最快加速线路…")
                        return
                    if stage == "verifying":
                        w.progress.emit(100, "下载完成，正在校验文件完整性…")
                        return
                    if stage == "done":
                        w.progress.emit(100, "校验通过，准备就绪…")
                        return
                    got = int(payload.get("got") or 0)
                    total = int(payload.get("total") or 0)
                    speed = float(payload.get("speed") or 0.0)
                    eta = float(payload.get("eta") if payload.get("eta") is not None else -1.0)
                    pct = int(payload.get("pct")) if payload.get("pct") is not None else (
                        min(100, int(got * 100 / total)) if total > 0 else -1
                    )
                    if total <= 0:
                        pct = -1
                    if got > 0 and (total <= 0 or got < total) and (now - state["last_emit"]) < 0.15:
                        return
                    state["last_emit"] = now
                    parts = []
                    if total > 0:
                        parts.append("%d%%" % max(0, min(100, pct)))
                        parts.append("%.1f / %.1f MB" % (got / 1048576.0, total / 1048576.0))
                    else:
                        parts.append("%.1f MB" % (got / 1048576.0))
                    sp = _fmt_speed(speed)
                    if sp:
                        parts.append(sp)
                    et = _fmt_eta(eta)
                    if et:
                        parts.append(et)
                    if mirror:
                        parts.append(mirror)
                    w.progress.emit(pct, " · ".join(parts))
                    return

                text = str(payload or "").strip()
                if text:
                    state["mirror"] = text
                    w.progress.emit(-1, text)

            def on_prog(got, total, *extra):
                now = time.monotonic()
                mirror = state["mirror"]
                ext_speed = None
                for item in extra:
                    if isinstance(item, str) and item.strip():
                        mirror = item.strip()
                        state["mirror"] = mirror
                    elif isinstance(item, (int, float)) and item >= 0:
                        ext_speed = float(item)
                if got < state["last_got"]:
                    state["last_got"] = 0
                    state["last_t"] = now
                    state["speed"] = 0.0
                dt = now - state["last_t"]
                if ext_speed is not None:
                    state["speed"] = ext_speed
                elif dt >= 0.25:
                    inst = (got - state["last_got"]) / dt
                    state["speed"] = inst if state["speed"] <= 0 else (state["speed"] * 0.6 + inst * 0.4)
                    state["last_got"] = got
                    state["last_t"] = now
                if got > 0 and (total <= 0 or got < total) and (now - state["last_emit"]) < 0.15:
                    return
                state["last_emit"] = now
                parts = []
                if total > 0:
                    pct = min(100, int(got * 100 / total))
                    parts.append("%d%%" % pct)
                    parts.append("%.1f / %.1f MB" % (got / 1048576.0, total / 1048576.0))
                else:
                    pct = -1
                    parts.append("%.1f MB" % (got / 1048576.0))
                sp = _fmt_speed(state["speed"])
                if sp:
                    parts.append(sp)
                if total > got > 0 and state["speed"] > 0:
                    eta = _fmt_eta((total - got) / state["speed"])
                    if eta:
                        parts.append(eta)
                if mirror:
                    parts.append(mirror)
                w.progress.emit(pct, " · ".join(parts))

            kwargs = {"expected_size": size}
            try:
                params = inspect.signature(backend.download_update).parameters
                if "status_cb" in params:
                    # 后端支持结构化 status_cb 时仅传单一信号源，避免双回调互殴导致进度条抽搐
                    kwargs["progress_cb"] = None
                    kwargs["status_cb"] = on_status
                else:
                    kwargs["progress_cb"] = on_prog
                if "cancel_event" in params:
                    kwargs["cancel_event"] = cancel_ev
                if "expected_sha256" in params and expected_sha256:
                    kwargs["expected_sha256"] = expected_sha256
            except (TypeError, ValueError):
                kwargs["progress_cb"] = on_prog
            return backend.download_update(url, asset, **kwargs)

        def done(res):
            was_cancelled = bool(cancel_ev.is_set())
            self._busy = False
            self._upd_cancel = None
            self.btn_cancel_upd.setVisible(False)
            self.btn_cancel_upd.setEnabled(True)
            self.btn_check_upd.setEnabled(True)
            ok, msg, saved_path, bat_path = res or (False, "下载未完成", "", "")
            if not ok:
                self.prog_upd.setVisible(False)
                self.prog_upd.setRange(0, 100)
                self.prog_upd.setValue(0)
                self.btn_open_upd_dir.setVisible(False)
                if was_cancelled or "取消" in str(msg):
                    self.badge_upd.setText("已取消")
                    self.badge_upd.state("")
                    self.lb_upd_msg.setText("已取消下载，可随时点击「立即拉取更新」重新下载。")
                else:
                    self.badge_upd.setText("下载失败")
                    self.badge_upd.state("bad")
                    self.lb_upd_msg.setText("拉取更新失败：%s（可重试或点「打开发布页」手动下载）" % msg)
                self.btn_pull_upd.setEnabled(True)
                return

            self._saved_upd_path = str(saved_path or "")
            self.prog_upd.setRange(0, 100)
            self.prog_upd.setValue(100)
            self.prog_upd.setVisible(True)
            self.badge_upd.setText("下载完成")
            self.badge_upd.state("ok")
            if self._saved_upd_path:
                self.btn_open_upd_dir.setVisible(True)
            in_appdata = "ssjj-toolbox" in self._saved_upd_path.lower() and "updates" in self._saved_upd_path.lower()
            loc_hint = "（当前目录不可写，已回落保存至 AppData 更新目录）" if in_appdata else "（已保存在当前工具箱同目录）"
            self.lb_upd_msg.setText("新版已保存%s：%s" % (loc_hint, self._saved_upd_path))

            box = QMessageBox(self)
            box.setWindowTitle("更新已就绪")
            box.setIcon(QMessageBox.Information)
            box.setText(
                "新版本已下载并校验完成，保存位置：\n%s\n%s"
                % (self._saved_upd_path, loc_hint)
            )
            btn_launch = None
            if bat_path:
                btn_launch = box.addButton("立即关闭并启动新版", QMessageBox.AcceptRole)
            btn_folder = box.addButton("打开所在文件夹", QMessageBox.ActionRole)
            box.addButton("稍后手动启动", QMessageBox.RejectRole)
            box.setDefaultButton(btn_launch or btn_folder)
            box.exec()
            clicked = box.clickedButton()
            if btn_launch is not None and clicked is btn_launch:
                launched = False
                try:
                    launched = bool(backend.launch_update_script(bat_path))
                except Exception:
                    launched = False
                if launched:
                    self.app.close()
                else:
                    self.badge_upd.setText("请手动启动")
                    self.badge_upd.state("bad")
                    self.lb_upd_msg.setText(
                        "自动拉起新版本失败，请点击「打开所在文件夹」手动运行：%s" % self._saved_upd_path
                    )
            elif clicked is btn_folder:
                self._open_update_dir(self._saved_upd_path)

        self.run(job, on_done=done, log=self.lb_upd_msg.setText, progress=self._on_upd_progress)

    # ------------------------------------------------------------ 诊断日志导出
    def _export_diagnostics(self):
        destination = choose_diagnostic_path(self)
        if not destination:
            return
        self.btn_export.setEnabled(False)
        self.lb_export_msg.setText("正在导出本次会话诊断日志…")

        def job(_w):
            try:
                return True, backend.export_diagnostics(destination)
            except Exception as exc:
                return False, repr(exc)

        def done(result):
            self.btn_export.setEnabled(True)
            ok, msg = result or (False, "导出未完成")
            self.lb_export_msg.setText(("诊断文件已保存：" if ok else "导出失败：") + str(msg))

        self.run(job, on_done=done)
