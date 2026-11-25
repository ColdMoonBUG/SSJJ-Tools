# -*- coding: utf-8 -*-
"""静态换肤（零注入）：只改资源，不碰游戏进程。

思路：皮肤的贴图就在「皮肤包」里，运行时按皮肤名取图挂到武器材质上。
所以把「B 皮肤的贴图」写进「A 枪的包」，再用 A 枪时外观就变成 B 皮肤，
全程不写内存、不改代码段，GameGuard 扫不出差异，退出也不留补丁。

注意（2026-11-24 修）：战斗端加载前会校验 `Preload/<md5>/data` 的内容 md5 是否
等于目录名，就地改文件会报 `PreloadMd5Mismatch` 并拒绝加载 —— 表现就是「只能进
大厅、进不去对局」。所以现在不再动 Preload，而是：
  ① 把改好的包放到 static_mods/；
  ② 在 bundle_overrides.json 里登记「bundle 名 → 新 md5/crc/size/文件」；
  ③ 由本地加速服务把 AssetBundle 清单里的 md5 改写成新的，并按新 md5 的 URL 下发
     改过的包。客户端校验通过、缓存到 Preload/<新 md5>/data，之后照常加载。
"""
import hashlib
import json
import os
import shutil
import time
import zlib
from pathlib import Path

import boot

DATA = Path(boot.bootstrap())

PRELOAD = Path(os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))) / "Wooduan" / "Preload"
STATE_DIR = Path(os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))) / "ssjj-toolbox"
STATE = STATE_DIR / "static_skins.json"
BACKUP_DIR = DATA / "static_backup"
MOD_DIR = DATA / "static_mods"
OVERRIDES = STATE_DIR / "bundle_overrides.json"

_INDEX = None


def _index_path():
    here = Path(__file__).resolve().parent / "data" / "bundle_index.json"
    if here.is_file():
        return here
    meipass = getattr(__import__("sys"), "_MEIPASS", None)
    if meipass:
        cand = Path(meipass) / "toolbox" / "data" / "bundle_index.json"
        if cand.is_file():
            return cand
    return here


def index():
    global _INDEX
    if _INDEX is None:
        p = _index_path()
        _INDEX = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    return _INDEX


def bundle_path(name):
    info = index().get(name)
    if not info:
        return None
    return PRELOAD / info["md5"] / "data"


def available_skins(weapon):
    """某把枪在缓存清单里有几个皮肤包：返回 [(皮肤短名, 包名)]。"""
    pref = ("weaponskin/skin_%s_" % weapon.lower())
    out = []
    for name in index():
        low = name.lower()
        if low.startswith(pref) and "$" not in low:
            out.append((name[len(pref):], name))
    out.sort()
    return out


_HAS = {}


def has_skins(weapon):
    """这把枪本地有没有可借的皮肤包（带缓存，界面里逐个判断不会卡）。"""
    w = (weapon or "").lower()
    if w not in _HAS:
        _HAS[w] = bool(available_skins(w)) if w else False
    return _HAS[w]


def _load_state():
    if STATE.is_file():
        try:
            d = json.loads(STATE.read_text(encoding="utf-8"))
            if isinstance(d, dict) and isinstance(d.get("applied"), list):
                return d
        except Exception:
            pass
    return {"applied": []}


def _save_state(d):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE)


def applied_list():
    return _load_state()["applied"]


def _load_overrides():
    if OVERRIDES.is_file():
        try:
            d = json.loads(OVERRIDES.read_text(encoding="utf-8"))
            if isinstance(d, dict) and isinstance(d.get("bundles"), dict):
                return d
        except Exception:
            pass
    return {"bundles": {}}


def _save_overrides(d):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = OVERRIDES.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(OVERRIDES)


def _swap_textures(src_env, donor_env):
    """把 donor 的贴图按「尺寸+格式」灌进 src，返回替换张数。"""
    pool = {}
    for obj in donor_env.objects:
        if obj.type.name != "Texture2D":
            continue
        d = obj.read()
        pool.setdefault((int(d.m_Width), int(d.m_Height), int(d.m_TextureFormat)), []).append(d.image)
    n = 0
    for obj in src_env.objects:
        if obj.type.name != "Texture2D":
            continue
        d = obj.read()
        key = (int(d.m_Width), int(d.m_Height), int(d.m_TextureFormat))
        images = pool.get(key)
        if not images:
            continue
        d.set_image(images.pop(0), target_format=key[2],
                    mipmap_count=int(getattr(d, "m_MipCount", 1) or 1))
        d.save()
        n += 1
    return n


def apply_weapon_skin(weapon, skin, emit=None):
    """把 skin 皮肤包的贴图写进 weapon 的包，并登记到覆盖表。返回 (ok, msg)。"""
    say = emit or (lambda s: None)
    if not weapon:
        return False, "没选武器"
    name = "weapon/%s" % weapon.lower()
    target = bundle_path(name)
    if target is None:
        return False, "清单里没有武器包 %s" % name
    if not target.is_file():
        return False, "缓存里还没这把枪的资源（先在游戏里用一次，或在资源库预下载）"
    donor = None
    for short, full in available_skins(weapon):
        if short == skin:
            donor = bundle_path(full)
            break
    if donor is None or not donor.is_file():
        return False, "找不到皮肤包或它不在缓存里: %s" % skin

    try:
        import UnityPy
    except Exception as exc:  # pragma: no cover
        return False, "缺少 UnityPy：%s" % exc

    say("正在把皮肤 %s 的贴图写进武器 %s …" % (skin, weapon))
    # 用字节加载，避免 UnityPy.load(路径) 长期占用文件句柄
    try:
        src_env = UnityPy.load(target.read_bytes())
        donor_env = UnityPy.load(donor.read_bytes())
    except Exception as exc:
        return False, "读不出这两个资源包：%r" % (exc,)
    n = _swap_textures(src_env, donor_env)
    if not n:
        return False, "两个包的贴图规格对不上（尺寸/格式不同），这套皮肤用不了"

    blob = src_env.file.save()
    if isinstance(blob, dict):
        blob = list(blob.values())[0]

    md5 = hashlib.md5(blob).hexdigest()
    crc = zlib.crc32(blob) & 0xFFFFFFFF
    MOD_DIR.mkdir(parents=True, exist_ok=True)
    mod = MOD_DIR / ("%s_%s.bundle" % (name.replace("/", "__"), md5))
    mod.write_bytes(blob)

    ov = _load_overrides()
    old = ov["bundles"].get(name) or {}
    ov["bundles"][name] = {
        "md5": md5,
        "crc": crc,
        "size": len(blob),
        "path": str(mod),
        "orig_md5": old.get("orig_md5") or (index().get(name) or {}).get("md5"),
        "weapon": weapon,
        "skin": skin,
        "ts": int(time.time()),
    }
    _save_overrides(ov)

    # 之前「就地改 Preload」的老版本可能留了备份，顺手还原，避免 md5 不一致
    old_bak = BACKUP_DIR / (target.parent.name + ".orig")
    if old_bak.is_file():
        try:
            shutil.copy2(old_bak, target)
            old_bak.unlink()
        except Exception:
            pass

    st = _load_state()
    st["applied"] = [x for x in st["applied"] if x.get("weapon") != weapon]
    st["applied"].append({"weapon": weapon, "skin": skin, "n": n, "bundle": name,
                          "md5": md5, "path": str(mod), "ts": int(time.time())})
    _save_state(st)
    say("完成：%d 张贴图已替换，新包 %d 字节，md5 %s" % (n, len(blob), md5[:8]))
    return True, ("已把【%s】的外观换成皮肤【%s】（%d 张贴图）。"
                  "进游戏时由本地加速按新 md5 下发这个包，客户端校验能过" % (weapon, skin, n))


def _drop_override(bundle):
    ov = _load_overrides()
    info = ov["bundles"].pop(bundle, None)
    if info:
        try:
            Path(info.get("path") or "").unlink(missing_ok=True)
        except Exception:
            pass
        _save_overrides(ov)
    return info


def restore_weapon(weapon):
    st = _load_state()
    keep, done = [], 0
    for item in st["applied"]:
        if item.get("weapon") != weapon:
            keep.append(item)
            continue
        if item.get("bundle"):
            if _drop_override(item["bundle"]):
                done += 1
        else:  # 老格式：就地改过 Preload，靠备份还原
            bak = Path(item.get("backup") or "")
            path = Path(item.get("path") or "")
            if bak.is_file() and path.is_file():
                shutil.copy2(bak, path)
                done += 1
            bak.unlink(missing_ok=True)
    st["applied"] = keep
    _save_state(st)
    return done


def restore_all():
    st = _load_state()
    done = 0
    for item in st["applied"]:
        if item.get("bundle"):
            if _drop_override(item["bundle"]):
                done += 1
        else:
            bak = Path(item.get("backup") or "")
            path = Path(item.get("path") or "")
            if bak.is_file() and path.is_file():
                shutil.copy2(bak, path)
                done += 1
            bak.unlink(missing_ok=True)
    st["applied"] = []
    _save_state(st)
    return done


def overrides_summary():
    ov = _load_overrides()["bundles"]
    return [{"bundle": k, "md5": v.get("md5"), "size": v.get("size"),
             "weapon": v.get("weapon"), "skin": v.get("skin")} for k, v in ov.items()]
