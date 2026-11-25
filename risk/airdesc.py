# -*- coding: utf-8 -*-
"""风险模式：AIR 应用描述符改写（帧率 / 渲染模式）。

Lobby\\META-INF\\AIR\\application.xml 决定 AIR 运行时的初始舞台参数。
线上文件里没有 <frameRate> 元素，所以 AIR 用 24fps；工程里也没写
<renderMode>，实际是 direct（Stage3D），这个不动。

注意：application.xml 本身不在 Lobby\\md5cache\\1.0.1.400.md5.list 里
（清单里只有 application.xml.temp），所以改它不会被启动器按需重下。
仍然留一份 .risk-bak，随时能退。
"""
import os
import re
import shutil

import ssjj_paths as P

APP_XML = os.path.join(P.GAME_DIR, "Lobby", "META-INF", "AIR", "application.xml")
TEMP_XML = os.path.join(P.GAME_DIR, "Lobby", "META-INF", "AIR", "application.xml.temp")
BAK = APP_XML + ".risk-bak"

FRAME_RATE_RE = re.compile(r"<frameRate>\s*([0-9.]+)\s*</frameRate>", re.I)
RENDER_MODE_RE = re.compile(r"<renderMode>\s*([a-zA-Z]+)\s*</renderMode>", re.I)


def available():
    return os.path.isfile(APP_XML)


def read():
    if not available():
        return {"exists": False, "path": APP_XML}
    with open(APP_XML, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    m = FRAME_RATE_RE.search(text)
    r = RENDER_MODE_RE.search(text)
    return {"exists": True, "path": APP_XML, "frameRate": float(m.group(1)) if m else None,
            "renderMode": r.group(1).lower() if r else None,
            "backup": os.path.isfile(BAK), "temp": os.path.isfile(TEMP_XML)}


def set_frame_rate(fps):
    """写入 / 修改 <frameRate>。fps=None 表示删掉该元素（回到官方默认 24）。"""
    if not available():
        raise FileNotFoundError("找不到 AIR 描述符: %s" % APP_XML)
    with open(APP_XML, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    if not os.path.isfile(BAK):
        shutil.copy2(APP_XML, BAK)

    fps_txt = ("%g" % float(fps)) if fps else None
    if FRAME_RATE_RE.search(text):
        if fps_txt:
            text = FRAME_RATE_RE.sub("<frameRate>%s</frameRate>" % fps_txt, text, count=1)
        else:
            text = FRAME_RATE_RE.sub("", text, count=1)
    elif fps_txt:
        # 放在 application 级、initialWindow 之前（schema 允许的可选元素）
        anchor = "<initialWindow>"
        ins = "\t<frameRate>%s</frameRate>\n\n" % fps_txt
        if anchor in text:
            text = text.replace(anchor, ins + anchor, 1)
        else:
            text = text.replace("</application>", ins + "</application>", 1)

    # 自检：仍然是合法 XML
    import xml.etree.ElementTree as ET
    try:
        ET.fromstring(text.encode("utf-8"))
    except Exception as e:
        raise ValueError("改写后 XML 不合法，已放弃: %r" % (e,))

    with open(APP_XML, "w", encoding="utf-8") as f:
        f.write(text)
    return read()


def restore():
    if os.path.isfile(BAK):
        shutil.copy2(BAK, APP_XML)
        os.remove(BAK)
        return True
    return False
