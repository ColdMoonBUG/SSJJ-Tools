"""把 hid.dll 代理编译出来（clang + lld-link，无 CRT）。

用法：
    python risk\\proxy\\build_proxy.py            # 编译到 risk\\proxy\\hid.dll
    python risk\\proxy\\build_proxy.py --payload <dll> --out <path>

载荷默认取 risk\\payload\\bin\\System.Configuration.Install.dll（--build 会生成）。
编译器优先用 LLVM 自带的那套（本机 C:\\Program Files\\LLVM\\bin），
不依赖 Visual Studio。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent                      # ssjj-tool
DEFAULT_PAYLOAD = ROOT / "risk" / "payload" / "bin" / "System.Configuration.Install.dll"
DEFAULT_OUT = HERE / "hid.dll"

LLVM_BIN = Path(os.environ.get("LLVM_BIN") or r"C:\Program Files\LLVM\bin")
KIT_LIB_CANDIDATES = [
    r"C:\Program Files (x86)\Windows Kits\10\Lib\10.0.26100.0\um\x64",
    r"C:\Program Files (x86)\Windows Kits\10\Lib\10.0.19041.0\um\x64",
]


def _tool(name: str) -> str:
    p = LLVM_BIN / name
    if p.exists():
        return str(p)
    raise SystemExit(f"找不到 {name}（{LLVM_BIN}）；可用环境变量 LLVM_BIN 指定目录")


def _kernel32() -> str:
    for d in KIT_LIB_CANDIDATES:
        f = Path(d) / "kernel32.lib"
        if f.exists():
            return str(Path(d))
    raise SystemExit("找不到 Windows SDK 的 kernel32.lib（um\\x64）")


def gen_header(payload: Path, out: Path) -> int:
    data = payload.read_bytes()
    lines = [
        "/* auto-generated, do not edit - src: %s (%d bytes) */" % (payload.name, len(data)),
        '#define PAYLOAD_NAME "%s"' % payload.name,
        "static const unsigned char payload_dll[] = {",
    ]
    chunk = 16
    for i in range(0, len(data), chunk):
        lines.append("    " + ",".join("0x%02x" % b for b in data[i:i + chunk]) + ",")
    lines.append("};")
    lines.append("static const unsigned int payload_dll_len = %d;" % len(data))
    out.write_text("\n".join(lines) + "\n", encoding="ascii")
    return len(data)


def build(payload: Path, out: Path) -> Path:
    if not payload.exists():
        raise SystemExit(f"载荷不存在：{payload}（先 python -m risk build --force）")
    hdr = HERE / "payload_dll.h"
    n = gen_header(payload, hdr)
    print(f"[1/3] 载荷已嵌入 {payload.name}（{n} 字节）-> payload_dll.h")

    obj = HERE / "version_proxy.obj"
    clang = _tool("clang.exe")
    cmd = [
        clang, "-target", "x86_64-pc-windows-msvc", "-O2", "-c",
        str(HERE / "version_proxy.c"), "-o", str(obj),
        "-fno-builtin", "-fno-stack-protector", "-Wno-everything",
    ]
    print("[2/3] clang -c")
    subprocess.run(cmd, check=True)

    lib_dir = _kernel32()
    link = _tool("lld-link.exe")
    cmd = [
        link, "/dll", "/entry:DllMain", "/nodefaultlib", "/machine:x64",
        "/out:" + str(out), str(obj), str(Path(lib_dir) / "kernel32.lib"),
        "/export:HidD_FlushQueue",
        "/export:HidD_FreePreparsedData",
        "/export:HidD_GetAttributes",
        "/export:HidD_GetConfiguration",
        "/export:HidD_GetFeature",
        "/export:HidD_GetHidGuid",
        "/export:HidD_GetIndexedString",
        "/export:HidD_GetInputReport",
        "/export:HidD_GetManufacturerString",
        "/export:HidD_GetMsGenreDescriptor",
        "/export:HidD_GetNumInputBuffers",
        "/export:HidD_GetPhysicalDescriptor",
        "/export:HidD_GetPreparsedData",
        "/export:HidD_GetProductString",
        "/export:HidD_GetSerialNumberString",
        "/export:HidD_Hello",
        "/export:HidD_SetConfiguration",
        "/export:HidD_SetFeature",
        "/export:HidD_SetNumInputBuffers",
        "/export:HidD_SetOutputReport",
        "/export:HidP_GetButtonCaps",
        "/export:HidP_GetCaps",
        "/export:HidP_GetData",
        "/export:HidP_GetExtendedAttributes",
        "/export:HidP_GetLinkCollectionNodes",
        "/export:HidP_GetScaledUsageValue",
        "/export:HidP_GetSpecificButtonCaps",
        "/export:HidP_GetSpecificValueCaps",
        "/export:HidP_GetUsageValue",
        "/export:HidP_GetUsageValueArray",
        "/export:HidP_GetUsages",
        "/export:HidP_GetUsagesEx",
        "/export:HidP_GetValueCaps",
        "/export:HidP_InitializeReportForID",
        "/export:HidP_MaxDataListLength",
        "/export:HidP_MaxUsageListLength",
        "/export:HidP_SetData",
        "/export:HidP_SetScaledUsageValue",
        "/export:HidP_SetUsageValue",
        "/export:HidP_SetUsageValueArray",
        "/export:HidP_SetUsages",
        "/export:HidP_TranslateUsagesToI8042ScanCodes",
        "/export:HidP_UnsetUsages",
        "/export:HidP_UsageListDifference",
    ]
    print("[3/3] lld-link")
    subprocess.run(cmd, check=True)

    print(f"[OK] {out}（{out.stat().st_size} 字节）")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--payload", default=str(DEFAULT_PAYLOAD))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    a = ap.parse_args(argv)
    build(Path(a.payload), Path(a.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
