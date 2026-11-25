# -*- coding: utf-8 -*-
"""本次工具箱运行的诊断记录与单文件导出。只读取游戏文件。"""
import datetime as dt
import glob
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_ROOT = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "ssjj-toolbox"
_SESSION_DIR = _ROOT / "sessions"
_LOCK = threading.RLock()
_START = None
_LOG = None
_BASELINE = {}
_STOP = threading.Event()
_PROCESSES = ("MicroClient.exe", "WDlauncher.exe", "AirLobbyPreloader.exe",
              "AirAloneChat.exe",
              "SSJJ_BattleClient_Unity.exe", "UnityCrashHandler64.exe")


def _iso(ts=None):
    return dt.datetime.fromtimestamp(ts or time.time()).astimezone().isoformat(timespec="milliseconds")


def _desktop_dir():
    if os.name == "nt":
        try:
            import ctypes
            buffer = ctypes.create_unicode_buffer(32768)
            # CSIDL_DESKTOPDIRECTORY follows Windows' redirected desktop location.
            if ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buffer) == 0:
                folder = Path(buffer.value)
                if folder.is_dir():
                    return folder
        except (OSError, AttributeError):
            pass
    folder = Path.home() / "Desktop"
    return folder if folder.is_dir() else _ROOT


def _game_dir():
    try:
        import ssjj_paths
        return Path(ssjj_paths.refresh_game_dir())
    except Exception:
        return None


def _sources():
    out = {}
    roots = [_ROOT]
    try:
        import boot
        roots.append(Path(boot.data_dir()))
    except Exception:
        pass
    for root in roots:
        if root.is_dir():
            for p in root.iterdir():
                if p.is_file() and (p.suffix.lower() in (".log", ".jsonl")
                                    or p.name.lower() == "risk_inspect_probe.txt"):
                    out[str(p.resolve())] = p
    wooduan_logs = _ROOT.parent / "Wooduan" / "log"
    if wooduan_logs.is_dir():
        for p in wooduan_logs.glob("*.log"):
            if p.is_file():
                out[str(p.resolve())] = p
    game = _game_dir()
    if game and game.is_dir():
        patterns = ("*.log", "MicroClient64/*.log", "MicroClient64/*/*.log",
                    "MicroClient/*.log",
                    "Lobby/*.log", "battle/*/log/*.log",
                    "battle/*/ssjj_risk.log", "battle/*/*/ssjj_risk.log")
        for pattern in patterns:
            for name in glob.glob(str(game / pattern)):
                p = Path(name)
                if p.is_file():
                    out[str(p.resolve())] = p
    fallback = Path(os.environ.get("TEMP") or os.environ.get("TMP") or "") / "ssjj_risk.log"
    if fallback.is_file():
        out[str(fallback.resolve())] = fallback
    return out


def _stat(p):
    try:
        s = p.stat()
        with p.open("rb") as f:
            head = hashlib.sha256(f.read(256)).hexdigest()
        return {"size": s.st_size, "inode": s.st_ino, "ctime": s.st_ctime,
                "head": head}
    except OSError:
        return None


def record(message):
    if _LOG is None:
        return
    try:
        with _LOCK:
            with _LOG.open("a", encoding="utf-8") as f:
                f.write("%s pid=%d %s\n" % (_iso(), os.getpid(), message.replace("\n", "\\n")))
    except OSError:
        pass


def start():
    global _START, _LOG, _BASELINE
    if _LOG is not None:
        return
    _SESSION_DIR.mkdir(parents=True, exist_ok=True)
    _START = time.time()
    _BASELINE = {name: _stat(path) for name, path in _sources().items()}
    _LOG = _SESSION_DIR / ("session-%s-%d.log" %
                           (dt.datetime.now().strftime("%Y%m%d-%H%M%S"), os.getpid()))
    record("启动 exe=%s frozen=%s" % (sys.executable, bool(getattr(sys, "frozen", False))))
    threading.Thread(target=_watch_processes, name="ssjj-diagnostic-watch", daemon=True).start()


def finish():
    record("工具箱退出")
    _STOP.set()


def _watch_processes():
    try:
        import psutil
    except ImportError:
        record("进程监测不可用：缺少 psutil")
        return
    previous = {}
    first = True
    while not _STOP.is_set():
        current = {}
        try:
            for proc in psutil.process_iter(["pid", "name", "create_time", "exe"]):
                info = proc.info
                if info.get("name") in _PROCESSES:
                    key = (info["pid"], info.get("create_time"))
                    current[key] = (info.get("name"), info.get("exe"))
            for key, (name, exe) in current.items():
                if key not in previous:
                    record("%s %s pid=%s 创建=%s 路径=%s" %
                           ("进程已存在" if first else "进程启动",
                            name, key[0], _iso(key[1]) if key[1] else "?", exe or "?"))
            for key, (name, _exe) in previous.items():
                if key not in current:
                    record("进程退出 %s pid=%s 创建=%s" %
                           (name, key[0], _iso(key[1]) if key[1] else "?"))
            previous = current
            first = False
        except Exception as exc:
            record("进程监测异常 %r" % exc)
        _STOP.wait(2)


_SENSITIVE_KEYS = (
    r'password|passwd|pwd|token|access_token|refresh_token|'
    r'authorization|cookie|secret|sessionid|session_key|'
    r'username|user_name|uname|account|loginname|'
    r'sign|fcmsign|fcm_sign|fcmclientip|fcm_client_ip|clientip|client_ip|deviceinfo|device_info'
)
_KEY = re.compile(
    r'(?i)(?<![A-Za-z0-9_])'
    r'((?:\\?["\'])?(?:' + _SENSITIVE_KEYS + r')(?:\\?["\'])?\s*[:=]\s*(?:\\?["\'])?)'
    r'((?:[^"\'\s,;&}\\]|\\(?!["\']))+)'
)
_URLENC_KEY = re.compile(
    r'(?i)((?:%5C)?(?:%22|%27)(?:' + _SENSITIVE_KEYS +
    r')(?:%5C)?(?:%22|%27)\s*(?:%3A|:|%3D|=)\s*(?:%5C)?(?:%22|%27))'
    r'(?:(?!%5C%22|%5C%27|%22|%27|%7D|%2C|["\'\s,&}]).)+'
)
_URLENC_PARAM = re.compile(
    r'(?i)(?:(?<=^)|(?<=%26)|(?<=%3F)|(?<=[&?;\s,{=:]))'
    r'((?:' + _SENSITIVE_KEYS + r')(?:%3D|%3A))'
    r'(?:(?!%26|%2C|%7D|%5C%22|%22|%27|["\'\s,&}]).)+'
)
_SPACE_SIGN = re.compile(r'(?i)(?<![A-Za-z0-9_])(Sign\s+)[0-9a-f]{16,}\b')
_CLI_LA = re.compile(r'(?i)(?<![A-Za-z0-9_-])(--la(?:\s+|=)"?)[A-Za-z0-9+/=_-]{24,}')
_BOOT_CFG_LA = re.compile(
    r'(?i)(GameBootConfig:\s*--la[ \t]*\r?\n[^\n]*?GameBootConfig:\s*)[A-Za-z0-9+/=_-]{24,}'
)
_JSON_ORG = re.compile(r'(?i)(\bjson\s+org\s*:\s*)[A-Za-z0-9+/=_-]{24,}')
_BEARER = re.compile(r'(?i)\b(Bearer|Basic)\s+[A-Za-z0-9_+./=-]+')
_COOKIE = re.compile(r'(?im)^(\s*(?:set-cookie|cookie)\s*:\s*)[^\r\n]+')
_URL = re.compile(r'(?:https?|wd\.ssjj\.[A-Za-z0-9_.-]+|wdapp)://[^\s"\'<>]+', re.I)
_EMAIL = re.compile(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}')
_VER_DAT_RE = re.compile(r'^\.ver[0-9]*\.dat$', re.I)


def redact(text):
    def url(m):
        raw = m.group(0)
        try:
            parsed = urlsplit(raw)
            netloc = parsed.hostname or ""
            if parsed.port:
                netloc += ":%d" % parsed.port
            query = parsed.query
            if query:
                stripped = query.lstrip()
                if stripped.startswith(("{", '{\\"', "%7B", "%7b")):
                    query = _URLENC_KEY.sub(lambda mm: mm.group(1) + "[REDACTED]", query)
                    query = _URLENC_PARAM.sub(lambda mm: mm.group(1) + "[REDACTED]", query)
                    query = _KEY.sub(lambda mm: mm.group(1) + "[REDACTED]", query)
                else:
                    query = urlencode([(k, "[REDACTED]") for k, _ in parse_qsl(
                        query, keep_blank_values=True)])
            return urlunsplit((parsed.scheme, netloc, parsed.path, query,
                               "[REDACTED]" if parsed.fragment else ""))
        except ValueError:
            return "[URL_REDACTED]"
    text = _URL.sub(url, text)
    text = _COOKIE.sub(lambda m: m.group(1) + "[REDACTED]", text)
    text = _URLENC_KEY.sub(lambda m: m.group(1) + "[REDACTED]", text)
    text = _URLENC_PARAM.sub(lambda m: m.group(1) + "[REDACTED]", text)
    text = _KEY.sub(lambda m: m.group(1) + "[REDACTED]", text)
    text = _SPACE_SIGN.sub(lambda m: m.group(1) + "[REDACTED]", text)
    text = _CLI_LA.sub(lambda m: m.group(1) + "[REDACTED]", text)
    text = _BOOT_CFG_LA.sub(lambda m: m.group(1) + "[REDACTED]", text)
    text = _JSON_ORG.sub(lambda m: m.group(1) + "[REDACTED]", text)
    text = _BEARER.sub(lambda m: m.group(1) + " [REDACTED]", text)
    text = _EMAIL.sub("[EMAIL_REDACTED]", text)
    return text


def _digests(p):
    """单遍读取计算文件的 SHA-256 与 MD5 摘要。"""
    sha256 = hashlib.sha256()
    md5 = hashlib.md5()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            sha256.update(chunk)
            md5.update(chunk)
    return sha256.hexdigest(), md5.hexdigest()


def _hash(p):
    return _digests(p)[0]


def _file_attrs(stat_res):
    attrs = getattr(stat_res, "st_file_attributes", 0)
    if not attrs:
        return ""
    flags = []
    if attrs & 0x1:
        flags.append("R")
    if attrs & 0x2:
        flags.append("H")
    if attrs & 0x4:
        flags.append("S")
    return (" attrs=" + "".join(flags)) if flags else ""


def _proxy_source_info():
    """定位内置代理 DLL 源文件并计算摘要，供目录残留比对。"""
    src = None
    try:
        from risk import proxy as _proxy
        src = _proxy.dll_source()
    except Exception:
        src = None
    if not src:
        cands = []
        base = getattr(sys, "_MEIPASS", None)
        if base:
            cands.append(Path(base) / "risk" / "proxy" / "version.dll")
        cands.append(Path(__file__).resolve().parents[1] / "risk" / "proxy" / "version.dll")
        for cand in cands:
            if cand.is_file():
                src = str(cand)
                break
    if not src:
        return None, 0, "", ""
    p = Path(src)
    try:
        s = p.stat()
        sha256, md5 = _digests(p)
        return p, s.st_size, md5, sha256
    except OSError:
        return p, 0, "", ""


def _format_battle_file(game, p, src_sha256="", check_match=False):
    rel = p.relative_to(game)
    try:
        s = p.stat()
        sha256, md5 = _digests(p)
        suffix = _file_attrs(s)
        if check_match and src_sha256:
            suffix += " match_source=%s" % (sha256 == src_sha256)
        return "  %s size=%d modified=%s sha256=%s md5=%s%s" % (
            rel, s.st_size, _iso(s.st_mtime), sha256, md5, suffix)
    except OSError as exc:
        return "  %s: %r" % (rel, exc)


def _battle_dir_lines(game, src_sha256):
    lines = []
    battle_root = game / "battle"
    if not battle_root.is_dir():
        lines.append("battle/*: 不存在")
        return lines
    try:
        subdirs = [d for d in sorted(battle_root.iterdir(), key=lambda p: p.name.lower()) if d.is_dir()]
    except OSError as exc:
        lines.append("battle/*: %r" % exc)
        return lines
    if not subdirs:
        lines.append("battle/*: 不存在")
        return lines

    any_ver_dat = False
    any_bak = False
    for bdir in subdirs:
        rel_dir = bdir.relative_to(game)
        try:
            entries = sorted(bdir.iterdir(), key=lambda p: p.name.lower())
        except OSError as exc:
            lines.append("%s: %r" % (rel_dir, exc))
            continue
        exe_file = next((p for p in entries if p.is_file() and p.name.lower() == "ssjj_battleclient_unity.exe"), None)
        ver_dll = next((p for p in entries if p.is_file() and p.name.lower() == "version.dll"), None)
        ver_dats = [p for p in entries if p.is_file() and _VER_DAT_RE.match(p.name)]
        baks = [p for p in entries if p.is_file() and p.name.lower().startswith("version.dll.bak-")]
        if ver_dats:
            any_ver_dat = True
        if baks:
            any_bak = True

        flags = []
        if ver_dll and ver_dats:
            flags.append("DUAL_CONFLICT")
        elif ver_dll:
            flags.append("ARMED")
        elif ver_dats:
            flags.append("HIDDEN")
        else:
            flags.append("CLEAN")
        if baks:
            flags.append("BAK_RESIDUAL")
        if len(ver_dats) > 1 or any(p.name.lower() != ".ver.dat" for p in ver_dats):
            flags.append("MULTI_ALT")

        dll_desc = "missing"
        if ver_dll:
            try:
                match_txt = (str(_digests(ver_dll)[0] == src_sha256) if src_sha256 else "unknown")
                if src_sha256 and _digests(ver_dll)[0] != src_sha256:
                    flags.append("SOURCE_MISMATCH")
                dll_desc = "present(match=%s)" % match_txt
            except OSError as exc:
                dll_desc = "error(%r)" % exc
        if ver_dats and src_sha256:
            try:
                if any(_digests(p)[0] != src_sha256 for p in ver_dats):
                    flags.append("ALT_SOURCE_MISMATCH")
            except OSError:
                pass

        dedup_flags = list(dict.fromkeys(flags))
        lines.append("%s status=%s exe=%s version.dll=%s ver_dat=%s bak=%s" % (
            rel_dir, "|".join(dedup_flags),
            "present" if exe_file else "missing",
            dll_desc,
            [p.name for p in ver_dats] or "none",
            [p.name for p in baks] or "none",
        ))
        if exe_file:
            lines.append(_format_battle_file(game, exe_file))
        if ver_dll:
            lines.append(_format_battle_file(game, ver_dll, src_sha256, check_match=True))
        for p in ver_dats:
            lines.append(_format_battle_file(game, p, src_sha256, check_match=True))
        for p in baks:
            lines.append(_format_battle_file(game, p))
    if not any_ver_dat:
        lines.append("battle/*/.ver*.dat: 不存在")
    if not any_bak:
        lines.append("battle/*/version.dll.bak-*: 不存在")
    return lines


def _proxy_state_lines():
    lines = []
    try:
        state = json.loads((_ROOT / "proxy_state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        lines.append("proxy_state 不可读: %r" % exc)
        return lines
    installed = state.get("installed") or {}
    ts_val = state.get("ts")
    ts_txt = _iso(ts_val) if isinstance(ts_val, (int, float)) else ts_val
    lines.append("proxy_state updated=%s md5=%s count=%d" % (
        ts_txt, state.get("md5") or "", len(installed)))
    if isinstance(installed, dict):
        for d, info in sorted(installed.items()):
            entry_md5 = info.get("md5", "") if isinstance(info, dict) else ""
            entry_ts = info.get("ts") if isinstance(info, dict) else None
            entry_ts_txt = _iso(entry_ts) if isinstance(entry_ts, (int, float)) else entry_ts
            dp = Path(d)
            has_dll = (dp / "version.dll").is_file() if dp.is_dir() else False
            has_alt = any(_VER_DAT_RE.match(p.name) for p in dp.iterdir()) if dp.is_dir() else False
            lines.append("  proxy_state.entry dir=%s md5=%s ts=%s dir_exists=%s has_dll=%s has_alt=%s" % (
                d, entry_md5, entry_ts_txt, dp.is_dir(), has_dll, has_alt))
    return lines


def _file_state():
    src_path, src_size, src_md5, src_sha256 = _proxy_source_info()
    game = _game_dir()
    lines = ["game_dir=%s" % (game or "未找到")]
    if src_path is not None and src_sha256:
        lines.append("proxy_source=%s size=%d md5=%s sha256=%s" % (
            src_path, src_size, src_md5, src_sha256))
    else:
        lines.append("proxy_source=未找到")
    if not game or not game.is_dir():
        lines.extend(_proxy_state_lines())
        return "\n".join(lines)
    for pattern in ("WDlauncher.exe", "MicroClient64/MicroClient.exe",
                    "MicroClient64/GameGuard/npggNT64.des"):
        matches = glob.glob(str(game / pattern))
        if not matches:
            lines.append("%s: 不存在" % pattern)
        for name in sorted(matches):
            p = Path(name)
            try:
                s = p.stat()
                lines.append("%s size=%d modified=%s sha256=%s" %
                             (p.relative_to(game), s.st_size, _iso(s.st_mtime), _hash(p)))
            except OSError as exc:
                lines.append("%s: %r" % (name, exc))
    lines.extend(_battle_dir_lines(game, src_sha256))
    lines.extend(_proxy_state_lines())
    dumps = list((_ROOT.parent / "CrashDumps").glob("*.dmp"))
    for pattern in ("MicroClient64/*.dmp", "battle/*/*.dmp"):
        dumps.extend(Path(p) for p in glob.glob(str(game / pattern)))
    for p in sorted(dumps):
        if not any(tag in p.name.lower() for tag in
                   ("ssjj", "microclient", "airlobby", "airalone", "unity")):
            continue
        try:
            s = p.stat()
            if _START is None or s.st_mtime >= _START - 60:
                lines.append("crash_dump=%s size=%d modified=%s（仅元数据）" %
                             (p, s.st_size, _iso(s.st_mtime)))
        except OSError:
            pass
    return "\n".join(lines)


def _windows_events(start_time):
    if os.name != "nt":
        return "仅 Windows 可读取事件日志"
    # 用本地事件记录确认异常模块、异常码与 WER 报告，不启用转储。
    script = r"""
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
$from = [DateTimeOffset]::FromUnixTimeSeconds([long]$env:SSJJ_DIAG_FROM).LocalDateTime
try {
  Get-WinEvent -FilterHashtable @{LogName='Application';StartTime=$from;Id=1000,1001,1002} -ErrorAction Stop |
    Where-Object { $_.Message -match 'SSJJ|MicroClient|AirLobby|AirAloneChat|Unity|GameGuard|npgg|生死狙击' } |
    Select-Object -First 100 | ForEach-Object {
      '[{0:yyyy-MM-ddTHH:mm:sszzz}] id={1} provider={2} {3}' -f $_.TimeCreated,$_.Id,$_.ProviderName,$_.Message
    }
} catch {
  if ($_.FullyQualifiedErrorId -like '*NoMatchingEventsFound*') {
    '未找到本次会话的相关应用程序事件'
  } else {
    '事件日志读取失败: ' + $_.Exception.Message
  }
}
"""
    env = os.environ.copy()
    env["SSJJ_DIAG_FROM"] = str(int(start_time) - 60)
    try:
        r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=18, env=env,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.stdout.strip() or r.stderr.strip() or "未找到本次会话的相关应用程序事件"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "事件日志读取失败: %r" % exc


def _windows_integrity(start_time):
    if os.name != "nt":
        return "仅 Windows 可读取系统状态"
    script = r"""
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
try {
  $v = Get-CimInstance -Namespace root\Microsoft\Windows\DeviceGuard -ClassName Win32_DeviceGuard -ErrorAction Stop
  'VBS status={0}; security_services_running={1}; available={2}' -f $v.VirtualizationBasedSecurityStatus,($v.SecurityServicesRunning -join ','),($v.AvailableSecurityProperties -join ',')
} catch { 'VBS 状态不可读: ' + $_.Exception.Message }
$from = [DateTimeOffset]::FromUnixTimeSeconds([long]$env:SSJJ_DIAG_FROM).LocalDateTime
try {
  Get-WinEvent -FilterHashtable @{LogName='Microsoft-Windows-CodeIntegrity/Operational';StartTime=$from} -ErrorAction Stop |
    Where-Object { $_.Message -match 'SSJJ|Wooduan|MicroClient|GameGuard|npgg|version\.dll|\.ver\.dat' } |
    Select-Object -First 100 | ForEach-Object {
      '[{0:yyyy-MM-ddTHH:mm:sszzz}] id={1} {2}' -f $_.TimeCreated,$_.Id,$_.Message
    }
} catch {
  if ($_.FullyQualifiedErrorId -like '*NoMatchingEventsFound*') {
    '本次会话无代码完整性事件'
  } else {
    '代码完整性事件不可读: ' + $_.Exception.Message
  }
}
"""
    env = os.environ.copy()
    env["SSJJ_DIAG_FROM"] = str(int(start_time) - 60)
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=18, env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return result.stdout.strip() or result.stderr.strip() or "无匹配系统事件"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "系统状态读取失败: %r" % exc


def _read_session_part(path, baseline):
    now = _stat(path)
    if now is None:
        return "文件不存在或不可读"
    offset = 0
    same_head = False
    if baseline is not None:
        try:
            with path.open("rb") as f:
                same_head = hashlib.sha256(f.read(min(256, baseline["size"]))).hexdigest() == baseline["head"]
        except OSError:
            pass
    if baseline is not None and now["inode"] == baseline["inode"] and (
            same_head) and (
            now["ctime"] == baseline["ctime"] or os.name == "nt") and now["size"] >= baseline["size"]:
        offset = baseline["size"]
    elif baseline is None and path.stat().st_mtime < _START - 2:
        return "本次会话前存在的文件；没有新增内容"
    try:
        with path.open("rb") as f:
            f.seek(offset)
            data = f.read()
        return ("bytes=%d..%d\n" % (offset, offset + len(data))) + (
            redact(data.decode("utf-8-sig", errors="replace").replace("\r\n", "\n")) if data
            else "本次会话无新增内容")
    except OSError as exc:
        return "读取失败: %r" % exc


def _read_zone_identifier(p):
    """检查文件是否存在 NTFS Zone.Identifier 备用数据流（Mark of the Web）。"""
    if os.name != "nt":
        return "n/a"
    ads_path = str(p) + ":Zone.Identifier"
    try:
        with open(ads_path, "rb") as f:
            raw = f.read(256)
        text = raw.decode("utf-8", errors="replace").strip()
        summary = "; ".join(line.strip() for line in text.splitlines() if line.strip())
        return "present(%s)" % (summary[:160] if summary else "empty")
    except FileNotFoundError:
        return "none"
    except OSError as exc:
        return "error(%s)" % getattr(exc, "winerror", exc.__class__.__name__)


def _probe_registry_dll_policy():
    """只读采集影响 DLL 加载顺序与安全缓解的注册表键值（毫秒级）。"""
    if os.name != "nt":
        return ["[注册表] 仅 Windows 支持"]
    try:
        import winreg
    except ImportError:
        return ["[注册表] winreg 不可用"]

    lines = ["[注册表 DLL 加载策略]"]
    access = winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)

    # 1. KnownDLLs
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\KnownDLLs",
            0, access,
        ) as k:
            idx = 0
            total = 0
            hits = []
            while True:
                try:
                    name, val, _typ = winreg.EnumValue(k, idx)
                    idx += 1
                    total += 1
                    n_low = (name or "").lower()
                    v_low = str(val or "").lower()
                    if n_low in ("version", "version.dll") or v_low in ("version", "version.dll"):
                        hits.append("%s=%s" % (name, val))
                except OSError:
                    break
            lines.append("  KnownDLLs: count=%d has_version=%s%s" % (
                total, bool(hits), (" hits=%s" % hits) if hits else ""))
    except OSError as exc:
        lines.append("  KnownDLLs: 读取失败(%r)" % exc)

    # 2. Session Manager: SafeDllSearchMode / CWDIllegalInDllSearch
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager",
            0, access,
        ) as k:
            vals = []
            for key_name in ("SafeDllSearchMode", "CWDIllegalInDllSearch"):
                try:
                    v, _t = winreg.QueryValueEx(k, key_name)
                    vals.append("%s=%r" % (key_name, v))
                except FileNotFoundError:
                    vals.append("%s=<unset>" % key_name)
                except OSError as exc:
                    vals.append("%s=err(%r)" % (key_name, exc))
            lines.append("  SessionManager: " + " ".join(vals))
    except OSError as exc:
        lines.append("  SessionManager: 读取失败(%r)" % exc)

    # 3. IFEO for SSJJ_BattleClient_Unity.exe
    ifeo_path = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\SSJJ_BattleClient_Unity.exe"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, ifeo_path, 0, access) as k:
            idx = 0
            items = []
            while True:
                try:
                    name, val, _typ = winreg.EnumValue(k, idx)
                    idx += 1
                    if isinstance(val, bytes):
                        val_str = "0x" + val.hex()
                    else:
                        val_str = repr(val)
                    items.append("%s=%s" % (name or "(Default)", val_str))
                except OSError:
                    break
            sub_idx = 0
            subs = []
            while True:
                try:
                    subs.append(winreg.EnumKey(k, sub_idx))
                    sub_idx += 1
                except OSError:
                    break
            lines.append("  IFEO(SSJJ_BattleClient_Unity.exe): values=%s subkeys=%s" % (
                items or "none", subs or "none"))
    except FileNotFoundError:
        lines.append("  IFEO(SSJJ_BattleClient_Unity.exe): 未设置")
    except OSError as exc:
        lines.append("  IFEO(SSJJ_BattleClient_Unity.exe): 读取失败(%r)" % exc)

    # 4. AppCompatFlags\Layers (HKLM & HKCU)
    layers_path = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers"
    for hive_name, hive in (("HKLM", winreg.HKEY_LOCAL_MACHINE), ("HKCU", winreg.HKEY_CURRENT_USER)):
        try:
            with winreg.OpenKey(hive, layers_path, 0, access) as k:
                idx = 0
                matched = []
                while True:
                    try:
                        name, val, _typ = winreg.EnumValue(k, idx)
                        idx += 1
                        blob = ("%s %s" % (name, val)).lower()
                        if "ssjj" in blob or "battleclient" in blob:
                            matched.append("%s => %s" % (name, val))
                    except OSError:
                        break
                lines.append("  AppCompatFlags\\Layers(%s): %s" % (
                    hive_name, ("; ".join(matched)) if matched else "无匹配项"))
        except FileNotFoundError:
            lines.append("  AppCompatFlags\\Layers(%s): 键不存在" % hive_name)
        except OSError as exc:
            lines.append("  AppCompatFlags\\Layers(%s): 读取失败(%r)" % (hive_name, exc))

    return lines


def _probe_av_and_mitigations():
    """单次轻量 PowerShell 查询本机杀软及安全缓解/Defender 最近 10 条事件（超时 <= 5s）。"""
    if os.name != "nt":
        return ["[安全环境] 仅 Windows 支持"]
    script = r"""
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
try {
  $avs = Get-CimInstance -Namespace root\SecurityCenter2 -ClassName AntiVirusProduct -ErrorAction Stop
  if ($avs) {
    foreach ($av in $avs) {
      'AV: displayName={0} productState=0x{1:X}' -f $av.displayName, ([int]$av.productState)
    }
  } else {
    'AV: none'
  }
} catch {
  'AV: 查询失败(' + $_.Exception.Message + ')'
}
$channels = @(
  @{ Name = 'Microsoft-Windows-Security-Mitigations/KernelMode'; Filter = @{ LogName = 'Microsoft-Windows-Security-Mitigations/KernelMode' } },
  @{ Name = 'Microsoft-Windows-Security-Mitigations/UserMode';   Filter = @{ LogName = 'Microsoft-Windows-Security-Mitigations/UserMode' } },
  @{ Name = 'Microsoft-Windows-Windows Defender/Operational';    Filter = @{ LogName = 'Microsoft-Windows-Windows Defender/Operational'; Level = 1,2,3 } }
)
foreach ($ch in $channels) {
  'EventLog[{0}]:' -f $ch.Name
  try {
    $evts = Get-WinEvent -FilterHashtable $ch.Filter -MaxEvents 10 -ErrorAction Stop
    foreach ($e in $evts) {
      $msg = ($e.Message -replace '\r?\n', ' ').Trim()
      if ($msg.Length -gt 220) { $msg = $msg.Substring(0, 220) + '...' }
      '  [{0:yyyy-MM-ddTHH:mm:sszzz}] id={1} level={2} {3}' -f $e.TimeCreated, $e.Id, $e.LevelDisplayName, $msg
    }
  } catch {
    if ($_.FullyQualifiedErrorId -like '*NoMatchingEventsFound*') {
      '  无匹配事件'
    } else {
      '  读取失败: ' + $_.Exception.Message
    }
  }
}
"""
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        out = (r.stdout or "").strip() or (r.stderr or "").strip()
        return ["[本机杀软与安全事件]"] + (["  " + ln for ln in out.splitlines()] if out else ["  无输出"])
    except subprocess.TimeoutExpired:
        return ["[本机杀软与安全事件]", "  查询超时(>5s)，已跳过"]
    except OSError as exc:
        return ["[本机杀软与安全事件]", "  查询失败: %r" % exc]


def _probe_battle_binary_attrs():
    """检查各 battle\\* 下 exe/UnityPlayer.dll 摘要及 version.dll Win32 属性与 MOTW。"""
    lines = ["[battle\\* 关键文件与 version.dll 属性探针]"]
    game = _game_dir()
    if not game or not game.is_dir():
        lines.append("  game_dir 未找到")
        return lines
    battle_root = game / "battle"
    if not battle_root.is_dir():
        lines.append("  battle 目录不存在")
        return lines
    try:
        subdirs = [d for d in sorted(battle_root.iterdir(), key=lambda p: p.name.lower()) if d.is_dir()]
    except OSError as exc:
        lines.append("  遍历失败: %r" % exc)
        return lines
    if not subdirs:
        lines.append("  无子目录")
        return lines

    for bdir in subdirs:
        rel_dir = bdir.relative_to(game)
        parts = []
        for fname in ("SSJJ_BattleClient_Unity.exe", "UnityPlayer.dll"):
            fp = bdir / fname
            if fp.is_file():
                try:
                    st = fp.stat()
                    sha_head = _digests(fp)[0][:16]
                    parts.append("%s(size=%d sha256=%s)" % (fname, st.st_size, sha_head))
                except OSError as exc:
                    parts.append("%s(err=%r)" % (fname, exc))
            else:
                parts.append("%s(missing)" % fname)
        ver_dll = bdir / "version.dll"
        if ver_dll.is_file():
            try:
                st = ver_dll.stat()
                sha_head = _digests(ver_dll)[0][:16]
                attrs = _file_attrs(st).strip() or "attrs=normal"
                motw = _read_zone_identifier(ver_dll)
                parts.append("version.dll(size=%d sha256=%s %s motw=%s)" % (
                    st.st_size, sha_head, attrs, motw))
            except OSError as exc:
                parts.append("version.dll(err=%r)" % exc)
        else:
            parts.append("version.dll(missing)")
        lines.append("  %s: %s" % (rel_dir, " | ".join(parts)))
    return lines


def _dll_load_policy_probe():
    """汇总 DLL 加载策略、杀软/安全缓解事件及 battle 二进制属性探针。"""
    sections = []
    for fn in (_probe_registry_dll_policy, _probe_av_and_mitigations, _probe_battle_binary_attrs):
        try:
            sections.extend(fn())
        except Exception as exc:
            sections.append("[%s 异常] %r" % (fn.__name__, exc))
    return "\n".join(sections)


def export(destination=None):
    """在事故后或正常运行中导出；返回生成文件的绝对路径。"""
    if _START is None:
        start()
    now = time.time()
    if destination is None:
        dest_dir = _desktop_dir()
        destination = dest_dir / ("SSJJ诊断-%s-%d.txt" %
                                  (dt.datetime.now().strftime("%Y%m%d-%H%M%S"), os.getpid()))
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    record("开始导出诊断日志")
    sources = _sources()
    if _LOG:
        sources[str(_LOG.resolve())] = _LOG
    os_version = platform.platform()
    if hasattr(sys, "getwindowsversion"):
        os_version += " build=%s" % sys.getwindowsversion().build
    with destination.open("w", encoding="utf-8") as out:
        out.write("SSJJ 诊断汇总\n会话开始=%s\n导出时间=%s\n工具箱PID=%d\n系统=%s\n"
                  "Python=%s\n工具箱=%s\n\n" %
                  (_iso(_START), _iso(now), os.getpid(), os_version,
                   platform.python_version(), sys.executable))
        out.write("===== 游戏文件与代理残留状态 =====\n%s\n\n" % redact(_file_state()))
        out.write("===== Windows 应用程序崩溃事件 =====\n%s\n\n" %
                  redact(_windows_events(_START)))
        out.write("===== Windows VBS 与代码完整性 =====\n%s\n\n" %
                  redact(_windows_integrity(_START)))
        out.write("=== DLL 加载策略与安全环境探针 ===\n%s\n\n" %
                  redact(_dll_load_policy_probe()))
        out.write("===== 日志来源（各来源保留原始顺序；只截取本次会话新增字节） =====\n")
        for name, path in sorted(sources.items()):
            out.write("\n----- %s -----\n" % redact(name))
            out.write(_read_session_part(path, _BASELINE.get(name)))
            out.write("\n")
        for name in sorted(set(_BASELINE) - set(sources)):
            out.write("\n----- %s -----\n文件已移走或删除\n" % redact(name))
    record("导出完成 %s" % destination)
    return str(destination.resolve())
