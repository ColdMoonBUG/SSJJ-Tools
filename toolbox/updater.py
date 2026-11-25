# -*- coding: utf-8 -*-
"""GitHub Releases 自动检测与手动拉取更新（仅 .exe 便携包）。"""
import json
import os
import re
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

APP_VERSION = "1.1.8"
GITHUB_OWNER = "ColdMoonBUG"
GITHUB_REPO = "SSJJ-Tools"
RELEASES_API = "https://api.github.com/repos/%s/%s/releases/latest" % (GITHUB_OWNER, GITHUB_REPO)
RELEASES_PAGE = "https://github.com/%s/%s/releases" % (GITHUB_OWNER, GITHUB_REPO)
UA = "SSJJ-Toolbox/%s" % APP_VERSION


def parse_version(text):
    """把 'v1.1.4' / '生死狙击工具箱-1.1.4.exe' 解析成整数元组 (1, 1, 4)。"""
    m = re.search(r"(\d+(?:\.\d+){1,3})", str(text or ""))
    if not m:
        return (0,)
    try:
        return tuple(int(x) for x in m.group(1).split("."))
    except ValueError:
        return (0,)


def is_newer(remote_ver, current_ver=APP_VERSION):
    rv = parse_version(remote_ver)
    cv = parse_version(current_ver)
    n = max(len(rv), len(cv))
    rv = rv + (0,) * (n - len(rv))
    cv = cv + (0,) * (n - len(cv))
    return rv > cv


def _http_get_json(url, timeout=12):
    """优先 urllib，遇到本机代理证书校验异常时自动回退 Windows 自带 curl.exe。"""
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": UA,
        },
    )
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"_not_found": True}
        raise
    except Exception:
        # 回退系统 curl.exe（走 Windows Schannel 证书库，兼容系统代理）
        r = subprocess.run(
            ["curl.exe", "-sSL", "--max-time", str(int(timeout)),
             "-H", "Accept: application/vnd.github+json",
             "-H", "User-Agent: " + UA,
             url],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=0x08000000,
        )
        if r.returncode != 0 or not r.stdout.strip():
            raise RuntimeError("无法连接 GitHub（%s）" % (r.stderr.strip() or "网络超时"))
        data = json.loads(r.stdout)
        if isinstance(data, dict) and data.get("message") == "Not Found":
            return {"_not_found": True}
        return data


def _pick_exe_asset(assets):
    exes = [a for a in (assets or [])
            if str(a.get("name") or "").lower().endswith(".exe") and a.get("browser_download_url")]
    if not exes:
        return None
    exes.sort(key=lambda a: int(a.get("size") or 0), reverse=True)
    return exes[0]


def check_update(timeout=12):
    """查询 GitHub 最新 Release，返回结构化字典。"""
    base = {
        "ok": False,
        "has_update": False,
        "current": APP_VERSION,
        "latest": APP_VERSION,
        "tag": "",
        "title": "",
        "notes": "",
        "asset_name": "",
        "asset_size": 0,
        "download_url": "",
        "html_url": RELEASES_PAGE,
        "error": "",
    }
    try:
        rel = _http_get_json(RELEASES_API, timeout=timeout)
    except Exception as e:
        base["error"] = "检查更新失败：%s" % e
        return base

    if not isinstance(rel, dict) or rel.get("_not_found"):
        base["ok"] = True
        base["error"] = "仓库尚未发布任何 Release"
        return base

    if rel.get("message") and not rel.get("tag_name"):
        base["error"] = "GitHub 接口返回：%s" % rel.get("message")
        return base

    tag = str(rel.get("tag_name") or "").strip()
    title = str(rel.get("name") or tag).strip()
    notes = str(rel.get("body") or "").strip()
    asset = _pick_exe_asset(rel.get("assets"))
    ver_str = tag.lstrip("vV") or (asset.get("name") if asset else "")
    latest_ver = ".".join(str(x) for x in parse_version(ver_str)) if parse_version(ver_str) != (0,) else tag

    base.update({
        "ok": True,
        "has_update": is_newer(latest_ver, APP_VERSION),
        "latest": latest_ver or APP_VERSION,
        "tag": tag,
        "title": title,
        "notes": notes,
        "asset_name": str(asset.get("name") or "") if asset else "",
        "asset_size": int(asset.get("size") or 0) if asset else 0,
        "download_url": str(asset.get("browser_download_url") or "") if asset else "",
        "html_url": str(rel.get("html_url") or RELEASES_PAGE),
    })
    return base


def _writable_target_dir():
    if getattr(sys, "frozen", False):
        cand = Path(sys.executable).resolve().parent
        try:
            probe = cand / ".ssjj_update_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return cand
        except Exception:
            pass
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "ssjj-toolbox" / "updates"
    local.mkdir(parents=True, exist_ok=True)
    return local


def _download_file(url, dest_path, expected_size=0, progress_cb=None, timeout=180):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp, \
                open(dest_path, "wb") as f:
            total = int(resp.headers.get("Content-Length") or expected_size or 0)
            got = 0
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if progress_cb:
                    progress_cb(got, total)
        return
    except Exception:
        # 回退系统 curl.exe 下载
        if progress_cb:
            progress_cb(0, expected_size or 0)
        r = subprocess.run(
            ["curl.exe", "-sSL", "--fail", "--max-time", str(int(timeout)),
             "-A", UA, "-o", str(dest_path), url],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=0x08000000,
        )
        if r.returncode != 0:
            raise RuntimeError("下载失败：%s" % (r.stderr.strip() or ("curl 退出码 %d" % r.returncode)))
        if progress_cb:
            sz = os.path.getsize(dest_path) if os.path.isfile(dest_path) else 0
            progress_cb(sz, expected_size or sz)


def download_update(download_url, asset_name, expected_size=0, progress_cb=None):
    """下载新版 .exe 并校验 PE 头，返回 (ok, msg, saved_exe_path, launch_bat_path)。"""
    if not download_url:
        return False, "该 Release 未附带 .exe 安装包", "", ""
    safe_name = os.path.basename(asset_name or "生死狙击工具箱-new.exe")
    if not safe_name.lower().endswith(".exe"):
        safe_name += ".exe"

    target_dir = _writable_target_dir()
    tmp_path = target_dir / (safe_name + ".downloading")
    final_path = target_dir / safe_name

    # 如果目标文件名与当前正在运行的 exe 完全重名，先落盘到 .new.exe 再由切换脚本覆盖
    same_as_running = (
        getattr(sys, "frozen", False)
        and os.path.normcase(str(final_path)) == os.path.normcase(os.path.abspath(sys.executable))
    )
    staged_path = (target_dir / (safe_name + ".new")) if same_as_running else final_path

    try:
        _download_file(download_url, tmp_path, expected_size=expected_size, progress_cb=progress_cb)
        sz = tmp_path.stat().st_size if tmp_path.exists() else 0
        if sz < 1024 * 1024:
            raise RuntimeError("下载的文件过小（%d 字节），可能不完整" % sz)
        with open(tmp_path, "rb") as f:
            magic = f.read(2)
        if magic != b"MZ":
            raise RuntimeError("下载的文件不是有效的 Windows 可执行程序")
        os.replace(str(tmp_path), str(staged_path))
    except Exception as e:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass
        return False, str(e), "", ""

    bat_path = ""
    if getattr(sys, "frozen", False):
        fd, bat_path = tempfile.mkstemp(prefix="ssjj_update_", suffix=".bat")
        os.close(fd)
        cur_exe = os.path.abspath(sys.executable)
        if same_as_running:
            script = (
                "@echo off\r\n"
                "chcp 65001 >nul\r\n"
                "timeout /t 2 /nobreak >nul\r\n"
                'move /y "%s" "%s" >nul\r\n'
                'start "" "%s"\r\n'
                'del /f /q "%%~f0" >nul 2>&1\r\n'
                % (staged_path, cur_exe, cur_exe)
            )
        else:
            script = (
                "@echo off\r\n"
                "chcp 65001 >nul\r\n"
                "timeout /t 1 /nobreak >nul\r\n"
                'start "" "%s"\r\n'
                'del /f /q "%%~f0" >nul 2>&1\r\n'
                % (staged_path,)
            )
        Path(bat_path).write_text(script, encoding="utf-8")

    return True, "新版已下载：%s" % final_path, str(final_path), bat_path


def launch_update_script(bat_path):
    if not bat_path or not os.path.isfile(bat_path):
        return False
    subprocess.Popen(
        ["cmd.exe", "/c", bat_path],
        creationflags=0x08000000 | 0x00000008,
        close_fds=True,
    )
    return True
