#!/usr/bin/env python3
"""构建 ScanPress.app ——scan2pdf.py 的原生 macOS 图形外壳。

用法:
  python3 app/build_app.py            # 构建并安装到 ~/Applications
  python3 app/build_app.py --to /Applications
  python3 app/build_app.py --no-install   # 只在 app/build/ 下产出 .app

做的事:swiftc 编译 ScanToPDF.swift → 组装 .app 包 → 生成图标(magick+iconutil)
       → 把 scan2pdf.py 复制进 Contents/Resources → 临时签名(codesign -s -)→ 安装。
只需 Xcode Command Line Tools,不需要完整 Xcode。编排用 python(macOS 系统 bash 3.2 不可靠)。
"""
import argparse
import os
import plistlib
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = [os.path.join(os.path.dirname(HERE), "scripts", n)
           for n in ("scan2pdf.py", "merge_pdfs.py")]
VERSION = "2.1"
APP_NAME = f"ScanPress {VERSION}"      # 版本号进应用名,一处改全处生效
BIN_NAME = "ScanToPDF"
BUILD = os.path.join(HERE, "build")


def run(args, **kw):
    r = subprocess.run(args, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        sys.exit(f"失败: {' '.join(str(a) for a in args[:4])}...\n{r.stderr.strip()[:1500]}")
    return r.stdout


def make_icon(dst_icns):
    """无 Xcode 也能做图标:magick 画 1024 图 → sips 出各尺寸 → iconutil 合成 .icns"""
    if not shutil.which("magick"):
        print("  (跳过图标:未装 magick)")
        return False
    tmp = os.path.join(BUILD, "icon.iconset")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    base = os.path.join(BUILD, "icon1024.png")
    # magick 在本机没有默认字体(annotate 会报 unable to read font),必须显式给字体文件
    font = next((f for f in ("/System/Library/Fonts/HelveticaNeue.ttc",
                             "/System/Library/Fonts/Helvetica.ttc",
                             "/System/Library/Fonts/ArialHB.ttc",
                             "/System/Library/Fonts/Geneva.ttf") if os.path.exists(f)), None)
    # 图标语义:扫描头打光 → 摊开的书 → PDF。深蓝渐变圆角底,冷色光束点题"扫描"。
    run(["magick", "-size", "1024x1024", "gradient:#46598f-#141b30",
         "(", "-size", "1024x1024", "xc:none", "-fill", "white",
         "-draw", "roundrectangle 30,30 994,994 205,205", ")",
         "-alpha", "off", "-compose", "CopyOpacity", "-composite",
         # 光束(先画后模糊再降透明度,得到柔和的锥形光)
         "(", "-size", "1024x1024", "xc:none", "-fill", "#7fd8ee",
         "-draw", "path 'M 470,250 L 300,600 L 724,600 L 554,250 Z'",
         "-blur", "0x22", "-channel", "A", "-evaluate", "multiply", "0.55", "+channel", ")",
         "-compose", "over", "-composite",
         # 扫描头 + 镜头
         "-stroke", "none", "-fill", "#cfd8ea",
         "-draw", "roundrectangle 432,120 592,215 30,30",
         "-fill", "#2a3a63", "-draw", "circle 512,168 512,196",
         "-fill", "#7fd8ee", "-draw", "circle 512,168 512,182",
         # 摊开的书(左右两页 + 书脊)
         "-fill", "white",
         "-draw", "path 'M 512,585 C 398,520 296,526 176,568 L 176,822 C 296,780 398,774 512,838 Z'",
         "-fill", "#e6ecf8",
         "-draw", "path 'M 512,585 C 626,520 728,526 848,568 L 848,822 C 728,780 626,774 512,838 Z'",
         "-fill", "#8fa0c0", "-draw", "rectangle 505,585 519,838",
         # 书页上的文字线
         "-stroke", "#aebbd4", "-strokewidth", "6", "-fill", "none",
         "-draw", "line 246,642 452,616", "-draw", "line 246,690 452,664",
         "-draw", "line 246,738 420,714", "-draw", "line 572,616 778,642",
         "-draw", "line 572,664 778,690", "-draw", "line 572,714 746,738",
         # PDF 红标
         "-stroke", "none", "-fill", "#e0483f",
         "-draw", "roundrectangle 636,742 924,862 24,24"]
        + (["-font", font, "-fill", "white", "-pointsize", "82",
            "-annotate", "+692+826", "PDF"] if font else [])
        + [base])
    for size in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            px = size * scale
            name = f"icon_{size}x{size}{'@2x' if scale == 2 else ''}.png"
            run(["sips", "-z", str(px), str(px), base, "--out", os.path.join(tmp, name)])
    run(["iconutil", "-c", "icns", tmp, "-o", dst_icns])
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", default=os.path.expanduser("~/Applications"))
    ap.add_argument("--no-install", action="store_true")
    o = ap.parse_args()

    for sc in SCRIPTS:
        if not os.path.exists(sc):
            sys.exit(f"找不到 {sc}")
    if not shutil.which("swiftc"):
        sys.exit("找不到 swiftc,请先装 Xcode Command Line Tools:xcode-select --install")

    shutil.rmtree(BUILD, ignore_errors=True)
    app = os.path.join(BUILD, APP_NAME + ".app")
    macos = os.path.join(app, "Contents", "MacOS")
    res = os.path.join(app, "Contents", "Resources")
    os.makedirs(macos)
    os.makedirs(res)

    print("1/5 编译 Swift…")
    run(["swiftc", "-parse-as-library", "-swift-version", "5", "-O",
         "-target", "arm64-apple-macos13.0",
         os.path.join(HERE, "ScanToPDF.swift"), "-o", os.path.join(macos, BIN_NAME)])

    print("2/5 生成图标…")
    has_icon = make_icon(os.path.join(res, "AppIcon.icns"))

    print("3/5 组装 .app…")
    for sc in SCRIPTS:
        shutil.copy2(sc, os.path.join(res, os.path.basename(sc)))
    info = {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleExecutable": BIN_NAME,
        "CFBundleIdentifier": "local.scan2pdf",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION.split(".")[0],
        "LSMinimumSystemVersion": "13.0",
        "NSHighResolutionCapable": True,
        "LSApplicationCategoryType": "public.app-category.productivity",
    }
    if has_icon:
        info["CFBundleIconFile"] = "AppIcon"
    with open(os.path.join(app, "Contents", "Info.plist"), "wb") as fh:
        plistlib.dump(info, fh)

    print("4/5 临时签名…")
    run(["codesign", "--force", "--deep", "-s", "-", app])

    if o.no_install:
        print(f"\n完成(未安装): {app}")
        return
    print("5/5 安装…")
    os.makedirs(o.to, exist_ok=True)
    target = os.path.join(o.to, APP_NAME + ".app")
    shutil.rmtree(target, ignore_errors=True)
    for old in os.listdir(o.to):          # 清掉改名前/旧版本的同族应用,免得启动台里堆一排
        if (old.startswith("ScanPress") or old.startswith("（自制）扫描转PDF")) \
                and old != APP_NAME + ".app":
            shutil.rmtree(os.path.join(o.to, old), ignore_errors=True)
            print(f"  (已移除旧版 {old})")
    shutil.copytree(app, target, symlinks=True)
    run(["codesign", "--force", "--deep", "-s", "-", target])
    print(f"\n已安装: {target}")
    print("从「启动台 / 应用程序」双击即可;也可拖到 Dock 常驻。")
    print("更新方式:改完 scan2pdf.py / merge_pdfs.py / ScanToPDF.swift 后重跑本脚本。")


if __name__ == "__main__":
    main()
