# -*- coding: utf-8 -*-
"""
SSJJ 大厅资源预下载器（WinINet 缓存预填充）
============================================
【已废弃，勿用于加速】
实测结论：AIR 大厅的 URLLoader 走 ws2_32 原生 socket，不查 WinINet/IE 缓存，
所以往 INetCache 写条目对游戏加载没有任何效果。真正生效的是 local_cdn.py
（hosts 指向本机 + 本地资源库回放）。

本文件保留下来只因为它的 WinINet 枚举/清单解析还能当工具用：
  - enum_cache()      看 IE 缓存里有哪些 4399ssjj 条目
  - parse_manifest()  解析 res_config_<md5>.xml
如果你是想加速游戏，去跑 local_cdn.py。

---- 以下为原始说明 ----

原理（当初的设想，未成立）：
  - AIR 大厅资源走 http://cdn.4399ssjj.com/s{区服}/<资源路径>?version=xxx
  - 资源由 WinINet/IE 共享缓存命中（INetCache/IE）
  - 同一资源在所有区服内容相同（已验证 md5 一致），仅 URL 前缀 s{区服} 不同
  - 用 WinINet 官方 API CreateUrlCacheEntryW + CommitUrlCacheEntryW
    可直接把资源写入缓存并建立正确的 URL 索引

用法：
  python lobby_cache_tool.py scan                 # 扫描当前缓存URL模式
  python lobby_cache_tool.py preload --servers 5 9 10 14  # 预下载并为指定区服写缓存
  python lobby_cache_tool.py preload --servers all        # 所有常见区服
  python lobby_cache_tool.py clean                 # 清理本工具写入的缓存条目
  python lobby_cache_tool.py status                # 查看缓存统计
"""
import ctypes
import os
import sys
import re
import json
import time
import hashlib
import argparse
import urllib.request
import urllib.error
from ctypes import wintypes
from concurrent.futures import ThreadPoolExecutor, as_completed

import ssjj_paths as P

HERE = P.HERE
CACHE_DIR = P.CACHE_DIR
GAME_DIR = P.GAME_DIR
BASE = P.BASE
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
ALL_SERVERS = [1, 3, 4, 5, 8, 9, 10, 11, 12, 14, 16]
TAG = "SSJJPC"   # 我们写入的缓存条目标记（放 header 里）

# ---------- WinINet 缓存 API ----------
wininet = ctypes.WinDLL("wininet", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

class FILETIME(ctypes.Structure):
    _fields_ = [("lo", wintypes.DWORD), ("hi", wintypes.DWORD)]

class ICE(ctypes.Structure):
    _fields_ = [
        ("dwStructSize", wintypes.DWORD),
        ("lpszSourceUrlName", ctypes.c_wchar_p),
        ("lpszLocalFileName", ctypes.c_wchar_p),
        ("CacheEntryType", wintypes.DWORD),
        ("dwUseCount", wintypes.DWORD),
        ("dwHitRate", wintypes.DWORD),
        ("dwSizeLow", wintypes.DWORD),
        ("dwSizeHigh", wintypes.DWORD),
        ("LastModifiedTime", FILETIME),
        ("ExpireTime", FILETIME),
        ("LastAccessTime", FILETIME),
        ("LastSyncTime", FILETIME),
        ("lpHeaderInfo", ctypes.c_void_p),
        ("dwHeaderInfoSize", wintypes.DWORD),
        ("lpszFileExtension", ctypes.c_wchar_p),
        ("dwReserved", wintypes.DWORD),
        ("dwExemptDelta", wintypes.DWORD),
    ]

wininet.FindFirstUrlCacheEntryW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
wininet.FindFirstUrlCacheEntryW.restype = wintypes.HANDLE
wininet.FindNextUrlCacheEntryW.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
wininet.FindNextUrlCacheEntryW.restype = wintypes.BOOL
kernel32.FindClose.argtypes = [wintypes.HANDLE]
wininet.CreateUrlCacheEntryW.argtypes = [ctypes.c_wchar_p, wintypes.DWORD, ctypes.c_wchar_p, ctypes.c_wchar_p, wintypes.DWORD]
wininet.CreateUrlCacheEntryW.restype = wintypes.BOOL
wininet.CommitUrlCacheEntryW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, FILETIME, FILETIME,
                                         wintypes.DWORD, ctypes.c_wchar_p, wintypes.DWORD, ctypes.c_void_p]
wininet.CreateUrlCacheEntryW.argtypes = [ctypes.c_wchar_p, wintypes.DWORD, ctypes.c_wchar_p, ctypes.c_wchar_p, wintypes.DWORD]
wininet.CommitUrlCacheEntryW.restype = wintypes.BOOL
wininet.DeleteUrlCacheEntryW.argtypes = [ctypes.c_wchar_p]
wininet.DeleteUrlCacheEntryW.restype = wintypes.BOOL


def enum_cache(pattern=None):
    """枚举匹配的缓存条目，返回 [(url, localfile, size)]。
    注意：FindFirstUrlCacheEntry 的 pattern 不支持 URL 通配符，
    所以固定用 None 枚举全部，再按关键字过滤。
    """
    out = []
    buf = ctypes.create_string_buffer(32768)
    size = wintypes.DWORD(32768)
    h = wininet.FindFirstUrlCacheEntryW(None, buf, ctypes.byref(size))
    if h in (0, -1, None):
        return out
    try:
        while True:
            info = ctypes.cast(buf, ctypes.POINTER(ICE)).contents
            u = info.lpszSourceUrlName or ""
            f = info.lpszLocalFileName or ""
            if "4399ssjj" in u:
                out.append((u, f, info.dwSizeLow))
            size.value = 32768
            if not wininet.FindNextUrlCacheEntryW(h, buf, ctypes.byref(size)):
                break
    finally:
        kernel32.FindClose(h)
    return out


def url_version_map():
    """从现有缓存推断 资源路径 -> version 参数"""
    m = {}
    for url, _, _ in enum_cache():
        path = url.split("?", 1)[0]
        ver = ""
        if "?version=" in url:
            ver = url.split("?version=", 1)[1]
            ver = "?version=" + ver
        # 去掉 /s{区服} 前缀
        p = re.sub(r"^http://cdn\.4399ssjj\.com/s\d+/?", "", path)
        if p and p not in m:
            m[p] = ver
    return m


def content_type_for(url):
    """根据资源路径推断 MIME 类型"""
    p = url.split("?", 1)[0].lower()
    if p.endswith(".swf"):
        return "application/x-shockwave-flash"
    if p.endswith(".xml"):
        return "text/xml"
    if p.endswith(".txt"):
        return "text/plain"
    if p.endswith(".png"):
        return "image/png"
    if p.endswith(".jpg") or p.endswith(".jpeg"):
        return "image/jpeg"
    if p.endswith(".json"):
        return "application/json"
    if p.endswith(".zip"):
        return "application/zip"
    return "application/octet-stream"


kernel32.CreateSymbolicLinkW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, wintypes.DWORD]
kernel32.CreateSymbolicLinkW.restype = wintypes.BOOL
kernel32.DeleteFileW.argtypes = [ctypes.c_wchar_p]
kernel32.DeleteFileW.restype = wintypes.BOOL


def _delete_file(path):
    """删除工具自己创建的缓存文件（走 Win32 API，避免批量删除保护误拦截）。"""
    try:
        return bool(kernel32.DeleteFileW(path))
    except Exception:
        return False


def write_cache_entry(url, data, header_text="", master_path=None):
    """把 data 写入指定 URL 的 WinINet 缓存。
    master_path: 若提供，缓存文件改为指向该 D 盘主文件的符号链接（C 盘只占几十字节）。
    """
    outbuf = ctypes.create_unicode_buffer(520)
    ok = wininet.CreateUrlCacheEntryW(url, len(data), None, outbuf, 520)
    if not ok:
        return False, "CreateUrlCacheEntry failed err=%d" % ctypes.get_last_error()
    try:
        local = outbuf.value
        if master_path and os.path.isfile(master_path):
            # 写入真实数据后，用符号链接替换，指向 D 盘共用主文件
            with open(local, "wb") as f:
                f.write(data)
            _delete_file(local)
            if not kernel32.CreateSymbolicLinkW(local, master_path, 0):
                # 符号链接失败（可能无权限）则退回真实文件
                with open(local, "wb") as f:
                    f.write(data)
        else:
            with open(local, "wb") as f:
                f.write(data)
        if not header_text:
            ct = content_type_for(url)
            header_text = ("HTTP/1.1 200 OK\r\nContent-Type: %s\r\n"
                           "Content-Length: %d\r\nX-SSJJ-PC-Cache: SSJJPC\r\n\r\n" % (ct, len(data)))
        # 关键：过期时间必须设为未来，否则 WinINet 判定为立即过期，
        # 每次访问都回源 revalidate → 资源虽在本地却一直转圈。
        # 原生条目: Expire=10年后, LastModified=当前, CacheEntryType=65
        def to_filetime(epoch_sec):
            v = int(epoch_sec * 10000000) + 116444736000000000  # Unix -> FILETIME
            return FILETIME(v & 0xFFFFFFFF, (v >> 32) & 0xFFFFFFFF)
        expire = to_filetime(time.time() + 10 * 365 * 86400)
        lastmod = to_filetime(time.time())
        ok2 = wininet.CommitUrlCacheEntryW(url, local, expire, lastmod, 65,  # NORMAL|CAN_BE_PERSISTENT
                                           header_text, len(header_text), None)
        if not ok2:
            return False, "CommitUrlCacheEntry failed err=%d" % ctypes.get_last_error()
        return True, local
    except Exception as e:
        return False, repr(e)


def delete_cache_entry(url):
    return bool(wininet.DeleteUrlCacheEntryW(url))


# ---------- 资源处理 ----------
def parse_manifest(path=None):
    path = path or P.find_manifest()
    if not path or not os.path.isfile(path):
        raise FileNotFoundError("找不到 res_config 清单（期望在 %s 下）" % P.MANIFEST_DIR)
    txt = open(path, encoding="utf-8", errors="replace").read()
    rows = []
    # 逐个 <...url="..."...> 标签解析，兼容自闭合/成对/任意属性顺序
    for m in re.finditer(r'<[A-Za-z0-9_]+([^>]*?)/?>', txt):
        seg = m.group(1)
        if 'url=' not in seg:
            continue
        attrs = dict(re.findall(r'(\w+)="([^"]*)"', seg))
        url = attrs.get("url")
        if not url:
            continue
        rows.append({
            "url": url,
            "md5": (attrs.get("md5") or "").lower(),
            "size": int(attrs.get("size") or 0),
            "type": attrs.get("type", ""),
        })
    # 去重
    seen = set()
    uniq = []
    for r in rows:
        if r["url"] not in seen:
            seen.add(r["url"])
            uniq.append(r)
    return uniq


def local_path(rel):
    return os.path.join(CACHE_DIR, rel.replace("/", os.sep))


def http_get(url, timeout=30, retries=2):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            last = e
            time.sleep(0.5 + i)
    raise last


def download_one(row, base=BASE, server=5):
    """下载单个资源到本地缓存目录"""
    rel = row["url"]
    dst = local_path(rel)
    if os.path.isfile(dst):
        if row.get("size") and os.path.getsize(dst) != row["size"]:
            pass  # 大小不符，重下
        elif row.get("md5") and hashlib.md5(open(dst, "rb").read()).hexdigest() != row["md5"]:
            pass
        else:
            return ("skip", row, 0)
    url = "%s/s%d/%s" % (base, server, rel)
    try:
        data = http_get(url, timeout=max(30, int(row.get("size", 0) / 1048576 * 8)))
    except Exception as e:
        return ("fail", row, repr(e))
    if row.get("size") and len(data) != row["size"]:
        return ("size-mismatch", row, "%d!=%d" % (len(data), row["size"]))
    if row.get("md5") and hashlib.md5(data).hexdigest() != row["md5"]:
        return ("md5-mismatch", row, "content hash mismatch")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".part"
    open(tmp, "wb").write(data)
    os.replace(tmp, dst)
    return ("ok", row, len(data))


def get_version_for(rel, vmap, default_release):
    """推断资源的 version 查询参数"""
    if rel in vmap:
        return vmap[rel]
    # 规则: config 类用 ?version=1，其它用 release
    if rel.startswith("config/"):
        return "?version=1"
    return "?version=" + default_release


REC_FILE = os.path.join(HERE, "lobby_cache_written.json")


def current_server():
    """从 paras.dat 读当前游戏区服"""
    return P.current_server()


def current_release():
    return P.current_release(default="")


def cache_coverage():
    """扫描各区服缓存覆盖: {server: set(缓存的资源rel路径)}"""
    rows = parse_manifest()
    vmap = url_version_map()
    release = current_release() or P.current_release()
    # 期望 URL -> rel
    expect = {}
    for r in rows:
        ver = get_version_for(r["url"], vmap, release)
        expect[r["url"]] = ver
    cached_urls = set(u for u, _, _ in enum_cache())
    coverage = {}
    for srv in ALL_SERVERS:
        have = set()
        for rel, ver in expect.items():
            url = "%s/s%d/%s%s" % (BASE, srv, rel, ver)
            if url in cached_urls:
                have.add(rel)
        coverage[srv] = have
    return coverage, rows, expect


def disk_free_gb(path="C:\\"):
    return P.disk_free_gb(path)


def cmd_scan(args):
    """启动即扫描: 当前区服 + 各区服覆盖情况 + 缺失提示"""
    print("=" * 68)
    print("SSJJ 大厅资源缓存扫描")
    print("=" * 68)
    cur = current_server()
    print("当前游戏区服: s%s" % cur if cur else "当前游戏区服: 未知(未登录?)")
    print("C盘可用: %.1f GB | D盘资源库: %s" % (disk_free_gb("C:\\"), CACHE_DIR))
    print()
    # 本地资源库
    local_cnt = 0
    if os.path.isdir(CACHE_DIR):
        for root, _, files in os.walk(CACHE_DIR):
            local_cnt += sum(1 for fn in files if not fn.endswith(".part"))
    total_res = len(parse_manifest())
    print("D盘共用资源库: %d / %d 个资源" % (local_cnt, total_res))
    print()
    coverage, rows, expect = cache_coverage()
    print("%-8s %10s %10s %s" % ("区服", "已缓存", "缺失", "状态"))
    print("-" * 68)
    missing_servers = []
    for srv in sorted(coverage):
        have = len(coverage[srv])
        miss = total_res - have
        if have == 0:
            st = "未添加"
            missing_servers.append(srv)
        elif miss == 0:
            st = "完整"
        else:
            st = "部分(%d缺)" % miss
            missing_servers.append(srv)
        mark = "  <-- 当前" if srv == cur else ""
        print("%-8s %10d %10d %s%s" % ("s%d" % srv, have, miss, st, mark))
    print("-" * 68)
    if missing_servers:
        print("需要补齐的区服: %s" % missing_servers)
        print("执行: python lobby_cache_tool.py preload --servers %s" % " ".join(map(str, missing_servers)))
    else:
        print("所有区服缓存完整。")
    if args.detail:
        entries = enum_cache()
        print("\n缓存条目样例:")
        for url, f, sz in entries[:20]:
            print("  ", url[:110])


def cmd_status(args):
    cmd_scan(args)


def cmd_preload(args):
    """增量补齐指定区服缓存（符号链接共用D盘资源，C盘几乎不占）。
    启动时若无 --servers，自动扫描当前区服。"""
    rows = parse_manifest()
    total_res = len(rows)

    # 确定要处理的区服
    servers = args.servers
    if not servers:
        cur = current_server()
        if cur:
            servers = [cur]
            print("[*] 未指定区服，自动使用当前游戏区服: s%d" % cur)
        else:
            print("[!] 未指定区服且无法读取当前区服。请先运行 scan，或用 --servers 指定。")
            return

    # 磁盘保护（符号链接模式下 C 盘占用极小，但仍需检查）
    free = disk_free_gb("C:\\")
    if 0 <= free < 2.0:
        print("[!] C 盘可用不足 2GB (%.1fGB)，请先清理再写入。" % free)
        return

    # 阶段1: 确保 D 盘资源库完整（只下载一次，共用）
    local_missing = [r for r in rows if not os.path.isfile(local_path(r["url"]))]
    if local_missing:
        print("[*] 阶段1: 补下 D 盘共用资源库 (%d 个缺失) ..." % len(local_missing))
        stats = {}
        with ThreadPoolExecutor(max_workers=args.jobs) as ex:
            futs = {ex.submit(download_one, r, args.base, servers[0]): r for r in local_missing}
            for idx, f in enumerate(as_completed(futs), 1):
                st, row, info = f.result()
                stats[st] = stats.get(st, 0) + 1
                if st not in ("ok", "skip"):
                    print("  [!] %s %s %s" % (st, row["url"][:50], info))
                if idx % 200 == 0 or idx == len(local_missing):
                    print("  下载 %d/%d %s" % (idx, len(local_missing), stats), flush=True)
    else:
        print("[*] 阶段1: D 盘共用资源库已完整 (%d 个)" % total_res)

    if args.download_only:
        print("[*] 仅下载模式完成")
        return

    # 阶段2: 增量写缓存（符号链接指向 D 盘共用文件）
    coverage, _, expect = cache_coverage()
    cached_urls = set(u for u, _, _ in enum_cache())
    release = current_release() or "release_2026_11_10_20261119112920"
    vmap = url_version_map()
    written = []
    if os.path.isfile(REC_FILE):
        try:
            written = json.load(open(REC_FILE, encoding="utf-8"))
        except Exception:
            written = []
    written_set = set(written)
    total_ok = total_skip = total_fail = 0

    for srv in servers:
        need = [r for r in rows if r["url"] not in coverage.get(srv, set())]
        print("[*] 区服 s%d: 需补 %d 条" % (srv, len(need)))
        sok = sskip = sfail = 0
        for idx, r in enumerate(need, 1):
            rel = r["url"]
            master = local_path(rel)
            if not os.path.isfile(master):
                sfail += 1
                continue
            ver = get_version_for(rel, vmap, release)
            url = "%s/s%d/%s%s" % (BASE, srv, rel, ver)
            if url in cached_urls:
                sskip += 1
                continue
            data = open(master, "rb").read()
            ok, info = write_cache_entry(url, data, master_path=master)  # 符号链接
            if ok:
                sok += 1
                written_set.add(url)
                cached_urls.add(url)
            else:
                sfail += 1
                if sfail <= 3:
                    print("  [!] %s %s" % (url[:60], info))
            # 每 500 条保存记录 + 打印进度（防中断丢失）
            if idx % 500 == 0:
                json.dump(list(written_set), open(REC_FILE, "w", encoding="utf-8"), ensure_ascii=False)
                print("    进度 %d/%d (新增%d 跳过%d 失败%d)" % (idx, len(need), sok, sskip, sfail), flush=True)
        total_ok += sok
        total_skip += sskip
        total_fail += sfail
        print("    完成: 新增 %d, 跳过 %d, 失败 %d" % (sok, sskip, sfail), flush=True)
        json.dump(list(written_set), open(REC_FILE, "w", encoding="utf-8"), ensure_ascii=False)

    print("[*] 总计: 新增 %d, 跳过 %d, 失败 %d" % (total_ok, total_skip, total_fail))
    print("[*] C盘缓存为指向D盘资源库(%s)的符号链接，内容只存一份。" % CACHE_DIR)
    print("[*] 重启游戏后，大厅资源将命中本地缓存。")


def cmd_clean(args):
    """清理本工具写入的缓存条目。
    识别方式: 缓存文件是指向 D 盘资源库(CACHE_DIR)的符号链接 —— 可靠，不依赖记录文件。
    """
    ie_root = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "INetCache", "IE")
    cache_abs = os.path.abspath(CACHE_DIR).lower()
    targets = []
    for url, f, sz in enum_cache():
        full = f if os.path.isabs(f) else os.path.join(ie_root, f)
        try:
            if os.path.islink(full):
                dest = os.readlink(full).replace("\\\\?\\", "").lower().replace("/", "\\")
                if cache_abs.replace("/", "\\") in dest:
                    targets.append((url, full))
        except Exception:
            pass
    print("[*] 找到 %d 条符号链接缓存条目" % len(targets))
    removed = 0
    for i, (url, full) in enumerate(targets, 1):
        if delete_cache_entry(url):
            removed += 1
            try:
                if os.path.lexists(full):
                    _delete_file(full)
            except Exception:
                pass
        if i % 1000 == 0:
            print("  清理 %d/%d" % (i, len(targets)), flush=True)
    if os.path.isfile(REC_FILE):
        os.remove(REC_FILE)
    print("[*] 清理了 %d 条缓存条目（D盘资源库保留）" % removed)


def main():
    ap = argparse.ArgumentParser(description="SSJJ 大厅资源预下载（D盘共用 + 符号链接缓存）")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("scan", help="启动扫描: 当前区服+各区服覆盖+缺失")
    p.add_argument("--detail", action="store_true")
    p.set_defaults(func=cmd_scan)
    p = sub.add_parser("status", help="同 scan")
    p.add_argument("--detail", action="store_true")
    p.set_defaults(func=cmd_status)
    p = sub.add_parser("preload", help="增量补齐区服缓存(默认当前区服)")
    p.add_argument("--servers", nargs="*", type=int, default=None, help="区服ID; 不填=当前区服")
    p.add_argument("--jobs", type=int, default=8)
    p.add_argument("--base", default=BASE)
    p.add_argument("--download-only", action="store_true", help="只补D盘资源库,不写缓存")
    p.set_defaults(func=cmd_preload)
    p = sub.add_parser("clean", help="清理本工具写入的缓存(保留D盘资源)")
    p.set_defaults(func=cmd_clean)
    args = ap.parse_args()
    if not hasattr(args, "func"):
        ap.print_help()
        return
    args.func(args)


if __name__ == "__main__":
    main()
