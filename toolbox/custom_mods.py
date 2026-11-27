# -*- coding: utf-8 -*-
"""第三方自定义 Mod 管理后端（零注入通道）。

本地目录结构：
  %LOCALAPPDATA%\\ssjj-toolbox\\custom_mods\\<mod_id>\\mod.json

支持两类 Mod：
  1. texture_pack：导出武器 AssetBundle 内的 Texture2D 为 PNG，用户修改后二次导入，
     按贴图名精确匹配回写为 static_mods/custom_<mod_id>.bundle；
  2. assetbundle：直接导入第三方构建的 Unity AssetBundle 文件（推荐 Unity 2019.4.x）。

所有启用的 Mod 经由 rebuild_overrides() 增量合并写入
  %LOCALAPPDATA%\\ssjj-toolbox\\bundle_overrides.json
（仅管理 source == "custom_mod" 的条目，不破坏 skin_pack 的原有覆盖项），
再由 local_cdn.py 在清单与资源下载阶段透明下发。
"""
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import zipfile
import zlib
from pathlib import Path

import boot
import skin_pack

DATA = Path(boot.bootstrap())
STATE_DIR = Path(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")) / "ssjj-toolbox"
CUSTOM_MODS_DIR = STATE_DIR / "custom_mods"
OVERRIDES = STATE_DIR / "bundle_overrides.json"
MOD_OUT_DIR = DATA / "static_mods"
PRELOAD = Path(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")) / "Wooduan" / "Preload"

_INVALID_FS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_SAFE_ID_RE = re.compile(r"[^a-zA-Z0-9_-]+")


def _safe_filename(name: str, fallback: str = "tex") -> str:
    cleaned = _INVALID_FS_RE.sub("_", str(name or "").strip()).strip(". ")
    return cleaned or fallback


def _sanitize_mod_id(raw: str, prefix: str = "mod") -> str:
    cleaned = _SAFE_ID_RE.sub("_", str(raw or "").strip()).strip("_-")[:64]
    if cleaned:
        return cleaned.lower()
    return "%s_%x" % (prefix, int(time.time() * 1000) & 0xFFFFFFFF)


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _normalize_bundle_name(raw: str) -> str:
    s = str(raw or "").strip().replace("\\", "/").lstrip("/")
    return s.lower()


def _resolve_target_bundle(weapon_code_or_bundle: str):
    """定位目标武器/皮肤 AssetBundle：返回 (bundle_name, file_path, error_msg)。"""
    raw = _normalize_bundle_name(weapon_code_or_bundle)
    if not raw:
        return None, None, "未指定武器代号或目标资源包名"

    idx = skin_pack.index() or {}
    # 构造大小写不敏感索引映射
    lower_map = {k.lower(): (k, v) for k, v in idx.items()}

    candidates = [raw] if "/" in raw else ["weapon/%s" % raw, "weaponskin/%s" % raw]
    matched_name = None
    matched_info = None
    for cand in candidates:
        hit = lower_map.get(cand)
        if hit:
            matched_name, matched_info = hit
            break

    if not matched_name or not matched_info:
        return None, None, "未在资源清单中找到目标包：%s" % weapon_code_or_bundle

    md5 = str(matched_info.get("md5") or "").strip()
    if not md5:
        return matched_name, None, "资源清单中缺少 %s 的 md5 信息" % matched_name

    # 优先使用本模块 PRELOAD（方便测试 monkeypatch），同时兼容 skin_pack.bundle_path
    fpath = PRELOAD / md5 / "data"
    if not fpath.is_file():
        alt = skin_pack.bundle_path(matched_name)
        if alt and alt.is_file():
            fpath = alt
    if not fpath.is_file():
        return (
            matched_name,
            fpath,
            "本地 Preload 缓存中还没有 %s（请先在游戏内使用一次或在资源库预下载）" % matched_name,
        )
    return matched_name, fpath, ""


def _inspect_assetbundle(path: Path) -> dict:
    """校验 UnityFS 头并读取内嵌 Unity 版本字符串。"""
    try:
        with open(path, "rb") as fh:
            head = fh.read(512)
    except OSError as exc:
        return {"valid": False, "signature": "", "unity_version": "", "warning": "无法读取文件：%s" % exc}

    if not head.startswith(b"UnityFS\x00"):
        sig = head.split(b"\x00", 1)[0].decode("ascii", "replace")[:24]
        return {
            "valid": False,
            "signature": sig,
            "unity_version": "",
            "warning": "不是有效的 UnityFS AssetBundle 文件（文件头：%r）" % (sig,),
        }

    # UnityFS 头格式: "UnityFS\0" + uint32(version) + unity_version\0 + unity_revision\0
    pos = 8 + 4
    parts = []
    for _ in range(2):
        nul = head.find(b"\x00", pos)
        if nul == -1:
            break
        parts.append(head[pos:nul].decode("ascii", "replace").strip())
        pos = nul + 1

    unity_ver = (parts[1] if len(parts) > 1 and parts[1] else (parts[0] if parts else ""))
    warning = ""
    if "2019.4" not in unity_ver:
        warning = (
            "该 AssetBundle 内嵌 Unity 版本为 %s（推荐使用 Unity 2019.4.x 构建，"
            "版本不一致可能导致游戏内加载失败或异常）" % (unity_ver or "未知")
        )
    return {
        "valid": True,
        "signature": "UnityFS",
        "unity_version": unity_ver,
        "warning": warning,
    }


def _safe_extract_zip(zip_path: Path, dst_dir: Path) -> None:
    """安全解压 ZIP，防止路径穿越（Zip Slip）。"""
    dst_root = dst_dir.resolve()
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            name = info.filename
            if not name:
                continue
            # 拒绝绝对路径、盘符、父目录跳转
            drive, _ = os.path.splitdrive(name)
            if drive or name.startswith(("/", "\\")):
                raise ValueError("压缩包包含非法绝对路径：%s" % name)
            target = (dst_root / name).resolve()
            if target != dst_root and dst_root not in target.parents:
                raise ValueError("压缩包包含越界路径：%s" % name)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info, "r") as src_f, open(target, "wb") as out_f:
                    shutil.copyfileobj(src_f, out_f)


def _safe_rel_path(base_dir: Path, rel: str) -> Path:
    """校验相对路径不越出 base_dir。"""
    base_root = base_dir.resolve()
    rel_s = str(rel or "").strip()
    if not rel_s:
        raise ValueError("文件路径为空")
    drive, _ = os.path.splitdrive(rel_s)
    if drive or rel_s.startswith(("/", "\\")):
        raise ValueError("不允许使用绝对路径：%s" % rel_s)
    p = (base_root / rel_s).resolve()
    if p != base_root and base_root not in p.parents:
        raise ValueError("路径越界：%s" % rel_s)
    return p


def _load_overrides() -> dict:
    if OVERRIDES.is_file():
        try:
            d = json.loads(OVERRIDES.read_text(encoding="utf-8"))
            if isinstance(d, dict) and isinstance(d.get("bundles"), dict):
                return d
        except Exception:
            pass
    return {"bundles": {}}


def _save_overrides(d: dict) -> None:
    _atomic_write_json(OVERRIDES, d)


def _mod_dir_for(mod_id: str) -> Path:
    mid = str(mod_id or "").strip()
    if not mid or "/" in mid or "\\" in mid or mid in (".", ".."):
        raise ValueError("非法的 mod_id：%r" % (mod_id,))
    base = CUSTOM_MODS_DIR.resolve()
    p = (base / mid).resolve()
    if base not in p.parents:
        raise ValueError("非法的 mod_id：%r" % (mod_id,))
    return p


def export_weapon_textures(weapon_code: str, out_dir: str) -> dict:
    """导出指定武器/皮肤 AssetBundle 的全部 Texture2D 为 PNG，并生成可二次导入的 mod.json 模板。"""
    out_path = Path(out_dir)
    bundle_name, bundle_file, err = _resolve_target_bundle(weapon_code)
    if err or not bundle_file:
        return {
            "ok": False,
            "count": 0,
            "out_dir": str(out_path),
            "mod_json": "",
            "files": [],
            "error": err or "未找到目标资源包",
        }

    try:
        import UnityPy
    except Exception as exc:  # pragma: no cover
        return {
            "ok": False,
            "count": 0,
            "out_dir": str(out_path),
            "mod_json": "",
            "files": [],
            "error": "缺少 UnityPy 依赖：%s" % exc,
        }

    try:
        env = UnityPy.load(bundle_file.read_bytes())
    except Exception as exc:
        return {
            "ok": False,
            "count": 0,
            "out_dir": str(out_path),
            "mod_json": "",
            "files": [],
            "error": "解析资源包失败：%r" % (exc,),
        }

    out_path.mkdir(parents=True, exist_ok=True)
    textures = {}
    files = []
    used_fnames = set()

    for obj in env.objects:
        if obj.type.name != "Texture2D":
            continue
        try:
            data = obj.read()
            img = data.image
        except Exception:
            continue
        tex_name = str(getattr(data, "m_Name", None) or getattr(data, "name", None) or "").strip()
        if not tex_name:
            tex_name = "tex_%s" % getattr(obj, "path_id", len(files))

        base_fname = _safe_filename(tex_name, fallback="tex_%d" % len(files))
        fname = base_fname + ".png"
        idx_num = 1
        while fname.lower() in used_fnames:
            fname = "%s_%d.png" % (base_fname, idx_num)
            idx_num += 1
        used_fnames.add(fname.lower())

        dst_file = out_path / fname
        img.save(str(dst_file), format="PNG")
        textures[tex_name] = fname
        files.append(str(dst_file))

    if not files:
        return {
            "ok": False,
            "count": 0,
            "out_dir": str(out_path),
            "mod_json": "",
            "files": [],
            "error": "该资源包中未找到可导出的 Texture2D 贴图",
        }

    short_code = bundle_name.split("/")[-1]
    mod_meta = {
        "id": _sanitize_mod_id("tex_%s_%x" % (short_code, int(time.time()) & 0xFFFF), prefix="tex"),
        "name": "%s 自定义贴图" % short_code,
        "type": "texture_pack",
        "target_bundle": bundle_name,
        "enabled": True,
        "textures": textures,
    }
    mod_json_path = out_path / "mod.json"
    _atomic_write_json(mod_json_path, mod_meta)

    return {
        "ok": True,
        "count": len(files),
        "out_dir": str(out_path),
        "mod_json": str(mod_json_path),
        "target_bundle": bundle_name,
        "files": files,
        "error": "",
    }


def _find_mod_root(root_dir: Path) -> Path:
    if (root_dir / "mod.json").is_file():
        return root_dir
    subdirs = [p for p in root_dir.iterdir() if p.is_dir()]
    if len(subdirs) == 1 and (subdirs[0] / "mod.json").is_file():
        return subdirs[0]
    raise ValueError("未在目录或压缩包中找到 mod.json")


def _install_mod_from_dir(mod_root: Path, target_bundle: str = "", mod_name: str = "") -> dict:
    meta_path = mod_root / "mod.json"
    if not meta_path.is_file():
        return {"ok": False, "error": "目录中缺少 mod.json"}

    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if not isinstance(meta, dict):
            return {"ok": False, "error": "mod.json 格式无效（需要 JSON 对象）"}
    except Exception as exc:
        return {"ok": False, "error": "解析 mod.json 失败：%s" % exc}

    mod_type = str(meta.get("type") or "").strip().lower()
    if mod_type not in ("texture_pack", "assetbundle"):
        return {"ok": False, "error": "不支持的 Mod 类型：%r（仅支持 texture_pack / assetbundle）" % (mod_type,)}

    tb = _normalize_bundle_name(target_bundle or meta.get("target_bundle") or "")
    if not tb:
        return {"ok": False, "error": "mod.json 缺少 target_bundle 字段"}
    if "/" not in tb:
        resolved, _, _ = _resolve_target_bundle(tb)
        tb = resolved or ("weapon/" + tb)

    warning = ""
    unity_ver = str(meta.get("unity_version") or "")

    if mod_type == "texture_pack":
        textures = meta.get("textures")
        if not isinstance(textures, dict) or not textures:
            return {"ok": False, "error": "texture_pack 的 mod.json 缺少非空 textures 映射"}
        valid_tex = 0
        for tex_name, rel_file in textures.items():
            try:
                fp = _safe_rel_path(mod_root, str(rel_file))
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            if fp.is_file():
                valid_tex += 1
        if valid_tex == 0:
            return {"ok": False, "error": "textures 中声明的贴图文件均不存在"}
    else:
        bundle_rel = str(meta.get("bundle_file") or "").strip()
        if not bundle_rel:
            # 兜底查找目录下唯一 .bundle 文件
            bundles = list(mod_root.glob("*.bundle"))
            if len(bundles) == 1:
                bundle_rel = bundles[0].name
            else:
                return {"ok": False, "error": "assetbundle 的 mod.json 缺少 bundle_file 字段"}
        try:
            bfp = _safe_rel_path(mod_root, bundle_rel)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if not bfp.is_file():
            return {"ok": False, "error": "找不到 AssetBundle 文件：%s" % bundle_rel}
        insp = _inspect_assetbundle(bfp)
        if not insp["valid"]:
            return {"ok": False, "error": insp["warning"]}
        unity_ver = insp["unity_version"]
        warning = insp["warning"]
        meta["bundle_file"] = bundle_rel

    mod_id = _sanitize_mod_id(meta.get("id") or mod_name or mod_root.name, prefix=mod_type[:3])
    meta["id"] = mod_id
    meta["name"] = str(mod_name or meta.get("name") or mod_id).strip()
    meta["type"] = mod_type
    meta["target_bundle"] = tb
    meta["enabled"] = True
    meta["imported_at"] = int(time.time())
    if unity_ver:
        meta["unity_version"] = unity_ver
    if warning:
        meta["warning"] = warning
    else:
        meta.pop("warning", None)

    CUSTOM_MODS_DIR.mkdir(parents=True, exist_ok=True)
    dst_dir = _mod_dir_for(mod_id)
    if dst_dir.exists():
        shutil.rmtree(dst_dir)
    shutil.copytree(mod_root, dst_dir)
    _atomic_write_json(dst_dir / "mod.json", meta)

    rb = rebuild_overrides()
    return {
        "ok": True,
        "mod": meta,
        "warning": warning,
        "rebuild": rb,
        "error": "",
    }


def import_mod(src_path: str, target_bundle: str = "", mod_name: str = "") -> dict:
    """导入第三方自定义 Mod：支持 mod.json 目录、.zip 压缩包或单个 Unity AssetBundle 文件。"""
    src = Path(str(src_path or "").strip())
    if not src_path or not src.exists():
        return {"ok": False, "error": "导入路径不存在：%s" % src_path}

    # 1. 包含 mod.json 的目录
    if src.is_dir():
        try:
            mod_root = _find_mod_root(src)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return _install_mod_from_dir(mod_root, target_bundle=target_bundle, mod_name=mod_name)

    # 2. 包含 mod.json 的 .zip 压缩包（安全解压防路径穿越）
    if src.is_file() and src.suffix.lower() == ".zip":
        tmp_dir = Path(tempfile.mkdtemp(prefix="ssjj_mod_zip_"))
        try:
            _safe_extract_zip(src, tmp_dir)
            mod_root = _find_mod_root(tmp_dir)
            return _install_mod_from_dir(mod_root, target_bundle=target_bundle, mod_name=mod_name)
        except (zipfile.BadZipFile, ValueError) as exc:
            return {"ok": False, "error": "ZIP 导入失败：%s" % exc}
        except Exception as exc:
            return {"ok": False, "error": "ZIP 解压导入异常：%r" % (exc,)}
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # 3. 单个第三方 Unity AssetBundle 文件
    if src.is_file():
        tb = _normalize_bundle_name(target_bundle)
        if not tb:
            return {"ok": False, "error": "导入单个 AssetBundle 文件时必须指定目标资源包（target_bundle）"}
        if "/" not in tb:
            resolved, _, _ = _resolve_target_bundle(tb)
            tb = resolved or ("weapon/" + tb)

        insp = _inspect_assetbundle(src)
        if not insp["valid"]:
            return {"ok": False, "error": insp["warning"]}

        mod_id = _sanitize_mod_id("ab_%s_%x" % (src.stem, int(time.time()) & 0xFFFF), prefix="ab")
        bundle_fname = _safe_filename(src.name, fallback="custom.bundle")
        if not bundle_fname.lower().endswith(".bundle"):
            bundle_fname += ".bundle"

        CUSTOM_MODS_DIR.mkdir(parents=True, exist_ok=True)
        dst_dir = _mod_dir_for(mod_id)
        if dst_dir.exists():
            shutil.rmtree(dst_dir)
        dst_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst_dir / bundle_fname)

        meta = {
            "id": mod_id,
            "name": str(mod_name or src.stem).strip() or mod_id,
            "type": "assetbundle",
            "target_bundle": tb,
            "bundle_file": bundle_fname,
            "unity_version": insp["unity_version"],
            "enabled": True,
            "imported_at": int(time.time()),
        }
        if insp["warning"]:
            meta["warning"] = insp["warning"]
        _atomic_write_json(dst_dir / "mod.json", meta)

        rb = rebuild_overrides()
        return {
            "ok": True,
            "mod": meta,
            "warning": insp["warning"],
            "rebuild": rb,
            "error": "",
        }

    return {"ok": False, "error": "不支持的导入路径类型：%s" % src_path}


def list_mods() -> list:
    """列出本地已导入的全部自定义 Mod。"""
    if not CUSTOM_MODS_DIR.is_dir():
        return []
    out = []
    for entry in sorted(CUSTOM_MODS_DIR.iterdir(), key=lambda p: p.name.lower()):
        if not entry.is_dir():
            continue
        meta_path = entry / "mod.json"
        if not meta_path.is_file():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if not isinstance(meta, dict):
                continue
        except Exception:
            continue
        item = dict(meta)
        item.setdefault("id", entry.name)
        item.setdefault("name", item["id"])
        item.setdefault("type", "texture_pack")
        item.setdefault("target_bundle", "")
        item["enabled"] = bool(item.get("enabled", True))
        item["mod_dir"] = str(entry)
        if item["type"] == "texture_pack":
            item["texture_count"] = len(item.get("textures") or {})
        out.append(item)
    out.sort(key=lambda x: (-int(x.get("imported_at") or 0), str(x.get("id") or "")))
    return out


def set_mod_enabled(mod_id: str, enabled: bool) -> dict:
    """启用或禁用指定自定义 Mod，并重新生成覆盖表。"""
    try:
        mod_dir = _mod_dir_for(mod_id)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    meta_path = mod_dir / "mod.json"
    if not meta_path.is_file():
        return {"ok": False, "error": "找不到指定 Mod：%s" % mod_id}

    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "error": "读取 mod.json 失败：%s" % exc}

    meta["enabled"] = bool(enabled)
    _atomic_write_json(meta_path, meta)
    rb = rebuild_overrides()
    return {"ok": True, "mod": meta, "rebuild": rb, "error": ""}


def delete_mod(mod_id: str) -> dict:
    """删除指定自定义 Mod 及其编译产物，并重新生成覆盖表。"""
    try:
        mod_dir = _mod_dir_for(mod_id)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    if not mod_dir.exists():
        return {"ok": False, "error": "找不到指定 Mod：%s" % mod_id}

    try:
        shutil.rmtree(mod_dir)
    except Exception as exc:
        return {"ok": False, "error": "删除 Mod 目录失败：%s" % exc}

    built = MOD_OUT_DIR / ("custom_%s.bundle" % mod_id)
    try:
        built.unlink(missing_ok=True)
    except Exception:
        pass

    rb = rebuild_overrides()
    return {"ok": True, "mod_id": mod_id, "rebuild": rb, "error": ""}


def _build_texture_pack_bundle(mod: dict, mod_dir: Path, orig_bundle_path: Path) -> tuple:
    """把 texture_pack 中的 PNG/JPG 按贴图名精确替换进原始 bundle，写出至 static_mods/custom_<id>.bundle。"""
    import UnityPy
    from PIL import Image

    env = UnityPy.load(orig_bundle_path.read_bytes())
    textures_map = mod.get("textures") or {}
    if not isinstance(textures_map, dict) or not textures_map:
        return None, 0, "textures 映射为空"

    resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.BICUBIC)
    replaced = 0

    for obj in env.objects:
        if obj.type.name != "Texture2D":
            continue
        data = obj.read()
        tex_name = str(getattr(data, "m_Name", None) or getattr(data, "name", None) or "").strip()
        if tex_name not in textures_map:
            continue
        rel_img = str(textures_map[tex_name] or "").strip()
        try:
            img_path = _safe_rel_path(mod_dir, rel_img)
        except ValueError:
            continue
        if not img_path.is_file():
            continue

        with Image.open(img_path) as im:
            img = im.convert("RGBA")
            w, h = int(data.m_Width), int(data.m_Height)
            if w > 0 and h > 0 and img.size != (w, h):
                img = img.resize((w, h), resample)
            data.set_image(
                img,
                target_format=int(data.m_TextureFormat),
                mipmap_count=int(getattr(data, "m_MipCount", 1) or 1),
            )
            data.save()
            replaced += 1

    if replaced == 0:
        return None, 0, "没有任何贴图名匹配到原始资源包内的 Texture2D"

    blob = env.file.save()
    if isinstance(blob, dict):
        blob = list(blob.values())[0]

    MOD_OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_bundle = MOD_OUT_DIR / ("custom_%s.bundle" % mod["id"])
    out_bundle.write_bytes(blob)
    return out_bundle, replaced, ""


def rebuild_overrides() -> dict:
    """增量重建 bundle_overrides.json 中 source == 'custom_mod' 的条目，不破坏原有 skin_pack 覆盖项。"""
    ov = _load_overrides()
    old_bundles = ov.get("bundles") or {}

    # 保留全部非 custom_mod 条目（如 skin_pack 写入的覆盖项）
    kept_bundles = {}
    old_custom_paths = set()
    for bname, info in old_bundles.items():
        if isinstance(info, dict) and info.get("source") == "custom_mod":
            p = info.get("path")
            if p:
                old_custom_paths.add(str(p))
        else:
            kept_bundles[bname] = info

    new_custom = {}
    active_custom_paths = set()
    errors = []
    idx = skin_pack.index() or {}

    for mod in list_mods():
        if not mod.get("enabled"):
            continue
        mod_id = str(mod.get("id") or "")
        mod_type = str(mod.get("type") or "").lower()
        mod_dir = Path(mod.get("mod_dir") or (CUSTOM_MODS_DIR / mod_id))
        target_bundle = _normalize_bundle_name(mod.get("target_bundle") or "")
        if not target_bundle:
            errors.append("%s: 缺少 target_bundle" % mod_id)
            continue

        out_bundle = None
        if mod_type == "texture_pack":
            resolved_name, orig_path, err = _resolve_target_bundle(target_bundle)
            if err or not orig_path:
                errors.append("%s: %s" % (mod_id, err or "找不到原始资源包"))
                continue
            target_bundle = resolved_name
            try:
                out_bundle, _n, berr = _build_texture_pack_bundle(mod, mod_dir, orig_path)
            except Exception as exc:
                errors.append("%s: 打包贴图失败：%r" % (mod_id, exc))
                continue
            if berr or not out_bundle:
                errors.append("%s: %s" % (mod_id, berr or "未生成 bundle"))
                continue
        elif mod_type == "assetbundle":
            bundle_rel = str(mod.get("bundle_file") or "").strip()
            try:
                bfp = _safe_rel_path(mod_dir, bundle_rel)
            except ValueError as exc:
                errors.append("%s: %s" % (mod_id, exc))
                continue
            if not bfp.is_file():
                errors.append("%s: 找不到自定义 bundle 文件 %s" % (mod_id, bundle_rel))
                continue
            out_bundle = bfp
        else:
            errors.append("%s: 未知 Mod 类型 %r" % (mod_id, mod_type))
            continue

        try:
            blob = out_bundle.read_bytes()
        except OSError as exc:
            errors.append("%s: 读取生成包失败：%s" % (mod_id, exc))
            continue

        md5 = hashlib.md5(blob).hexdigest()
        crc = zlib.crc32(blob) & 0xFFFFFFFF
        orig_info = idx.get(target_bundle) or {}
        new_custom[target_bundle] = {
            "md5": md5,
            "crc": crc,
            "size": len(blob),
            "path": str(out_bundle),
            "orig_md5": orig_info.get("md5"),
            "source": "custom_mod",
            "mod_id": mod_id,
            "mod_name": mod.get("name") or mod_id,
            "mod_type": mod_type,
            "ts": int(time.time()),
        }
        active_custom_paths.add(str(out_bundle))

    # 清理不再使用的旧 custom_*.bundle 编译产物（仅清理 MOD_OUT_DIR 下的文件，绝不删 custom_mods 里的源文件）
    mod_out_root = MOD_OUT_DIR.resolve()
    for old_p in old_custom_paths - active_custom_paths:
        try:
            op = Path(old_p).resolve()
            if mod_out_root in op.parents and op.name.startswith("custom_") and op.suffix == ".bundle":
                op.unlink(missing_ok=True)
        except Exception:
            pass

    merged = dict(kept_bundles)
    merged.update(new_custom)
    ov["bundles"] = merged
    _save_overrides(ov)

    return {
        "ok": len(errors) == 0,
        "applied": len(new_custom),
        "total_overrides": len(merged),
        "errors": errors,
    }
