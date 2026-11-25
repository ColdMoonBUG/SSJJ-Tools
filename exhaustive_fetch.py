# -*- coding: utf-8 -*-
"""
SSJJ 全资源穷举下载器 v2
========================
目标：把大厅所有"动态拼名"的零碎资源（武器图标/道具图标/皮肤/段位…）
全部镜像到 D 盘一份，之后再统一映射给各区服，做到点什么都本地命中。

v2 相对 v1 的改进：
  1. 单次 GET（200 存盘 / 404 记缺失），请求量减半，更不容易触发 429
  2. 断点续传：已探测过的 ID 不再重复请求（state 文件记录）
  3. 429/5xx 自动退避 + 动态降并发
  4. 按分类分批跑，随时可中断

用法：
  python exhaustive_fetch.py probe                     # 采样探测各类命中率
  python exhaustive_fetch.py fetch                     # 穷举全部（可随时 Ctrl+C）
  python exhaustive_fetch.py fetch --category weapon   # 只跑武器图标
  python exhaustive_fetch.py stats                     # 看本地库现状
"""
import os
import re
import json
import time
import random
import argparse
import threading
import socket
import http.client
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

import ssjj_paths as P

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
BASE = P.BASE
CACHE = P.CACHE_DIR
STATE = os.path.join(P.HERE, "exhaustive_progress.json")
# 404 不是永久事实：新武器/新皮肤上线后同名资源会变成 200。
# 记下探测时间，超过 NEG_TTL 秒后重探。
NEG_TTL = 7 * 86400
NEG_TTL = int(os.environ.get("SSJJ_NEG_TTL", NEG_TTL))

# ---------------------------------------------------------------- 候选定义
# (分类key, 目录, 文件名模板, ID起, ID止, 说明)
SPECS = [
    ("weapon",  "resource/swf/WeaponAssets",             "weapon_{n}.swf",    1, 17000, "武器图标"),
    ("prop",    "resource/swf/WeaponPropertyIconAssets", "prop_{n}.swf",      1, 3000,  "武器属性图标"),
    ("stuff",   "resource/swf/StuffAssets",              "item_{n}_1.swf",    1, 2000,  "道具图标"),
    ("factory", "resource/swf/WeaponFactory",            "factory_{n}_1.swf", 1, 200,   "武器工厂"),
    ("skin",    "resource/swf/SkinAssets",               "skin_{n}_1_2.swf",  1, 3000,  "皮肤"),
    ("signin",  "resource/swf/SignInAssets",             "icon_v{n}.swf",     1, 50,    "签到图标"),
    ("money",   "resource/swf/RpgAssets/Money",          "money_{n}.swf",     1, 200,   "RPG货币"),
    ("medal",   "resource/swf/MedalAssets/mainIcon",     "medal_{n}.swf",     1, 500,   "勋章图标"),
]


def upstream_ip():
    """真实 CDN IP（宿主 hosts 已把域名指向 127.0.0.1，必须绕开）

    优先用 paras.dat 的 cdnIp —— 客户端自己挑的节点通常比 DoH 给的快得多。
    """
    ip = P.cdn_ip()
    if ip:
        return ip
    try:
        d = json.load(open(P.UPSTREAM_FILE, encoding="utf-8"))
        if d.get("ip"):
            return d["ip"]
    except Exception:
        pass
    req = urllib.request.Request("https://1.1.1.1/dns-query?name=cdn.4399ssjj.com&type=A",
                                 headers={"accept": "application/dns-json"})
    d = json.loads(urllib.request.urlopen(req, timeout=8).read())
    ip = [a["data"] for a in d.get("Answer", []) if a.get("type") == 1][0]
    json.dump({"ip": ip, "ts": time.time()}, open(P.UPSTREAM_FILE, "w"))
    return ip


class DirectConn(http.client.HTTPConnection):
    """直连指定 IP，但仍然发 Host: cdn.4399ssjj.com"""
    def __init__(self, host, ip, port=80, timeout=20):
        super().__init__(host, port, timeout=timeout)
        self._ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)


def version_q():
    rel = P.current_release()
    return "?version=" + rel


# ---------------------------------------------------------------- state
_lock = threading.Lock()


def load_state():
    if os.path.isfile(STATE):
        try:
            return json.load(open(STATE, encoding="utf-8"))
        except Exception:
            pass
    return {"done": {}, "hits": []}


def _is_settled(val):
    """done[key] 的含义：
       1        → 命中过，永久跳过
       0        → 曾经 404，只当 NEG_TTL 内有效
       {"s":0,"t":ts} → 新版记录（带探测时间）
    """
    if val == 1:
        return True
    if isinstance(val, dict):
        return val.get("s") == 1 or time.time() - val.get("t", 0) < NEG_TTL
    return False


def save_state(st):
    tmp = STATE + ".tmp"
    json.dump(st, open(tmp, "w", encoding="utf-8"))
    os.replace(tmp, STATE)


# ---------------------------------------------------------------- 网络
class Limiter:
    """动态并发/延迟控制：遇到 429 就整体减速一段时间"""
    def __init__(self, delay=0.0):
        self.delay = delay
        self.backoff_until = 0.0
        self.hits429 = 0
        self.lk = threading.Lock()

    def before(self):
        with self.lk:
            wait = self.backoff_until - time.time()
            d = self.delay
        if wait > 0:
            time.sleep(wait)
        if d > 0:
            time.sleep(d)

    def on_429(self):
        with self.lk:
            self.hits429 += 1
            self.delay = min(self.delay + 0.05, 1.0)
            wait = min(2 ** min(self.hits429, 5), 30) + random.random()
            self.backoff_until = time.time() + wait
        return wait


LIM = Limiter()
_stop = threading.Event()


IP = None


def fetch_one(directory, fname, dst, tries=3):
    """返回 ('ok',字节) / ('404',0) / ('fail',err)"""
    global IP
    if IP is None:
        IP = upstream_ip()
    path = "/s5/%s/%s%s" % (directory, fname, version_q())
    for attempt in range(tries):
        if _stop.is_set():
            return "fail", "stopped"
        LIM.before()
        conn = None
        try:
            conn = DirectConn("cdn.4399ssjj.com", IP, 80, timeout=20)
            conn.request("GET", path, headers={"Host": "cdn.4399ssjj.com",
                                               "User-Agent": UA["User-Agent"],
                                               "Accept-Encoding": "identity"})
            r = conn.getresponse()
            if r.status == 404:
                return "404", 0
            if r.status in (429, 403, 503):
                w = LIM.on_429()
                time.sleep(w)
                continue
            if r.status != 200:
                return "fail", "HTTP %d" % r.status
            data = r.read()
            if not data:
                return "fail", "empty"
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            tmp = dst + ".part"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, dst)
            return "ok", len(data)
        except Exception as e:
            if attempt == tries - 1:
                return "fail", repr(e)
            time.sleep(0.5 * (attempt + 1))
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    return "fail", "retry exhausted"


# ---------------------------------------------------------------- probe
def cmd_probe(args):
    print("%-10s %-20s %-8s %s" % ("分类", "模板", "采样", "命中"))
    for cat, directory, template, lo, hi, desc in SPECS:
        pts = sorted({lo, 1, 2, 5, 10, 30, 50, 100, 200, 500, 1000, 2000, 5000, 10000, hi})
        pts = [p for p in pts if lo <= p <= hi]
        hit = []
        for p in pts:
            st, info = fetch_one(directory, template.format(n=p), os.path.join(CACHE, ".probe.tmp"))
            if st == "ok":
                hit.append(p)
        try:
            os.remove(os.path.join(CACHE, ".probe.tmp"))
        except Exception:
            pass
        print("%-10s %-20s %-8d %s/%d  %s" % (cat, template, len(pts), len(hit), len(pts), hit))


# ---------------------------------------------------------------- fetch
def cmd_fetch(args):
    cats = None
    if args.category:
        cats = {c.strip() for c in args.category.split(",")}
    specs = [s for s in SPECS if cats is None or s[0] in cats]
    if not specs:
        print("[!] 没有匹配的分类，可选: %s" % sorted({s[0] for s in SPECS}))
        return

    st = load_state()
    done = st["done"]
    # 旧记录里 done[key] 是 0/1 标量，统一升级成带时间戳的结构。
    # 老的 404 记录时间未知（多半是几个月前探的），给 t=0 让它们立刻重探一轮；
    # 否则历史上那 2 万多个 404 又要白等一个 TTL，新版本资源还是补不到。
    upgraded = 0
    for k, v in list(done.items()):
        if not isinstance(v, dict):
            done[k] = {"s": v, "t": 0 if v == 0 else time.time()}
            upgraded += 1
    if upgraded:
        print("[*] 升级 %d 条旧记录为带 TTL 结构（404 记录 %d 天后重探）"
              % (upgraded, NEG_TTL // 86400))

    jobs = []
    skipped = 0
    for cat, directory, template, lo, hi, desc in specs:
        for n in range(lo, hi + 1):
            key = "%s|%s|%d" % (cat, template, n)
            if key in done and _is_settled(done[key]):
                skipped += 1
                continue
            jobs.append((key, directory, template.format(n=n)))
    print("[*] 分类 %s  待跑 %d 个候选（已确认的跳过 %d 个）"
          % (sorted({s[0] for s in specs}), len(jobs), skipped))
    if not jobs:
        print("[*] 全部已完成")
        return

    stats = {"ok": 0, "404": 0, "fail": 0}
    got_bytes = 0
    t0 = time.time()
    last_save = time.time()
    n_done = 0

    def work(item):
        key, directory, fname = item
        dst = os.path.join(CACHE, directory.replace("/", os.sep), fname)
        if os.path.isfile(dst) and os.path.getsize(dst) > 0:
            return key, "ok", 0, True
        s, info = fetch_one(directory, fname, dst)
        return key, s, info if s == "ok" else 0, False

    try:
        with ThreadPoolExecutor(max_workers=args.jobs) as ex:
            futs = {ex.submit(work, j): j for j in jobs}
            for fut in as_completed(futs):
                key, s, nbytes, cached = fut.result()
                with _lock:
                    stats[s] = stats.get(s, 0) + 1
                    if s == "ok":
                        got_bytes += nbytes
                        done[key] = {"s": 1, "t": time.time()}
                    elif s == "404":
                        done[key] = {"s": 0, "t": time.time()}
                    n_done += 1
                if n_done % 500 == 0:
                    el = time.time() - t0
                    rate = n_done / max(el, 1)
                    print("  %d/%d  命中 %d  失败 %d  %.0f/s  剩余约 %.0f 分  新增 %.1f MB"
                          % (n_done, len(jobs), stats["ok"], stats["fail"], rate,
                             (len(jobs) - n_done) / max(rate, 0.1) / 60, got_bytes / 1048576), flush=True)
                if time.time() - last_save > 15:
                    with _lock:
                        save_state(st)
                    last_save = time.time()
    except KeyboardInterrupt:
        print("\n[!] 手动中断，保存进度…")
        _stop.set()
    finally:
        save_state(st)

    print("[*] 本轮完成: 新增 %d, 缺失 %d, 失败 %d, %.1f MB, 耗时 %.0f 分"
          % (stats["ok"], stats["404"], stats["fail"], got_bytes / 1048576, (time.time() - t0) / 60))
    print("[*] 累计命中记录 %d 条" % len(done))


# ---------------------------------------------------------------- stats
def cmd_stats(args):
    total = 0
    n = 0
    print("%-28s %8s %10s" % ("目录", "文件数", "大小MB"))
    if os.path.isdir(CACHE):
        for d in sorted(os.listdir(CACHE)):
            p = os.path.join(CACHE, d)
            if not os.path.isdir(p):
                continue
            cnt = sz = 0
            for root, _, fs in os.walk(p):
                for f in fs:
                    if f.endswith(".part"):
                        continue
                    cnt += 1
                    sz += os.path.getsize(os.path.join(root, f))
            total += sz
            n += cnt
            print("%-28s %8d %10.1f" % (d, cnt, sz / 1048576))
    print("-" * 50)
    print("%-28s %8d %10.1f" % ("合计", n, total / 1048576))
    print("C盘可用 %.1f GB" % P.disk_free_gb("C:\\"))


# ---------------------------------------------------------------- cover
def _cache_path(url_path):
    """URL 路径 -> 本地缓存文件路径（与服务端 path_to_file 保持一致）。"""
    p = url_path.lstrip("/")
    if not p.endswith("hall_config.xml"):
        p = re.sub(r"^s\d+/", "", p)          # 静态资源跨区服共用
    return os.path.join(P.CACHE_DIR, p.replace("/", os.sep))


_SKIP_RE = re.compile(r"^(/ecs/|/ecs\b|/online/)|(update\.php|version\.php|lobby\.php|lobby_truly\.php|\.manifest$|AssetBundles|update\.cfg)")


def _requests_from_log(limit_lines=400000):
    """从 local_cdn.log 提取客户端真实请求过的 URL（含 HTTP 与 HTTPS MITM）。"""
    logf = os.path.join(P.HERE, "local_cdn.log")
    if not os.path.isfile(logf):
        return {}
    with open(logf, encoding="utf-8", errors="replace") as f:
        txt = f.read()[-4_000_000:] if limit_lines else f.read()
    out = {}
    for m in re.finditer(r"(?:HIT|MISS)s? +[0-9.]+ms +[0-9]+B (?:GET |POST )?(/[^ \r\n]*)", txt):
        u = m.group(1)
        if u.startswith("//") or _SKIP_RE.search(u):
            continue
        key = u.split("?")[0]
        # 同一路径可能出现在不同目录（历史上 resource/swf/Levelup.swf 是我
        # 自己预下载写错的路径，并非客户端真实请求），优先信带 query 的、
        # 且本地已存在的那一条。
        if key not in out or (os.path.isfile(_cache_path(key))
                              and not os.path.isfile(_cache_path(out[key].split("?")[0]))):
            out[key] = u
    return out


_NEG_FILE = os.path.join(P.HERE, "cover_failed.json")
_NEG_TTL = 7 * 86400


def _load_neg():
    try:
        with open(_NEG_FILE, encoding="utf-8") as f:
            d = json.load(f)
        now = time.time()
        return {k: v for k, v in d.items() if now - v < _NEG_TTL}
    except Exception:
        return {}


def _save_neg(d):
    try:
        with open(_NEG_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
    except Exception:
        pass


def cmd_cover(args):
    """扫日志里客户端真实请求过的资源，把缺失的全部补进本地库。

    这是把命中率推到 100% 的关键：res_config 清单只覆盖启动必需资源，
    角色动画 / 角色模型 / 大厅武器预览这些是客户端按需请求的，清单里
    根本没有，只能靠「实际请求过就补」来补全。
    """
    import urllib.request
    req = _requests_from_log()
    if not req:
        print("[!] 日志里没找到请求记录，先运行一次游戏")
        return
    neg = _load_neg()
    lack, known_bad = [], []
    for k, v in req.items():
        if os.path.isfile(_cache_path(k)):
            continue
        (known_bad if k in neg else lack).append((k, v))
    have = len(req) - len(lack) - len(known_bad)
    print("客户端请求过的唯一资源 %d | 已缓存 %d (%.1f%%) | 缺失 %d | 已知 404 %d"
          % (len(req), have, 100.0 * have / len(req), len(lack), len(known_bad)))
    if not lack:
        print("已经 100% 命中。")
        return
    if args.dry_run:
        for k, v in lack[:40]:
            print("   ", v[:100])
        return

    lock = threading.Lock()
    ok = fail = 0
    done = [0]

    def one(item):
        nonlocal ok, fail
        key, url = item
        full = "http://127.0.0.1/" + url.lstrip("/")
        try:
            r = urllib.request.urlopen(
                urllib.request.Request(full, headers={"Host": P.HOST,
                                                     "User-Agent": "cover"}), timeout=90)
            n = len(r.read())
            with lock:
                if n:
                    ok += 1
                else:
                    fail += 1
                    print("    空响应 %s" % url[:80])
        except Exception as e:
            # 本地代理没跑就直连上游兜底
            try:
                up = "http://%s/%s" % (P.cdn_ip(), url.lstrip("/"))
                r = urllib.request.urlopen(
                    urllib.request.Request(up, headers={"Host": P.HOST,
                                                       "User-Agent": "cover"}), timeout=90)
                d = r.read()
                if d:
                    dst = _cache_path(key)
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    with open(dst, "wb") as f:
                        f.write(d)
                    with lock:
                        ok += 1
                    return
            except Exception:
                pass
            with lock:
                fail += 1
                code = getattr(e, "code", None)
                if code == 404 or getattr(e, "code", None) == 403:
                    neg[key] = time.time()      # 记成已知失败，下次不再白试
                    print("    404 %s" % url[:80])
                else:
                    print("    失败 %s  %r" % (url[:70], e))
        finally:
            with lock:
                done[0] += 1
                if done[0] % 20 == 0:
                    print("    ... %d/%d" % (done[0], len(lack)))

    ts = [threading.Thread(target=one, args=(it,)) for it in lack]
    for i in range(0, len(ts), 8):
        batch = ts[i:i + 8]
        [t.start() for t in batch]
        [t.join() for t in batch]
    _save_neg(neg)
    print("补齐完成：成功 %d | 失败 %d" % (ok, fail))


# ---------------------------------------------------------------- prewarm
def cmd_prewarm(args):
    """预热：大厅启动必需的主 SWF + res_config 清单里所有缺失资源"""
    import re
    tasks = []

    # 1) paras.dat 里点名的主程序文件
    paras = P.read_paras()
    keys = ["lobbyFileName", "playerModelFileName", "weaponModelFileName",
            "renderThreadFileName", "ClientView", "roleCreatorFileName",
            "globalRenderThreadFileName", "resConfigFileName", "signConfigFileName",
            "globalSignConfigFileName"]
    for k in keys:
        n = paras.get(k)
        if not n:
            continue
        for pre in ("", "config/", "resource/flas/"):
            tasks.append(pre + n)

    # 2) res_config 清单（大厅全部 5000+ 资源）
    mf = P.find_manifest()
    if mf:
        txt = open(mf, encoding="utf-8", errors="replace").read()
        tasks.extend(re.findall(r'url="([^"]+)"', txt))
    else:
        print("[!] 找不到 res_config 清单，只预热 paras.dat 点名的文件")

    # 3) hall_config.xml —— 客户端每次带时间戳请求，必须本地备一份
    tasks.append("config/hall_config.xml")

    # 去重 + 只保留本地缺失的
    seen = set()
    todo = []
    for rel in tasks:
        rel = rel.split("?")[0].lstrip("/")
        if rel in seen:
            continue
        seen.add(rel)
        dst = os.path.join(CACHE, rel.replace("/", os.sep))
        if not (os.path.isfile(dst) and os.path.getsize(dst) > 0):
            todo.append(rel)
    print("[*] 清单 %d 项，本地缺失 %d 项" % (len(seen), len(todo)))

    ok = fail = 0
    total = 0
    with ThreadPoolExecutor(max_workers=args.jobs) as ex:
        futs = {}
        for rel in todo:
            d, _, f = rel.rpartition("/")
            dst = os.path.join(CACHE, rel.replace("/", os.sep))
            futs[ex.submit(fetch_one, d, f, dst)] = rel
        for i, fut in enumerate(as_completed(futs), 1):
            st, info = fut.result()
            if st == "ok":
                ok += 1
                total += info
            elif st == "fail":
                fail += 1
                if fail <= 5:
                    print("  [!]", futs[fut], info)
            if i % 300 == 0:
                print("  %d/%d 新增 %d %.1fMB" % (i, len(todo), ok, total / 1048576), flush=True)
    print("[*] 预热完成: 新增 %d, 失败 %d, %.1f MB" % (ok, fail, total / 1048576))


def main():
    ap = argparse.ArgumentParser(description="SSJJ 全资源穷举下载 v2")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("probe"); p.set_defaults(func=cmd_probe)
    p = sub.add_parser("fetch")
    p.add_argument("--jobs", type=int, default=16)
    p.add_argument("--category", default=None)
    p.set_defaults(func=cmd_fetch)
    p = sub.add_parser("prewarm")
    p.add_argument("--jobs", type=int, default=12)
    p.set_defaults(func=cmd_prewarm)
    p = sub.add_parser("stats"); p.set_defaults(func=cmd_stats)
    p = sub.add_parser("cover", help="补齐日志里客户端真实请求过、但本地缺失的资源")
    p.add_argument("--dry-run", action="store_true", dest="dry_run", help="只列出缺失，不下载")
    p.set_defaults(func=cmd_cover)
    a = ap.parse_args()
    if not hasattr(a, "func"):
        ap.print_help(); return
    a.func(a)


if __name__ == "__main__":
    main()
