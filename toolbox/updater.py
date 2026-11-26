# -*- coding: utf-8 -*-
"""GitHub Releases 自动检测与多源加速拉取更新（仅 .exe 便携包）。"""
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
import re
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

APP_VERSION = "1.1.10"
GITHUB_OWNER = "ColdMoonBUG"
GITHUB_REPO = "SSJJ-Tools"
RELEASES_API = "https://api.github.com/repos/%s/%s/releases/latest" % (GITHUB_OWNER, GITHUB_REPO)
RELEASES_PAGE = "https://github.com/%s/%s/releases" % (GITHUB_OWNER, GITHUB_REPO)
RELEASES_LATEST_WEB = "https://github.com/%s/%s/releases/latest" % (GITHUB_OWNER, GITHUB_REPO)
UA = "SSJJ-Toolbox/%s" % APP_VERSION

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008

# 国内 GitHub Release 加速镜像列表 + 官方直连兜底
DOWNLOAD_MIRRORS = [
    ("ghfast.top", "https://ghfast.top/"),
    ("gh-proxy.com", "https://gh-proxy.com/"),
    ("ghproxy.net", "https://ghproxy.net/"),
    ("GitHub 直连", ""),
]


class _DownloadCancelled(Exception):
    pass


class _IntegrityError(RuntimeError):
    pass


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


def extract_sha256(text):
    """从 Release 正文中提取 SHA256（支持中英文冒号与 Markdown 反引号）。"""
    m = re.search(r"SHA256[：:\s`*]+([0-9A-Fa-f]{64})", str(text or ""), re.IGNORECASE)
    return m.group(1).upper() if m else ""


def build_mirror_urls(url, mirrors=None):
    """根据原始 GitHub 下载链接构造有序候选镜像列表 [(mirror_name, candidate_url), ...]。"""
    raw = str(url or "").strip()
    if not raw:
        return []
    m_list = DOWNLOAD_MIRRORS if mirrors is None else list(mirrors)
    is_gh = raw.lower().startswith("https://github.com/") or raw.lower().startswith("http://github.com/")
    out = []
    seen = set()
    for name, prefix in m_list:
        if prefix and is_gh:
            cand = prefix.rstrip("/") + "/" + raw
        else:
            cand = raw
        if cand not in seen:
            seen.add(cand)
            out.append((name, cand))
    return out


def _curl_get(url, headers=None, timeout=12):
    """通过 Windows 自带 curl.exe 发起 GET，返回 (http_code, text_body, err_msg)。"""
    cmd = [
        "curl.exe", "-sS", "-L",
        "--connect-timeout", str(min(8, max(3, int(timeout)))),
        "--max-time", str(max(4, int(timeout))),
        "-A", UA,
        "-w", "\n%{http_code}",
    ]
    for k, v in (headers or {}).items():
        cmd += ["-H", "%s: %s" % (k, v)]
    cmd.append(url)
    try:
        r = subprocess.run(
            cmd,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception as e:
        return 0, "", str(e)
    if r.returncode != 0:
        return 0, "", (r.stderr.strip() or ("curl 退出码 %d" % r.returncode))
    body, _, code_str = r.stdout.rpartition("\n")
    code = int(code_str.strip()) if code_str.strip().isdigit() else 0
    return code, body, ""


def _http_get_json(url, timeout=12):
    """优先 curl.exe（兼容 Windows Schannel 与系统代理），失败再回退 urllib。"""
    headers = {"Accept": "application/vnd.github+json"}
    code, body, err = _curl_get(url, headers=headers, timeout=timeout)
    if code == 404:
        return {"_not_found": True}
    if code == 403:
        raise RuntimeError("GitHub API 限流（HTTP 403）")
    if 200 <= code < 300 and body.strip():
        data = json.loads(body)
        if isinstance(data, dict) and data.get("message") == "Not Found":
            return {"_not_found": True}
        return data

    # 回退 urllib
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
        raise RuntimeError("GitHub 接口 HTTP %d" % e.code)
    except Exception as e:
        raise RuntimeError("无法连接 GitHub（%s）" % (err or str(e) or "网络超时"))


def _pick_exe_asset(assets):
    exes = [a for a in (assets or [])
            if str(a.get("name") or "").lower().endswith(".exe") and a.get("browser_download_url")]
    if not exes:
        return None
    exes.sort(key=lambda a: int(a.get("size") or 0), reverse=True)
    return exes[0]


def _parse_human_size(text):
    m = re.search(r"(\d+(?:\.\d+)?)\s*(KB|MB|GB|B)\b", str(text or ""), re.IGNORECASE)
    if not m:
        return 0
    val = float(m.group(1))
    unit = m.group(2).upper()
    mult = {"B": 1, "KB": 1024, "MB": 1024 * 1024, "GB": 1024 * 1024 * 1024}.get(unit, 1)
    return int(val * mult)


def _check_update_web_fallback(timeout=12):
    """免 API 限流通道：跟随 /releases/latest 302 取 tag，再解析 expanded_assets 页面。"""
    # 1. 获取最新 tag（依次尝试直连与镜像）
    tag = ""
    effective_urls = [RELEASES_LATEST_WEB] + [
        p.rstrip("/") + "/" + RELEASES_LATEST_WEB for _n, p in DOWNLOAD_MIRRORS if p
    ]
    for target in effective_urls:
        try:
            r = subprocess.run(
                [
                    "curl.exe", "-sS", "-L",
                    "--connect-timeout", str(min(6, max(3, int(timeout)))),
                    "--max-time", str(max(6, int(timeout))),
                    "-A", UA,
                    "-o", os.devnull,
                    "-w", "%{http_code} %{url_effective}",
                    target,
                ],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                creationflags=CREATE_NO_WINDOW,
            )
            if r.returncode == 0 and r.stdout.strip():
                parts = r.stdout.strip().split(" ", 1)
                eff = parts[1] if len(parts) > 1 else ""
                m = re.search(r"/releases/tag/([^/?#]+)", eff)
                if m:
                    tag = m.group(1).strip()
                    break
        except Exception:
            continue

    if not tag:
        return None

    # 2. 拉取 expanded_assets HTML 提取 .exe 资产
    exp_url = "https://github.com/%s/%s/releases/expanded_assets/%s" % (GITHUB_OWNER, GITHUB_REPO, tag)
    asset_name = ""
    asset_url = ""
    asset_size = 0
    for target in [exp_url] + [p.rstrip("/") + "/" + exp_url for _n, p in DOWNLOAD_MIRRORS if p]:
        code, html, _err = _curl_get(target, timeout=timeout)
        if not (200 <= code < 300 and html):
            continue
        hrefs = re.findall(
            r'href="(/[^"]*/releases/download/[^"]+?\.exe)"',
            html, re.IGNORECASE,
        )
        if hrefs:
            rel_href = hrefs[0]
            asset_name = os.path.basename(rel_href)
            asset_url = "https://github.com" + rel_href
            # 顺便从附近文本估一个大小（精确校验在下载时由服务端 Content-Length 兜底）
            idx = html.find(rel_href)
            if idx >= 0:
                snippet = html[idx:idx + 600]
                asset_size = _parse_human_size(snippet)
            break

    # 若 expanded_assets 未解析出具体文件名，按仓库标准命名兜底
    if not asset_url:
        ver_clean = tag.lstrip("vV")
        asset_name = "SSJJ-Toolbox-%s.exe" % ver_clean
        asset_url = "https://github.com/%s/%s/releases/download/%s/%s" % (
            GITHUB_OWNER, GITHUB_REPO, tag, asset_name
        )

    return {
        "tag_name": tag,
        "name": "生死狙击工具箱 %s" % tag,
        "body": "",
        "html_url": "https://github.com/%s/%s/releases/tag/%s" % (GITHUB_OWNER, GITHUB_REPO, tag),
        "assets": [{
            "name": asset_name,
            "size": asset_size,
            "browser_download_url": asset_url,
        }],
        "_source": "web",
    }


def check_update(timeout=12):
    """查询 GitHub 最新 Release（API + 免限流 Web 双通道），返回结构化字典。"""
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
        "asset_sha256": "",
        "download_url": "",
        "asset_url": "",
        "html_url": RELEASES_PAGE,
        "release_url": RELEASES_PAGE,
        "source": "",
        "error": "",
    }
    rel = None
    api_err = ""
    try:
        rel = _http_get_json(RELEASES_API, timeout=timeout)
    except Exception as e:
        api_err = str(e)

    # API 失败、限流或返回无 tag_name 的错误对象时，自动回退免限流 Web 通道
    if rel is None or (isinstance(rel, dict) and not rel.get("_not_found") and not rel.get("tag_name")):
        if isinstance(rel, dict) and rel.get("message"):
            api_err = "GitHub 接口返回：%s" % rel.get("message")
        web_rel = _check_update_web_fallback(timeout=timeout)
        if web_rel is not None:
            rel = web_rel
        else:
            base["error"] = "检查更新失败：%s" % (api_err or "网络超时")
            return base

    if not isinstance(rel, dict) or rel.get("_not_found"):
        base["ok"] = True
        base["error"] = "仓库尚未发布任何 Release"
        return base

    tag = str(rel.get("tag_name") or "").strip()
    title = str(rel.get("name") or tag).strip()
    notes = str(rel.get("body") or "").strip()
    asset = _pick_exe_asset(rel.get("assets"))
    ver_str = tag.lstrip("vV") or (asset.get("name") if asset else "")
    latest_ver = ".".join(str(x) for x in parse_version(ver_str)) if parse_version(ver_str) != (0,) else tag
    dl_url = str(asset.get("browser_download_url") or "") if asset else ""
    rel_url = str(rel.get("html_url") or RELEASES_PAGE)
    sha256 = extract_sha256(notes)

    base.update({
        "ok": True,
        "has_update": is_newer(latest_ver, APP_VERSION),
        "latest": latest_ver or APP_VERSION,
        "tag": tag,
        "title": title,
        "notes": notes,
        "asset_name": str(asset.get("name") or "") if asset else "",
        "asset_size": int(asset.get("size") or 0) if asset else 0,
        "asset_sha256": sha256,
        "download_url": dl_url,
        "asset_url": dl_url,
        "html_url": rel_url,
        "release_url": rel_url,
        "source": str(rel.get("_source") or "api"),
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


def _probe_single_mirror(name, url, timeout=4.0):
    """发轻量 Range: bytes=0-1023 探活测速，返回 (ok, elapsed_sec, name, url)。"""
    t0 = time.monotonic()
    cmd = [
        "curl.exe", "-sS", "-L", "--fail",
        "--range", "0-1023",
        "--connect-timeout", str(min(3, max(1, int(timeout)))),
        "--max-time", str(max(2, int(timeout))),
        "-A", UA,
        "-o", os.devnull,
        "-w", "%{http_code}",
        url,
    ]
    try:
        r = subprocess.run(
            cmd,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        code = int(r.stdout.strip()) if r.stdout.strip().isdigit() else 0
        if r.returncode == 0 and code in (200, 206):
            return True, time.monotonic() - t0, name, url
    except Exception:
        pass
    return False, 999.0, name, url


def probe_mirrors(candidates, timeout=4.0, cancel_event=None):
    """并发测速候选下载源，返回按响应速度排序的 [(name, url), ...]。"""
    if len(candidates) <= 1:
        return list(candidates)
    if cancel_event is not None and cancel_event.is_set():
        return list(candidates)
    ok_list = []
    failed_list = []
    with ThreadPoolExecutor(max_workers=len(candidates)) as ex:
        futs = {ex.submit(_probe_single_mirror, name, url, timeout): (idx, name, url)
                for idx, (name, url) in enumerate(candidates)}
        for fut in as_completed(futs):
            idx, name, url = futs[fut]
            try:
                ok, dt, _n, _u = fut.result()
            except Exception:
                ok, dt = False, 999.0
            if ok:
                ok_list.append((dt, idx, name, url))
            else:
                failed_list.append((idx, name, url))
    ok_list.sort(key=lambda x: (x[0], x[1]))
    failed_list.sort(key=lambda x: x[0])
    return [(n, u) for _dt, _i, n, u in ok_list] + [(n, u) for _i, n, u in failed_list]


def _emit_status(progress_cb, status_cb, got, total, speed=0.0, eta=-1.0, mirror="", stage="downloading"):
    if progress_cb:
        try:
            progress_cb(got, total)
        except Exception:
            pass
    if status_cb:
        pct = min(100, int(got * 100 / total)) if total > 0 else 0
        try:
            status_cb({
                "pct": pct,
                "got": got,
                "total": total,
                "speed": float(speed),
                "eta": float(eta),
                "mirror": mirror,
                "stage": stage,
            })
        except Exception:
            pass


def _download_via_curl(url, tmp_path, mirror_name, expected_size=0,
                       progress_cb=None, status_cb=None, cancel_event=None):
    """使用 Popen(curl.exe -C -) 断点续传下载，每 0.15s 轮询文件大小计算实时速度与 ETA。"""
    cmd = [
        "curl.exe", "-sS", "-L", "--fail",
        "-C", "-",
        "--connect-timeout", "10",
        "--speed-limit", "10240",
        "--speed-time", "20",
        "-A", UA,
        "-o", str(tmp_path),
        url,
    ]
    start_sz = tmp_path.stat().st_size if tmp_path.is_file() else 0
    _emit_status(progress_cb, status_cb, start_sz, expected_size, 0.0, -1.0, mirror_name, "downloading")

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=CREATE_NO_WINDOW,
    )
    samples = [(time.monotonic(), start_sz)]
    try:
        while proc.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                try:
                    proc.terminate()
                    proc.wait(timeout=2)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                raise _DownloadCancelled("用户取消下载")
            time.sleep(0.15)
            now = time.monotonic()
            got = tmp_path.stat().st_size if tmp_path.is_file() else 0
            samples.append((now, got))
            # 保留近 1.5s 采样窗口计算平滑速度
            while len(samples) > 2 and (now - samples[0][0]) > 1.5:
                samples.pop(0)
            dt = now - samples[0][0]
            dg = max(0, got - samples[0][1])
            speed = (dg / dt) if dt > 0.05 else 0.0
            eta = ((expected_size - got) / speed) if (expected_size > got and speed > 1.0) else -1.0
            _emit_status(progress_cb, status_cb, got, expected_size, speed, eta, mirror_name, "downloading")
    except _DownloadCancelled:
        raise
    finally:
        stderr_bytes = b""
        if proc.stderr is not None:
            try:
                stderr_bytes = proc.stderr.read() or b""
            except Exception:
                pass
            try:
                proc.stderr.close()
            except Exception:
                pass

    rc = proc.returncode
    got = tmp_path.stat().st_size if tmp_path.is_file() else 0
    if rc != 0:
        err_text = stderr_bytes.decode("utf-8", "replace").strip()
        raise RuntimeError("%s 下载中断：%s" % (mirror_name, err_text or ("curl 退出码 %d" % rc)))
    _emit_status(progress_cb, status_cb, got, expected_size or got, 0.0, 0.0, mirror_name, "verifying")


def _file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def _verify_downloaded_exe(tmp_path, expected_size=0, expected_sha256=""):
    """严格校验下载产物：精确大小 + >=1MB + MZ 头 + 可选 SHA256。"""
    sz = tmp_path.stat().st_size if tmp_path.is_file() else 0
    if expected_size > 0 and sz != expected_size:
        raise _IntegrityError("文件大小不完整（实得 %d / 期望 %d 字节）" % (sz, expected_size))
    if sz < 1024 * 1024:
        raise _IntegrityError("下载的文件过小（%d 字节），可能不完整" % sz)
    with open(tmp_path, "rb") as f:
        magic = f.read(2)
    if magic != b"MZ":
        raise _IntegrityError("下载的文件不是有效的 Windows 可执行程序")
    want_sha = str(expected_sha256 or "").strip().upper()
    if want_sha and len(want_sha) == 64:
        actual_sha = _file_sha256(tmp_path)
        if actual_sha != want_sha:
            raise _IntegrityError("SHA256 校验不匹配（实得 %s... / 期望 %s...）" % (actual_sha[:12], want_sha[:12]))


def _write_replace_script(staged_path, final_path, same_as_running):
    """生成 UTF-8 BOM PowerShell 替换启动脚本：循环等待旧 PID 退出后再原子替换并启动新版。"""
    fd, ps1_path = tempfile.mkstemp(prefix="ssjj_update_", suffix=".ps1")
    os.close(fd)
    old_pid = os.getpid()

    def _ps_quote(s):
        return "'" + str(s).replace("'", "''") + "'"

    staged_q = _ps_quote(staged_path)
    final_q = _ps_quote(final_path)
    lines = [
        "$ErrorActionPreference = 'SilentlyContinue'",
        "$oldPid = %d" % old_pid,
        "$staged = %s" % staged_q,
        "$target = %s" % final_q,
        "for ($i = 0; $i -lt 30; $i++) {",
        "    if (-not (Get-Process -Id $oldPid -ErrorAction SilentlyContinue)) { break }",
        "    Start-Sleep -Milliseconds 500",
        "}",
    ]
    if same_as_running:
        lines += [
            "for ($j = 0; $j -lt 10; $j++) {",
            "    try {",
            "        Move-Item -LiteralPath $staged -Destination $target -Force -ErrorAction Stop",
            "        break",
            "    } catch {",
            "        Start-Sleep -Milliseconds 300",
            "    }",
            "}",
            "if (Test-Path -LiteralPath $target) { Start-Process -FilePath $target }",
        ]
    else:
        lines += [
            "if (Test-Path -LiteralPath $staged) { Start-Process -FilePath $staged }",
        ]
    lines.append("Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue")
    Path(ps1_path).write_text("\r\n".join(lines) + "\r\n", encoding="utf-8-sig")
    return ps1_path


def download_update(download_url, asset_name, expected_size=0, progress_cb=None,
                    cancel_event=None, status_cb=None, expected_sha256=""):
    """多源测速 + 断点续传下载新版 .exe 并严格校验，返回 (ok, msg, saved_exe_path, launch_script_path)。"""
    if not download_url:
        return False, "该 Release 未附带 .exe 安装包", "", ""
    safe_name = os.path.basename(asset_name or "生死狙击工具箱-new.exe")
    if not safe_name.lower().endswith(".exe"):
        safe_name += ".exe"

    target_dir = _writable_target_dir()
    tmp_path = target_dir / (safe_name + ".downloading")
    final_path = target_dir / safe_name

    same_as_running = (
        getattr(sys, "frozen", False)
        and os.path.normcase(str(final_path)) == os.path.normcase(os.path.abspath(sys.executable))
    )
    staged_path = (target_dir / (safe_name + ".new")) if same_as_running else final_path

    # 1. 构造候选镜像并并发测速排序
    candidates = build_mirror_urls(download_url)
    _emit_status(progress_cb, status_cb, 0, expected_size, 0.0, -1.0, "测速中", "probing")
    ordered = probe_mirrors(candidates, timeout=4.0, cancel_event=cancel_event)

    if cancel_event is not None and cancel_event.is_set():
        return False, "已取消下载", "", ""

    # 若已有残留 .downloading 且超过期望大小（明显错乱），先清理
    if tmp_path.is_file() and expected_size > 0:
        try:
            if tmp_path.stat().st_size > expected_size:
                tmp_path.unlink()
        except OSError:
            pass

    # 2. 按测速顺序依次尝试下载，网络中断保留 .downloading 走 -C - 断点续传；仅校验损坏时清理重下
    errors = []
    used_mirror = ""
    success = False
    for mirror_name, cand_url in ordered:
        if cancel_event is not None and cancel_event.is_set():
            return False, "已取消下载", "", ""
        try:
            _download_via_curl(
                cand_url, tmp_path, mirror_name,
                expected_size=expected_size,
                progress_cb=progress_cb,
                status_cb=status_cb,
                cancel_event=cancel_event,
            )
            _verify_downloaded_exe(tmp_path, expected_size=expected_size, expected_sha256=expected_sha256)
            os.replace(str(tmp_path), str(staged_path))
            used_mirror = mirror_name
            success = True
            break
        except _DownloadCancelled:
            return False, "已取消下载", "", ""
        except _IntegrityError as e:
            errors.append("%s: %s" % (mirror_name, e))
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
        except Exception as e:
            errors.append("%s: %s" % (mirror_name, e))
            # 保留 .downloading 供下一条线路 -C - 断点续传；但若文件小于 1KB 且非 MZ（可能是错误 HTML），清理
            try:
                if tmp_path.is_file():
                    sz_now = tmp_path.stat().st_size
                    if 0 < sz_now < 4096:
                        with open(tmp_path, "rb") as f:
                            if f.read(2) != b"MZ":
                                tmp_path.unlink()
            except OSError:
                pass

    if not success:
        detail = "；".join(errors[-2:]) if errors else "所有下载线路均失败"
        return False, "下载失败（已尝试 %d 条线路）：%s" % (len(ordered), detail), "", ""

    final_sz = staged_path.stat().st_size if staged_path.is_file() else expected_size
    _emit_status(progress_cb, status_cb, final_sz, expected_size or final_sz, 0.0, 0.0, used_mirror, "done")

    script_path = ""
    if getattr(sys, "frozen", False):
        cur_exe = os.path.abspath(sys.executable)
        script_path = _write_replace_script(staged_path, cur_exe if same_as_running else final_path, same_as_running)

    return True, "新版已通过 %s 下载完成：%s" % (used_mirror, final_path), str(final_path), script_path


def launch_update_script(script_path):
    if not script_path or not os.path.isfile(script_path):
        return False
    if str(script_path).lower().endswith(".ps1"):
        subprocess.Popen(
            [
                "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-WindowStyle", "Hidden", "-File", script_path,
            ],
            creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS,
            close_fds=True,
        )
        return True
    subprocess.Popen(
        ["cmd.exe", "/c", script_path],
        creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS,
        close_fds=True,
    )
    return True
