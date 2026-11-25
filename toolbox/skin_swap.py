# -*- coding: utf-8 -*-
"""武器皮肤与建模替换后端：目录构建、规则读写（供战斗端注入载荷热读取）、单包预热。"""
import json
import os
import shutil
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import boot

DATA = boot.bootstrap()
import core

SJSF_ROOT = Path(r"D:\Ccode\sjsf")
SJSF_WEAPON_JSON = SJSF_ROOT / "_work" / "reverse" / "official" / "gamedata" / "shop_weapon.json"
SJSF_SKIN_JSON = SJSF_ROOT / "_work" / "reverse" / "official" / "gamedata" / "shop_skin.json"
SJSF_MANIFEST = SJSF_ROOT / "_manifest_cache.xml"

SWAP_DIR = Path(os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))) / "ssjj-toolbox"
SWAP_CONFIG = SWAP_DIR / "skin_swap.json"

SUBTYPE_NAMES = {
    0: "近战",
    1: "步枪",
    2: "狙击枪",
    3: "冲锋枪",
    4: "机枪",
    5: "霰弹枪",
    6: "手枪",
    7: "投掷",
    8: "特殊",
    9: "特殊",
}


def _catalog_candidates():
    cands = [Path(DATA) / "weapon_catalog.json"]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        cands.append(Path(meipass) / "weapon_catalog.json")
    cands.append(Path(__file__).resolve().parent.parent / "weapon_catalog.json")
    return cands


def catalog_path():
    for p in _catalog_candidates():
        if p.is_file():
            return p
    return Path(DATA) / "weapon_catalog.json"


def build_catalog_from_sjsf():
    """从 D:\\Ccode\\sjsf 完整提取全部官方武器与武器皮肤（不做 manifest 截断过滤，一把不漏）。"""
    if not (SJSF_WEAPON_JSON.is_file() and SJSF_SKIN_JSON.is_file()):
        return False, "未找到 D:\\Ccode\\sjsf 中的武器/皮肤数据文件", None

    sw = json.loads(SJSF_WEAPON_JSON.read_text(encoding="utf-8"))
    id_to_code = {}
    weapons_by_code = {}
    for k, v in sw.items():
        code = str(v.get("codeName") or "").strip()
        name = str(v.get("name") or "").strip()
        wid = str(v.get("id") or k)
        if not code or not name:
            continue
        id_to_code[wid] = code
        if code not in weapons_by_code:
            st = int(v.get("subType") or 0)
            weapons_by_code[code] = {
                "code": code,
                "name": name,
                "aliases": [],
                "id": int(wid) if wid.isdigit() else 0,
                "cat": SUBTYPE_NAMES.get(st, "特殊"),
                "subType": st,
            }
        else:
            entry = weapons_by_code[code]
            if name != entry["name"] and name not in entry["aliases"]:
                # 优先保留更简短的基础名作为主名（例如避免被“（乱斗）”顶掉）
                if ("（" in entry["name"] or "(" in entry["name"]) and ("（" not in name and "(" not in name):
                    entry["aliases"].append(entry["name"])
                    entry["name"] = name
                else:
                    entry["aliases"].append(name)

    ss = json.loads(SJSF_SKIN_JSON.read_text(encoding="utf-8"))
    skins = []
    seen_skin = set()
    for _k, v in ss.items():
        if v.get("skinType") != 1:
            continue
        code = str(v.get("codeName") or "").strip()
        name = str(v.get("name") or "").strip()
        if not code or not name or code in seen_skin:
            continue
        seen_skin.add(code)
        wids = [x.strip() for x in str(v.get("weaponId") or "").split("|") if x.strip()]
        wcodes = sorted({id_to_code[i] for i in wids if i in id_to_code})
        desc = str(v.get("description") or "").replace("\n", " ").strip()[:80]
        # 把皮肤描述中提到的别名（如“适用于屠魔/血魔”）同步加入对应武器搜索关键词
        for wc in wcodes:
            wobj = weapons_by_code.get(wc)
            if wobj and desc and desc not in wobj["aliases"]:
                wobj["aliases"].append(desc)
        skins.append({
            "code": code,
            "name": name,
            "weapons": wcodes,
            "desc": desc,
        })

    # 补充 manifest 中双阵营武器（如 awm_legend_ct / awm_legend_t），继承基础武器的名称与皮肤
    manifest_weapons = set()
    if SJSF_MANIFEST.is_file():
        try:
            tree = ET.parse(str(SJSF_MANIFEST))
            for el in tree.iter():
                n = (el.attrib.get("Name") or el.attrib.get("name") or "").lower()
                if n.startswith("weapon/"):
                    manifest_weapons.add(n.split("/", 1)[1].split("$")[0])
        except Exception:
            pass

    extra_weapons = {}
    for code, w in list(weapons_by_code.items()):
        for suf, label in (("_ct", "·警"), ("_t", "·匪")):
            sub_code = code + suf
            if sub_code.lower() in manifest_weapons and sub_code not in weapons_by_code:
                extra_weapons[sub_code] = {
                    "code": sub_code,
                    "name": w["name"] + label,
                    "aliases": list(w["aliases"]) + [w["name"], code],
                    "id": w["id"],
                    "cat": w["cat"],
                    "subType": w["subType"],
                }
                for s in skins:
                    if code in s["weapons"] and sub_code not in s["weapons"]:
                        s["weapons"].append(sub_code)
    weapons_by_code.update(extra_weapons)

    weapons = []
    for w in weapons_by_code.values():
        w["search"] = " ".join([w["name"], w["code"]] + w.pop("aliases", [])).lower()
        weapons.append(w)
    weapons.sort(key=lambda x: (x["subType"], x["name"]))
    skins.sort(key=lambda x: x["name"])
    cat = {"weapons": weapons, "skins": skins}
    out = Path(DATA) / "weapon_catalog.json"
    out.write_text(json.dumps(cat, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return True, "已全量同步 %d 把武器、%d 款武器皮肤" % (len(weapons), len(skins)), cat


_CATALOG_CACHE = None


def load_catalog(force=False):
    global _CATALOG_CACHE
    if _CATALOG_CACHE is not None and not force:
        return _CATALOG_CACHE
    p = catalog_path()
    if p.is_file():
        try:
            _CATALOG_CACHE = json.loads(p.read_text(encoding="utf-8"))
            return _CATALOG_CACHE
        except Exception:
            pass
    ok, _msg, cat = build_catalog_from_sjsf()
    if ok and cat:
        _CATALOG_CACHE = cat
        return cat
    return {"weapons": [], "skins": []}


def load_rules():
    default = {"enabled": True, "rules": []}
    if not SWAP_CONFIG.is_file():
        return default
    try:
        d = json.loads(SWAP_CONFIG.read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            return default
        rules = d.get("rules")
        if not isinstance(rules, list):
            rules = []
        return {"enabled": bool(d.get("enabled", True)), "rules": rules}
    except Exception:
        return default


def _rules_log(text):
    """规则文件的写入日志（排查「规则自己变空」这类问题用）。"""
    try:
        SWAP_DIR.mkdir(parents=True, exist_ok=True)
        with open(str(SWAP_DIR / "rules_guard.log"), "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text))
    except Exception:
        pass


def save_rules(data, allow_empty=False):
    SWAP_DIR.mkdir(parents=True, exist_ok=True)
    clean_rules = []
    for r in (data.get("rules") or []):
        src = str(r.get("src") or "").strip()
        model = str(r.get("model") or "").strip()
        skin = str(r.get("skin") or "").strip()
        if not src or (not model and not skin):
            continue
        rule = {
            "src": src,
            "src_name": str(r.get("src_name") or src),
            "model": model,
            "model_name": str(r.get("model_name") or model),
            "skin": skin,
            "skin_name": str(r.get("skin_name") or skin),
            "enabled": bool(r.get("enabled", True)),
        }
        # mode=static 表示这条规则来自「一键启动」的本地贴图替换（不注入）；
        # 缺省/其它值 = 注入类规则。UI 靠它区分标注，所以必须原样保留。
        if str(r.get("mode") or "") == "static":
            rule["mode"] = "static"
        clean_rules.append(rule)
    payload = {"enabled": bool(data.get("enabled", True)), "rules": clean_rules}
    # 保险 1：没有显式声明 allow_empty 时，绝不把「原来有规则的配置」写成空表。
    # 之前出现过规则莫名其妙变空（界面建页/竞态时的空 _rules_data 被写回），用户丢了配置。
    try:
        old_n = len(load_rules().get("rules") or [])
    except Exception:
        old_n = 0
    if not clean_rules and old_n and not allow_empty:
        _rules_log("拒绝写入空规则：磁盘上原有 %d 条，caller 未声明 allow_empty。data keys=%s"
                   % (old_n, sorted(list((data or {}).keys()))))
        return False, "拒绝写入空规则（原有 %d 条），这次保存已忽略" % old_n
    # 保险 2：覆盖前留一份上一版，写坏了还能捞回来。
    try:
        if SWAP_CONFIG.is_file():
            shutil.copy2(str(SWAP_CONFIG), str(SWAP_DIR / (SWAP_CONFIG.name + ".prev")))
    except Exception:
        pass
    tmp = str(SWAP_CONFIG) + ".tmp"
    Path(tmp).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, str(SWAP_CONFIG))
    return True, "已保存 %d 条替换规则（战斗端自动热加载）" % len(clean_rules)


def ensure_bundles_cached(codes, emit=None):
    """确保目标武器/皮肤的 AssetBundle 已预下载到客户端 Preload 缓存，避免进局隐形。"""
    wants = [c.strip().lower() for c in (codes or []) if c and c.strip()]
    if not wants:
        return True, "无需预下载"
    import ssjj_boost
    try:
        items, _src = ssjj_boost.load_manifest(core.DEFAULT_BASE, core.DEFAULT_PLATFORM, refresh=False)
    except Exception as e:
        return False, "读取清单失败：%r" % (e,)

    selected = []
    for it in items:
        low = it["name"].lower()
        for w in wants:
            if (low == "weapon/" + w or low.startswith("weapon/" + w + "$")
                    or low == "weaponskin/" + w or low.startswith("weaponskin/" + w + "$")):
                selected.append(it)
                break
    if not selected:
        return True, "未在清单中匹配到额外资源包"

    preload_dir = core.default_preload_dir()
    ok = skip = fail = 0
    for it in selected:
        st, _it, info = core.download_one(it, preload_dir, core.DEFAULT_BASE,
                                          core.DEFAULT_PLATFORM, False, True)
        if st == "ok":
            ok += 1
        elif st == "skip":
            skip += 1
        else:
            fail += 1
            if emit:
                emit("[!] 下载失败 %s: %s" % (it["name"], info))
    return fail == 0, "资源校验完成：新下载 %d / 已缓存 %d / 失败 %d" % (ok, skip, fail)
