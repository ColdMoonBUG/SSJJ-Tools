# -*- coding: utf-8 -*-
"""路径引导：源码运行用 ssjj-tool 根目录，打包后数据目录默认取 exe 所在目录。"""
import json
import os
import sys
from pathlib import Path


def app_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def source_root():
    if getattr(sys, "frozen", False):
        return app_dir()
    return app_dir().parent


def data_dir():
    env = os.environ.get("SSJJ_TOOLBOX_DATA")
    if env and Path(env).is_dir():
        return Path(env)
    cfg = app_dir() / "toolbox_data.json"
    try:
        d = json.loads(cfg.read_text(encoding="utf-8"))
        if d.get("data_root"):
            return Path(d["data_root"])
    except Exception:
        pass
    for cand in (app_dir(), app_dir().parent):
        if (cand / "lobby_res_cache").is_dir():
            return cand
    # 免安装分发：exe 放在只读位置（U 盘只读、Program Files、压缩包内直接双击）时，
    # 数据目录退回 %LOCALAPPDATA%，保证换肤配置/载荷/日志照样能写。
    if getattr(sys, "frozen", False) and not _writable(app_dir()):
        return fallback_dir()
    return app_dir()


def _writable(p):
    try:
        p = Path(p)
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".ssjj_write_probe"
        probe.write_text("1", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


def fallback_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / "ssjj-toolbox" / "data"


def bootstrap():
    root = source_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    data = data_dir()
    if getattr(sys, "frozen", False):
        import ssjj_paths as P
        P.HERE = str(data)
        P.CACHE_DIR = os.path.join(str(data), "lobby_res_cache")
        P.MANIFEST_DIR = os.path.join(P.CACHE_DIR, "config")
        P.UPSTREAM_FILE = os.path.join(str(data), "upstream.json")
    return data
