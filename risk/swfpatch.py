# -*- coding: utf-8 -*-
"""风险模式：大厅主 SWF 的美化改写。

为什么能改
----------
preloader 的 ModuleLoader.as:121-136 只做一件事：
    _loc2_ = swfUrl.substring(swfUrl.lastIndexOf("_")+1, swfUrl.indexOf("."))
    if (_loc2_.length == 32 && _loc2_ != md5(下载的字节)) 重载
也就是说文件名里的 32 位 md5 必须等于内容 md5。所以：
  · 原地覆盖文件 → md5 不符 → 重载 5 次后卡住（老坑）
  · 改完内容再按新 md5 改名，并把 paras.dat 的 lobbyFileName 指过去 → 一次通过
本模块就是走第二条路。

改的是什么
----------
大厅是 AIR + GreenSock。实测「不够丝滑」的来源不是帧率（24fps→60fps
CPU 涨 18 倍、肉眼无差别），而是 com.greensock.easing:Back* 的过冲回弹
（清单里 Back 用了 501 处）。GreenSock 2.x 的 Back 系列过冲量就是一个
double 常量 1.70158（BackInOut 用 1.525），它躺在 DoABC 的 DoubleConstants
常量池里。

所以本模块干的事是：把 ABC 常量池里那几个 double 换成小一点的过冲量。
· 长度完全不变（8 字节换 8 字节）
· 不改 tag 结构、不重编译 AS3、不动帧标签
· 换完重算 md5 命名，客户端校验天然通过

需要恢复时把 paras.dat 指回原名即可（manifest 里有记录）。
"""
import hashlib
import json
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
from .paths import DATA_ROOT as ROOT      # 打包后也不能落到临时解包目录
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import swf_fps  # noqa: E402

MANIFEST = os.path.join(ROOT, "risk_lobby.json")

# GreenSock 2.x 的过冲常量：Back 用 1.70158，BackInOut 用 1.525
BACK_BASE = 1.70158
BACKIO_BASE = 1.525

# 预设：值 = Back 的新过冲量。1.70158 ≈ 经典回弹（官方），越小越收敛。
EASING_PRESETS = {
    "原版": 1.70158,
    "轻微": 1.10,
    "柔和": 0.70,
    "丝滑": 0.35,
    "无回弹": 0.0,
}

DO_ABC = 0x52
DO_ABC_LEGACY = 0x48


# ---------------------------------------------------------------- 基础解析
def _u30(b, p):
    v = 0
    shift = 0
    while True:
        x = b[p]
        p += 1
        v |= (x & 0x7F) << shift
        if not x & 0x80:
            return v, p
        shift += 7


def _s32(b, p):
    v = 0
    shift = 0
    while True:
        x = b[p]
        p += 1
        v |= (x & 0x7F) << shift
        shift += 7
        if not x & 0x80:
            if shift < 32 and x & 0x40:
                v |= -(1 << shift)
            return v, p


def iter_tags(body):
    """遍历 SWF body 里的 tag：(code, 数据起始偏移, 数据长度)"""
    pos = swf_fps._rect_len(body) + 4
    n = len(body)
    while pos + 2 <= n:
        cl = struct.unpack("<H", body[pos:pos + 2])[0]
        code = cl >> 6
        length = cl & 0x3F
        pos += 2
        if length == 0x3F:
            length = struct.unpack("<I", body[pos:pos + 4])[0]
            pos += 4
        yield code, pos, length
        if code == 0:
            return
        pos += length


def find_abc(body):
    """返回 [(abc 在 body 内的起始偏移, abc 长度, abc 名字)]"""
    out = []
    for code, off, length in iter_tags(body):
        if code not in (DO_ABC, DO_ABC_LEGACY):
            continue
        data = body[off:off + length]
        z = data.find(b"\x00", 4)
        if z < 0:
            continue
        name = data[4:z].decode("latin-1", "replace")
        out.append((off + z + 1, length - z - 1, name))
    return out


def parse_pools(abc):
    """解析 ABC 常量池，返回 dict：
       {"doubles": [(序号, abc 内偏移, 值)], "strings": {序号: 字符串}}
    """
    p = 4
    cnt, p = _u30(abc, p)
    for _ in range(max(0, cnt - 1)):
        _, p = _s32(abc, p)
    cnt, p = _u30(abc, p)
    for _ in range(max(0, cnt - 1)):
        _, p = _u30(abc, p)
    cnt, p = _u30(abc, p)
    doubles = []
    for i in range(max(0, cnt - 1)):
        doubles.append((i + 1, p, struct.unpack("<d", abc[p:p + 8])[0]))
        p += 8
    cnt, p = _u30(abc, p)
    strings = {}
    for i in range(max(0, cnt - 1)):
        n, p = _u30(abc, p)
        strings[i + 1] = abc[p:p + n].decode("utf-8", "replace")
        p += n
    return {"doubles": doubles, "strings": strings, "end": p}


def _find_double(doubles, value, tol=1e-9):
    return [(i, off, v) for i, off, v in doubles if abs(v - value) <= tol]


# ---------------------------------------------------------------- 读现状
def inspect(path):
    """看一眼大厅 SWF：帧率、ABC 数量、缓动常量在不在"""
    blob = open(path, "rb").read()
    body, sig = swf_fps.decode(blob)
    info = {"path": path, "size": len(blob), "body": len(body),
            "sig": sig, "fps": swf_fps.read_fps(path),
            "abcs": [], "back": [], "backio": [], "greensock": 0, "md5": hashlib.md5(blob).hexdigest()}
    for aoff, alen, name in find_abc(body):
        abc = body[aoff:aoff + alen]
        pools = parse_pools(abc)
        back = _find_double(pools["doubles"], BACK_BASE)
        backio = _find_double(pools["doubles"], BACKIO_BASE)
        gs = sum(1 for s in pools["strings"].values() if "com.greensock" in s)
        info["abcs"].append({"name": name, "len": alen, "offset": aoff,
                             "doubles": len(pools["doubles"]),
                             "strings": len(pools["strings"]),
                             "back": len(back), "backio": len(backio),
                             "greensock": gs})
        info["back"] += [{"abc": name, "index": i, "offset": aoff + off, "value": v}
                         for i, off, v in back]
        info["backio"] += [{"abc": name, "index": i, "offset": aoff + off, "value": v}
                           for i, off, v in backio]
        info["greensock"] += gs
    return info


# ---------------------------------------------------------------- 改写
def patch_body(body, back_overshoot=None, fps=None):
    """就地改写 body 里的缓动常量 / 帧率，返回 (新 body, 报告 dict)

    只做定长替换，body 长度必须保持不变——这是本方案的核心不变量。
    """
    report = {"back": [], "backio": [], "fps": None, "len_before": len(body)}
    out = bytearray(body)

    if back_overshoot is not None:
        ratio = back_overshoot / BACK_BASE if BACK_BASE else 1.0
        want = {BACK_BASE: back_overshoot, BACKIO_BASE: round(BACKIO_BASE * ratio, 6)}
        for aoff, alen, name in find_abc(body):
            abc = body[aoff:aoff + alen]
            pools = parse_pools(abc)
            for old, new in want.items():
                for idx, off, val in _find_double(pools["doubles"], old):
                    pos = aoff + off
                    assert out[pos:pos + 8] == body[pos:pos + 8]
                    out[pos:pos + 8] = struct.pack("<d", new)
                    report["back" if old == BACK_BASE else "backio"].append(
                        {"abc": name, "index": idx, "old": old, "new": new})

    if fps:
        out, pos = swf_fps._set_fps_field(bytes(out), float(fps))
        out = bytearray(out)
        report["fps"] = {"offset": pos, "value": fps}

    out = bytes(out)
    if len(out) != len(body):
        raise ValueError("body 长度变化 %d -> %d，已中止" % (len(body), len(out)))
    report["len_after"] = len(out)
    return out, report


def make_variant(cache_dir, lobby_name, easing=None, fps=None, out_stem=None):
    """生成美化版大厅 SWF。

    返回 dict: {"dst": 新文件名, "md5": 新 md5, "report": 改写明细, "src": 原名}
    """
    src = os.path.join(cache_dir, lobby_name)
    if not os.path.isfile(src):
        raise FileNotFoundError("资源库里没有大厅主 SWF: %s" % src)

    overshoot = None
    if easing:
        if isinstance(easing, str):
            if easing not in EASING_PRESETS:
                raise ValueError("未知缓动预设: %s（可选 %s）"
                                 % (easing, "/".join(EASING_PRESETS)))
            overshoot = EASING_PRESETS[easing]
        else:
            overshoot = float(easing)

    blob = open(src, "rb").read()
    body, sig = swf_fps.decode(blob)
    new_body, report = patch_body(body, overshoot, fps if fps else None)
    props = blob[12:17] if sig == "ZWS" else None
    out_blob = swf_fps.encode(new_body, sig.encode(), blob[3], props)

    # 自检 1：能解回来
    chk_body, chk_sig = swf_fps.decode(out_blob)
    if len(chk_body) != len(body):
        raise ValueError("自检失败：body 长度不一致")
    # 自检 2：常量确实变了
    if overshoot is not None:
        pools = parse_pools(chk_body[find_abc(chk_body)[0][0]:][:find_abc(chk_body)[0][1]])
        got = [round(v, 6) for _i, _o, v in pools["doubles"]
               if abs(v - overshoot) < 1e-6]
        if not got:
            raise ValueError("自检失败：常量池里找不到新过冲量 %.5f" % overshoot)
    # 自检 3：帧率（如果改了）
    if fps:
        tmp = os.path.join(cache_dir, "_verify.swf")
        with open(tmp, "wb") as f:
            f.write(out_blob)
        try:
            if abs(swf_fps.read_fps(tmp) - float(fps)) > 0.01:
                raise ValueError("自检失败：帧率没改上")
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

    md5 = hashlib.md5(out_blob).hexdigest()
    stem = out_stem or (lobby_name.rsplit("_", 1)[0] if "_" in lobby_name else "Lobby")
    dst_name = "%s_%s.swf" % (stem, md5)
    dst = os.path.join(cache_dir, dst_name)
    with open(dst, "wb") as f:
        f.write(out_blob)

    return {"src": lobby_name, "dst": dst_name, "path": dst, "md5": md5,
            "size": len(out_blob), "easing": easing, "overshoot": overshoot,
            "fps": fps, "report": report}


def revert_variant(cache_dir, dst_name):
    p = os.path.join(cache_dir, dst_name)
    if os.path.isfile(p):
        os.remove(p)
        return True
    return False


def load_manifest():
    try:
        with open(MANIFEST, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_manifest(data):
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return MANIFEST


if __name__ == "__main__":
    cache = os.path.join(ROOT, "lobby_res_cache")
    argv = sys.argv[1:]
    if not argv or argv[0] == "inspect":
        name = argv[1] if len(argv) > 1 else None
        if not name:
            cands = [f for f in os.listdir(cache) if f.lower().startswith("lobby_")
                     and f.lower().endswith(".swf")]
            name = sorted(cands)[-1] if cands else None
        print(json.dumps(inspect(os.path.join(cache, name)), ensure_ascii=False, indent=2))
    elif argv[0] == "make":
        easing = argv[1] if len(argv) > 1 else "柔和"
        fps = int(argv[2]) if len(argv) > 2 and argv[2] not in ("0", "-") else None
        import ssjj_paths as P
        cur = P.read_paras().get("lobbyFileName")
        print(json.dumps(make_variant(cache, cur, easing, fps), ensure_ascii=False, indent=2))
