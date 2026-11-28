"""Prepare a version-checked, standalone experimental streamer lobby.

This is deliberately a preparation API: it does not update paras.dat, start
the game, or claim the experimental AIR code has passed runtime testing.
"""
import bz2
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

import swf_fps


_BASE_SHA256 = "eccd118a33474dc495104d416493b21b42e8ab69c141d062b5097b60678adef3"
_TEMPLATE_SHA256 = "e808cd93d70edf95eabbd2f19e67e6cfc425cca81bcb40ad0cf7ae6feb0fb0ed"
_PLACEHOLDER = b"C:\\__SSJJ_STREAMER_CONFIG__|" + b"~" * 512
_DELTA = Path(__file__).resolve().parent / "data" / "lobby_streamer_eccd118a.bsdiff"


def _number(data):
    if len(data) != 8:
        raise ValueError("补丁整数不完整")
    value = int.from_bytes(data, "little") & ((1 << 63) - 1)
    return -value if data[7] & 0x80 else value


def _apply_delta(old, patch):
    """Apply BSDIFF40 data, including ranges outside the original file."""
    if len(patch) < 32 or patch[:8] != b"BSDIFF40":
        raise ValueError("大厅实验补丁格式错误")
    control_size = _number(patch[8:16])
    diff_size = _number(patch[16:24])
    new_size = _number(patch[24:32])
    if (min(control_size, diff_size, new_size) < 0 or new_size > 64 * 1024 * 1024
            or 32 + control_size + diff_size > len(patch)):
        raise ValueError("大厅实验补丁长度错误")
    try:
        control = bz2.decompress(patch[32:32 + control_size])
        diff = bz2.decompress(patch[32 + control_size:32 + control_size + diff_size])
        extra = bz2.decompress(patch[32 + control_size + diff_size:])
    except (OSError, EOFError) as exc:
        raise ValueError("大厅实验补丁解压失败") from exc
    out = bytearray()
    oldpos = cpos = dpos = epos = 0
    while len(out) < new_size:
        if cpos + 24 > len(control):
            raise ValueError("大厅实验补丁控制数据不完整")
        x = _number(control[cpos:cpos + 8])
        y = _number(control[cpos + 8:cpos + 16])
        z = _number(control[cpos + 16:cpos + 24])
        cpos += 24
        if (x < 0 or y < 0 or x + y == 0 or len(out) + x + y > new_size
                or dpos + x > len(diff) or epos + y > len(extra)):
            raise ValueError("大厅实验补丁控制数据错误")
        part = diff[dpos:dpos + x]
        if oldpos >= 0 and oldpos + x <= len(old) and not any(part):
            out.extend(old[oldpos:oldpos + x])
        else:
            block = bytearray(part)
            start, end = max(0, -oldpos), min(x, len(old) - oldpos)
            for i in range(start, max(start, end)):
                block[i] = (block[i] + old[oldpos + i]) & 255
            out.extend(block)
        out.extend(extra[epos:epos + y])
        dpos += x
        epos += y
        oldpos += x + z
    return bytes(out)


def prepare_variant(source, output_dir, config_path):
    """Build an isolated variant for the known lobby; return its path."""
    source, output_dir = Path(source), Path(output_dir)
    config_path = Path(config_path)
    if not config_path.is_absolute():
        raise ValueError("直播配置路径必须为绝对路径")
    config_bytes = str(config_path).encode("utf-8")
    if b"|" in config_bytes or b"\x00" in config_bytes or len(config_bytes) >= len(_PLACEHOLDER):
        raise ValueError("直播配置路径过长或包含不支持的字符")

    blob = source.read_bytes()
    body, signature = swf_fps.decode(blob)
    if hashlib.sha256(body).hexdigest() != _BASE_SHA256:
        raise ValueError("大厅版本不匹配，未生成直播实验版")
    template = _apply_delta(body, _DELTA.read_bytes())
    if hashlib.sha256(template).hexdigest() != _TEMPLATE_SHA256:
        raise ValueError("直播实验补丁校验失败")
    if template.count(_PLACEHOLDER) != 1:
        raise ValueError("直播配置入口校验失败")
    replacement = config_bytes + b"|" + b"~" * (len(_PLACEHOLDER) - len(config_bytes) - 1)
    patched = template.replace(_PLACEHOLDER, replacement, 1)
    props = blob[12:17] if signature == "ZWS" else None
    result = swf_fps.encode(patched, signature.encode(), blob[3], props)
    checked, _ = swf_fps.decode(result)
    if checked != patched:
        raise ValueError("大厅实验文件校验失败")
    name = "Lobby_%s.swf" % hashlib.md5(result).hexdigest()
    destination = output_dir / name
    if destination.resolve() == source.resolve():
        raise ValueError("实验文件不能覆盖源大厅")
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        with destination.open("xb") as handle:
            handle.write(result)
    except FileExistsError:
        if destination.read_bytes() != result:
            raise ValueError("同名大厅文件内容不一致")
    return destination


def _read_json(path):
    path = Path(path)
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _write_json(path, data, bom=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".streamer-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8-sig" if bom else "utf-8",
                       newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def install(paras_path, cache_dir, manifest_path, config_path):
    """Prepare an experimental lobby without changing the game's parameters."""
    paras_path, cache_dir, manifest_path = map(Path, (paras_path, cache_dir, manifest_path))
    paras = _read_json(paras_path)
    current = paras.get("lobbyFileName")
    if not isinstance(current, str) or not current or current != os.path.basename(current):
        raise ValueError("paras.dat 没有有效的大厅文件名")
    if manifest_path.exists():
        manifest = _read_json(manifest_path)
        if current in (manifest.get("variant"), manifest.get("original"), manifest.get("previous_variant")):
            if manifest.get("config_path") != str(Path(config_path)):
                raise ValueError("实验大厅使用的配置路径已变化，请先恢复大厅")
            original_name = manifest.get("original")
            if not isinstance(original_name, str) or original_name != os.path.basename(original_name):
                raise ValueError("实验大厅原文件名无效")
            if manifest.get("template_sha256") != _TEMPLATE_SHA256:
                variant = prepare_variant(cache_dir / original_name, cache_dir, config_path)
                if (_read_json(paras_path).get("lobbyFileName") != current
                        or _read_json(manifest_path) != manifest):
                    raise ValueError("构建期间大厅配置发生变化，请重试")
                new_manifest = dict(manifest, variant=variant.name, template_sha256=_TEMPLATE_SHA256)
                if current != original_name:
                    new_manifest["previous_variant"] = current
                _write_json(manifest_path, new_manifest)
                return {"original": original_name, "variant": variant.name, "already_active": False}
            variant_name = manifest["variant"]
            if not re.fullmatch(r"Lobby_[0-9a-f]{32}\.swf", variant_name):
                raise ValueError("实验大厅文件名无效")
            variant_file = cache_dir / variant_name
            expected = "Lobby_%s.swf" % hashlib.md5(variant_file.read_bytes()).hexdigest()
            if variant_name != expected:
                raise ValueError("实验大厅文件校验失败，请先恢复大厅")
            return {"original": manifest["original"], "variant": variant_name, "already_active": True}
        raise ValueError("检测到未恢复的实验大厅记录，请先恢复大厅")
    original = cache_dir / current
    variant = prepare_variant(original, cache_dir, config_path)
    if _read_json(paras_path).get("lobbyFileName") != current:
        raise ValueError("构建期间大厅配置发生变化，请重试")
    manifest = {"original": current, "variant": variant.name, "config_path": str(Path(config_path)),
                "template_sha256": _TEMPLATE_SHA256}
    _write_json(manifest_path, manifest)
    return {"original": current, "variant": variant.name, "already_active": False}


_BOOTSTRAP_PATH = re.compile(r"^/s[0-9]+/lobby_truly\.php$", re.IGNORECASE)
_LOBBY_NAME = re.compile(r"^Lobby_[0-9a-f]{32}\.swf$")
_BOOTSTRAP_FIELD = re.compile(
    rb"""(?i)(?:\b|%22)lobbyFileName(?:\\?["']|%22)?\s*"""
    rb"""(?::|=|%3a)\s*(?:\\?["']|%22)(Lobby_[0-9a-f]{32}\.swf)"""
)


def rewrite_bootstrap(path, body, headers, manifest_path, cache_dir, config_path):
    """Replace one unambiguous lobby filename in the login page, or leave it intact."""
    if not _BOOTSTRAP_PATH.fullmatch(path.split("?", 1)[0]):
        return body, "not-target"
    try:
        cfg = _read_json(config_path)
        if not cfg.get("enabled") or not cfg.get("lobby_overlay", True):
            return body, "disabled"
        if any(k.lower() == "content-encoding" and v.lower().strip() not in ("", "identity")
               for k, v in headers):
            return body, "encoded-response"
        manifest = _read_json(manifest_path)
        original, variant = manifest["original"], manifest["variant"]
        if (not isinstance(original, str) or not _LOBBY_NAME.fullmatch(original)
                or not isinstance(variant, str) or not _LOBBY_NAME.fullmatch(variant)
                or manifest.get("config_path") != str(Path(config_path))):
            return body, "invalid-record"
        data = (Path(cache_dir) / variant).read_bytes()
        if hashlib.md5(data).hexdigest() != variant[6:-4]:
            return body, "invalid-variant"
        matches = list(_BOOTSTRAP_FIELD.finditer(body))
        if len(matches) != 1:
            return body, "field-count-%d" % len(matches)
        if matches[0].group(1).decode("ascii") != original:
            return body, "unexpected-lobby"
        start, end = matches[0].span(1)
        return body[:start] + variant.encode("ascii") + body[end:], "rewritten"
    except (OSError, ValueError, TypeError, KeyError):
        return body, "unavailable"


def restore(paras_path, manifest_path):
    """Restore only if paras.dat still names the experiment we installed."""
    paras_path, manifest_path = Path(paras_path), Path(manifest_path)
    if not manifest_path.exists():
        return False
    manifest = _read_json(manifest_path)
    original, variant = manifest.get("original"), manifest.get("variant")
    if not all(isinstance(x, str) and x and x == os.path.basename(x) for x in (original, variant)):
        raise ValueError("实验大厅恢复记录无效")
    paras = _read_json(paras_path)
    current = paras.get("lobbyFileName")
    previous = manifest.get("previous_variant")
    if previous is not None and (not isinstance(previous, str) or not previous
                                 or previous != os.path.basename(previous)):
        raise ValueError("实验大厅恢复记录无效")
    if current == variant or (previous is not None and current == previous):
        bom = paras_path.read_bytes().startswith(b"\xef\xbb\xbf")
        paras["lobbyFileName"] = original
        _write_json(paras_path, paras, bom=bom)
    elif current != original:
        raise ValueError("大厅文件已被其他操作修改，未覆盖当前设置")
    manifest_path.unlink()
    return True
