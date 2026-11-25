# -*- coding: utf-8 -*-
"""SSJJ 加速器 - 图形界面 (tkinter, 无第三方依赖)"""
import os
import sys
import json
import threading
import queue
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import core

HERE = os.path.dirname(os.path.abspath(__file__))


class App:
    def __init__(self, root, args=None):
        self.root = root
        self.args = args or type("A", (), {})()
        self.game_dir = tk.StringVar(value=getattr(self.args, "game_dir", r"C:\Program Files (x86)\Wooduan\SSJJ-4399"))
        self.preload_dir = tk.StringVar(value=getattr(self.args, "preload_dir", core.default_preload_dir()))
        self.base = tk.StringVar(value=getattr(self.args, "base", core.DEFAULT_BASE))
        self.jobs = tk.IntVar(value=8)
        self.q = queue.Queue()
        self.running = False
        root.title("SSJJ 加速器 - 资源预下载 & 本地加速")
        root.geometry("820x720")
        self._build()
        self._pump()
        self.svc_refresh()

    def _build(self):
        f = ttk.Frame(self.root, padding=10)
        f.pack(fill="both", expand=True)

        # 路径配置
        cfg = ttk.LabelFrame(f, text="路径配置", padding=8)
        cfg.pack(fill="x", pady=(0, 8))
        for i, (label, var) in enumerate([("游戏目录", self.game_dir), ("缓存目录", self.preload_dir), ("资源CDN", self.base)]):
            ttk.Label(cfg, text=label, width=10).grid(row=i, column=0, sticky="w", pady=2)
            ttk.Entry(cfg, textvariable=var, width=62).grid(row=i, column=1, sticky="ew", pady=2)
        cfg.columnconfigure(1, weight=1)

        # 缓存状态
        st = ttk.LabelFrame(f, text="缓存状态", padding=8)
        st.pack(fill="x", pady=(0, 8))
        self.status_lbl = ttk.Label(st, text="点击「刷新状态」查看本地资源缓存情况")
        self.status_lbl.pack(anchor="w")
        ttk.Button(st, text="刷新状态", command=self.refresh_status).pack(anchor="e", pady=(4, 0))

        # 本地加速服务
        sv = ttk.LabelFrame(f, text="本地加速服务（大厅资源）", padding=8)
        sv.pack(fill="x", pady=(0, 8))
        self.svc_lbl = ttk.Label(sv, text="检查中…", justify="left")
        self.svc_lbl.pack(anchor="w")
        srow = ttk.Frame(sv)
        srow.pack(anchor="e", pady=(4, 0))
        ttk.Button(srow, text="启动服务", command=lambda: self.svc_action("start")).pack(side="left", padx=4)
        ttk.Button(srow, text="停止并还原 hosts", command=lambda: self.svc_action("stop")).pack(side="left", padx=4)
        ttk.Button(srow, text="刷新", command=self.svc_refresh).pack(side="left", padx=4)

        # 操作区
        op = ttk.LabelFrame(f, text="操作", padding=8)
        op.pack(fill="x", pady=(0, 8))
        ttk.Label(op, text="并发线程").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(op, from_=2, to=32, textvariable=self.jobs, width=6).grid(row=0, column=1, sticky="w", padx=(4, 16))
        ttk.Button(op, text="预下载全部资源 (17.3GB)", command=lambda: self.start("preload_all")).grid(row=0, column=2, padx=4)
        ttk.Button(op, text="选择分类下载", command=self.choose_category).grid(row=0, column=3, padx=4)

        ttk.Button(op, text="预抓取大厅资源", command=lambda: self.start("lobby")).grid(row=1, column=2, padx=4, pady=(6, 0))
        ttk.Label(op, text="大厅资源预抓取会把 res_config 清单里缺失的资源补到本地库，供本地加速服务回放。").grid(
            row=2, column=0, columnspan=5, sticky="w", pady=(6, 0))

        # 进度
        pf = ttk.LabelFrame(f, text="进度", padding=8)
        pf.pack(fill="x", pady=(0, 8))
        self.prog = ttk.Progressbar(pf, mode="determinate", value=0)
        self.prog.pack(fill="x")
        self.prog_lbl = ttk.Label(pf, text="空闲")
        self.prog_lbl.pack(anchor="w", pady=(4, 0))

        # 日志
        lf = ttk.LabelFrame(f, text="日志", padding=6)
        lf.pack(fill="both", expand=True)
        self.log = tk.Text(lf, height=12, wrap="word")
        self.log.pack(fill="both", expand=True, side="left")
        sb = ttk.Scrollbar(lf, command=self.log.yview)
        sb.pack(fill="y", side="right")
        self.log.config(yscrollcommand=sb.set)

        self._log("就绪。\n"
                  "「本地加速服务」用于大厅资源：命中本地库时毫秒级返回，未命中自动回源并存下来。\n"
                  "「预下载全部资源」用于战斗资源（AssetBundle），下完点地图/武器秒开。\n"
                  "启动服务需要管理员权限（要写 hosts）。")

    def _log(self, s):
        self.log.insert("end", s + "\n")
        self.log.see("end")

    def _pump(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "log":
                    self._log(data)
                elif kind == "prog":
                    self.prog["value"] = data[0]
                    self.prog_lbl.config(text=data[1])
                elif kind == "done":
                    self.running = False
                    self._log("[完成] " + str(data))
        except queue.Empty:
            pass
        self.root.after(150, self._pump)

    def emit(self, kind, data):
        self.q.put((kind, data))

    def refresh_status(self):
        d = self.preload_dir.get()
        if not os.path.isdir(d):
            self.status_lbl.config(text="战斗资源缓存目录不存在（尚无本地资源）")
            return
        total = cnt = 0
        for n in os.listdir(d):
            p = os.path.join(d, n, "data")
            if os.path.isfile(p):
                total += os.path.getsize(p)
                cnt += 1
        self.status_lbl.config(text="战斗资源：已缓存 %d 个包，占用 %.2f GB" % (cnt, total / 1024 ** 3))
        self.refresh_lobby_status()

    def refresh_lobby_status(self):
        """大厅资源库（local_cdn 的库）单独统计，和战斗资源分开。"""
        import ssjj_paths as P
        d = P.CACHE_DIR
        if not os.path.isdir(d):
            return
        cnt = total = 0
        for root, _, fs in os.walk(d):
            for f in fs:
                if f.endswith(".part"):
                    continue
                cnt += 1
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        self.status_lbl.config(
            text="战斗资源：已缓存；大厅资源库：%d 个文件 %.2f GB" % (cnt, total / 1024 ** 3))

    # ---------------- 本地加速服务 ----------------
    def svc_refresh(self):
        """探测 local_cdn 是否在跑，并拉一次统计。"""
        import local_cdn as C
        if not C._ping():
            self.svc_lbl.config(text="状态：未运行")
            return
        try:
            import http.client as hc
            c = hc.HTTPConnection("127.0.0.1", 80, timeout=3)
            c.request("GET", "/__stats__")
            d = json.loads(c.getresponse().read().decode("utf-8"))
        except Exception as e:
            self.svc_lbl.config(text="状态：运行中（统计读取失败 %r）" % (e,))
            return
        hit, miss = d.get("hit", 0), d.get("miss", 0)
        rate = (100.0 * hit / (hit + miss)) if (hit + miss) else 0.0
        txt = ("状态：运行中   命中 %d / 回源 %d（%.1f%%）   新存 %d 个 %.1f MB"
               % (hit, miss, rate, d.get("save", 0), d.get("saved_mb", 0.0)))
        if d.get("upstream_fail"):
            txt += "   回源失败 %d" % d["upstream_fail"]
        self.svc_lbl.config(text=txt)

    def svc_action(self, what):
        """启停 local_cdn。启动要写 hosts，所以走 UAC 提权。"""
        import local_cdn as C
        try:
            if what == "start":
                C.hosts_set(True)
                import subprocess
                pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
                if not os.path.isfile(pyw):
                    pyw = sys.executable
                subprocess.Popen([pyw, os.path.join(HERE, "local_cdn.py"), "daemon"],
                                 cwd=HERE, close_fds=True)
                self._log("[*] 已拉起本地加速服务（写 hosts 失败的话请用管理员运行）")
            else:
                C.hosts_set(False)
                C._kill_pid_file(C.DAEMON_PID)
                C._del(C.DAEMON_PID)
                C._kill_pid_file(C.PID_FILE)
                C._del(C.PID_FILE)
                self._log("[*] 已停止服务并还原 hosts")
        except Exception as e:
            self._log("[!] 操作失败: %r" % (e,))
        self.root.after(1500, self.svc_refresh)

    def choose_category(self):
        cats = ["maps/", "weaponskin/", "weapon/", "player/", "lobbyweaponskin/",
                "lobbyweapon/", "ui/", "playeranim/", "effect/", "sound/", "all"]
        win = tk.Toplevel(self.root)
        win.title("选择分类")
        win.geometry("320x420")
        ttk.Label(win, text="可多选（按住 Ctrl）").pack(anchor="w", padx=10, pady=6)
        lb = tk.Listbox(win, selectmode="multiple")
        for c in cats:
            lb.insert("end", c)
        lb.pack(fill="both", expand=True, padx=10)
        def go():
            sel = [lb.get(i) for i in lb.curselection()]
            win.destroy()
            if sel:
                self.start("preload_cats", sel)
        ttk.Button(win, text="开始下载", command=go).pack(pady=8)

    def start(self, action, payload=None):
        if self.running:
            messagebox.showinfo("提示", "有任务正在运行")
            return
        self.running = True
        self.prog["value"] = 0
        t = threading.Thread(target=self._worker, args=(action, payload), daemon=True)
        t.start()

    def _worker(self, action, payload):
        try:
            if action == "preload_all":
                self._do_preload(["all"])
            elif action == "preload_cats":
                self._do_preload(payload)
            elif action == "lobby":
                self._do_lobby()
        except Exception as ex:
            self.emit("log", "[错误] %r" % ex)
            self.emit("done", "异常中断")

    def _ctx(self):
        class C: pass
        c = C()
        c.game_dir = self.game_dir.get()
        c.preload_dir = self.preload_dir.get()
        c.base = self.base.get()
        c.platform = core.DEFAULT_PLATFORM
        return c

    def _do_preload(self, match):
        c = self._ctx()
        import ssjj_boost
        self.emit("log", "[*] 加载清单 ...")
        items, src = ssjj_boost.load_manifest(c.base, c.platform, refresh=False)
        selected = core.select_items(items, match)
        selected.sort(key=lambda x: x["size"])
        total = sum(i["size"] for i in selected)
        self.emit("log", "[*] %d 个包 / %.2f GB，来源 %s" % (len(selected), total / 1024 ** 3, src))
        done_bytes = 0
        stats = {}
        jobs = self.jobs.get()
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=jobs) as ex:
            futs = {ex.submit(core.download_one, it, c.preload_dir, c.base, c.platform, False, True): it for it in selected}
            for idx, fu in enumerate(as_completed(futs), 1):
                st, it, info = fu.result()
                stats[st] = stats.get(st, 0) + 1
                if st in ("ok", "skip"):
                    done_bytes += it["size"]
                else:
                    self.emit("log", "  [!] %s %s %s" % (st, it["name"][:40], info))
                if idx % 10 == 0 or idx == len(selected):
                    self.emit("prog", (int(100.0 * idx / len(selected)),
                                       "%d/%d | %.2f/%.2f GB" % (idx, len(selected), done_bytes / 1024 ** 3, total / 1024 ** 3)))
        self.emit("done", str(stats))

    def _do_lobby(self):
        c = self._ctx()
        import ssjj_boost
        a = self._ctx()
        a.jobs = self.jobs.get()
        ssjj_boost.cmd_lobby(a)
        self.emit("done", "大厅资源已抓取")


def main(args=None):
    root = tk.Tk()
    App(root, args)
    root.mainloop()
