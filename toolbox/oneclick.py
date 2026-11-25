# -*- coding: utf-8 -*-
"""一键启动的编排层：只走注入换肤。

2026-11-24 决定：静态（改资源包）那条路彻底停用 —— 战斗端加载前会校验
`Preload/<md5>/data` 的内容 md5 是否等于目录名，绕过去要么改清单要么改缓存，
都容易被完整性检查咬住；注入一次、冷加载、零常驻线程反而更干净。

所以这里的规则永远是「注入规则」：写进 skin_swap.json 的 rules（没有 mode 字段），
由战斗端里的载荷读出来改材质。静态换肤的老记录只保留「还原」能力。
"""
import time

import quick_login
import skin_pack
import skin_swap

MODE_INJECT = "inject"
MODE_STATIC = "static"          # 仅为兼容旧调用保留，实际不再产生静态规则
MODE_NAMES = {MODE_INJECT: "注入", MODE_STATIC: "静态"}
MODE_LABELS = {"": "注入", "inject": "注入", "static": "静态"}


def _load_rules():
    try:
        return (skin_swap.load_rules() or {}).get("rules") or []
    except Exception:
        return []


def _save_rules(data, allow_empty=False):
    try:
        return skin_swap.save_rules(data, allow_empty=allow_empty)
    except TypeError:
        return skin_swap.save_rules(data)


def rule_of(weapon, rules=None):
    w = (weapon or "").lower()
    for r in (rules if rules is not None else _load_rules()):
        if (r.get("src") or "").lower() == w:
            return r
    return None


def skin_code(weapon, skin):
    """短名 -> 完整皮肤 code：raise -> skin_ak47_raise。"""
    if not weapon or not skin:
        return ""
    if skin.startswith("skin_"):
        return skin
    return "skin_%s_%s" % (weapon.lower(), skin)


def skin_short(weapon, code):
    """完整 code -> 短名；不是这把枪的就原样返回。"""
    if not code:
        return ""
    pref = "skin_%s_" % (weapon or "").lower()
    return code[len(pref):] if code.lower().startswith(pref) else code


def upsert_rule(weapon, skin="", model="", weapon_name="", skin_name="",
                model_name="", mode=MODE_INJECT):
    """写一条注入规则（同 src 只保留一条）。返回 (ok, msg)。"""
    w = (weapon or "").lower()
    if not w:
        return False, "没选武器"
    if not skin and not model:
        return False, "至少选择一款皮肤或目标建模"
    data = skin_swap.load_rules() or {}
    rules = list(data.get("rules") or [])
    m_code = (model or "").lower()
    rule = {"src": w, "src_name": weapon_name or w,
            "skin": skin_code(w, skin) if skin else "",
            "skin_name": skin_name or "",
            "model": m_code, "model_name": (model_name or m_code) if m_code else "",
            "enabled": True}
    rules = [r for r in rules if (r.get("src") or "").lower() != w]
    rules.append(rule)
    data["rules"] = rules
    data["enabled"] = True
    ok, msg = _save_rules(data)
    return ok, (msg or "已写入规则")


def drop_rule(weapon, only_mode=None):
    w = (weapon or "").lower()
    data = skin_swap.load_rules() or {}
    rules = list(data.get("rules") or [])
    keep = [r for r in rules if (r.get("src") or "").lower() != w]
    data["rules"] = keep
    return _save_rules(data, allow_empty=True)


def rules_summary():
    out = []
    for r in _load_rules():
        out.append({"src": r.get("src"), "name": r.get("src_name"),
                    "skin": r.get("skin"), "skin_name": r.get("skin_name"),
                    "model": r.get("model"), "enabled": r.get("enabled", True)})
    return out


def status():
    rules = _load_rules()
    prof = {}
    try:
        prof = quick_login.load_profile() or {}
    except Exception:
        prof = {}
    inj = {}
    try:
        import risk.guard as guard
        inj = guard.inject_state() or {}
    except Exception:
        inj = {}
    try:
        import risk.config as rcfg
        rcfg.load()
        cold = bool(rcfg.get("inject.cold_swap"))
        ace = rcfg.get("inject.ace_mode") or "none"
        auto = bool(rcfg.get("inject.auto_elevate"))
        exit_hold = rcfg.get("inject.exit_hold")
        exit_hold = True if exit_hold is None else bool(exit_hold)
        exit_delay = float(rcfg.get("inject.exit_delay") or 1.5)
        method = str(rcfg.get("inject.method") or "proxy")
    except Exception:
        cold, ace, auto, exit_hold, exit_delay = True, "none", False, True, 2.0
        method = "proxy"
    px = inj.get("proxy") or {}
    return {
        "rules": rules,
        "total": len(rules),
        "static": 0,
        "inject": len(rules),
        "enabled": bool((skin_swap.load_rules() or {}).get("enabled", True)),
        "applied": skin_pack.applied_list(),
        "account": {"username": prof.get("username") or "",
                    "server": prof.get("server") or ""},
        "inject_on": bool(inj.get("enabled")),
        "injected": inj.get("injected") or [],
        "cold_swap": cold,
        "ace_mode": ace,
        "auto_elevate": auto,
        "exit_hold": exit_hold,
        "exit_delay": exit_delay,
        "method": method,
        "proxy_installed": px.get("installed", 0),
        "proxy_dirs": px.get("dirs") or [],
        "mode": "inject",
    }


def clear_all_rules(restore_files=True, emit=None):
    say = emit or (lambda s: None)
    if restore_files:
        try:
            n = skin_pack.restore_all()
            if n:
                say("已还原 %d 个曾经被静态改过的资源包" % n)
        except Exception as exc:
            say("[!] 还原静态资源失败：%r" % (exc,))
    data = skin_swap.load_rules() or {}
    data["rules"] = []
    ok, msg = _save_rules(data, allow_empty=True)
    say("已清空全部规则" if ok else str(msg))
    return ok, "已清空全部规则（战斗端下一局不再改外观）" if ok else msg


def launch(mode, weapon, skin, username, password, server, model="",
           emit=None, weapon_name="", skin_name="", model_name=""):
    """换肤启动：写规则（或沿用已有规则）→ 开启注入守护 → 直登进游戏。"""
    say = emit or (lambda s: None)
    has_sel = bool(weapon and (skin or model))
    existing = _load_rules()
    if not has_sel and not existing:
        return False, "请先选武器并选择一款皮肤或目标建模（或点「只启动游戏」）"
    if has_sel:
        say("① 注入换肤：把【%s】的外观改成【%s】" % (weapon_name or weapon, skin_name or skin or model_name or model))
        ok, msg = upsert_rule(weapon, skin, model, weapon_name=weapon_name,
                              skin_name=skin_name, model_name=model_name, mode=MODE_INJECT)
        if not ok:
            return False, msg
        say("② 规则已写入")
    else:
        say("① 沿用已保存的 %d 条换肤/建模规则" % len(existing))
    try:
        import risk.guard as guard
        import risk.config as risk_config
        st = guard.inject_state() or {}
        method = str(risk_config.get("inject.method") or "proxy").lower()
        if method == "proxy":
            # 代理模式：载荷由 battle\*_64\version.dll 在战斗端启动时自己 load，
            # 不需要常驻守护，也不需要 OpenProcess / CreateRemoteThread。
            ok2, msg2 = guard.inject_start()
            say("③ 加载方式：version.dll 代理 · %s" % msg2)
            if not ok2:
                return False, "代理 DLL 未部署，换肤启动已停止：%s" % msg2
        elif not risk_config.get("inject.enabled") or not st.get("thread_alive"):
            ok2, msg2 = guard.inject_start()
            say("③ 注入守护：%s" % (msg2 if isinstance(msg2, str) else ok2))
            if not ok2:
                return False, "注入守护未启动，换肤启动已停止：%s" % msg2
        else:
            say("③ 注入守护已在运行")
    except Exception as exc:
        say("[!] 加载通道启动失败：%r" % (exc,))
        return False, "加载通道起不来，换肤不会生效：%r" % (exc,)
    if quick_login.active_official_lobby():
        say("④ 已在官方大厅，沿用当前会话；注入换肤已就绪")
        return True, "已在大厅，注入换肤已就绪（下一局生效）"
    say("④ 打开官方启动器（在游戏窗口登录并选区）")
    return quick_login.launch_official(emit=say)


def restore(emit=None):
    say = emit or (lambda s: None)
    n = 0
    try:
        n = skin_pack.restore_all()
    except Exception as exc:
        say("[!] 还原静态资源失败：%r" % (exc,))
    data = skin_swap.load_rules() or {}
    data["rules"] = []
    ok, msg = _save_rules(data, allow_empty=True)
    if n:
        say("已还原 %d 个资源包" % n)
    return ok, "已清空规则%s；进游戏后武器恢复原样" % ("并还原 %d 个资源包" % n if n else "")
