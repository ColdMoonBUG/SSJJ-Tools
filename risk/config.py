# -*- coding: utf-8 -*-
"""风险模式：注入、过检测、大厅美化、全量本地化。

普通模式只做「不碰游戏进程/不改游戏文件」的事：本地 CDN、缓存预取、
启动器优化。风险模式在此之上再开三件事：

1. lobby   —— 改写大厅主 SWF 的缓动常量与刷新率（CDN 下发层，不动磁盘客户端）
2. inject  —— 往战斗端 Mono 运行时注入内存托管程序集（不落盘、不进 PEB 模块链）
3. mitm    —— 给 https 的 globalResurl 做本地 CA 拦截，让 https 资源也能命中本地库

所有开关都在 risk_config.json，关掉即回到普通模式。
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
from .paths import DATA_ROOT as ROOT      # 打包后也不能落到临时解包目录
CONFIG_PATH = os.path.join(ROOT, "risk_config.json")

DEFAULTS = {
    "enabled": False,              # 风险模式总开关
    "lobby": {
        "enabled": False,          # 已移除大厅动效
    },
    "inject": {
        "enabled": True,
        "auto": True,               # 守护：Mono 就绪并经过缓冲后注入
        # 加载方式（2026-11-24 夜新增）：
        #   proxy  = 往 battle\*_64\ 放一个 version.dll 代理，战斗端启动时由加载器
        #            直接把载荷 load 进 mono。全程不跨进程，不建远程线程。
        #            —— 这是当前唯一能在 GameGuard 下活下来的通道。
        #   thread = 老的 CreateRemoteThread 注入。GG 一看到「新线程的起始地址不在
        #            任何模块镜像内」就抛 0x80000004 杀进程（实测同一毫秒），
        #            只在没有 GG 的场合（单开战斗端调试）还有用。
        "method": "proxy",
        "settle_seconds": 0.8,       # 与 SkyDome 的 AutoInjector 等待时机一致
        "targets": ["SSJJ_BattleClient_Unity.exe"],
        "guard_bypass": True,       # 注入后斩断 RuntimeCheatGuard / AceClientManager 检测
        "verbose": True,
        # 守护发现进程但自己不是管理员时，是否自动拉一次提权副本（会弹 UAC）。
        # 默认关：开局弹 UAC 太吵，交给界面上的「提权」按钮显式触发。
        "auto_elevate": False,
        # 冷加载：皮肤/模型规则只在武器第一次被创建时应用一次，不再每 500ms
        # 全量重建（热加载是卡顿和运行时特征的来源）。
        "cold_swap": True,
        # ACE 处理策略：none=一点都不碰 ACE（默认，登录/心跳/退出全走官方原样，
        # 客户端痕迹最少）；teardown=只拦退出清理 Exit/eixt_ace/user_loginOff_ace/Close；
        # full=19 个方法全打掉（会打掉登录/上报，服务端判第三方软件）。
        "ace_mode": "none",
        # 退出保护：退出前先把补丁字节还原，再等一会儿让 GameGuard(npggNT64)
        # 的收尾扫描跑完才放行。挂在 Application.wantsToQuit，抢在退出流程之前。
        "exit_hold": True,
        "exit_delay": 2.0,          # 秒；还原后等多久再退出
    },
    "mitm": {
        "enabled": False,
        "hosts": ["cdn.4399ssjj.com"],
    },
}

_cache = None


def _deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load(reload=False):
    global _cache
    if _cache is not None and not reload:
        return _cache
    import json
    data = {}
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    _cache = _deep_merge(DEFAULTS, data)
    return _cache


def save(cfg=None):
    global _cache
    import json
    if cfg is not None:
        _cache = cfg
    if _cache is None:
        load()
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(_cache, f, ensure_ascii=False, indent=2)
    return CONFIG_PATH


def get(path, default=None):
    """get("lobby.easing")"""
    cur = load()
    for seg in path.split("."):
        if not isinstance(cur, dict) or seg not in cur:
            return default
        cur = cur[seg]
    return cur


def set_(path, value):
    cfg = load()
    segs = path.split(".")
    cur = cfg
    for seg in segs[:-1]:
        cur = cur.setdefault(seg, {})
    cur[segs[-1]] = value
    save(cfg)
    return value


def enabled():
    return bool(load().get("enabled"))
