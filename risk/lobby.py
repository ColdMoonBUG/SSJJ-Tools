# -*- coding: utf-8 -*-
"""风险模式：大厅美化的一次性落地（改 paras.dat + 生成变体 + 可回滚）。

调用顺序
--------
1. 关掉普通模式的 fps 覆盖（local_cdn fps off），避免两套机制抢 paras.dat
2. 用官方大厅 SWF 生成变体（改缓动常量 / 可选改帧率）
3. 备份 paras.dat 一次，把 lobbyFileName 指到变体名（文件名里的 md5 = 内容 md5）
4. 写 risk_lobby.json 清单，供 restore 精确回滚
"""
import json
import os
import shutil
import time

import ssjj_paths as P

from . import swfpatch, airdesc

MANIFEST = swfpatch.MANIFEST
PARAS_BAK = P.PARAS + ".risk-bak"

# 私服大厅（D:\Ccode\sjsf）自己的模块下载目录。存在就同步变体过去，
# 否则那边按 paras.dat 的 lobbyFileName 下载会 404，整个大厅起不来。
_MIRROR_DIRS = (
    r"D:\Ccode\sjsf\_work\lobby_host\cdn_official",
    r"D:\Ccode\sjsf\_work\runtime\Lobby",
)


def _paras():
    try:
        return P.read_paras()
    except Exception:
        return {}


def _write_paras(d):
    with open(P.PARAS, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, separators=(",", ":"))


def _clear_fps_override(log):
    cfg = os.path.join(P.HERE, "fps_override.json")
    if not os.path.isfile(cfg):
        return
    try:
        import local_cdn as C
        C.cmd_fps(["off"])
        log.append("[*] 已关闭普通模式的帧率覆盖（它也要改 paras.dat，避免打架）")
    except Exception as e:
        log.append("[!] 关闭普通帧率覆盖失败（不影响继续）: %r" % (e,))


def status():
    man = swfpatch.load_manifest()
    paras = _paras()
    name = paras.get("lobbyFileName")
    out = {"name": name, "official": man.get("official"), "variant": man.get("variant"),
           "easing": man.get("easing"), "fps": man.get("fps"),
           "active": bool(man.get("variant") and name == man.get("variant")),
           "variant_exists": bool(man.get("variant") and
                                  os.path.isfile(os.path.join(P.CACHE_DIR, man["variant"]))),
           "paras_backup": os.path.isfile(PARAS_BAK),
           "air": airdesc.read()}
    # 顺带把官方文件的现状也读一下
    target = out["official"] or name
    if target:
        p = os.path.join(P.CACHE_DIR, target)
        if os.path.isfile(p):
            try:
                info = swfpatch.inspect(p)
                out["fps_now"] = info["fps"]
                out["has_back_const"] = bool(info["back"])
            except Exception as e:
                out["inspect_error"] = repr(e)
    return out


def apply(easing="柔和", fps=0, use_descriptor_fps=False):
    """生成并启用美化版大厅。返回若干行日志。"""
    log = []
    man = swfpatch.load_manifest()
    paras = _paras()
    name = paras.get("lobbyFileName")
    if not name:
        raise RuntimeError("paras.dat 里没有 lobbyFileName，先启动一次游戏")

    _clear_fps_override(log)

    # 当前如果已经是变体，回到官方名再生成，避免「变体的变体」
    if man and name == man.get("variant") and man.get("official"):
        official = man["official"]
        log.append("[*] 当前已是美化版，基于官方文件重新生成")
    else:
        official = name

    src = os.path.join(P.CACHE_DIR, official)
    if not os.path.isfile(src):
        raise FileNotFoundError("资源库里没有官方大厅 SWF：%s\n"
                                "先跑一次加速服务进一次游戏把大厅拉下来" % src)

    res = swfpatch.make_variant(P.CACHE_DIR, official,
                                easing=easing, fps=(int(fps) or None))
    log.append("[*] 已生成变体: %s (%d 字节)" % (res["dst"], res["size"]))
    rep = res["report"]
    if rep.get("back"):
        log.append("    缓动: Back 过冲 %.5f -> %.5f（%d 处常量）"
                   % (rep["back"][0]["old"], rep["back"][0]["new"], len(rep["back"])))
    if rep.get("backio"):
        log.append("          BackInOut 过冲 %.5f -> %.5f"
                   % (rep["backio"][0]["old"], rep["backio"][0]["new"]))
    if res.get("fps"):
        log.append("    帧率: 改为 %g fps" % float(res["fps"]))

    # 备份 paras.dat（只在第一次）
    if not os.path.isfile(PARAS_BAK):
        try:
            shutil.copy2(P.PARAS, PARAS_BAK)
            log.append("[*] paras.dat 已备份 -> %s" % PARAS_BAK)
        except Exception as e:
            log.append("[!] 备份 paras.dat 失败: %r" % (e,))

    # 指过去
    try:
        paras = _paras()
        paras["lobbyFileName"] = res["dst"]
        _write_paras(paras)
        log.append("[*] paras.dat: lobbyFileName -> %s" % res["dst"])
        log.append("    文件名 md5 = 内容 md5，ModuleLoader 一次校验通过")
    except Exception as e:
        raise RuntimeError("写 paras.dat 失败（游戏是否正在运行？）: %r" % (e,))

    # 旧变体清掉（留着占盘）
    old = man.get("variant")
    if old and old != res["dst"]:
        try:
            if swfpatch.revert_variant(P.CACHE_DIR, old):
                log.append("[*] 已删除旧变体 %s" % old)
        except Exception:
            pass

    # 私服大厅（D:\Ccode\sjsf）会拿 paras.dat 的 lobbyFileName 去它自己的
    # cdn_official 目录下载模块；变体只在我们缓存里、CDN 上没有 -> 那边
    # urllib HTTPError 404，整个大厅起不来。发现该目录就顺手同步一份。
    for mirror in _MIRROR_DIRS:
        try:
            if os.path.isdir(mirror):
                dst = os.path.join(mirror, os.path.basename(res["dst"]))
                if not os.path.isfile(dst) or os.path.getsize(dst) != res["size"]:
                    shutil.copy2(res["dst"], dst)
                    log.append("[*] 已同步变体到私服大厅: %s" % dst)
        except Exception as e:
            log.append("[!] 同步到 %s 失败: %r" % (mirror, e))

    if use_descriptor_fps and fps:
        try:
            a = airdesc.set_frame_rate(fps)
            log.append("[*] AIR 描述符 frameRate -> %g（备份 %s）" % (float(fps), airdesc.BAK))
        except Exception as e:
            log.append("[!] 改 AIR 描述符失败: %r" % (e,))

    swfpatch.save_manifest({"official": official, "variant": res["dst"],
                            "md5": res["md5"], "easing": easing,
                            "fps": int(fps) or 0, "overshoot": res.get("overshoot"),
                            "ts": time.time(), "paras_backup": PARAS_BAK})
    log.append("[*] 重启加速服务 + 重开大厅后生效")
    return log


def restore():
    log = []
    man = swfpatch.load_manifest()
    paras = _paras()
    if man.get("official"):
        try:
            paras["lobbyFileName"] = man["official"]
            _write_paras(paras)
            log.append("[*] paras.dat: lobbyFileName 恢复 -> %s" % man["official"])
        except Exception as e:
            log.append("[!] 恢复 paras.dat 失败: %r" % (e,))
    elif os.path.isfile(PARAS_BAK):
        try:
            shutil.copy2(PARAS_BAK, P.PARAS)
            log.append("[*] 已用备份覆盖 paras.dat")
        except Exception as e:
            log.append("[!] 覆盖失败: %r" % (e,))

    if man.get("variant"):
        try:
            if swfpatch.revert_variant(P.CACHE_DIR, man["variant"]):
                log.append("[*] 已删除变体 %s" % man["variant"])
        except Exception as e:
            log.append("[!] 删除变体失败: %r" % (e,))

    if airdesc.restore():
        log.append("[*] AIR 描述符已还原")

    try:
        if os.path.isfile(PARAS_BAK):
            os.remove(PARAS_BAK)
    except OSError:
        pass
    if os.path.isfile(MANIFEST):
        os.remove(MANIFEST)
    log.append("[*] 已回到官方大厅。重启加速服务 + 重开大厅生效。")
    return log
