# -*- coding: utf-8 -*-
"""
SSJJ 资源核心库
================
从《生死狙击》(4399/Wooduan) 官方 CDN 预下载资源到客户端本地缓存目录。

已验证的资源链路（来自官方客户端与私服项目实测）：
  - 战斗 AssetBundle 清单: http://cdn.4399ssjj.com/s3/Windows/AssetBundles_<ver>2019.manifest
  - 单个 bundle:            http://cdn.4399ssjj.com/s3/Windows/<bundleName>_<md5>
  - 客户端本地缓存格式:     %LOCALAPPDATA%/Wooduan/Preload/<md5>/data
  - 客户端原生支持 --preload_dir 参数优先读本地缓存（不修改任何游戏文件，不碰反作弊）

本模块只做两件事：下载 + 校验。不修改游戏客户端、不注入内存、不绕过反作弊。
"""
import hashlib
import os
import re
import sys
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# 官方 CDN 基准。若本机 hosts 把域名劫持到私服网关，工具会照常工作（内容一致）；
# 想直连官方请恢复 hosts 里的 cdn.4399ssjj.com 映射。
DEFAULT_BASE = "http://cdn.4399ssjj.com/s3/"
DEFAULT_PLATFORM = "Windows"

# 客户端本地缓存根目录（战斗资源按 <md5>/data 放置即可被 --preload_dir 命中）
def default_preload_dir():
    local = os.environ.get("LOCALAPPDATA", os.path.join(os.path.expanduser("~"), "AppData", "Local"))
    return os.environ.get("SSJJ_PRELOAD_DIR", os.path.join(local, "Wooduan", "Preload"))


def http_get(url, timeout=30, retries=3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as ex:
            if ex.code == 404:
                raise
            last = ex
        except Exception as ex:
            last = ex
        time.sleep(0.8 + i * 0.7)
    raise last


def fetch_version(base, platform):
    raw = http_get("%s%s/version.php" % (base, platform), timeout=20).decode("utf-8", "replace").strip()
    if not raw:
        raise RuntimeError("version.php 返回空")
    return raw


def fetch_manifest(base, platform, ver, cache_path=None):
    """下载 AssetBundles 清单（约 3MB）。有本地缓存则优先用缓存。"""
    if cache_path and os.path.isfile(cache_path) and os.path.getsize(cache_path) > 1024 * 1024:
        txt = open(cache_path, "rb").read().decode("utf-8", "replace")
        if "AssetBundles" in txt[:200]:
            return txt, "cache"
    url = "%s%s/AssetBundles_%s2019.manifest" % (base, platform, ver)
    raw = http_get(url, timeout=120)
    if cache_path:
        try:
            open(cache_path, "wb").write(raw)
        except Exception:
            pass
    return raw.decode("utf-8", "replace"), "network"


def parse_manifest(txt):
    """解析清单，兼容自闭合与成对标签。返回 [{name, md5, crc, size, deps}]."""
    out = []
    for m in re.finditer(r"<AssetBundle\s+([^>]*?)(?:/>|>(.*?)</AssetBundle>)", txt, re.S):
        attrs, body = m.group(1), m.group(2) or ""
        d = dict(re.findall(r'(\w+)="([^"]*)"', attrs))
        name, md5 = d.get("name"), d.get("md5")
        if not name or not md5:
            continue
        out.append({
            "name": name,
            "md5": md5.lower(),
            "crc": int(d.get("crc", 0) or 0),
            "size": int(d.get("size", 0) or 0),
            "deps": re.findall(r'<Dependency name="([^"]+)"', body),
        })
    return out


def cache_path_for(preload_dir, md5):
    """客户端缓存命中路径: <preload>/<md5>/data"""
    return os.path.join(preload_dir, md5, "data")


def file_md5(path, chunk=1 << 20):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


def verify_cached(preload_dir, item, fast=False):
    """检查本地是否已有且完整的资源。返回 True/False.
    fast=True 时只比对文件大小（快，适合海量包）；否则比对 md5。"""
    p = cache_path_for(preload_dir, item["md5"])
    if not os.path.isfile(p):
        return False
    if os.path.getsize(p) != item["size"]:
        return False
    if fast:
        return True
    return file_md5(p) == item["md5"]


def download_one(item, preload_dir, base, platform, verify=True, fast_skip=False):
    """下载单个 bundle 到缓存。返回 (status, item, info)."""
    fname = "%s_%s" % (item["name"], item["md5"])
    target = cache_path_for(preload_dir, item["md5"])
    if os.path.isfile(target) and os.path.getsize(target) == item["size"]:
        if fast_skip or not verify or file_md5(target) == item["md5"]:
            return ("skip", item, None)

    url = "%s%s/%s" % (base, platform, fname.replace("\\", "/"))
    tmo = max(45, min(180, int(item["size"] / 1048576 * 12)))
    try:
        data = http_get(url, timeout=tmo, retries=2)
    except Exception as ex:
        return ("fail", item, repr(ex))
    if item["size"] and len(data) != item["size"]:
        return ("size-mismatch", item, "%d != %d" % (len(data), item["size"]))
    if verify and file_md5_from_bytes(data) != item["md5"]:
        return ("md5-mismatch", item, "%s != %s" % (file_md5_from_bytes(data), item["md5"]))

    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = target + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, target)
    return ("ok", item, len(data))


def file_md5_from_bytes(data):
    return hashlib.md5(data).hexdigest()


def select_items(items, patterns, exact=False):
    if not patterns:
        return items
    if patterns == ["all"]:
        return items
    if exact:
        wanted = set(patterns)
        return [i for i in items if i["name"] in wanted]
    return [i for i in items if any(i["name"].startswith(p) for p in patterns)]


def group_stats(items):
    groups = {}
    for it in items:
        top = it["name"].split("/")[0] if "/" in it["name"] else "(root)"
        g = groups.setdefault(top, [0, 0])
        g[0] += 1
        g[1] += it["size"]
    return groups
