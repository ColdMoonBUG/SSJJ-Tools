# -*- coding: utf-8 -*-
"""
SWF 帧率读写 + 大厅帧率解除
===========================
SWF 头结构：
  [0:3]   签名  FWS(未压缩)/CWS(zlib)/ZWS(lzma)
  [3]     版本
  [4:8]   未压缩总长度 (LE u32)
  ---- CWS ----
  [8:]    zlib 流
  ---- ZWS ----
  [8:12]  压缩数据长度 (LE u32)
  [12:17] LZMA properties (5 字节: 1 字节控制位 + 4 字节 dictSize)
  [17:]   LZMA RAW 流
头部之后是 body：
  RECT (位压缩，长度 = (5 + 4*nbits + 7) // 8)
  frameRate : 2 字节 FIXED8【小端】—— 低字节是小数部分，高字节是整数部分
  frameCount: 2 字节 u16

上一版本的 `swf_fps.py` 把 ZWS 的 LZMA properties 当成位于 [8:13]、数据位于
[13:]，实际是 [12:17] 和 [17:]，整体偏移了 4 字节。结果它解不开任何 ZWS 文件，
inspect() 永远返回"解压失败"，从头到尾没有真正工作过。另外帧率字段按大端
解析，24fps 会被读成 0.09375。这两处都修了。

大厅帧率的真相（实测 2026-11-22）：
  - 线上 Lobby_*.swf 头里写的是 24fps
  - 包里没有任何 stage.frameRate 赋值（唯一那处 "stage.frameRate = 12" 是
    com.junkbyte.console 的调试帮助文本，不是代码）
  - application.xml 里没有 <frameRate>，所以 AIR 就用 SWF 头的 24fps
  => 大厅帧率完全由 SWF 头决定，改它有效

被加载模块的文件名必须形如 <name>_<32位md5>.swf，ModuleLoader.as:124-134 会拿
文件名里的 md5 和实际内容比对，不符就重载 5 次后放弃。所以改完内容必须换一个
新文件名（新内容的 md5），不能原地覆盖。
"""
import os
import re
import struct
import zlib
import lzma
import hashlib

MD5_SUFFIX = re.compile(r"_([0-9a-f]{32})\.swf$", re.I)


# ---------------------------------------------------------------- 编解码
def _lzma_filters(props):
    """从 ZWS 的 5 字节 properties 还原 filter 参数"""
    dec = props[0]
    if dec >= 9 * 5 * 5:
        raise ValueError("LZMA properties 非法: %r" % props.hex())
    return [{"id": lzma.FILTER_LZMA1,
             "dict_size": struct.unpack("<I", props[1:5])[0],
             "lc": dec % 9,
             "lp": (dec // 9) % 5,
             "pb": dec // 45}]


def decode(blob):
    """返回 (body, sig)，sig 是 'FWS'/'CWS'/'ZWS'"""
    sig = blob[:3]
    if sig == b"FWS":
        return blob[8:], "FWS"
    if sig == b"CWS":
        return zlib.decompress(blob[8:]), "CWS"
    if sig == b"ZWS":
        props = blob[12:17]
        return lzma.decompress(blob[17:], format=lzma.FORMAT_RAW,
                               filters=_lzma_filters(props)), "ZWS"
    raise ValueError("不是 SWF 文件: %r" % sig)


def encode(body, sig=b"ZWS", version=19, props=None):
    """按原格式重新压回。props 沿用原文件的，保证 properties 不变。"""
    head = bytes([version])
    if sig == b"FWS":
        return b"FWS" + head + struct.pack("<I", len(body) + 8) + body
    if sig == b"CWS":
        comp = zlib.compress(body, 9)
        return b"CWS" + head + struct.pack("<I", len(body) + 8) + comp
    if props is None:
        props = lzma._encode_filter_properties({"id": lzma.FILTER_LZMA1,
                                                "dict_size": 1 << 24})
    comp = lzma.compress(body, format=lzma.FORMAT_RAW,
                         filters=_lzma_filters(props))
    return (b"ZWS" + head + struct.pack("<I", len(body) + 8)
            + struct.pack("<I", len(comp)) + props + comp)


# ---------------------------------------------------------------- 帧率
def _rect_len(body):
    if not body:
        raise ValueError("SWF body 为空")
    return (5 + 4 * (body[0] >> 3) + 7) // 8


def read_fps(path):
    """读头部帧率。注意 FIXED8 是小端：低字节小数、高字节整数。"""
    with open(path, "rb") as f:
        blob = f.read()
    body, _ = decode(blob)
    pos = _rect_len(body)
    lo, hi = body[pos], body[pos + 1]
    return hi + lo / 256.0


def _set_fps_field(body, fps):
    """把 FIXED8 帧率写进 body 副本"""
    pos = _rect_len(body)
    integer = int(fps)
    frac = int(round((fps - integer) * 256))
    if frac >= 256:
        integer += 1
        frac = 0
    if not 0 < integer < 256:
        raise ValueError("帧率超出 FIXED8 整数部分范围: %r" % (fps,))
    out = bytearray(body)
    out[pos] = frac & 0xFF        # 低字节 = 小数
    out[pos + 1] = integer & 0xFF  # 高字节 = 整数
    return bytes(out), pos


def patch_file(src, dst, fps):
    """把 src 按 fps 改写后存为 dst。返回 (dst, 新md5, 原fps, 新fps)"""
    with open(src, "rb") as f:
        blob = f.read()
    body, sig = decode(blob)
    old = read_fps(src) if False else None
    pos = _rect_len(body)
    old = body[pos + 1] + body[pos] / 256.0
    newbody, _ = _set_fps_field(body, fps)
    props = blob[12:17] if sig == "ZWS" else None
    out = encode(newbody, sig.encode(), blob[3], props)
    # 自检：解压回来帧率要对得上
    check_body, _ = decode(out)
    cpos = _rect_len(check_body)
    got = check_body[cpos + 1] + check_body[cpos] / 256.0
    if abs(got - fps) > 0.001:
        raise ValueError("改写自检失败：期望 %r 实际 %r" % (fps, got))
    if len(check_body) != len(body):
        raise ValueError("改写后 body 长度变化 %d -> %d" % (len(body), len(check_body)))
    with open(dst, "wb") as f:
        f.write(out)
    return dst, hashlib.md5(out).hexdigest(), old, got


# ---------------------------------------------------------------- 大厅用途
def find_lobby_swf(cache_dir, name):
    """在资源库里找大厅主 SWF。name 是 paras.dat 的 lobbyFileName。"""
    p = os.path.join(cache_dir, name)
    if os.path.isfile(p):
        return p
    return None


def make_fps_variant(cache_dir, lobby_name, fps, out_name=None):
    """生成指定帧率的大厅主 SWF，返回新文件名。

    新文件名必须是 <stem>_<新md5>.swf —— 客户端会按文件名里的 md5 校验内容。
    """
    src = find_lobby_swf(cache_dir, lobby_name)
    if not src:
        raise FileNotFoundError("资源库里没有大厅主 SWF: %s\n"
                                "先跑一次 1-启动加速并进游戏.bat 让它回源下来" % lobby_name)
    m = MD5_SUFFIX.search(lobby_name)
    if not m:
        raise ValueError("大厅文件名里没有 md5 后缀，不符合客户端校验规则: %s" % lobby_name)
    stem = lobby_name[:m.start()]
    if out_name is None:
        # 先写临时名，算出真实 md5 后再定名
        tmp = os.path.join(cache_dir, stem + "_PATCHING.swf")
        _, new_md5, old_fps, new_fps = patch_file(src, tmp, fps)
        out_name = "%s_%s.swf" % (stem, new_md5)
        os.replace(tmp, os.path.join(cache_dir, out_name))
    else:
        _, new_md5, old_fps, new_fps = patch_file(
            src, os.path.join(cache_dir, out_name), fps)
    return out_name, new_md5, old_fps, new_fps
