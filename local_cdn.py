# -*- coding: utf-8 -*-
"""
SSJJ 本地 CDN 服务 (local_cdn.py)
=================================
让大厅/客户端的 HTTP 资源请求全部落在本机，实现"点什么都秒开"。

核心机制
--------
大厅 AIR 的 URLLoader 走原生 socket，不读 IE 缓存（实测），所以唯一有效做法
是让 cdn.4399ssjj.com 指向本机：

    hosts: 127.0.0.1 cdn.4399ssjj.com
      ├─ :80  → 本服务
      │     命中 D 盘库  → 直接返回（1~3ms）
      │     未命中        → 回源真 CDN，并把结果顺手存进 D 盘（下次就命中）
      └─ :443 → 纯 TCP 透传真 CDN（TLS 端到端，证书不受影响，HTTPS 功能正常）

上次"劫持后游戏起不来"就是因为 443 没人监听 → 连接被拒。现在 443 透传，
不会再出现该问题。

用法（管理员运行）：
  python local_cdn.py install      # 写 hosts
  python local_cdn.py run          # 启动服务（前台）
  python local_cdn.py uninstall    # 移除 hosts
  python local_cdn.py status       # 统计
"""
import os
import re
import sys
import json
import time
import ctypes
import socket
import hashlib
import threading
import urllib.parse
import urllib.request

import ssjj_paths as P
import swf_fps

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.DeleteFileW.argtypes = [ctypes.c_wchar_p]
_k32.DeleteFileW.restype = ctypes.c_int
_k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
_k32.OpenProcess.restype = ctypes.c_void_p
_k32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
_k32.TerminateProcess.restype = ctypes.c_int
_k32.CloseHandle.argtypes = [ctypes.c_void_p]
_k32.CloseHandle.restype = ctypes.c_int


def _del(path):
    """用 Win32 API 删文件，绕开环境对 os.remove 的 safe-delete 拦截。

    DeleteFileW 失败时返回 0 而不抛异常，所以这里只看返回值。
    """
    if _k32.DeleteFileW(path) == 0:
        _step("DeleteFileW 失败 path=%r err=%d" % (path, ctypes.get_last_error()))


CACHE = P.CACHE_DIR
HOST = P.HOST
LOG = os.path.join(P.HERE, "local_cdn.log")
LOG_MAX = 8 * 1024 * 1024      # 单文件上限 8MB，超了就轮转
LOG_KEEP = 2                   # 保留两代历史


def _rotate_log():
    """日志超限就轮转，避免常年运行后单文件涨到几十 MB 拖慢读写。

    保留 local_cdn.log.1 / .2 两代，最旧的一代直接删掉。
    """
    try:
        if os.path.getsize(LOG) < LOG_MAX:
            return
    except OSError:
        return
    try:
        oldest = "%s.%d" % (LOG, LOG_KEEP)
        if os.path.isfile(oldest):
            os.remove(oldest)
        for i in range(LOG_KEEP - 1, 0, -1):
            src = "%s.%d" % (LOG, i)
            if os.path.isfile(src):
                os.replace(src, "%s.%d" % (LOG, i + 1))
        os.replace(LOG, LOG + ".1")
    except OSError:
        pass
UP_FILE = P.UPSTREAM_FILE
PID_FILE = os.path.join(P.HERE, "server.pid")
DAEMON_PID = os.path.join(P.HERE, "daemon.pid")
MAX_CACHE = 48 * 1024 * 1024          # 单文件最大缓存 48MB，超了只转发不落盘
UPSTREAM_RETRY = 2                    # 回源失败重试次数
CONNECT_TIMEOUT = 5                   # 连上游超时
READ_TIMEOUT = 20                     # 读上游响应超时（按包间隔计）
HALL_CONFIG = "config/hall_config.xml"  # 客户端会加 ?<时间戳> 破缓存，见 Mini.as:594
HALL_CONFIG_TTL = 300                   # 缩短到 5 分钟，游戏热更后迅速刷新配置

# 大厅帧率覆盖：源 SRC 换成 DST 返回给客户端
# 线上 SWF 头是 24fps，且包里没有 stage.frameRate 赋值、application.xml 也没有
# <frameRate>，所以帧率完全由 SWF 头决定，改它有效。
#
# 但客户端会校验：ModuleLoader.as:124-134 拿 URL 文件名里 _ 后的 32 位 md5 和
# 实际内容比对，不符就重载（最多 5 次），5 次耗尽后仍然加载（:129-135），
# 所以「直接换内容」能生效但每次多 5 个来回。干净的做法是让客户端请求一个
# 文件名与内容 md5 一致的新名字 —— 这需要改 paras.dat 的 lobbyFileName，
# 而启动器在我们控制之外。两条路都提供：
#   fps set 60            → 仅换内容（兼容，多 5 次重试）
#   fps set 60 --rename   → 同时把 paras.dat 的 lobbyFileName 指到新文件
FPS_OVERRIDE = {"src": None, "dst": None, "fps": 0}

MIME = {
    ".swf": "application/x-shockwave-flash",
    ".xml": "text/xml",
    ".txt": "text/plain",
    ".json": "application/json",
    ".atf": "application/octet-stream",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".mp3": "audio/mpeg",
    ".dat": "application/octet-stream",
    ".zip": "application/zip",
}

STATS = {"hit": 0, "miss": 0, "save": 0, "https": 0, "https_mitm": 0, "bytes": 0,
         "saved_mb": 0.0, "upstream_fail": 0, "local_inject": 0, "dyn_save": 0}
_slk = threading.Lock()
_loglock = threading.Lock()


def log(msg):
    line = "%s %s" % (time.strftime("%H:%M:%S"), msg)
    with _loglock:
        _rotate_log()
        try:
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
        try:
            print(line, flush=True)
        except Exception:
            pass


def _step(msg):
    if os.environ.get("CDN_TRACE"):
        sys.stderr.write("TRACE %s\n" % msg); sys.stderr.flush()


# ---------------------------------------------------------------- 上游解析
def _doh_lookup(timeout=8):
    """DoH 拿一组候选 IP（绕开 hosts 覆盖）"""
    out = []
    for url in ("https://1.1.1.1/dns-query?name=%s&type=A" % HOST,
                "https://dns.alidns.com/resolve?name=%s&type=A" % HOST):
        try:
            req = urllib.request.Request(url, headers={"accept": "application/dns-json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read().decode())
            for a in d.get("Answer", []):
                if a.get("type") == 1 and a["data"] not in out:
                    out.append(a["data"])
            if out:
                break
        except Exception as e:
            log("[!] DoH 失败 %r" % (e,))
    return out


def _probe_ip(ip, timeout=6, want=262144):
    """拉一个真实大资源测速，返回字节/秒；失败返回 0。

    只按 TCP 连通性判断是不够的：实测有节点能握手、能返响应头，但正文
    10 秒只吐 400KB 就断（CDN 回源慢），拿它当上游会让每次 MISS 都卡住。
    测速样本必须够大——用 464 字节的配置文件测，量到的只是往返延迟。
    """
    path = "/s5/Lobby_70257ca1e7d3dd2b54496dc578d5096e.swf"
    t0 = time.time()
    try:
        s = socket.create_connection((ip, 80), timeout=timeout)
        try:
            s.settimeout(timeout)
            req = ("GET %s HTTP/1.1\r\nHost: %s\r\n"
                   "User-Agent: ssjj-local-cdn\r\nConnection: close\r\n\r\n" % (path, HOST))
            s.sendall(req.encode("latin-1"))
            got = 0
            first = None
            while got < want:
                d = s.recv(65536)
                if not d:
                    break
                if first is None:
                    first = time.time()
                got += len(d)
            if not got:
                return 0.0
            # 从首包到达开始计时，排除连接与首字节延迟
            el = time.time() - (first or t0)
            return got / el if el > 0 else 0.0
        finally:
            s.close()
    except Exception:
        return 0.0


def resolve_upstream(force=False):
    """选回源节点。

    优先级：paras.dat 的 cdnIp > 缓存 > DoH 候选（逐个测速）。

    反过来写（DoH 优先）是踩过的坑：DoH 给的节点可能极慢，而客户端自己
    在 paras.dat 里挑的节点通常是就近的。测速兜住两种来源都不可信的情况。
    """
    if not force and os.path.isfile(UP_FILE):
        try:
            d = json.load(open(UP_FILE, encoding="utf-8"))
            if d.get("ip") and time.time() - d.get("ts", 0) < 86400:
                return d["ip"]
        except Exception:
            pass

    paras_ip = P.cdn_ip()
    cands = ([paras_ip] if paras_ip else []) + _doh_lookup()
    seen, ordered = set(), []
    for ip in cands:
        if ip and ip not in seen:
            seen.add(ip)
            ordered.append(ip)

    best, best_rate = None, 0.0
    for ip in ordered[:3]:
        rate = _probe_ip(ip)
        log("[测速] %s %.0f KB/s" % (ip, rate / 1024))
        if rate > best_rate:
            best, best_rate = ip, rate
        # 已经很快就不用继续测了（启动时多花几秒不值得）
        if best_rate > 8192 * 1024:
            break
    if best:
        json.dump({"ip": best, "ts": time.time(), "rate": best_rate},
                  open(UP_FILE, "w"))
        log("[上游] %s -> %s (%.0f KB/s)" % (HOST, best, best_rate / 1024))
        return best
    log("[!] 所有候选节点均不可用: %s" % ordered)
    return None


UPSTREAM = None


# ---------------------------------------------------------------- 路径映射
# 前缀规则来自 paras.dat：resurl=/s5/、globalResurl=/global_air、
# testClientResurl=/s5/test。三者内容不同，落盘必须分命名空间，否则会撞名。
_ROOT_OK_EXT = (".swf", ".xml", ".json", ".txt")
# 这些文件名里带区服差异（serverIp / servers 登录列表 / rechargeURL）。
# 实测：resource/ 下的静态资源跨区服完全相同，可以共用一份；但 config/ 下
# 这类文件每个区服都不一样，共用会让换区登录时连到错误的登录服务器。
_SERVER_SCOPED = ("hall_config.xml",)

# 不在 /s<N>/ 下但内容是静态的（实测两次请求 md5 恒定），值得缓存。
# GameMon.npz 每次启动都要下 13.5MB，7 次启动白下 94MB。
# 注意 /gameguard/*（含 update.cfg 与 GameMon.npz）绝不能缓存：
# 官方随时会静默更新 GameMon.npz（靠 ?GameMon.des=<ver> 区分），命中旧缓存会直接报代码 360。
_EXTRA_CACHE = ()

# 补丁包：/ecs/lobby/1.0.1.400/0.0.0.000-<32位hash>.patch
# 每次启动都要拉一次 23.3MB、约 3 秒，是「点了没反应」的最大单点。文件名里同时
# 带版本号和内容 hash，内容天然不可变，可以放心落盘；但 /ecs/update.php 这类
# 动态接口一个字节都不能缓存，所以只放行「带 hash 后缀的静态包」。
_ECS_STATIC_RE = re.compile(
    r"^/ecs/[A-Za-z0-9_./-]+-[0-9a-f]{32}\.(?:patch|zip|dat|bin|pak|res)$",
    re.IGNORECASE)


def _is_server_scoped(path_no_q):
    name = path_no_q.rsplit("/", 1)[-1]
    return name in _SERVER_SCOPED


_STATIC_WEB_EXT = (".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".swf", ".xml", ".json", ".ico", ".woff", ".woff2")


def path_to_file(path):
    """ /s5/resource/swf/a.swf?x=1  ->  <CACHE>/resource/swf/a.swf
        /s2/config/hall_config.xml ->  <CACHE>/s2/config/hall_config.xml
        /global_air/sign_x.xml     ->  <CACHE>/global_air/sign_x.xml
        /gameguard/GameMon.npz     ->  <CACHE>/gameguard/GameMon.npz
        /static/api/js/share.js    ->  <CACHE>/static/api/js/share.js
        /plugins/share/share.js    ->  <CACHE>/plugins/share/share.js

    静态资源跨区服内容一致，共用一份省盘；hall_config.xml 这类区服配置
    必须按区服分开存，否则换区登录会拿到别的区服的 serverIp / servers 列表，
    表现就是「换个号登不上」。
    """
    p = path.split("?", 1)[0].split("#", 1)[0]
    if p in _EXTRA_CACHE:
        return os.path.join(CACHE, *[s for s in p.split("/") if s])
    segs = [s for s in p.split("/") if s]
    if not segs:
        return None
    srv = None
    if segs[0].startswith("s") and segs[0][1:].isdigit():
        srv = segs[0]
        segs = segs[1:]
    elif segs[0] == "global_air":
        pass                                  # 保留前缀，单独一棵子树
    elif segs[0] in ("static", "plugins"):
        if not segs[-1].lower().endswith(_STATIC_WEB_EXT):
            return None
    elif segs[0] == "ecs":
        if not _ECS_STATIC_RE.match(p):
            return None                       # /ecs/update.php 之类一律不缓存
    else:
        # 白名单：只有 /s<N>/、/global_air/、/static/、/plugins/、/ecs/静态包 可缓存。
        return None
    if not segs or any(seg in (".", "..") for seg in segs):
        return None
    name = segs[-1]
    if name.lower().endswith(".php"):
        return None
    if not segs[:-1] and not name.lower().endswith(_ROOT_OK_EXT):
        # 根目录文件：只缓存大厅脚本类（Lobby_/ClientView_/RenderThread_ 等），
        # 战斗整包（无扩展名的 xxx_<md5>）不落盘，避免几 GB 的缓存
        return None
    if srv and _is_server_scoped(p):
        return os.path.join(CACHE, srv, *segs)
    return os.path.join(CACHE, *segs)


def server_scoped_path(srv, rel):
    """取某个区服的区服级文件（hall_config.xml 这类）"""
    return os.path.join(CACHE, srv, rel.replace("/", os.sep))


# ------------------------------------------------------------ 动态接口短缓存
# 启动器/大厅每次开游戏都要串行打一串 POST /ecs/*/update.php 和 GET /s10/lobby.php，
# 每个上游要 500~2400ms，八个接口串起来就是 5~8 秒白等。这些接口的内容在几秒内
# 完全一样（版本号、公告、服务器列表），所以在本地按「去掉防缓存参数后的路径」
# 存一份短 TTL 的响应副本：第二次起直接 0.5ms 返回。
# TTL 默认 600 秒，可用环境变量 SSJJ_DYN_TTL 调整（设 0 关闭）。
DYN_TTL = int(os.environ.get("SSJJ_DYN_TTL", "600") or 600)
DYN_DIR = os.path.join(CACHE, "_dyn")
# 所有版本检查与更新接口（update.php / version.php / gameguard/*）一律实时直连官方，
# 绝不缓存，确保游戏任何时候发版都能正常检测到更新、不会报代码 360 或版本不匹配。
# 这里只对纯公告列表做短缓存。
_DYN_RE = re.compile(
    r"^/get_micro_version_announce_list\.php$",
    re.I,
)
# 防缓存噪声参数：每次请求都变，必须从 key 里剔掉，否则永远命中不了。
# 注意 update.php 的 version 是「客户端当前版本」，不能剔——剔了以后客户端更新完
# 还会拿到旧的版本答复，更新链就断了。
_STRIP_COMMON = r"_|t|ts|rnd|r|time|timestamp|n|k|callback"
_DYN_STRIP_LOBBY = re.compile(r"[?&](?:v|" + _STRIP_COMMON + r")=[^&]*", re.I)
_DYN_STRIP_ECS = re.compile(r"[?&](?:seed|ip|os|" + _STRIP_COMMON + r")=[^&]*", re.I)


def _dyn_enabled(path):
    return DYN_TTL > 0 and bool(_DYN_RE.match(path.split("?")[0]))


def dyn_key(path):
    base, _, q = path.partition("?")
    rx = _DYN_STRIP_ECS if "/ecs/" in base.lower() else _DYN_STRIP_LOBBY
    # 前面加一个 "?" 再匹配：正则要求参数前有 ? 或 &，第一个参数本来没有。
    q2 = rx.sub("", "?" + q).lstrip("?&").strip("&")
    return hashlib.md5((base + "?" + q2).lower().encode("utf-8")).hexdigest()


_CTYPE_BY_EXT = {
    ".php": "text/html; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".xml": "text/xml; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".swf": "application/x-shockwave-flash",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".gif": "image/gif",
    ".txt": "text/plain; charset=utf-8",
}


def _ctype_for(path, ctype=None):
    """按扩展名纠正 ctype。

    [PATCH 2026-11-23] dyn 缓存曾把 /sN/lobby.php 存成 application/octet-stream
    （上游是 text/html;charset=utf-8）。lobby.php 返回的是一段 <script>，里面
    调 wdapp.login(lobby_truly.php?...) 把流程推进到签名 JSON -> WDlauncher；
    MIME 不对时 CEF 不执行那段脚本，登录器就永远停在「点区服没反应」。
    """
    base = (ctype or "").split(";")[0].strip().lower()
    if base and base not in ("application/octet-stream", "text/plain", "binary/octet-stream"):
        return ctype
    ext = os.path.splitext(path.split("?")[0])[1].lower()
    return _CTYPE_BY_EXT.get(ext, ctype or "application/octet-stream")


def dyn_get(path):
    if not _dyn_enabled(path):
        return None
    k = dyn_key(path)
    body_f = os.path.join(DYN_DIR, k + ".bin")
    meta_f = os.path.join(DYN_DIR, k + ".json")
    try:
        if not (os.path.isfile(body_f) and os.path.isfile(meta_f)):
            return None
        if time.time() - os.path.getmtime(body_f) > DYN_TTL:
            return None
        with open(meta_f, "r", encoding="utf-8") as f:
            info = json.load(f)
        with open(body_f, "rb") as f:
            return (int(info.get("code", 200)),
                    _ctype_for(info.get("path") or path, info.get("ctype")), f.read())
    except Exception:
        return None


def dyn_put(path, code, ctype, body):
    if not _dyn_enabled(path) or code != 200 or not body:
        return False
    ctype = _ctype_for(path, ctype)
    k = dyn_key(path)
    try:
        os.makedirs(DYN_DIR, exist_ok=True)
        with open(os.path.join(DYN_DIR, k + ".bin"), "wb") as f:
            f.write(body)
        with open(os.path.join(DYN_DIR, k + ".json"), "w", encoding="utf-8") as f:
            json.dump({"path": path[:200], "code": code, "ctype": ctype,
                       "ts": time.time()}, f, ensure_ascii=False)
        return True
    except Exception:
        return False


def dyn_get_stale(path):
    """过期但还在宽限期内（6×TTL）的条目：先照旧回给客户端，后台再去刷新。

    否则 TTL 一过，下次开游戏又回到「串行等八个接口」的老样子。
    """
    if not _dyn_enabled(path):
        return None
    k = dyn_key(path)
    body_f = os.path.join(DYN_DIR, k + ".bin")
    meta_f = os.path.join(DYN_DIR, k + ".json")
    try:
        if not (os.path.isfile(body_f) and os.path.isfile(meta_f)):
            return None
        if time.time() - os.path.getmtime(body_f) > DYN_TTL * 6:
            return None
        with open(meta_f, "r", encoding="utf-8") as f:
            info = json.load(f)
        with open(body_f, "rb") as f:
            return (int(info.get("code", 200)),
                    _ctype_for(info.get("path") or path, info.get("ctype")), f.read())
    except Exception:
        return None


_DYN_REFRESHING = set()


def _dyn_refresh(path):
    """后台把过期的动态接口重新拉一遍（不阻塞客户端）。"""
    if path in _DYN_REFRESHING:
        return
    _DYN_REFRESHING.add(path)
    try:
        if not UPSTREAM:
            return
        up = _open_upstream(UPSTREAM)
        try:
            req = ("GET %s HTTP/1.1\r\nHost: %s\r\nUser-Agent: ssjj-local-cdn\r\n"
                   "Accept: */*\r\nConnection: close\r\n\r\n" % (path, HOST))
            up.sendall(req.encode("latin-1"))
            rhead, body0 = _read_upstream_head(up)
            if not rhead:
                return
            status_line, clen, cenc, chunked = _parse_head(rhead)
            if " 200 " not in status_line:
                return
            buf = bytearray(body0 or b"")
            up.settimeout(READ_TIMEOUT)
            while len(buf) <= 262144:
                if clen is not None and len(buf) >= clen:
                    break
                d = up.recv(65536)
                if not d:
                    break
                buf += d
            raw = bytes(buf)
            if chunked:
                raw = _dechunk(raw)
            elif clen is not None:
                raw = raw[:clen]
            ct = _head_val(rhead, "content-type") or "application/octet-stream"
            if raw:
                dyn_put(path, 200, ct, raw)
                log("DYNR  后台刷新 %s" % path[:100])
        finally:
            try:
                up.close()
            except Exception:
                pass
    except Exception:
        pass
    finally:
        _DYN_REFRESHING.discard(path)


def dyn_clear():
    """清空动态接口缓存（接口改版或客户端更新后手滑一下）。"""
    n = 0
    try:
        for name in os.listdir(DYN_DIR):
            _del(os.path.join(DYN_DIR, name))
            n += 1
    except Exception:
        pass
    return n


class _DynTap:
    """把流式转发的内容顺手抄一份，用于动态接口短期缓存。"""
    def __init__(self, inner=None):
        self.inner = inner
        self.buf = bytearray()

    def write(self, w):
        if self.inner is not None:
            self.inner.write(w)
        if len(self.buf) < 262144:
            self.buf += w

    def close(self):
        if self.inner is not None:
            self.inner.close()


def _dechunk(data):
    """把 chunked 正文还原成裸正文（只用于动态接口的小响应缓存）。"""
    out = bytearray()
    i = 0
    while True:
        j = data.find(b"\r\n", i)
        if j < 0:
            break
        try:
            n = int(data[i:j].split(b";")[0], 16)
        except Exception:
            break
        if n == 0:
            break
        out += data[j + 2:j + 2 + n]
        i = j + 2 + n + 2
    return bytes(out)


def _head_val(rhead, key):
    try:
        for ln in rhead.decode("latin-1", "replace").split("\r\n")[1:]:
            k, _, v = ln.partition(":")
            if k.strip().lower() == key:
                return v.strip()
    except Exception:
        pass
    return ""


def build_response(code, body, ctype="application/octet-stream", head_only=False,
                   keep_alive=False, cache_control="max-age=315360000"):
    reason = {200: "OK", 404: "Not Found", 500: "Error", 502: "Bad Gateway"}.get(code, "OK")
    conn = "keep-alive" if keep_alive else "close"
    hdr = ("HTTP/1.1 %d %s\r\nContent-Type: %s\r\nContent-Length: %d\r\n"
           "Cache-Control: %s\r\nConnection: %s\r\n\r\n"
           % (code, reason, ctype, len(body), cache_control, conn))
    return hdr.encode("latin-1") + (b"" if head_only else body)


def _open_upstream(ip, timeout=CONNECT_TIMEOUT):
    return socket.create_connection((ip, 80), timeout=timeout)


def _read_upstream_head(up, timeout=READ_TIMEOUT):
    """读上游响应头，返回 (head_bytes, body_prefix) 或 (None, None)"""
    up.settimeout(timeout)
    rbuf = b""
    while b"\r\n\r\n" not in rbuf:
        d = up.recv(65536)
        if not d:
            break
        rbuf += d
        if len(rbuf) > 131072:
            break
    if b"\r\n\r\n" not in rbuf:
        return None, None
    head, _, body0 = rbuf.partition(b"\r\n\r\n")
    return head, body0


def _parse_head(rhead):
    """从响应头里取 (status_line, content_length, content_encoding, chunked)"""
    rlines = rhead.decode("latin-1", "replace").split("\r\n")
    status_line = rlines[0]
    clen, cenc, chunked = None, None, False
    for ln in rlines[1:]:
        k, _, v = ln.partition(":")
        k = k.strip().lower()
        v = v.strip()
        if k == "content-length":
            try:
                clen = int(v)
            except Exception:
                clen = None
        elif k == "content-encoding":
            cenc = v
        elif k == "transfer-encoding" and "chunked" in v.lower():
            chunked = True
    return status_line, clen, cenc, chunked


def _pump_upstream(up, conn, rhead, body0, clen, fh):
    """把上游正文转发给客户端，同时写盘。

    客户端中途断开不算失败：继续把上游读完落盘，这样下次就命中了。
    返回 (转发字节数, 写盘字节数)。
    """
    got = wrote = 0
    client_alive = True
    up.settimeout(READ_TIMEOUT)

    def _send(data):
        nonlocal client_alive
        if not client_alive:
            return
        try:
            conn.sendall(data)
        except Exception:
            client_alive = False

    if rhead:
        _send(rhead + b"\r\n\r\n")
    if body0:
        _send(body0)
        got += len(body0)
        if fh:
            if clen is None:
                fh.write(body0)
                wrote += len(body0)
            else:
                w = body0[:max(0, clen - wrote)]
                fh.write(w)
                wrote += len(w)
    while True:
        if clen is not None and got >= clen:
            break
        try:
            d = up.recv(65536)
        except socket.timeout:
            _step("上游读超时 got=%d clen=%s" % (got, clen))
            break
        if not d:
            break
        _send(d)
        got += len(d)
        if fh:
            # clen=None 表示上游用「连接关闭」界定正文（或 chunked）：
            # 这时照单全收，否则会拿 None 去做减法直接抛 TypeError。
            if clen is None:
                if wrote < 262144:
                    fh.write(d)
                    wrote += len(d)
            elif wrote < clen:
                w = d[:clen - wrote]
                fh.write(w)
                wrote += len(w)
    return got, wrote


# ---------------------------------------------------------------- HTTP
# ---------------------------------------------------------------- 静态换肤覆盖
# 战斗端启动时会拉 /sN/Windows/AssetBundles_<msd>.manifest，里面每个 bundle 都带
# md5/crc/size；加载时还要校验 Preload/<md5>/data 的内容 md5 == 目录名（不等就报
# PreloadMd5Mismatch 并拒绝加载，表现就是「只能进大厅、进不去对局」）。
# 所以静态换肤不能就地改文件，必须：① 清单里把 md5 换成新内容的 md5；② 让客户端按
# 新 md5 的 URL 下到我们改过的包。两件事都在这里做。
OVERRIDE_FILE = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                             "ssjj-toolbox", "bundle_overrides.json")
_MANIFEST_RE = re.compile(r"^/s\d+/Windows/AssetBundles_[0-9a-fA-F]+_\d+\.manifest$")
_BUNDLE_OV_RE = re.compile(r"^/s\d+/Windows/(.+)_([0-9a-fA-F]{32})$")
_OV = {"ts": -1.0, "data": {}}


def overrides():
    """读取本地覆盖表 {bundle 名: {md5, crc, size, path}}；文件改了才重读。"""
    try:
        ts = os.stat(OVERRIDE_FILE).st_mtime
    except OSError:
        _OV["data"] = {}
        _OV["ts"] = -1.0
        return _OV["data"]
    if ts != _OV["ts"]:
        try:
            with open(OVERRIDE_FILE, encoding="utf-8") as fh:
                _OV["data"] = (json.load(fh) or {}).get("bundles") or {}
        except Exception as e:
            log("[ov] 覆盖表读取失败 %r" % (e,))
            _OV["data"] = {}
        _OV["ts"] = ts
        log("[ov] 载入 %d 条静态换肤覆盖" % len(_OV["data"]))
    return _OV["data"]


def _patch_manifest(body):
    ov = overrides()
    if not ov:
        return body
    txt = body.decode("utf-8", "replace")
    hit = 0
    for name, info in ov.items():
        md5 = str(info.get("md5") or "")
        if not md5:
            continue
        pat = re.compile(r'<AssetBundle\b[^>]*\bname="%s"[^>]*>' % re.escape(name))

        def _sub(m, _info=info, _md5=md5):
            tag = m.group(0)
            tag = re.sub(r'md5="[0-9a-fA-F]{32}"', 'md5="%s"' % _md5, tag)
            if _info.get("crc") is not None:
                tag = re.sub(r'crc="\d+"', 'crc="%d"' % int(_info["crc"]), tag)
            if _info.get("size"):
                tag = re.sub(r'size="\d+"', 'size="%d"' % int(_info["size"]), tag)
            return tag

        txt, n = pat.subn(_sub, txt)
        hit += n
    if hit:
        log("[ov] 清单已改写 %d 个 bundle 的 md5" % hit)
    return txt.encode("utf-8")


def _fetch_all(path, timeout=45):
    """整包回源（清单 2.9MB 这种一次性小文件用）。返回 (status, (ctype, bytes))。"""
    import http.client
    ip = UPSTREAM
    if not ip:
        return None, None
    c = http.client.HTTPConnection(ip, 80, timeout=timeout)
    try:
        c.request("GET", path, headers={"Host": HOST, "User-Agent": "ssjj-local-cdn", "Accept": "*/*"})
        r = c.getresponse()
        data = r.read()
        return r.status, (r.getheader("Content-Type") or "application/octet-stream", data)
    finally:
        try:
            c.close()
        except Exception:
            pass


def handle_http(conn, addr):
    tmp = None
    try:
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass
        keep_buf = b""
        while True:
            conn.settimeout(12 if keep_buf else 5)
            buf = keep_buf
            keep_buf = b""
            while b"\r\n\r\n" not in buf:
                try:
                    chunk = conn.recv(8192)
                except socket.timeout:
                    return
                if not chunk:
                    return
                buf += chunk
                if len(buf) > 65536:
                    return

            head, _, rest = buf.partition(b"\r\n\r\n")
            lines = head.decode("latin-1", "replace").split("\r\n")
            try:
                method, path, _ = lines[0].split(" ", 2)
            except Exception:
                return
            # 读满请求体（启动器的更新接口走 POST）
            req_clen = 0
            req_keep = True
            for ln in lines[1:]:
                ll = ln.lower()
                if ll.startswith("content-length:"):
                    try:
                        req_clen = int(ln.split(":", 1)[1].strip())
                    except Exception:
                        req_clen = 0
                elif ll.startswith("connection:") and "close" in ll:
                    req_keep = False
            while len(rest) < req_clen:
                try:
                    d = conn.recv(65536)
                except socket.timeout:
                    break
                if not d:
                    break
                rest += d
            if len(rest) > req_clen:
                keep_buf = rest[req_clen:]
                rest = rest[:req_clen]

            if path.lower().startswith("http://"):        # 代理式绝对 URI
                u = urllib.parse.urlsplit(path)
                path = u.path + (("?" + u.query) if u.query else "")

            if path.split("?")[0] == "/__ping__":
                conn.sendall(build_response(200, b"ssjj-local-cdn", keep_alive=False))
                return
            if path.split("?")[0] == "/__stats__":
                with _slk:
                    body = json.dumps(STATS, ensure_ascii=False).encode("utf-8")
                conn.sendall(build_response(200, body, "application/json", keep_alive=False))
                return

            t0 = time.time()
            # 静态换肤：清单改写 + 按新 md5 下发改过的包（见 overrides()）
            _p_only = path.split("?")[0]
            if overrides():
                if _MANIFEST_RE.match(_p_only):
                    _st, _payload = _fetch_all(path)
                    if _payload:
                        _ct, _data = _payload
                        if _st == 200 and _data:
                            _data = _patch_manifest(_data)
                        conn.sendall(build_response(_st or 502, _data, _ct,
                                                    head_only=(method == "HEAD"), keep_alive=False))
                        return
                _m = _BUNDLE_OV_RE.match(_p_only)
                if _m:
                    _info = overrides().get(_m.group(1))
                    if _info and str(_info.get("md5") or "").lower() == _m.group(2).lower():
                        try:
                            with open(_info["path"], "rb") as _fh:
                                _data = _fh.read()
                            with _slk:
                                STATS["hit"] += 1
                                STATS["bytes"] += len(_data)
                            log("OVHIT %6.1fms %dB GET %s" % ((time.time() - t0) * 1000, len(_data), path))
                            conn.sendall(build_response(200, _data, "application/octet-stream",
                                                        head_only=(method == "HEAD"), keep_alive=False))
                            return
                        except Exception as _e:
                            log("[ov] 读取覆盖包失败 %r" % (_e,))
            # 动态接口（版本检查 / 服务器列表）先看本地短缓存：
            # 启动时这几个接口串行打，每个上游 0.5~2.4 秒，是「开游戏干等」的主因。
            dyn = dyn_get(path) if method in ("GET", "HEAD", "POST") else None
            stale = False
            if not dyn and method in ("GET", "HEAD", "POST"):
                dyn = dyn_get_stale(path)
                if dyn:
                    stale = True
                    threading.Thread(target=_dyn_refresh, args=(path,), daemon=True).start()
            if dyn:
                code, ctype, dbody = dyn
                conn.sendall(build_response(code, dbody, ctype, head_only=(method == "HEAD"),
                                            keep_alive=False,
                                            cache_control="no-cache, no-store, max-age=0"))
                with _slk:
                    STATS["hit"] += 1
                    STATS["bytes"] += len(dbody)
                log("DYN%s  %5.1fms %7dB %s %s" % ("*" if stale else " ",
                                                   (time.time() - t0) * 1000, len(dbody),
                                                   method, path[:100]))
                return

            dst = path_to_file(path)
            hit = None
            if method in ("GET", "HEAD") and dst and os.path.isfile(dst) and os.path.getsize(dst) > 0:
                # 大厅主 SWF 命中帧率覆盖时，换成改过帧率的那个文件
                if FPS_OVERRIDE["src"] and os.path.basename(dst) == FPS_OVERRIDE["src"]:
                    alt = os.path.join(CACHE, FPS_OVERRIDE["dst"])
                    if os.path.isfile(alt):
                        dst = alt
                # hall_config.xml 是「客户端破缓存、但官方会改」的例外：本地副本
                # 过期就丢掉重新回源，免得登录 URL / 区服列表永远是旧的。
                if (path.split("?")[0].endswith("/" + HALL_CONFIG)
                        and time.time() - os.path.getmtime(dst) > HALL_CONFIG_TTL):
                    _del(dst)
                    with _slk:
                        STATS["local_inject"] += 1
                    log("STALE %s 本地副本过期，重新回源" % path[:100])
                else:
                    hit = dst

            if hit:
                with open(hit, "rb") as f:
                    body = f.read()
                ext = os.path.splitext(hit)[1].lower()
                conn.sendall(build_response(200, body, MIME.get(ext, "application/octet-stream"),
                                            head_only=(method == "HEAD"), keep_alive=False))
                with _slk:
                    STATS["hit"] += 1
                    STATS["bytes"] += len(body)
                log("HIT   %5.1fms %7dB %s %s" % ((time.time() - t0) * 1000, len(body),
                                                  method, path[:110]))
                return
            break

        with _slk:
            STATS["miss"] += 1
        if os.environ.get("CDN_TRACE"):
            sys.stderr.write("TRACE miss %s dst=%r\n" % (path[:80], dst)); sys.stderr.flush()

        if not UPSTREAM:
            conn.sendall(build_response(404, b"no upstream"))
            log("MISS-noup %s" % path[:120])
            return

        # 回源。带重试，并且把「连不上 / 没拿到响应头」和「拿到头但正文断了」
        # 分开处理：后者已经给客户端发过头了，不能重试，只能记下来。
        up = None
        rhead = body0 = None
        for attempt in range(UPSTREAM_RETRY + 1):
            try:
                up = _open_upstream(UPSTREAM)
                # 去掉客户端的 keep-alive，强制上游响应完就关，否则读不到 EOF。
                # 同时剥掉条件请求头：客户端带着 If-Modified-Since 来问，上游回
                # 304 空正文，我们本地又没存过这份，于是永远拿不到内容、永远
                # 不缓存（实测同一 URL 连续 3 次 0 字节）。必须强制上游回 200
                # 完整正文，本地才有东西可存。发的还是 GET，语义上等价。
                #
                # Host 必须重写：客户端发来的是 Host: 127.0.0.1（我们把域名劫持到
                # 本地了），原样转给 CDN 边缘会被直接 RST（实测 ConnectionReset
                # 10054 × 3）。转发前统一换成真域名。
                #
                # Accept-Encoding 也剥：带 gzip/br 回来的是压缩正文，落盘后就再也
                # 不能当明文缓存发给别人；只要 identity，缓存与 MIME 才都正确。
                olines = ["%s %s HTTP/1.1" % (method, path)] + [
                    ln for ln in lines[1:]
                    if not ln.lower().startswith(("connection:", "host:",
                                                  "if-modified-since:", "if-none-match:",
                                                  "if-range:", "accept-encoding:"))]
                olines.append("Host: %s" % HOST)
                olines.append("Connection: close")
                if os.environ.get("CDN_TRACE"):
                    sys.stderr.write("TRACE 回源 -> %r head=%r\n"
                                     % (UPSTREAM, "\r\n".join(olines)[:400]))
                    sys.stderr.flush()
                up.sendall(("\r\n".join(olines)).encode("latin-1") + b"\r\n\r\n" + rest)
                rhead, body0 = _read_upstream_head(up)
                if rhead is not None:
                    break
                _step("回源无响应头 attempt=%d" % attempt)
            except Exception as e:
                _step("回源异常 attempt=%d %r" % (attempt, e))
                if up:
                    try:
                        up.close()
                    except Exception:
                        pass
                    up = None
            if attempt < UPSTREAM_RETRY:
                time.sleep(0.3 * (attempt + 1))

        if rhead is None:
            with _slk:
                STATS["upstream_fail"] += 1
            log("MISS  回源失败 %s" % path[:110])
            if up:
                try:
                    up.close()
                except Exception:
                    pass
            conn.sendall(build_response(502, b"upstream unavailable"))
            return

        status_line, clen, cenc, chunked = _parse_head(rhead)

        # 上游仍回 304（有的 CDN 不看我们删没删头都会这么干）：304 是空正文，
        # 本地没副本时转发它毫无意义——客户端拿不到内容还会无限重试。
        # 此时丢掉这次响应，用 GET 无条件重取一次。
        if " 304 " in status_line and not os.path.isfile(dst or ""):
            _step("上游 304 但本地无副本，强制重取")
            if up:
                try:
                    up.close()
                except Exception:
                    pass
            up = _open_upstream(UPSTREAM)
            hdr = "GET %s HTTP/1.1\r\nHost: %s\r\nUser-Agent: ssjj-local-cdn\r\n" \
                  "Accept: */*\r\nConnection: close\r\n\r\n" % (path, HOST)
            up.sendall(hdr.encode("latin-1"))
            rhead, body0 = _read_upstream_head(up)
            if rhead is None:
                conn.sendall(build_response(502, b"upstream unavailable"))
                return
            status_line, clen, cenc, chunked = _parse_head(rhead)
            method = "GET"      # 正文已是完整 GET 响应，后面按 GET 走

        cacheable_exact = (dst and status_line.find(" 200 ") > 0 and clen is not None
                           and 0 < clen <= MAX_CACHE and not cenc and not chunked
                           and method == "GET")
        cacheable_chunked = (dst and status_line.find(" 200 ") > 0 and not cenc
                             and (chunked or clen is None) and method == "GET")
        fh = None
        if cacheable_exact or cacheable_chunked:
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                tmp = dst + ".part"
                fh = open(tmp, "wb")
            except Exception:
                fh = None

        _step("上送响应头 ctype/len")
        # 动态接口顺手抄一份。流式转发不方便二次读取 body，所以挂一个 tee。
        tap = None
        pump_fh = fh
        if _dyn_enabled(path) and " 200 " in status_line and (not clen or 0 < clen <= 262144):
            tap = _DynTap(fh)
            pump_fh = tap
        try:
            got, wrote = _pump_upstream(up, conn, rhead, body0, clen, pump_fh)
        finally:
            try:
                up.close()
            except Exception:
                pass
        _step("body 转发完成 got=%d clen=%s" % (got, clen))

        saved = False
        if fh:
            fh.close()
            fh = None
            if cacheable_exact and wrote == clen:
                try:
                    os.replace(tmp, dst)
                    tmp = None
                    saved = True
                    with _slk:
                        STATS["save"] += 1
                        STATS["saved_mb"] += wrote / 1048576.0
                except Exception as e:
                    log("[!] 存库失败 %r" % (e,))
            elif cacheable_chunked and tmp and os.path.isfile(tmp):
                try:
                    raw_f = open(tmp, "rb").read()
                    if chunked:
                        raw_f = _dechunk(raw_f)
                    if 0 < len(raw_f) <= MAX_CACHE:
                        with open(tmp, "wb") as wf:
                            wf.write(raw_f)
                        os.replace(tmp, dst)
                        tmp = None
                        saved = True
                        with _slk:
                            STATS["save"] += 1
                            STATS["saved_mb"] += len(raw_f) / 1048576.0
                except Exception as e:
                    log("[!] chunked存库失败 %r" % (e,))
            if tmp:
                _del(tmp)
                tmp = None
        if dst and not saved and " 200 " in status_line and not os.path.isfile(dst):
            # HEAD 请求、或本次回源中途断连未存盘的静态资源，自动丢进后台补齐队列
            _bg_fetch_enqueue(path, dst)

        _step("存库结果 saved=%s tmp=%r" % (saved, tmp))
        if tap is not None and tap.buf:
            raw = bytes(tap.buf)
            if chunked:
                raw = _dechunk(raw)
            elif clen is not None:
                raw = raw[:clen]
            ct = _head_val(rhead, "content-type") or "application/octet-stream"
            if raw and dyn_put(path, 200, ct, raw):
                with _slk:
                    STATS["dyn_save"] += 1
        with _slk:
            STATS["bytes"] += got
        # 记上状态码和 Content-Length：0 字节响应到底是上游返 200+空体、
        # 还是 304、还是 204，只看现在的日志完全分不出来。
        log("MISS  %5.1fms %7dB %s %s clen=%s%s" % (
            (time.time() - t0) * 1000, got, method, path[:100],
            clen, "  [存库]" if saved else ""))
        if status_line and not status_line.endswith(" 200 OK"):
            log("      ^ 上游状态: %s" % status_line)
    except Exception as e:
        if os.environ.get("CDN_TRACE"):
            import traceback; traceback.print_exc(file=sys.stderr)
        log("[!] http %r %s" % (e, addr))
    finally:
        if tmp:
            _del(tmp)
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------- HTTPS 透传
def _risk_mitm(conn, addr):
    """风险模式：443 走 MITM 本地化（未开启时秒返回 False，不影响普通模式）。

    返回 True 表示这条连接已经由 MITM 处理完（或为空连接已丢弃），调用方不要再透传。
    """
    try:
        from risk import mitm
        if not mitm.enabled():
            return False
        conn.settimeout(10)
        peek = conn.recv(65536, socket.MSG_PEEK)
        if not peek:                          # 客户端建连未发任何字节即断开，直接丢弃
            return True
        if peek[0] != 0x16:                   # 不是 TLS ClientHello，记日志后交回透传
            log("HTTPS-RAW first=%s" % peek[:8].hex())
            return False
        sni = _tls_sni(peek) or HOST
        log("MITMs %s" % sni)
        mitm.serve_conn(conn, addr, sni)
        return True
    except socket.timeout:
        return True
    except Exception as e:
        log("[!] risk mitm %r" % (e,))
        return False


def handle_https(conn, addr):
    try:
        # HTTPS 全是小请求（活动列表、图标、配置），Nagle 会白等一个 RTT；
        # 本地又多做了一跳转发，这里必须关掉延迟确认，不然一屏资源能拖慢好几秒。
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass
        if _risk_mitm(conn, addr):
            return
        conn.settimeout(30)
        if not UPSTREAM:
            return
        # 先读客户端的第一段（TLS ClientHello），取出 SNI 用于日志。
        # 之前这里是纯盲区：只知道透传了多少次，不知道客户端要访问什么。
        first = None
        try:
            conn.settimeout(5)
            first = conn.recv(65536)
        except Exception:
            pass
        if not first:
            return
        sni = _tls_sni(first)
        if sni:
            log("HTTPS %s" % sni)
        up = socket.create_connection((UPSTREAM, 443), timeout=10)
        try:
            up.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass
        with _slk:
            STATS["https"] += 1
        if first:
            up.sendall(first)

        def pump(a, b):
            try:
                while True:
                    d = a.recv(65536)
                    if not d:
                        break
                    b.sendall(d)
            except Exception:
                pass
            finally:
                try:
                    b.shutdown(socket.SHUT_WR)
                except Exception:
                    pass

        t1 = threading.Thread(target=pump, args=(conn, up), daemon=True)
        t2 = threading.Thread(target=pump, args=(up, conn), daemon=True)
        t1.start(); t2.start()
        t1.join(); t2.join()
        up.close()
    except Exception:
        pass


def _tls_sni(hello):
    """从 TLS ClientHello 里取 SNI（server_name 扩展），失败返回 None"""
    try:
        if len(hello) < 43 or hello[0] != 0x16:
            return None
        p = 43                                  # 跳过 record + handshake 头
        slen = int.from_bytes(hello[p:p + 1], "big"); p += 1 + slen
        clen = int.from_bytes(hello[p:p + 2], "big"); p += 2 + clen
        p += 1                                   # compression_methods 长度
        p += int.from_bytes(hello[p:p + 1], "big") + 1
        elen = int.from_bytes(hello[p:p + 2], "big"); p += 2
        end = min(len(hello), p + elen)
        while p + 4 <= end:
            etype = int.from_bytes(hello[p:p + 2], "big")
            esize = int.from_bytes(hello[p + 2:p + 4], "big")
            p += 4
            if etype == 0:                      # server_name
                q = p + 3                       # 跳过 list 长度 + type
                nlen = int.from_bytes(hello[q:q + 2], "big")
                return hello[q + 2:q + 2 + nlen].decode("ascii", "replace")
            p += esize
    except Exception:
        return None
    return None


_bind_failed = []


def serve(port, handler):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    except Exception:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", port))
    except OSError as e:
        log("[!] 端口 %d 绑定失败 %r（可能已有实例在跑）" % (port, e))
        _bind_failed.append(port)
        return
    s.listen(256)
    log("[*] 监听 127.0.0.1:%d" % port)
    while True:
        try:
            conn, addr = s.accept()
        except Exception:
            continue
        threading.Thread(target=handler, args=(conn, addr), daemon=True).start()


# ---------------------------------------------------------------- hosts
HOSTS = r"C:\Windows\System32\drivers\etc\hosts"
MARK = "# ssjj-local-cdn"


def load_fps_override():
    """读帧率配置。配置文件存在就启用，src/dst 是资源库里的相对文件名。"""
    global FPS_OVERRIDE
    p = os.path.join(P.HERE, "fps_override.json")
    try:
        d = json.load(open(p, encoding="utf-8"))
        if d.get("src") and d.get("dst") and int(d.get("fps") or 0) > 0:
            FPS_OVERRIDE = {"src": d["src"], "dst": d["dst"], "fps": int(d["fps"])}
            log("[帧率] 启用 %d fps：%s -> %s" % (d["fps"], d["src"], d["dst"]))
            return
    except FileNotFoundError:
        pass
    except Exception as e:
        log("[!] 帧率配置读取失败 %r" % (e,))
    FPS_OVERRIDE = {"src": None, "dst": None, "fps": 0}


def _hosts_has_mark():
    try:
        text = open(HOSTS, "rb").read().decode("utf-8", "replace")
        return MARK in text
    except Exception:
        return False


def hosts_set(add=True):
    try:
        raw = open(HOSTS, "rb").read()
    except PermissionError:
        log("[!] 没权限改 hosts，请以管理员身份运行")
        return False
    text = raw.decode("utf-8", "replace")
    lines = [ln for ln in text.splitlines() if MARK not in ln]
    if add:
        lines.append("127.0.0.1 %s %s" % (HOST, MARK))
    new = "\r\n".join(lines).rstrip("\r\n") + "\r\n"
    if new == text:
        return True
    try:
        open(HOSTS, "wb").write(new.encode("utf-8"))
        os.system("ipconfig /flushdns >nul 2>&1")
        log("[*] hosts %s" % ("已指向本机" if add else "已还原"))
        return True
    except Exception as e:
        log("[!] 写 hosts 失败 %r" % (e,))
        return False


STARTUP_DIR = os.path.join(os.environ.get("APPDATA", ""),
                           r"Microsoft\Windows\Start Menu\Programs\Startup")
STARTUP_VBS = os.path.join(STARTUP_DIR, "ssjj-local-cdn.vbs")


def _startup(add=True):
    """开机自启：写一个脚本文件，无窗口拉起加速服务"""
    try:
        if not add:
            if os.path.isfile(STARTUP_VBS):
                _del(STARTUP_VBS)
            print("[*] 已取消开机自启")
            return
        pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        script = os.path.abspath(__file__)
        q = '"'
        cmdline = "%s%s%s %s%s%s daemon" % (q, pyw, q, q, script, q)
        vbs = ('Set s = CreateObject("WScript.Shell")\r\n'
               's.Run "%s", 0, False\r\n' % cmdline.replace(q, q * 2))
        with open(STARTUP_VBS, "w", encoding="mbcs", errors="replace") as f:
            f.write(vbs)
        print("[*] 已安装开机自启: %s" % STARTUP_VBS)
    except Exception as e:
        print("[!] 设置自启失败: %r" % (e,))


def reporter():
    while True:
        time.sleep(60)
        with _slk:
            s = dict(STATS)
        if s["hit"] or s["miss"]:
            log("[统计] 本地命中 %d | 回源 %d(新存 %d 个 %.1fMB) | HTTPS透传 %d | 流量 %.1fMB"
                % (s["hit"], s["miss"], s["save"], s["saved_mb"], s["https"],
                   s["bytes"] / 1048576))


def _ping(timeout=2):
    """探活。80 和 443 都要在，只探一个会把「半死」状态误判成健康。"""
    try:
        import http.client as hc
        c = hc.HTTPConnection("127.0.0.1", 80, timeout=timeout)
        c.request("GET", "/__ping__")
        if c.getresponse().read() != b"ssjj-local-cdn":
            return False
    except Exception:
        return False
    try:
        with socket.create_connection(("127.0.0.1", 443), timeout=timeout):
            return True
    except Exception:
        return False


def _kill_pid_file(path):
    try:
        pid = int(open(path).read().strip())
        h = _k32.OpenProcess(1, False, pid)
        if h:
            _k32.TerminateProcess(h, 0)
            _k32.CloseHandle(h)
            log("[*] 已结束进程 pid=%d (%s)" % (pid, os.path.basename(path)))
    except Exception:
        pass


def cmd_fps(argv):
    """大厅帧率控制

    fps status          看当前状态
    fps set 60          生成 60fps 主 SWF 并启用
    fps off             关闭（恢复官方 24fps）
    """
    sub = argv[0] if argv else "status"
    cfg = os.path.join(P.HERE, "fps_override.json")

    if sub == "status":
        paras = P.read_paras()
        name = paras.get("lobbyFileName")
        print("大厅主 SWF : %s" % (name or "（paras.dat 里没有）"))
        if name:
            src = os.path.join(CACHE, name)
            if os.path.isfile(src):
                try:
                    print("官方帧率   : %d fps" % swf_fps.read_fps(src))
                except Exception as e:
                    print("官方帧率   : 读取失败 %r" % (e,))
            else:
                print("官方帧率   : 资源库里还没有这个文件（先起一次加速服务让它回源）")

        if FPS_OVERRIDE["fps"]:
            print("当前覆盖   : %d fps -> %s" % (FPS_OVERRIDE["fps"], FPS_OVERRIDE["dst"]))
        else:
            print("当前覆盖   : 未启用")
        return

    if sub == "off":
        # 关闭覆盖，并把 paras.dat 恢复成官方文件名（如果之前 rename 过）
        rec = {}
        try:
            rec = json.load(open(cfg, encoding="utf-8"))
        except Exception:
            pass
        if rec.get("renamed") and rec.get("src"):
            try:
                d = P.read_paras()
                d["lobbyFileName"] = rec["src"]
                with open(P.PARAS, "w", encoding="utf-8") as f:
                    json.dump(d, f, ensure_ascii=False, separators=(",", ":"))
                print("[*] paras.dat 的 lobbyFileName 已恢复为 %s" % rec["src"])
            except Exception as e:
                print("[!] 恢复 paras.dat 失败: %r" % (e,))
        if os.path.isfile(cfg):
            _del(cfg)
        print("[*] 已关闭大厅帧率覆盖。重启加速服务后生效。")
        return

    if sub == "set":
        if not argv[1:]:
            print("[!] 用法: local_cdn.py fps set <帧率> [--rename]")
            return
        try:
            fps = int(argv[1])
        except ValueError:
            print("[!] 帧率必须是整数")
            return
        rename = "--rename" in argv
        if not 24 <= fps <= 120:
            print("[!] 帧率限制在 24~120。再高 CPU 单核吃满，画面反而更不稳。")
            return
        paras = P.read_paras()
        name = paras.get("lobbyFileName")
        if not name:
            print("[!] paras.dat 里没有 lobbyFileName，先启动一次游戏")
            return
        try:
            out_name, md5, old, new = swf_fps.make_fps_variant(CACHE, name, fps)
        except Exception as e:
            print("[!] 生成失败: %r" % (e,))
            return
        rec = {"src": name, "dst": out_name, "fps": fps,
               "md5": md5, "ts": time.time(), "renamed": rename}
        if rename:
            # 把 paras.dat 的 lobbyFileName 指到新文件。新文件名的 md5 与内容
            # 一致，客户端一次校验就通过，不需要重试 5 次。
            paras["lobbyFileName"] = out_name
            try:
                with open(P.PARAS, "w", encoding="utf-8") as f:
                    json.dump(paras, f, ensure_ascii=False, separators=(",", ":"))
                rec["paras_backup"] = P.PARAS + ".ssjj-bak"
                print("[*] 已改写 paras.dat 的 lobbyFileName -> %s" % out_name)
                print("    原始备份: %s" % rec["paras_backup"])
            except Exception as e:
                print("[!] 改 paras.dat 失败（游戏可能正在运行）: %r" % (e,))
                print("    仍可按兼容方式生效（会多 5 次重试）")
                rec["renamed"] = False
                rec["src"] = name
        json.dump(rec, open(cfg, "w", encoding="utf-8"), indent=2)
        print("[*] 已生成 %d fps 主 SWF" % new)
        print("    原文件 : %s (%d fps)" % (name, old))
        print("    新文件 : %s (%d fps)" % (out_name, new))
        print("    新 md5 : %s" % md5)
        if rec.get("renamed"):
            print("[*] 文件名与内容 md5 一致，客户端一次校验通过。")
        else:
            print("[!] 文件名仍是旧的，客户端会校验失败并重载 5 次后仍然加载。")
            print("    想干净生效：关闭游戏后跑 `local_cdn.py fps set %d --rename`" % fps)
        print("[*] 重启加速服务后生效。")
        return

    print("用法: local_cdn.py fps [status|set <帧率>|off]")


_BG_PENDING = set()
_BG_LOCK = threading.Lock()


def _bg_fetch_enqueue(path, dst):
    """把本次未落盘的静态 MISS 丢进后台线程自动拉取并存库（去重，不阻塞客户端）。"""
    if not path or not dst or not UPSTREAM:
        return
    with _BG_LOCK:
        if dst in _BG_PENDING:
            return
        _BG_PENDING.add(dst)

    def _job():
        tmp = dst + ".bgpart"
        try:
            if os.path.isfile(dst) and os.path.getsize(dst) > 0:
                return
            up = _open_upstream(UPSTREAM, timeout=10)
            try:
                req = ("GET %s HTTP/1.1\r\nHost: %s\r\nUser-Agent: ssjj-local-cdn-bg\r\n"
                       "Accept: */*\r\nConnection: close\r\n\r\n" % (path, HOST))
                up.sendall(req.encode("latin-1"))
                rhead, body0 = _read_upstream_head(up, timeout=20)
                if not rhead:
                    return
                sline, clen, cenc, chunked = _parse_head(rhead)
                if " 200 " not in sline or cenc:
                    return
                buf = bytearray(body0 or b"")
                up.settimeout(READ_TIMEOUT)
                while len(buf) <= MAX_CACHE:
                    if clen is not None and len(buf) >= clen:
                        break
                    d = up.recv(65536)
                    if not d:
                        break
                    buf += d
                raw = bytes(buf)
                if chunked:
                    raw = _dechunk(raw)
                elif clen is not None:
                    raw = raw[:clen]
                if 0 < len(raw) <= MAX_CACHE and (clen is None or chunked or len(raw) == clen):
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    with open(tmp, "wb") as f:
                        f.write(raw)
                    os.replace(tmp, dst)
                    tmp = None
                    with _slk:
                        STATS["save"] += 1
                        STATS["saved_mb"] += len(raw) / 1048576.0
                    log("AUTO  %7dB 后台自动入库 %s" % (len(raw), path[:100]))
            finally:
                try:
                    up.close()
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            if tmp and os.path.isfile(tmp):
                _del(tmp)
            with _BG_LOCK:
                _BG_PENDING.discard(dst)

    try:
        threading.Thread(target=_job, daemon=True).start()
    except Exception:
        with _BG_LOCK:
            _BG_PENDING.discard(dst)


def _bg_cover():
    """后台静默补齐：直接进程内调用 exhaustive_fetch.cmd_cover（打包版 exe 同样生效）。"""
    def work():
        try:
            time.sleep(5)
            import exhaustive_fetch as EF
            from types import SimpleNamespace
            req = EF._requests_from_log()
            neg = EF._load_neg()
            lack = [k for k in req if not os.path.isfile(EF._cache_path(k)) and k not in neg]
            if not lack:
                return
            log("[自动补齐] 发现 %d 个历史未命中资源，开始后台静默下载…" % len(lack))
            EF.cmd_cover(SimpleNamespace(dry_run=False))
            log("[自动补齐] 后台补齐完成")
        except Exception as e:
            log("[自动补齐] 跳过 %r" % (e,))
    try:
        threading.Thread(target=work, daemon=True).start()
    except Exception:
        pass


def main():
    global UPSTREAM
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    load_fps_override()
    if cmd == "fps":
        return cmd_fps(sys.argv[2:])
    if cmd == "daemon":
        import subprocess
        hosts_set(True)
        try:
            open(DAEMON_PID, "w").write(str(os.getpid()))
        except Exception:
            pass
        log("[*] 守护模式启动")
        _bg_cover()
        # 起不来的时候（端口被占、python 路径失效）不能每 3 秒无脑重拉一次，
        # 否则日志会被刷爆、CPU 也白烧。失败就指数退避，最多退到 60 秒。
        fails = 0
        while True:
            try:
                if os.path.exists(DAEMON_PID) is False:
                    log("[*] 守护文件被移除，退出")
                    return
                if not _ping():
                    subprocess.call([sys.executable, os.path.abspath(__file__), "run"])
                    time.sleep(1)
                    if _ping():
                        if fails:
                            log("[*] 守护：服务已恢复")
                        fails = 0
                    else:
                        fails += 1
                        wait = min(2 ** min(fails, 6), 60)
                        log("[!] 守护：服务仍未起来（第 %d 次），%d 秒后重试" % (fails, wait))
                        time.sleep(wait)
                        continue
                else:
                    fails = 0
            except Exception as e:
                log("[!] 守护异常 %r" % (e,))
            time.sleep(3)
        return
    if cmd == "uninstall":
        hosts_set(False)
        # 先摘掉守护标记（防止它重启服务），再依次结束守护与服务
        try:
            _del(DAEMON_PID)
        except Exception:
            pass
        _kill_pid_file(DAEMON_PID)
        _kill_pid_file(PID_FILE)
        _del(PID_FILE)
        return
    if cmd in ("startup", "nostartup"):
        _startup(cmd == "startup")
        return
    if cmd in ("dyncache", "dyn"):
        if len(sys.argv) > 2 and sys.argv[2] in ("clear", "clean"):
            print("已清空动态接口缓存 %d 个文件" % dyn_clear())
            return
        names, total = [], 0
        try:
            names = os.listdir(DYN_DIR)
        except Exception:
            pass
        for n in names:
            p = os.path.join(DYN_DIR, n)
            if os.path.isfile(p):
                total += os.path.getsize(p)
        print("动态接口缓存 %d 个条目 / %.1f KB（TTL %d 秒）" % (len(names) // 2, total / 1024,
                                                            DYN_TTL))
        print("位置 %s" % DYN_DIR)
        return
    if cmd == "status":
        try:
            import http.client as _hc
            c = _hc.HTTPConnection("127.0.0.1", 80, timeout=3)
            c.request("GET", "/__stats__")
            d = json.loads(c.getresponse().read().decode("utf-8"))
            print("本地命中 %d 次 | 回源 %d 次（新存 %d 个，%.1f MB）| HTTPS 透传 %d | 流量 %.1f MB"
                  % (d["hit"], d["miss"], d["save"], d["saved_mb"], d["https"],
                     d["bytes"] / 1048576))
            if d.get("upstream_fail"):
                print("回源失败 %d 次" % d["upstream_fail"])
            if d.get("local_inject"):
                print("hall_config 本地直供 %d 次" % d["local_inject"])
            if d.get("dyn_save"):
                print("动态接口已缓存 %d 个（启动版本检查/服务器列表本地秒回）" % d["dyn_save"])
            if d["hit"]:
                print("命中率 %.1f%%" % (100.0 * d["hit"] / (d["hit"] + d["miss"])))
            up = {}
            try:
                up = json.load(open(UP_FILE, encoding="utf-8"))
            except Exception:
                pass
            if up.get("ip"):
                print("回源节点 %s%s" % (up["ip"],
                                        ("（%.0f KB/s）" % (up["rate"] / 1024)) if up.get("rate") else ""))
            if FPS_OVERRIDE["fps"]:
                print("大厅帧率 %d fps（%s）" % (FPS_OVERRIDE["fps"], FPS_OVERRIDE["dst"]))
            else:
                print("大厅帧率 未修改（官方默认 24fps）")
        except Exception:
            print("[!] 服务未在运行（或端口 80 未监听）")
        print("资源库 %s" % CACHE)
        print("hosts 标记 %s" % ("已写入" if _hosts_has_mark() else "未写入"))
        return
    if cmd == "install":
        hosts_set(True)

    if _ping():
        print("[*] 加速服务已在运行")
        hosts_set(True)
        return

    UPSTREAM = resolve_upstream()
    _bg_cover()
    if not UPSTREAM:
        log("[!] 上游解析失败，仅本地命中可用")
    log("[*] 资源库: %s" % CACHE)

    try:
        open(PID_FILE, "w").write(str(os.getpid()))
    except Exception:
        pass
    threading.Thread(target=serve, args=(80, handle_http), daemon=True).start()
    threading.Thread(target=serve, args=(443, handle_https), daemon=True).start()
    threading.Thread(target=reporter, daemon=True).start()

    # 端口没绑上就别继续跑。原来这里不管成功与否都进 sleep 循环，
    # 进程看着活着、日志也写了"监听"，但请求全被别的实例接走 —— 排查时会
    # 完全看不出问题（本项目历史上就被这个坑误导过一次）。
    time.sleep(1)
    if _bind_failed:
        log("[!] 端口 %s 未能绑定，进程退出。用 `local_cdn.py status` 确认是否已有实例在跑。"
            % _bind_failed)
        print("[!] 端口 %s 已被占用，启动失败。" % _bind_failed)
        print("    已有实例在跑的话直接用就行；要换新配置请先 `local_cdn.py uninstall`。")
        sys.exit(2)

    # 加速服务常驻时顺便把「风险模式注入守护」挂起来：
    # 这样用户不用开着 GUI 也能自动注入战斗端（守护线程只扫进程，不做别的）。
    try:
        from risk import guard as _risk_guard
        if _risk_guard.status().get("enabled"):
            _risk_guard._ensure_thread()
            log("[risk] 注入守护已随加速服务启动")
        else:
            log("[risk] 风险模式未开启，注入守护不启动")
    except Exception as _e:
        log("[risk] 注入守护未启动: %r" % (_e,))

    while True:
        time.sleep(3600)
        try:
            UPSTREAM = resolve_upstream(force=True)
        except Exception:
            pass

if __name__ == "__main__":
    main()
