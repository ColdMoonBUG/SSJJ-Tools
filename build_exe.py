# -*- coding: utf-8 -*-
"""打包 生死狙击工具箱.exe。

    python build_exe.py            打包到 dist\\，不动现有 exe
    python build_exe.py --deploy   打包后把新 exe 放到根目录（旧 exe 挪进 _trash\\）

为什么要自己写：仓库里没有 .spec，之前那个 49MB 的 onefile 是手工敲
PyInstaller 命令行出来的，参数没留下来。风险模式的 risk 包、预编译载荷、
证书目录都要跟着进包，所以参数必须固定成脚本，不能每次靠记忆。
"""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENTRY = ROOT / "toolbox" / "ssjj_toolbox.py"
NAME = "生死狙击工具箱"
PAYLOAD = ROOT / "risk" / "payload"
DIST = ROOT / "dist"


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    deploy = "--deploy" in argv
    dist_arg = next((a.split("=", 1)[1] for a in argv if a.startswith("--dist-dir=")), None)
    dist = Path(dist_arg).resolve() if dist_arg else DIST

    if not ENTRY.is_file():
        raise SystemExit("找不到入口: %s" % ENTRY)

    # 代理将托管载荷直接编进 version.dll。每次打包先重编这两层，
    # 否则 exe 会携带上次的代理，即使 payload/bin 已经是新版本。
    if "--use-existing-proxy" not in argv:
        from risk import injector
        payload, lines = injector.csc_build()
        for line in lines:
            print(line)
        if not payload:
            raise SystemExit("载荷编译失败，停止打包")
        subprocess.run([sys.executable, str(ROOT / "risk" / "proxy" / "build_proxy.py"),
                        "--payload", payload], cwd=str(ROOT), check=True)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile", "--noconsole", "--uac-admin",
        "--name", NAME,
        "--distpath", str(dist),
        "--paths", str(ROOT),            # ssjj_paths / local_cdn / risk
        "--paths", str(ENTRY.parent),    # backend / ui
        # 预编译载荷：打包版没有 dotnet，注入时只能读包内这一份
        # 目标目录必须正好是 risk\payload（injector.payload_candidates() 里按这个找）
        "--add-data", "%s;risk\\payload" % PAYLOAD,
        # version.dll 代理（进程内加载通道）。不带这个，点「部署代理」会找不到 DLL。
        "--add-data", "%s;risk\\proxy" % (ROOT / "risk" / "proxy" / "version.dll"),
        "--add-data", "%s;." % (ROOT / "weapon_catalog.json"),
        # 保留 bundle_index.json 供旧静态记录还原与皮肤目录兜底
        "--add-data", "%s;toolbox\\data" % (ENTRY.parent / "data"),
        # 大厅反编译脚本、缓存库、_analysis 及已停用的静态改包重依赖都不进包
        "--exclude-module", "UnityPy",
        "--exclude-module", "fmod_toolkit",
        "--exclude-module", "pyfmodex",
        "--exclude-module", "matplotlib",
        "--exclude-module", "numpy",
        "--exclude-module", "pandas",
        "--exclude-module", "PySide6.Qt3DCore",
        "--exclude-module", "PySide6.QtWebEngineCore",
        "--exclude-module", "PySide6.QtQuick",
        "--exclude-module", "PySide6.QtMultimedia",
    ]
    # backend 里风险模式是 importlib.import_module("risk." + name) 动态导入的，
    # PyInstaller 静态分析看不到，必须在命令行里逐个点名，否则打包版点「风险」页
    # 直接 ModuleNotFoundError。
    for mod in ("quick_login", "skin_swap", "skin_pack", "oneclick",
                "updater", "diagnostics", "ui.diagnostic_export",
                "risk", "risk.config", "risk.paths", "risk.swfpatch", "risk.airdesc",
                "risk.lobby", "risk.injector", "risk.mitm", "risk.guard",
                "risk.proxy", "risk.proxy.build_proxy", "risk.privilege",
                "risk.selftest", "risk.livetest", "risk.__main__",
                "cryptography", "cryptography.x509", "cryptography.hazmat.primitives.asymmetric.rsa"):
        cmd += ["--hidden-import", mod]
    cmd += [
        str(ENTRY),
    ]
    print("[*] %s" % " ".join('"%s"' % c if " " in c else c for c in cmd))
    t0 = time.time()
    rc = subprocess.call(cmd, cwd=str(ROOT))
    if rc != 0:
        print("[!] PyInstaller 失败，退出码 %d" % rc)
        return rc
    exe = dist / (NAME + ".exe")
    if not exe.is_file():
        print("[!] 没找到产物: %s" % exe)
        return 3
    print("[OK] %s  %.1f MB  用时 %.0fs"
          % (exe, exe.stat().st_size / 1048576.0, time.time() - t0))

    if deploy:
        trash = ROOT / "_trash"
        trash.mkdir(exist_ok=True)
        cur = ROOT / (NAME + ".exe")
        if cur.is_file():
            old = trash / ("%s.exe.%s" % (NAME, time.strftime("%Y%m%d-%H%M%S")))
            try:
                cur.replace(old)
                print("[*] 旧 exe -> %s" % old)
            except Exception as e:
                print("[!] 旧 exe 挪不动（是否还在运行？）: %r" % (e,))
                return 4
        shutil.copy2(str(exe), str(cur))
        print("[OK] 已部署到 %s" % cur)

    if "--release" in argv:
        import zipfile
        rel_root = ROOT / "release"
        stage_dir = rel_root / "_stage"
        if stage_dir.exists():
            shutil.rmtree(stage_dir, ignore_errors=True)
        stage_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(exe), str(stage_dir / (NAME + ".exe")))

        readme = stage_dir / "使用说明.txt"
        readme.write_text(
            "================ 生死狙击工具箱（绿色免安装版 1.1） ================\r\n\r\n"
            "就一件事：选好皮肤，点一下，进游戏。别的什么都不用开。\r\n\r\n"
            "【怎么用（三步）】\r\n"
            "1. 双击「生死狙击工具箱.exe」，弹出的管理员确认点「是」（必须，因为要往\r\n"
            "   游戏目录写文件）。\r\n"
            "2. 首页「一键启动」：选武器 → 选想借用的皮肤 → 填账号密码和区服 → 点\r\n"
            "   「一键启动」。工具箱会自己部署代理、写规则、登录、拉起游戏。\r\n"
            "3. 想停用换肤：在首页「一键启动」页「⑤ 高级开关」点「移除代理」（正常关闭\r\n"
            "   工具箱也会自动清理已投放的代理文件），下次进游戏就是原样。\r\n\r\n"
            "【换肤是怎么进去的】\r\n"
            "- 加载方式是「代理 DLL」：工具箱往游戏的 battle\\*_64\\ 目录写一个\r\n"
            "  version.dll。战斗端启动时 Windows 加载器会先加载它，载荷在游戏进程\r\n"
            "  内部自己进 mono 运行时。\r\n"
            "- 全程不 OpenProcess、不 VirtualAllocEx、不 CreateRemoteThread、不留匿名\r\n"
            "  可执行页 —— 那套跨进程注入会被 GameGuard 当场杀掉（npggNT64.des 报\r\n"
            "  0x80000004），代理 DLL 是唯一能活下来的通道。\r\n"
            "- 代理一进进程就把自己从模块链表里摘掉并伪装系统路径，同时由自身定时器与\r\n"
            "  工具箱守护进程协同将文件快速改名成 .ver.dat，绕开 RuntimeCheatGuard 自检。\r\n"
            "- 换肤规则是「冷加载」：进场应用一次就结束，进程里不留常驻线程。\r\n\r\n"
            "【生效时机】\r\n"
            "- 代理部署后新开一局生效；改规则要退出这一局、重进一局才生效。\r\n"
            "- 退出时会把改过的代码字节写回原样，等 2 秒再放行，避免退出瞬间被\r\n"
            "  GameGuard 的收尾扫描判成异常（治「退出偶发崩溃」的那个开关）。\r\n\r\n"
            "【默认关闭的东西】\r\n"
            "- 本地加速、资源缓存、根证书、HTTPS 拦截：全部默认关闭，不会偷偷写 hosts、\r\n"
            "  不会往硬盘塞缓存。只影响下载速度，对换肤没帮助，需要时自己在侧栏开。\r\n\r\n"
            "【账号密码】\r\n"
            "- 用 Windows DPAPI 加密保存在本机，只在登录时用，不上传任何服务器。\r\n"
            "- 不想存就取消勾选「记住账号和密码」。\r\n\r\n"
            "【换台电脑能不能用】\r\n"
            "- 全部依赖都打在 exe 里：Qt 界面、代理 version.dll、托管载荷、武器皮肤清单\r\n"
            "  （1200+ 武器 / 470+ 皮肤）全在包内，不需要 Python、不需要 .NET、不需要\r\n"
            "  联网下任何数据文件，解压就能用。\r\n"
            "- 游戏目录自动探测（默认路径 + 注册表 + 卸载表 + 各盘符扫描），装在 D 盘、\r\n"
            "  E 盘也能认出来。\r\n"
            "- 配置写在 exe 同目录；放在只读位置（只读 U 盘、Program Files）时自动改存\r\n"
            "  %LOCALAPPDATA%\\ssjj-toolbox\\data，功能不受影响。\r\n\r\n"
            "【其它】\r\n"
            "- 「创建桌面快捷方式.bat」可在桌面生成快捷图标。\r\n"
            "- 日志在 %LOCALAPPDATA%\\ssjj-toolbox\\（proxy.log / risk_inject.log /\r\n"
            "  risk_mode.log），出问题时把这三个发出来就能定位。\r\n"
            "- 卸载：先把代理「移除」，再直接删掉本文件夹，不写注册表、不装服务、\r\n"
            "  不留残留。\r\n",
            encoding="utf-8-sig",
        )

        bat = stage_dir / "创建桌面快捷方式.bat"
        bat.write_text(
            "@echo off\r\n"
            "chcp 65001 >nul\r\n"
            "set \"TARGET=%~dp0生死狙击工具箱.exe\"\r\n"
            "powershell -NoProfile -Command \""
            "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\\生死狙击工具箱.lnk');"
            "$s.TargetPath='%TARGET%';"
            "$s.WorkingDirectory='%~dp0';"
            "$s.Description='生死狙击工具箱 - 一键直登与武器换肤';"
            "$s.Save()\"\r\n"
            "echo 已在桌面创建「生死狙击工具箱」快捷方式！\r\n"
            "pause\r\n",
            encoding="utf-8",
        )

        zip_name = "生死工具箱1.1"
        for a in argv:
            if a.startswith("--zip-name="):
                zip_name = a.split("=", 1)[1].strip() or zip_name
        zip_path = rel_root / ("%s.zip" % zip_name)
        if zip_path.exists():
            zip_path.unlink()
        with zipfile.ZipFile(str(zip_path), "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for item in sorted(stage_dir.iterdir()):
                zf.write(str(item), arcname="%s/%s" % (zip_name, item.name))
        shutil.rmtree(stage_dir, ignore_errors=True)
        print("[OK] 已生成分发压缩包: %s (%.1f MB)"
              % (zip_path, zip_path.stat().st_size / 1048576.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
