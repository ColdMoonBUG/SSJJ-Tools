# -*- coding: utf-8 -*-
"""风险模式的「数据落点」统一解析。

源码运行时就是 ssjj-tool 根目录；打包成 生死狙击工具箱.exe 之后，模块文件被
塞进 PyInstaller 的临时解包目录（每次启动都换一个），风险模式自己的状态
（risk_config.json、risk_lobby.json、risk_ca\\ 证书、预编译载荷）如果跟着
那个临时目录走，一重启就全丢：配置回到默认、证书每次都重签、根证书装了一堆
孤魂野鬼。所以这些必须落在 exe 旁边的真实目录，和 lobby_res_cache 同级。
"""
import os
import sys


def data_root():
    env = os.environ.get("SSJJ_TOOLBOX_DATA")
    if env and os.path.isdir(env):
        return env
    try:                       # GUI 里 boot 已经算好了，直接复用
        import boot
        return str(boot.data_dir())
    except Exception:
        pass
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


DATA_ROOT = data_root()

# 预编译托管载荷的落地目录（打包版没有 dotnet 也能注入）
PAYLOAD_DIR = os.path.join(DATA_ROOT, "risk_payload")
