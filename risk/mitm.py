# -*- coding: utf-8 -*-
r"""风险模式：443 MITM —— 把 https://cdn.4399ssjj.com/global_air 也本地化。

为什么需要它
------------
普通模式只劫持了 http（paras.dat 的 resurl）。活动、大厅广告、部分全局资源
走的是 paras.dat 里的 globalResurl = https://cdn.4399ssjj.com/global_air，
走 443 + TLS，中间人看不见路径，只能盲透传。于是这部分资源永远要回源，
点活动、开新页签就是几秒白屏。

做法
----
1. 生成一张本地根 CA（`risk_ca\ssjj-risk-ca.crt.pem` + `.key.pem`）
2. 用它签一张 cdn.4399ssjj.com 的叶子证书（SAN 覆盖域名）
3. 把 CA 装进当前用户的 Root 存储（`certutil -addstore`，需管理员）
4. 443 端口按 SNI 下发叶子证书，请求在本地解密后可读路径 → 走和 80 端口
   同一套缓存（`local_cdn.path_to_file`）→ 命中就本地直接返回，未命中才
   用系统信任链回源真实 CDN 并落盘

回滚
----
`uninstall_ca()` 从 Root 存储删掉根证书，`cleanup()` 连密钥一起删。关掉风险
模式后 443 自动退回原来的纯透传（本地 C2 不会残留中间人）。
"""
import ctypes
import datetime
import ipaddress
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
from .paths import DATA_ROOT
ROOT = DATA_ROOT                         # 证书落在 exe 旁边，不进临时目录
CA_DIR = os.path.join(ROOT, "risk_ca")
CA_KEY = os.path.join(CA_DIR, "ssjj-risk-ca.key.pem")
CA_CRT = os.path.join(CA_DIR, "ssjj-risk-ca.crt.pem")
CA_CER = os.path.join(CA_DIR, "ssjj-risk-ca.cer")
LEAF_KEY = os.path.join(CA_DIR, "cdn.4399ssjj.com.key.pem")
LEAF_CRT = os.path.join(CA_DIR, "cdn.4399ssjj.com.crt.pem")
CA_CN = "SSJJ Toolbox Risk CA"

DEFAULT_HOSTS = ["cdn.4399ssjj.com"]
# 回源超时（秒）。存在的意义不是「省时间」，而是给调用方的 UI 线程兜上限：
# 上游卡住时我们宁可回 502，也不能让等待方一直挂着。
UP_TIMEOUT = float(os.environ.get("SSJJ_UP_TIMEOUT", "8") or 8)
# 客户端握完手后等它发第一个请求的最长时间；超时说明是空连接（预连接/探测）。
IDLE_FIRST = float(os.environ.get("SSJJ_HTTPS_IDLE", "15") or 15)
_STATE = {"ctx": None, "hosts": (), "built": 0.0}
_lock = threading.Lock()

# --------------------------------------------------------------- https 路径台账
# https 侧的资源名是运行时拼出来的（活动页、广告位、页签），离线无从枚举。
# 所以边服务边记账：客户端跑一次，这里就攒出一份完整清单；以后换机器/清缓存
# 直接 prewarm 回填，不用再等它现场回源。
SEEN_PATH = os.path.join(DATA_ROOT, "risk_https_seen.json")
_seen_lock = threading.Lock()
_seen = {"data": None, "dirty": False, "flushed": 0.0, "pending": 0}


def _seen_load():
    with _seen_lock:
        if _seen["data"] is None:
            try:
                with open(SEEN_PATH, "r", encoding="utf-8") as f:
                    _seen["data"] = json.load(f)
            except Exception:
                _seen["data"] = {}
            if not isinstance(_seen["data"], dict):
                _seen["data"] = {}
        return _seen["data"]


def _seen_flush(force=False):
    with _seen_lock:
        if not _seen["dirty"]:
            return
        if not force and _seen["pending"] < 20 and time.time() - _seen["flushed"] < 3.0:
            return
        data = _seen["data"] or {}
        try:
            os.makedirs(os.path.dirname(SEEN_PATH), exist_ok=True)
            tmp = SEEN_PATH + ".part"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
            os.replace(tmp, SEEN_PATH)
            _seen["dirty"] = False
            _seen["pending"] = 0
            _seen["flushed"] = time.time()
        except Exception:
            pass


def record(path, host=None, code=0, size=0, local=False):
    """记一条 https 路径。local=True 表示这次是本地命中。"""
    if not path:
        return
    key = path.split("?")[0] if path.endswith((".swf", ".xml", ".json", ".dat")) else path
    with _seen_lock:
        data = _seen["data"]
        if data is None:
            _seen["data"] = data = {}
        e = data.get(key)
        if e is None:
            e = data[key] = {"n": 0, "code": 0, "size": 0, "local": 0, "ts": 0.0}
        e["n"] = int(e.get("n") or 0) + 1
        if code:
            e["code"] = int(code)
        if size:
            e["size"] = int(size)
        if local:
            e["local"] = int(e.get("local") or 0) + 1
        e["ts"] = round(time.time(), 1)
        _seen["dirty"] = True
        _seen["pending"] = int(_seen.get("pending") or 0) + 1
    _seen_flush()


def seen():
    """返回 {path: {...}} 的副本。"""
    with _seen_lock:
        data = _seen["data"] or {}
        return {k: dict(v) for k, v in data.items()}


def prewarm(limit=600, host=None):
    """把台账里还没落到本地的路径全部回源填进缓存。"""
    import local_cdn as L
    host = host or DEFAULT_HOSTS[0]
    stat = {"ok": 0, "fail": 0, "skip": 0, "total": 0}
    items = sorted(seen().items())
    stat["total"] = len(items)
    for path, info in items:
        if limit and (stat["ok"] + stat["fail"]) >= limit:
            break
        dst = L.path_to_file(path)
        if dst and os.path.isfile(dst) and os.path.getsize(dst) > 0:
            stat["skip"] += 1
            continue
        try:
            code, data = _fetch_upstream(path, host)
        except Exception:
            stat["fail"] += 1
            continue
        if code == 200 and dst and 0 < len(data) <= L.MAX_CACHE:
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                tmp = dst + ".part"
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, dst)
                stat["ok"] += 1
                continue
            except Exception:
                pass
        stat["fail"] += 1
    return stat


# ------------------------------------------------------------------ 证书
def _crypto():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    return x509, hashes, serialization, rsa, ExtendedKeyUsageOID, NameOID


def ca_exists():
    return os.path.isfile(CA_KEY) and os.path.isfile(CA_CRT)


def ensure_ca(force=False):
    """生成根 CA（幂等）。返回 (crt_pem_path, 生成日志)"""
    x509, hashes, serialization, rsa, EKU, NameOID = _crypto()
    msgs = []
    os.makedirs(CA_DIR, exist_ok=True)
    if ca_exists() and not force:
        return CA_CRT, ["[*] 根 CA 已存在: %s" % CA_CRT]
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, CA_CN),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "ssjj-toolbox"),
    ])
    now = datetime.datetime.utcnow()
    cert = (x509.CertificateBuilder()
            .subject_name(subject).issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                                         key_encipherment=False, data_encipherment=False,
                                         key_agreement=False, key_cert_sign=True,
                                         crl_sign=True, encipher_only=False,
                                         decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                           critical=False)
            .sign(key, hashes.SHA256()))
    with open(CA_KEY, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM,
                                  serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    with open(CA_CRT, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(CA_CER, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.DER))
    msgs.append("[*] 已生成根 CA: %s" % CA_CRT)
    return CA_CRT, msgs


def ensure_leaf(hosts=None, force=False):
    """签叶子证书（SAN 覆盖 hosts）。返回 (crt, key, 日志)"""
    hosts = list(hosts or DEFAULT_HOSTS)
    x509, hashes, serialization, rsa, EKU, NameOID = _crypto()
    msgs = []
    stamp = os.path.join(CA_DIR, "leaf.hosts")
    want = ",".join(sorted(hosts))
    if os.path.isfile(LEAF_CRT) and os.path.isfile(LEAF_KEY) and not force:
        old = ""
        try:
            old = open(stamp, encoding="utf-8").read().strip()
        except Exception:
            pass
        leaf = x509.load_pem_x509_certificate(open(LEAF_CRT, "rb").read())
        try:
            san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName)
            have = ",".join(sorted(san.value.get_values_for_type(x509.DNSName)))
        except Exception:
            have = ""
        if old == want and have == want:
            return LEAF_CRT, LEAF_KEY, ["[*] 叶子证书已存在: %s" % LEAF_CRT]
    ensure_ca()
    ca_key = serialization.load_pem_private_key(open(CA_KEY, "rb").read(), password=None)
    ca_crt = x509.load_pem_x509_certificate(open(CA_CRT, "rb").read())
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.datetime.utcnow()
    san = [x509.DNSName(h) for h in hosts] + [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hosts[0])]))
            .issuer_name(ca_crt.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=820))
            .add_extension(x509.SubjectAlternativeName(san), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([EKU.SERVER_AUTH]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    with open(LEAF_KEY, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM,
                                  serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    with open(LEAF_CRT, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(stamp, "w", encoding="utf-8") as f:
        f.write(want)
    # 证书链：叶子 + 根，一起喂给客户端
    with open(LEAF_CRT, "ab") as f:
        f.write(ca_crt.public_bytes(serialization.Encoding.PEM))
    msgs.append("[*] 已签发叶子证书: %s（SAN: %s）" % (LEAF_CRT, ", ".join(hosts)))
    return LEAF_CRT, LEAF_KEY, msgs


# ------------------------------------------------------------------ 安装 / 卸载
def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def ca_thumbprint():
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        cert = x509.load_pem_x509_certificate(open(CA_CRT, "rb").read())
        return cert.fingerprint(hashes.SHA1()).hex()
    except Exception:
        return ""


_INST = {"ts": 0.0, "val": False, "tp": ""}


def ca_installed(fresh=False):
    """证书是否已装。结果缓存 30 秒——每个 TLS 连接都去 spawn certutil 会把 443 拖死。"""
    now = time.time()
    if not fresh and now - _INST["ts"] < 30:
        return _INST["val"]
    val = _probe_installed()
    _INST.update(ts=now, val=val, tp=ca_thumbprint())
    return val


def _probe_installed():
    """证书是否已在 Root 存储里。

    直接读注册表，不 spawn certutil：
      * 每 30 秒每条 TLS 连接都去启动一个 certutil，本身就会拖慢 443；
      * 打包成 --noconsole 的 exe 之后 certutil 的输出拿不稳，探测会假阴性，
        结果就是「风险模式开着、但 MITM 一直没接上」，所有 https 资源照样回源
        （这正是「打开就下载资源、还慢」的根因）。
    注册表路径是 certutil -user -addstore Root 与机器级 addstore 的落点，稳定可靠。
    """
    tp = ca_thumbprint().upper()
    if not tp:
        return False
    try:
        import winreg
    except Exception:
        return False
    rel = "Software\\Microsoft\\SystemCertificates\\Root\\Certificates\\" + tp
    rel_ca = "Software\\Microsoft\\SystemCertificates\\CA\\Certificates\\" + tp
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for sub in (rel, rel_ca):
            try:
                k = winreg.OpenKey(hive, sub)
            except OSError:
                continue
            try:
                winreg.QueryInfoKey(k)
                return True
            finally:
                winreg.CloseKey(k)
    # 注册表没读到再退回 certutil（老系统 / 非标准安装路径的兜底）
    try:
        r = subprocess.run(["certutil", "-user", "-store", "Root", tp],
                           capture_output=True, creationflags=0x08000000, timeout=8)
        out = (r.stdout or b"") + (r.stderr or b"")
        if r.returncode == 0 and (b"================" in out or b"NotBefore" in out
                                  or b"\xd4\xda" in out):
            return True
    except Exception:
        pass
    return False


def install_ca():
    """把根 CA 装进 Root 存储。返回 (ok, 日志行)"""
    ensure_ca()
    if ca_installed(fresh=True):
        return True, ["[*] 根 CA 已在 Root 存储中（缩略码 %s）" % ca_thumbprint()[:16]]
    lines = []
    store = "Root"
    # 先试「当前用户」Root：AIR/Flash 与 .NET 都会认，且不需要提权
    for args in (["-user", "-addstore", "-f", store, CA_CER],
                 ["-addstore", "-f", store, CA_CER]):
        r = subprocess.run(["certutil"] + args, capture_output=True, creationflags=0x08000000)
        out = ((r.stdout or b"") + (r.stderr or b"")).decode("mbcs", "replace").strip()
        lines.append("[*] certutil %s → 退出码 %d" % (" ".join(args[:-1]), r.returncode))
        if r.returncode == 0:
            _INST.update(ts=0.0, val=False)
            ca_installed(fresh=True)
            lines.append("[OK] 根 CA 已安装（%s）" % ("当前用户" if "-user" in args else "本机"))
            return True, lines
        if out:
            lines.append("    " + out.splitlines()[-1][:160])
    lines.append("[!] 根证书没装上。用管理员运行工具箱（或右键「以管理员身份运行」）再试一次。")
    return False, lines


def uninstall_ca():
    ok = True
    lines = []
    tp = ca_thumbprint()
    if not tp:
        return False, ["[!] 没有本地 CA 证书文件"]
    for args in (["-user", "-delstore", "Root", tp], ["-delstore", "Root", tp]):
        r = subprocess.run(["certutil"] + args, capture_output=True, creationflags=0x08000000)
        lines.append("[*] certutil %s → %d" % (" ".join(args[:-1]), r.returncode))
    if ca_installed(fresh=True):
        ok = False
        lines.append("[!] 根证书仍在存储里（可能装着两份，用 certmgr.msc 手动删 %s）" % tp[:16])
    else:
        lines.append("[OK] 根 CA 已从存储移除")
    _INST.update(ts=0.0, val=False)
    return ok, lines


def cleanup():
    names = [CA_KEY, CA_CRT, CA_CER, LEAF_KEY, LEAF_CRT,
             os.path.join(CA_DIR, "leaf.hosts")]
    n = 0
    for p in names:
        try:
            if os.path.isfile(p):
                os.remove(p)
                n += 1
        except Exception:
            pass
    _reset()
    return n


# ------------------------------------------------------------------ TLS 服务
def _reset():
    with _lock:
        _STATE["ctx"] = None
        _STATE["hosts"] = ()
        _STATE["built"] = 0.0


def context(hosts=None, force=False):
    """构造（并缓存）带 SNI 证书的 SSLContext"""
    hosts = tuple(hosts or DEFAULT_HOSTS)
    with _lock:
        if _STATE["ctx"] and _STATE["hosts"] == hosts and not force:
            return _STATE["ctx"]
    crt, key, msgs = ensure_leaf(list(hosts), force=force)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(crt, key)
    try:
        ctx.set_alpn_protocols(["http/1.1"])
    except Exception:
        pass
    with _lock:
        _STATE["ctx"] = ctx
        _STATE["hosts"] = hosts
        _STATE["built"] = time.time()
    return ctx


_EN = {"ts": 0.0, "val": False}


def enabled():
    """MITM 是否可用：开关打开 + 根证书已装。

    带 3 秒缓存：这条判断在每条 443 连接上都会走一次，之前每次都读 config 文件、
    探测证书，连接一多就是纯开销。
    """
    now = time.time()
    if now - _EN["ts"] < 3.0:
        return _EN["val"]
    try:
        from . import config
        val = bool(config.get("mitm.enabled")) and ca_installed()
    except Exception:
        val = False
    _EN.update(ts=now, val=val)
    return val


def _ctype(ext):
    return {
        ".swf": "application/x-shockwave-flash", ".xml": "text/xml; charset=utf-8",
        ".json": "application/json; charset=utf-8", ".png": "image/png", ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg", ".gif": "image/gif", ".dat": "application/octet-stream",
        ".zlib": "application/octet-stream", ".mp3": "audio/mpeg", ".atf": "application/octet-stream",
        ".php": "text/html; charset=utf-8", ".html": "text/html; charset=utf-8",
        ".htm": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".txt": "text/plain; charset=utf-8",
    }.get(ext.lower(), "application/octet-stream")


def _upstream_ip():
    """拿到真实 CDN 节点 IP（1043 端口回源用）。

    三个来源按序取，全都失败才报错：
      1. __main__.UPSTREAM —— 服务端以 `python local_cdn.py` 跑时，它是 __main__，
         import local_cdn 拿到的是另一个同样 None 的模块实例，必须看 __main__；
      2. local_cdn.UPSTREAM —— 被 toolbox/backend 以模块方式 import 时的情形；
      3. upstream.json —— 上次探好的落盘 IP，服务刚起、还没测速时靠它兜底。
    """
    import json
    import sys
    import local_cdn as L
    for name in ("__main__", "local_cdn"):
        m = sys.modules.get(name)
        ip = getattr(m, "UPSTREAM", None) if m is not None else None
        if ip:
            return ip
    try:
        with open(L.UP_FILE, "r", encoding="utf-8") as f:
            ip = (json.load(f) or {}).get("ip")
        if ip:
            return ip
    except Exception:
        pass
    try:                       # 最后自己解析一次，成功就顺手写回全局
        ip = L.resolve_upstream()
        if ip:
            m = sys.modules.get("__main__")
            if m is not None and hasattr(m, "UPSTREAM"):
                m.UPSTREAM = ip
            L.UPSTREAM = ip
            return ip
    except Exception:
        pass
    return None


_UP_TLS = threading.local()

_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length",
})


def _upstream_conn(ip, host):
    r"""取一条线程私有的可复用上游连接。

    以前所有工作线程共用同一个全局 HTTPSConnection，并发回源时直接串包/报错。
    改用 threading.local() 为每个线程维护独立长连接，既保留复用加速，又彻底线程安全。
    """
    import http.client
    key = (ip, host)
    cur_key = getattr(_UP_TLS, "key", None)
    cur_conn = getattr(_UP_TLS, "conn", None)
    if cur_key == key and cur_conn is not None:
        return cur_conn
    if cur_conn is not None:
        try:
            cur_conn.close()
        except Exception:
            pass
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    # 8 秒而不是 20 秒：调用方（MicroClient / Flash 大厅）是在自己的 UI 线程里
    # 同步等 HTTP 回复的，上游一慢，我们就等于把它的消息循环一起冻住。
    # 实测过一次「MicroClient 无响应 56.5 秒」，栈就是 UI 线程阻塞在 select()。
    c = http.client.HTTPSConnection(ip, 443, timeout=UP_TIMEOUT, context=ctx)
    _UP_TLS.key = key
    _UP_TLS.conn = c
    return c


def _drop_upstream():
    c = getattr(_UP_TLS, "conn", None)
    _UP_TLS.key = None
    _UP_TLS.conn = None
    try:
        if c:
            c.close()
    except Exception:
        pass


def _is_timeout(e):
    """区分「超时」和「连接坏了」。超时不该重试，重试只会让等的人多等一倍。"""
    if isinstance(e, (TimeoutError, socket.timeout)):
        return True
    return "timed out" in repr(e).lower()


def _fetch_upstream_full(path, host, method="GET", body=None, headers=None):
    """真正回源，返回 (status, reason, resp_headers_list, data)。

    保留上游所有非 hop-by-hop 响应头（Content-Type / Set-Cookie / Location /
    Cache-Control / CORS 等），避免登录跳转和动态接口失效。
    """
    ip = _upstream_ip()
    if not ip:
        raise RuntimeError("上游 IP 还没解析出来")
    hdrs = {"Host": host, "User-Agent": "ssjj-toolbox-risk/1.0",
            "Accept": "*/*", "Connection": "keep-alive"}
    for k, v in (headers or {}).items():
        lk = k.lower()
        if lk in ("host", "connection", "content-length", "accept-encoding",
                  "transfer-encoding"):
            continue
        hdrs[k] = v
    last = None
    t_start = time.time()
    for _attempt in (0, 1):
        conn = _upstream_conn(ip, host)
        try:
            conn.request(method, path, body=body, headers=hdrs)
            r = conn.getresponse()
            data = r.read()
            resp_hdrs = [(k, v) for (k, v) in r.getheaders()
                         if k.lower() not in _HOP_BY_HOP]
            status, reason = r.status, (r.reason or "OK")
            if r.will_close:
                _drop_upstream()
            return status, reason, resp_hdrs, data
        except Exception as e:
            last = e
            _drop_upstream()          # 复用的连接被上游关了，重建后重试一次
            # 超时不再重试：重试等于把调用方的等待翻倍（20s→40s），
            # 只会把「慢」放大成「卡死」。连接被重置/复用失效才值得重来一次。
            if _is_timeout(e) or (time.time() - t_start) > 2 * UP_TIMEOUT:
                raise
    raise last


def _fetch_upstream(path, host, method="GET", body=None, headers=None):
    """兼容旧调用方（prewarm / selftest），返回 (status, data)。"""
    status, _reason, _hdrs, data = _fetch_upstream_full(
        path, host, method=method, body=body, headers=headers)
    return status, data


def _build_passthrough_response(code, reason, resp_headers, body,
                                head_only=False, keep_alive=False, fallback_ctype="application/octet-stream"):
    """按上游原始响应头构造 HTTP/1.1 响应，完整保留 Content-Type / Set-Cookie / Location 等。"""
    lines = ["HTTP/1.1 %d %s" % (int(code), reason or "OK")]
    has_ctype = False
    for k, v in (resp_headers or []):
        lk = k.lower()
        if lk in _HOP_BY_HOP:
            continue
        if lk == "content-type":
            has_ctype = True
        lines.append("%s: %s" % (k, v))
    if not has_ctype:
        lines.append("Content-Type: %s" % fallback_ctype)
    lines.append("Content-Length: %d" % len(body))
    lines.append("Connection: %s" % ("keep-alive" if keep_alive else "close"))
    head = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1", "replace")
    return head + (b"" if head_only else body)


def _log(msg):
    try:
        from . import guard
        guard.log("[mitm] " + msg)
    except Exception:
        pass


def _live_cdn():
    """拿到「真正在跑的那个」local_cdn 模块。

    服务端是以 `python local_cdn.py` 起进程的，那时它的 STATS / log / FPS_OVERRIDE
    都活在 __main__ 里；`import local_cdn` 得到的是另一个全新的模块实例，
    计数器写进去就等于扔进黑洞（/__stats__ 永远读不到）。所以优先认 __main__。
    """
    m = sys.modules.get("__main__")
    f = getattr(m, "__file__", "") or ""
    if m is not None and os.path.basename(f).lower() == "local_cdn.py":
        return m
    import local_cdn
    return local_cdn


_DYN_REFRESHING = set()


def _dyn_refresh_https(L, path, host):
    """后台刷新过期的动态接口（https 侧），不阻塞客户端。"""
    if path in _DYN_REFRESHING:
        return
    _DYN_REFRESHING.add(path)
    try:
        code, _reason, resp_hdrs, data = _fetch_upstream_full(path, host)
        if code == 200 and data and len(data) <= 262144:
            ext = os.path.splitext(path.split("?")[0])[1]
            ct = ""
            for k, v in resp_hdrs:
                if k.lower() == "content-type":
                    ct = v
                    break
            if L.dyn_put(path, code, ct or _ctype(ext), data):
                L.log("DYNRs 后台刷新 %s" % path[:110])
    except Exception:
        pass
    finally:
        _DYN_REFRESHING.discard(path)


def _serve_request(L, tls, head, host, body=b""):
    """处理一个 HTTP 请求。返回 True 表示同一条 TLS 连接可以继续收下一个请求。

    单连接多请求很关键：客户端（UnityWebRequest / Flash）会复用连接连续拉几十个
    活动资源，以前每拉一个就断一次、重来一次完整 TLS 握手，白白多出好几百毫秒。
    POST/PUT 一律原样转发（不带缓存）——把请求体丢掉、用 GET 顶上，
    上游返回的东西就不对，接口会整体失灵。
    """
    lines = head.decode("latin-1", "replace").split("\r\n")
    try:
        method, path, _ = lines[0].split(" ", 2)
    except Exception:
        return False
    hdrs = {}
    for ln in lines[1:]:
        k, _, v = ln.partition(":")
        if k:
            hdrs[k.strip().lower()] = v.strip()
    keep = hdrs.get("connection", "").lower() != "close"
    if method not in ("GET", "HEAD", "POST", "PUT", "DELETE", "OPTIONS"):
        keep = False
    cacheable = method in ("GET", "HEAD")
    if path.lower().startswith("https://"):
        u = urllib.parse.urlsplit(path)
        path = u.path + (("?" + u.query) if u.query else "")
    t0 = time.time()
    with _lock:
        L.STATS["https_mitm"] = L.STATS.get("https_mitm", 0) + 1
    dst = L.path_to_file(path) if cacheable else None
    if dst and os.path.isfile(dst) and os.path.getsize(dst) > 0:
        data0 = open(dst, "rb").read()
        ext = os.path.splitext(dst)[1]
        tls.sendall(L.build_response(200, data0, _ctype(ext), head_only=(method == "HEAD"), keep_alive=keep))
        with L._slk:
            L.STATS["hit"] += 1
            L.STATS["bytes"] += len(data0)
        L.log("HITs  %5.1fms %7dB %s %s" % ((time.time() - t0) * 1000, len(data0),
                                            method, path[:110]))
        record(path, host, 200, len(data0), local=True)
        return keep

    # 动态接口（版本检查 / 服务器列表）走同一套本地短缓存：
    # 这几个接口上游要 0.5~2.4 秒，启动时串行打好几个，是「开游戏干等」的主因。
    if cacheable:
        dyn = L.dyn_get(path)
        stale = False
        if not dyn:
            dyn = L.dyn_get_stale(path)
            if dyn:
                stale = True
                threading.Thread(target=_dyn_refresh_https, args=(L, path, host),
                                 daemon=True).start()
        if dyn:
            dcode, dctype, dbody = dyn
            tls.sendall(L.build_response(dcode, dbody, dctype,
                                         head_only=(method == "HEAD"), keep_alive=keep,
                                         cache_control="no-cache, no-store, max-age=0"))
            with L._slk:
                L.STATS["hit"] += 1
                L.STATS["bytes"] += len(dbody)
            L.log("DYN%ss %5.1fms %7dB %s %s" % ("*" if stale else "",
                                                 (time.time() - t0) * 1000, len(dbody),
                                                 method, path[:110]))
            record(path, host, dcode, len(dbody), local=True)
            return keep

    with L._slk:
        L.STATS["miss"] += 1
    try:
        code, reason, resp_hdrs, data = _fetch_upstream_full(
            path, host, method=method,
            body=(body or None) if not cacheable else None,
            headers=hdrs)
    except Exception as e:
        _log("回源失败 https://%s%s %r" % (host, path[:80], e))
        tls.sendall(L.build_response(502, b"upstream unavailable",
                                     cache_control="no-store"))
        return False
    ext = os.path.splitext(path.split("?")[0])[1]
    upstream_ct = ""
    for k, v in resp_hdrs:
        if k.lower() == "content-type":
            upstream_ct = v
            break
    # 只有 200 才落盘。以前这里不看状态码，结果上游 404 的 146 字节错误页
    # 被当成资源存进缓存，之后永久命中一个假文件（比不回源更糟）。
    saved_local = False
    if dst and code == 200 and 0 < len(data) <= L.MAX_CACHE:
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            tmp = dst + ".part"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, dst)
            saved_local = True
            with L._slk:
                L.STATS["save"] += 1
                L.STATS["saved_mb"] += len(data) / 1048576.0
        except Exception:
            pass
    if dst and not saved_local and code == 200 and not os.path.isfile(dst):
        try:
            L._bg_fetch_enqueue(path, dst)
        except Exception:
            pass
    record(path, host, code, len(data))
    if cacheable and code == 200 and data and len(data) <= 262144:
        try:
            L.dyn_put(path, code, upstream_ct or _ctype(ext), data)
        except Exception:
            pass
    tls.sendall(_build_passthrough_response(
        code, reason, resp_hdrs, data,
        head_only=(method == "HEAD"), keep_alive=keep,
        fallback_ctype=upstream_ct or _ctype(ext)))
    with L._slk:
        L.STATS["bytes"] += len(data)
    L.log("MISSs %5.1fms %7dB %s %s" % ((time.time() - t0) * 1000, len(data),
                                        method, path[:110]))
    return keep


def serve_conn(conn, addr, sni=None):
    """在已 accept 的 socket 上跑 TLS + HTTP/1.1（支持同连接多请求）。"""
    L = _live_cdn()
    host = sni or DEFAULT_HOSTS[0]
    try:
        ctx = context()
    except Exception as e:
        _log("上下文构造失败 %r" % (e,))
        return False
    tls = None
    try:
        conn.settimeout(20)
        tls = ctx.wrap_socket(conn, server_side=True)
    except Exception as e:
        _log("握手失败 %s %r" % (host, e))
        try:
            conn.close()
        except Exception:
            pass
        return True
    try:
        # 首个请求稍宽容，之后的空闲等待短一些，避免空连接长期占线程
        tls.settimeout(IDLE_FIRST)
        n = 0
        while True:
            req = b""
            while b"\r\n\r\n" not in req:
                d = tls.recv(16384)
                if not d:
                    return True
                req += d
                if len(req) > 65536:
                    return True
            head, _, rest = req.partition(b"\r\n\r\n")
            # 读出请求体（POST/PUT 才有），否则转发出去上游会等 body 等到超时
            clen = 0
            for ln in head.decode("latin-1", "replace").split("\r\n")[1:]:
                k, _, v = ln.partition(":")
                if k.strip().lower() == "content-length":
                    try:
                        clen = int(v.strip())
                    except Exception:
                        clen = 0
            body = rest
            while len(body) < clen:
                d = tls.recv(min(65536, clen - len(body)))
                if not d:
                    break
                body += d
            if not _serve_request(L, tls, head, host, body[:clen] if clen else b""):
                return True
            n += 1
            tls.settimeout(10)   # keep-alive 空闲 10 秒就放手
    except (TimeoutError, socket.timeout):
        # 客户端握完手却一直不说话（AIR/CEF 的预连接、被我们拦下的探测连接都会这样）。
        # 这是正常现象，记一行就好，不要当异常刷屏。
        _log("空连接释放 %s:%s（握手后 %ds 无请求）"
             % (addr[0] if isinstance(addr, tuple) else addr,
                addr[1] if isinstance(addr, tuple) else "", IDLE_FIRST))
        return True
    except Exception as e:
        _log("处理异常 %r" % (e,))
        return True
    finally:
        try:
            if tls:
                tls.close()
            else:
                conn.close()
        except Exception:
            pass


# 服务进程被关掉时（用户退工具箱、重启机器前）把没落盘的记账补写完
import atexit as _atexit

_atexit.register(lambda: _seen_flush(force=True))


# ------------------------------------------------------------------ 状态 / CLI
def status():
    hosts = (DEFAULT_HOSTS,)
    try:
        from . import config
        hosts = config.get("mitm.hosts") or DEFAULT_HOSTS
    except Exception:
        pass
    return {
        "enabled": bool(enabled()),
        "admin": is_admin(),
        "ca_exists": ca_exists(),
        "ca_installed": ca_installed(),
        "thumbprint": ca_thumbprint()[:16],
        "hosts": list(hosts),
        "dir": CA_DIR,
    }


def selftest(port=18443):
    """自检：本地起一个 TLS 监听 → 用系统信任链连上去拿一份缓存文件。

    不动 80/443，不影响正在跑的服务，只验证「根证书装对了 + 叶子证书能过验证
    + 请求能在本地解密并命中缓存」这三件事。
    """
    import local_cdn as L
    probe_dir = os.path.join(L.CACHE, "global_air")
    os.makedirs(probe_dir, exist_ok=True)
    probe = os.path.join(probe_dir, "_mitm_selftest.bin")
    with open(probe, "wb") as f:
        f.write(b"RISK-MITM-OK")
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(1)
    err = []

    def _run():
        try:
            c, a = srv.accept()
            serve_conn(c, a, "cdn.4399ssjj.com")
        except Exception as e:
            err.append(repr(e))

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    buf = b""
    status = ""
    try:
        ctx = ssl.create_default_context()        # 走系统根证书存储
        raw = socket.create_connection(("127.0.0.1", port), timeout=10)
        ss = ctx.wrap_socket(raw, server_hostname="cdn.4399ssjj.com")
        ss.sendall(b"GET /global_air/_mitm_selftest.bin HTTP/1.1\r\n"
                   b"Host: cdn.4399ssjj.com\r\nConnection: close\r\n\r\n")
        while True:
            d = ss.recv(65536)
            if not d:
                break
            buf += d
        ss.close()
    except Exception as e:
        err.append(repr(e))
    finally:
        try:
            srv.close()
        except Exception:
            pass
        try:
            os.remove(probe)
        except Exception:
            pass
        t.join(timeout=2)
    for ln in buf.split(b"\r\n")[:1]:
        status = ln.decode("latin-1")
    ok = b"RISK-MITM-OK" in buf
    return {"ok": ok, "status": status, "errors": err,
            "bytes": len(buf),
            "hint": "" if ok else "多半是根证书没装进 Root 存储，或 18443 被占"}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "status"
    if cmd == "status":
        st = status()
        for k in ("enabled", "admin", "ca_exists", "ca_installed", "thumbprint", "hosts", "dir"):
            print("%-12s %s" % (k, st[k]))
        return
    if cmd == "selftest":
        r = selftest()
        print("MITM 自检: %s  响应=%r  %d 字节  %s"
              % ("通过" if r["ok"] else "失败", r["status"], r["bytes"],
                 r["hint"] or "（TLS 解密 + 本地命中 全链路正常）"))
        for e in r["errors"]:
            print("  错误: %s" % e)
        return
    if cmd == "init":
        _, m1 = ensure_ca()
        _, _, m2 = ensure_leaf()
        print("\n".join(m1 + m2))
        return
    if cmd == "install":
        ok, lines = install_ca()
        print("\n".join(lines))
        return
    if cmd == "uninstall":
        ok, lines = uninstall_ca()
        print("\n".join(lines))
        return
    if cmd == "cleanup":
        n = cleanup()
        print("[*] 已删除 %d 个证书文件" % n)
        return
    if cmd == "seen":
        data = seen()
        rows = sorted(data.items(), key=lambda kv: -(kv[1].get("size") or 0))
        print("https 路径台账: %d 条  文件 %s" % (len(rows), SEEN_PATH))
        for path, info in rows[:60]:
            print("  %7dB  x%-4d %s %s" % (info.get("size") or 0, info.get("n") or 0,
                                           "本地" if info.get("local") else "回源",
                                           path[:100]))
        return
    if cmd == "prewarm":
        r = prewarm()
        print("预热完成: 新填 %d / 已存在 %d / 失败 %d / 台账 %d"
              % (r["ok"], r["skip"], r["fail"], r["total"]))
        return
    print("用法: python -m risk.mitm [status|init|install|uninstall|cleanup|selftest|seen|prewarm]")


if __name__ == "__main__":
    main()
