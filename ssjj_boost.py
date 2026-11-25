#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SSJJ 加速器 —— 生死狙击资源预下载 & 大厅帧率解锁
==================================================
作者工具 / 仅用于加速本机已安装游戏的本地资源加载。
不修改游戏程序文件、不注入内存、不绕过反作弊。

功能：
  1. preload   全量/分类预下载战斗资源(17.3GB)到官方缓存目录 -> 战斗秒开
  2. lobby     预抓取大厅 res_config 清单资源(约97MB)到本地备用
  3. fps       查看/解锁大厅 SWF 帧率上限（带备份与还原）
  4. status    查看当前缓存占用与完整性
  5. gui       启动图形界面

用法示例：
  python ssjj_boost.py status
  python ssjj_boost.py preload --list
  python ssjj_boost.py preload --match all --jobs 12
  python ssjj_boost.py preload --match maps/ weapon/ --jobs 8
  python ssjj_boost.py lobby
  python ssjj_boost.py fps --inspect
  python ssjj_boost.py fps --set 60
  python ssjj_boost.py fps --restore
  python ssjj_boost.py gui
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import core
import ssjj_paths as P

HERE = P.HERE
GAME_DIR = P.GAME_DIR
MANIFEST_CACHE = os.path.join(HERE, "_manifest_cache.xml")
STATE_FILE = os.path.join(HERE, "state.json")


def load_manifest(base, platform, refresh=False):
    cache = None if refresh else MANIFEST_CACHE
    txt, src = core.fetch_manifest(base, platform, "0.0.0", cache)
    return core.parse_manifest(txt), src


def human(n):
    for unit in ["B", "KB", "MB", "GB"]:
        if n < 1024:
            return "%.1f %s" % (n, unit)
        n /= 1024
    return "%.1f TB" % n


def cmd_status(args):
    preload = args.preload_dir
    print("=" * 64)
    print("SSJJ 加速器 - 缓存状态")
    print("=" * 64)
    print("游戏目录     :", args.game_dir if os.path.isdir(args.game_dir) else args.game_dir + " (未找到)")
    print("缓存目录     :", preload)
    if not os.path.isdir(preload):
        print("\n[!] 缓存目录不存在（还没有任何本地资源）")
        return
    total = 0
    dirs = 0
    bad = 0
    for name in os.listdir(preload):
        p = os.path.join(preload, name, "data")
        if os.path.isfile(p):
            total += os.path.getsize(p)
            dirs += 1
            if len(name) != 32:
                bad += 1
    print("已缓存资源   : %d 个包" % dirs)
    print("占用空间     : %s" % human(total))
    if bad:
        print("非标准命名   : %d 个" % bad)
    if os.path.isfile(MANIFEST_CACHE):
        items = core.parse_manifest(open(MANIFEST_CACHE, "rb").read().decode("utf-8", "replace"))
        have = 0
        for it in items:
            p = core.cache_path_for(preload, it["md5"])
            if os.path.isfile(p) and os.path.getsize(p) == it["size"]:
                have += 1
        print("清单匹配     : %d / %d 包 (%.1f%%)" % (have, len(items), 100.0 * have / max(len(items), 1)))
    print("=" * 64)


def cmd_preload(args):
    base = args.base
    platform = args.platform
    print("[*] 加载资源清单 ...")
    items, src = load_manifest(base, platform, refresh=args.refresh)
    print("[*] 清单来源: %s | %d 个 bundle | 总计 %s" % (src, len(items), human(sum(i["size"] for i in items))))

    if args.list:
        g = core.group_stats(items)
        print("\n%-18s %8s %14s" % ("分类", "包数", "大小"))
        print("-" * 46)
        for k, (n, sz) in sorted(g.items(), key=lambda x: -x[1][1]):
            print("%-18s %8d %14s" % (k, n, human(sz)))
        print("-" * 46)
        if not args.match:
            print("\n提示: 加 --match all 全量下载；--match maps/ weapon/ 按分类下载")
            return

    selected = core.select_items(items, args.match, exact=args.exact)
    if not selected:
        print("[!] 没有匹配的资源")
        return
    if not args.match:
        print("\n提示: 未指定 --match，仅展示统计。加 --match all 开始全量下载。")
        return

    # 断点续传/校验策略：默认只看文件大小（海量包时 md5 全校验很慢），--verify 强制 md5
    selected.sort(key=lambda x: x["size"])
    total = sum(i["size"] for i in selected)
    print("\n[*] 选中 %d 个包 / %s -> %s" % (len(selected), human(total), args.preload_dir))
    print("[*] 并发 %d | 校验: %s | 断点续传: 开" % (args.jobs, "md5" if args.verify else "大小"))

    done = 0
    done_bytes = 0
    stats = {}
    t0 = time.time()
    try:
        with ThreadPoolExecutor(max_workers=args.jobs) as ex:
            futs = {
                ex.submit(core.download_one, it, args.preload_dir, base, platform,
                          args.verify, not args.verify): it
                for it in selected
            }
            for idx, fut in enumerate(as_completed(futs), 1):
                try:
                    status, item, info = fut.result()
                except Exception as ex2:
                    status, item, info = "error", futs[fut], repr(ex2)
                stats[status] = stats.get(status, 0) + 1
                if status in ("ok", "skip"):
                    done_bytes += item["size"]
                else:
                    print("  [!] %-14s %-44s %s" % (status, item["name"][:44], info))
                if idx % 20 == 0 or idx == len(selected):
                    el = max(time.time() - t0, 0.001)
                    print("  进度 %d/%d | %s/%s | %.1f MB/s | 成功%d 失败%d" % (
                        idx, len(selected), human(done_bytes), human(total),
                        done_bytes / 1048576 / el,
                        stats.get("ok", 0) + stats.get("skip", 0),
                        sum(v for k, v in stats.items() if k not in ("ok", "skip"))), flush=True)
    except KeyboardInterrupt:
        print("\n[!] 已中断。已完成的部分会自动跳过，重跑本命令即可续传。")
        return

    print("\n[*] 完成，耗时 %.1fs | 统计: %s" % (time.time() - t0, stats))
    bad = sum(v for k, v in stats.items() if k not in ("ok", "skip"))
    if bad:
        print("[!] %d 个失败，可重跑（已完成的会跳过）" % bad)


def cmd_lobby(args):
    """把大厅 res_config 清单里缺的资源补进本地资源库（local_cdn 用的那个库）。

    注意这里用 http，不是 https：local_cdn 的 443 是纯 TCP 透传，走 https
    抓下来的东西进了缓存也没法被回放命中，而 80 才是本地服务监听的口。
    """
    import re
    paras = P.read_paras()
    if not paras:
        print("[!] 未找到 paras.dat，请先登录一次游戏生成该文件")
        return
    base = paras.get("resurl") or (P.BASE + "/s5/")
    base = base.rstrip("/") + "/"
    out = P.CACHE_DIR

    mf = P.find_manifest()
    if not mf:
        print("[!] 本地还没有 res_config 清单，先跑一次游戏或 exhaustive_fetch.py prewarm")
        return
    cfg = open(mf, encoding="utf-8", errors="replace").read()
    print("[*] 清单: %s" % mf)

    resources = set(re.findall(r'url="([^"]+)"', cfg))
    for k in ("lobbyFileName", "playerModelFileName", "weaponModelFileName",
              "renderThreadFileName", "ClientView", "roleCreatorFileName"):
        v = paras.get(k)
        if v and v.endswith(".swf"):
            resources.add(v)
    resources.add("config/hall_config.xml")

    todo = []
    for rel in sorted(resources):
        rel = rel.split("?")[0].lstrip("/")
        dst = os.path.join(out, rel.replace("/", os.sep))
        if not (os.path.isfile(dst) and os.path.getsize(dst) > 0):
            todo.append(rel)
    print("[*] 清单 %d 项，本地缺失 %d 项（并发 %d）" % (len(resources), len(todo), args.jobs))

    rel_q = P.current_release()

    def grab(rel):
        dst = os.path.join(out, rel.replace("/", os.sep))
        d, _, f = rel.rpartition("/")
        try:
            data = core.http_get("%s%s%s?version=%s" % (base, d + "/" if d else "", f, rel_q),
                                 timeout=30, retries=2)
        except Exception as e:
            return "fail", rel, repr(e)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tmp = dst + ".part"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, dst)
        return "ok", rel, len(data)

    stats = {}
    got = 0
    with ThreadPoolExecutor(max_workers=args.jobs) as ex:
        futs = [ex.submit(grab, r) for r in todo]
        for i, f in enumerate(as_completed(futs), 1):
            st, rel, info = f.result()
            stats[st] = stats.get(st, 0) + 1
            if st == "ok":
                got += info
            elif stats[st] <= 5:
                print("  [!] %s %s" % (rel, info))
            if i % 100 == 0:
                print("  进度 %d/%d 新增 %.1fMB" % (i, len(todo), got / 1048576), flush=True)
    print("[*] 完成: %s | 新增 %.1f MB | 资源库: %s" % (stats, got / 1048576, out))


def cmd_fps(args):
    """大厅帧率：基于官方逆向源码的准确说明 + 安全配置定位。

    逆向结论（来自私服项目 _work/reverse/official/source/Assets.Sources.Config）：
      - 战斗端: Application.targetFrameRate = Config.FrameLimit
                QualitySettings.vSyncCount = Config.VSync
                FrameLimit 被代码钳制在 [60, 200]（FrameLimitMin/Max）
      - 大厅主 SWF 是 ZWS(LZMA) 压缩，真实帧率由 AS3 stage.frameRate 控制，
        改 SWF 头默认值基本无效（且会破坏 CDN 文件名 hash 校验 -> 客户端反复重试）。
    因此：
      - 60~200 之间：直接用游戏内「设置-画面-帧率上限」即可，最安全。
      - 超过 200：必须改 Assembly-CSharp.dll (GameSetting)，正式服有
        GameGuard + ACE 反作弊，检测到改动会封号。本工具不提供该操作。
    """
    print("=" * 72)
    print("大厅/战斗帧率机制（依据官方客户端逆向源码）")
    print("=" * 72)
    print("  战斗端: Application.targetFrameRate = Config.FrameLimit")
    print("          QualitySettings.vSyncCount    = Config.VSync")
    print("          帧率上限被代码限制在 60 ~ 200")
    print("  大厅  : 主 SWF 为 ZWS(LZMA) 压缩，帧率由 AS3 stage.frameRate 控制")
    print()

    if args.inspect or (not args.set and not args.restore):
        # 列出大厅 SWF 头帧率（如实标注：头值通常被 AS3 覆盖）
        swf_dir = os.path.join(args.game_dir, "Lobby")
        print("大厅 SWF 头帧率（注意：实际帧率由 AS3 覆盖，此值仅供参考）:")
        print("-" * 72)
        import swf_fps
        seen = 0
        for name in (os.listdir(swf_dir) if os.path.isdir(swf_dir) else []):
            if name.lower().endswith(".swf"):
                info = swf_fps.inspect(os.path.join(swf_dir, name))
                print("  %-46s %-4s %s" % (name[:46], info.get("sig", "?"),
                                            ("%.2f fps" % info["fps"]) if info.get("fps") else "解析受限"))
                seen += 1
        if not seen:
            print("  (未找到 SWF)")
        print("-" * 72)
        print("\n[安全做法] 进游戏 -> 设置 -> 画面 -> 帧率上限，可调 60~200，关闭垂直同步。")
        print("[高阶做法] 超过 200 需改 Assembly-CSharp.dll，但正式服有 GameGuard+ACE，")
        print("           改客户端文件会被检测并可能封号 —— 本工具不提供此操作。")
        print("[私服环境] 私服已实现帧率解锁（OfflineGameController 设 targetFrameRate=999），")
        print("           在你的私服里可直接用，无需改正式服客户端。")
        return

    if args.set:
        print("[!] 为了你的账号安全，本工具不会修改游戏程序文件或注入内存。")
        print("    请在游戏内 设置->画面 调整帧率上限（范围 60~200）。")
        print("    如需 >200，请仅在你的私服环境操作（见私服 OfflineGameController）。")
        return

    if args.restore:
        # 还原历史上本工具可能改过的 SWF 头备份（兼容旧版本）
        import glob
        n = 0
        for bak in glob.glob(os.path.join(args.game_dir, "Lobby", "*.swf.bak")):
            os.replace(bak, bak[:-4])
            print("[OK] 还原", os.path.basename(bak[:-4]))
            n += 1
        print("[*] 还原了 %d 个文件" % n)


def main():
    ap = argparse.ArgumentParser(description="SSJJ 加速器 - 资源预下载与帧率解锁")
    ap.add_argument("--game-dir", default=GAME_DIR)
    ap.add_argument("--preload-dir", default=core.default_preload_dir())
    ap.add_argument("--base", default=core.DEFAULT_BASE)
    ap.add_argument("--platform", default=core.DEFAULT_PLATFORM)
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("status", help="查看缓存状态")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("preload", help="预下载战斗资源")
    p.add_argument("--match", nargs="*", default=None, help="分类前缀，如 maps/ weapon/ 或 all")
    p.add_argument("--exact", action="store_true")
    p.add_argument("--list", action="store_true", help="只列出分类统计")
    p.add_argument("--jobs", type=int, default=8)
    p.add_argument("--verify", action="store_true", help="强制 md5 校验（慢）")
    p.add_argument("--refresh", action="store_true", help="强制刷新清单")
    p.set_defaults(func=cmd_preload)

    p = sub.add_parser("lobby", help="预抓取大厅资源")
    p.add_argument("--jobs", type=int, default=8)
    p.set_defaults(func=cmd_lobby)

    p = sub.add_parser("fps", help="查看/修改大厅帧率")
    p.add_argument("--inspect", action="store_true")
    p.add_argument("--set", type=float, default=None, metavar="FPS", help="设定帧率，如 60")
    p.add_argument("--restore", action="store_true")
    p.set_defaults(func=cmd_fps)

    p = sub.add_parser("gui", help="启动图形界面")
    p.set_defaults(func=lambda a: __import__("gui").main(a))

    args = ap.parse_args()
    if not hasattr(args, "func"):
        ap.print_help()
        return
    args.func(args)


if __name__ == "__main__":
    main()
