# -*- coding: utf-8 -*-
"""
SSJJ 公共路径与客户端参数读取
=============================
把「游戏装在哪、资源库在哪、paras.dat 里写了什么」集中到一处，
免得每个脚本各写一份硬编码路径（历史上就是因此写歪的）。

paras.dat 是 AIR 大厅预加载器读的启动参数文件（Mini.as:284 读它、
Mini.as:417-501 消费里面的 resurl / version / cdnIp / versionList），
编码为 UTF-8 with BOM。
"""
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))


def _is_valid_game_dir(d):
    if not d or not os.path.isdir(d):
        return False
    return any(
        os.path.isfile(os.path.join(d, rel))
        for rel in (
            "WDlauncher.exe",
            os.path.join("MicroClient64", "MicroClient.exe"),
            os.path.join("MicroClient", "MicroClient.exe"),
            "MicroClient.exe",
        )
    )


def _normalize_game_dir(path):
    """从 WDlauncher.exe / MicroClient.exe 或其所在目录归一化出游戏根目录。"""
    if not path:
        return None
    p = path.strip().strip('"').strip("'")
    if "," in p and not os.path.exists(p):
        p = p.split(",", 1)[0].strip().strip('"')
    if os.path.isfile(p):
        p = os.path.dirname(os.path.abspath(p))
    if not os.path.isdir(p):
        return None
    base = os.path.basename(p).lower()
    if base in ("microclient64", "microclient", "lobby"):
        parent = os.path.dirname(p)
        if _is_valid_game_dir(parent):
            return parent
    if _is_valid_game_dir(p):
        return p
    return None


def _detect_game_dir():
    """自动探测官方 4399 生死狙击安装目录（默认 -> 注册表协议/卸载项 -> 桌面快捷方式 -> 常见盘符）。"""
    default = r"C:\Program Files (x86)\Wooduan\SSJJ-4399"
    for d0 in (default, r"C:\Program Files\Wooduan\SSJJ-4399"):
        if _is_valid_game_dir(d0):
            return d0

    # 1. 注册表 URL 协议 + 卸载项（支持任意自定义安装路径）
    try:
        import winreg
        for root, sub in (
            (winreg.HKEY_CLASSES_ROOT, r"wd.ssjj.4399\shell\open\command"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Classes\wd.ssjj.4399\shell\open\command"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Classes\wd.ssjj.4399\shell\open\command"),
        ):
            try:
                with winreg.OpenKey(root, sub) as k:
                    val, _ = winreg.QueryValueEx(k, "")
                m = re.search(r'"([^"]*?(?:WDlauncher|MicroClient)\.exe)"', val, re.I) or \
                    re.search(r'(\S+?(?:WDlauncher|MicroClient)\.exe)', val, re.I)
                if m:
                    gd = _normalize_game_dir(m.group(1))
                    if gd:
                        return gd
            except OSError:
                pass

        for root, sub in (
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        ):
            try:
                with winreg.OpenKey(root, sub) as parent:
                    i = 0
                    while True:
                        try:
                            name = winreg.EnumKey(parent, i)
                            i += 1
                        except OSError:
                            break
                        try:
                            with winreg.OpenKey(parent, name) as sk:
                                vals = {}
                                for key in ("DisplayName", "InstallLocation", "DisplayIcon", "UninstallString"):
                                    try:
                                        vals[key], _ = winreg.QueryValueEx(sk, key)
                                    except OSError:
                                        pass
                            blob = " ".join(str(v) for v in vals.values())
                            if not any(tag in blob.lower() for tag in ("ssjj", "wooduan", "生死狙击")):
                                continue
                            for key in ("InstallLocation", "DisplayIcon", "UninstallString"):
                                gd = _normalize_game_dir(str(vals.get(key) or ""))
                                if gd:
                                    return gd
                        except OSError:
                            pass
            except OSError:
                pass
    except Exception:
        pass

    # 2. 扫描桌面快捷方式（.lnk 二进制中提取路径）
    try:
        desktops = [
            os.path.join(os.environ.get("USERPROFILE", ""), "Desktop"),
            os.path.join(os.environ.get("PUBLIC", r"C:\Users\Public"), "Desktop"),
        ]
        for desk in desktops:
            if not os.path.isdir(desk):
                continue
            for fn in os.listdir(desk):
                if not fn.lower().endswith(".lnk"):
                    continue
                if not any(tag in fn.lower() for tag in ("生死狙击", "ssjj", "4399", "微端")):
                    continue
                try:
                    raw = open(os.path.join(desk, fn), "rb").read()
                    for enc in ("utf-16le", "gbk", "latin1"):
                        txt = raw.decode(enc, errors="ignore")
                        for m in re.finditer(r'([A-Za-z]:\\[^\x00-\x1f"*?<>|]{2,180}?(?:WDlauncher|MicroClient)\.exe)', txt, re.I):
                            gd = _normalize_game_dir(m.group(1))
                            if gd:
                                return gd
                except Exception:
                    pass
    except Exception:
        pass

    # 3. 扫描常见盘符路径
    for drv in ("C", "D", "E", "F", "G", "H"):
        for rel in (
            r"Program Files (x86)\Wooduan\SSJJ-4399",
            r"Program Files\Wooduan\SSJJ-4399",
            r"Wooduan\SSJJ-4399",
            r"Games\SSJJ-4399",
            r"Game\SSJJ-4399",
            r"SSJJ-4399",
            r"4399\SSJJ-4399",
            r"生死狙击",
            r"4399生死狙击",
        ):
            cand = os.path.join(drv + ":\\", rel)
            if _is_valid_game_dir(cand):
                return cand
    return default


def refresh_game_dir():
    """重新探测游戏目录并更新模块级 GAME_DIR / PARAS。"""
    global GAME_DIR, PARAS
    GAME_DIR = _detect_game_dir()
    PARAS = os.path.join(GAME_DIR, "Lobby", "paras.dat")
    return GAME_DIR


GAME_DIR = _detect_game_dir()
PARAS = os.path.join(GAME_DIR, "Lobby", "paras.dat")

# 大厅资源库：/s<N>/ 与 /global_air/ 下的静态资源落在这里
CACHE_DIR = os.path.join(HERE, "lobby_res_cache")
# 清单文件（config/res_config_<md5>.xml）的实际存放处，随 CACHE_DIR
MANIFEST_DIR = os.path.join(CACHE_DIR, "config")
# 战斗资源库（AssetBundle）
PRELOAD_DIR = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Wooduan", "Preload")

UPSTREAM_FILE = os.path.join(HERE, "upstream.json")

# 官方 CDN。HOST 用于 hosts 重定向与回源 Host 头，BASE 用于拼下载 URL。
HOST = "cdn.4399ssjj.com"
BASE = "http://" + HOST

# 客户端要求的资源文件名形如 Lobby_<32位md5>.swf，ModuleLoader.as:124-134
# 会拿文件名里的 md5 和实际内容比对，不符就重载（默认 5 次）。
MD5_SUFFIX_RE = r"_[0-9a-f]{32}"


def read_paras(path=PARAS):
    """读 paras.dat，失败返回 {}。"""
    try:
        with open(path, "rb") as f:
            return json.loads(f.read().decode("utf-8-sig"))
    except Exception:
        return {}


def current_release(default="release_2026_11_10_20261119112920"):
    """大厅资源版本号，用作 ?version= 参数。"""
    return read_paras().get("version") or default


def current_server():
    """当前区服号（resurl 里的 /s<N>/）。"""
    try:
        return int(read_paras().get("serverId", 0))
    except Exception:
        return 0


def cdn_ip():
    """paras.dat 里客户端自己选定的 CDN 节点。"""
    ip = read_paras().get("cdnIp")
    return ip if ip else None


def find_manifest():
    """找 res_config_<md5>.xml，优先用 paras.dat 点名的那个。"""
    name = read_paras().get("resConfigFileName")
    if name:
        p = os.path.join(MANIFEST_DIR, name)
        if os.path.isfile(p):
            return p
    if os.path.isdir(MANIFEST_DIR):
        for fn in sorted(os.listdir(MANIFEST_DIR)):
            if fn.startswith("res_config") and fn.endswith(".xml"):
                return os.path.join(MANIFEST_DIR, fn)
    return None


def disk_free_gb(path="C:\\"):
    import shutil
    try:
        return shutil.disk_usage(path).free / 1024 ** 3
    except Exception:
        return 0.0
