#!/usr/bin/env python3
"""构建 ScanPress.app ——scan2pdf.py 的原生 macOS 图形外壳。

用法:
  python3 app/build_app.py            # 构建并安装到 ~/Applications
  python3 app/build_app.py --to /Applications
  python3 app/build_app.py --no-install   # 只在 app/build/ 下产出 .app

做的事:swiftc 编译 ScanToPDF.swift → 组装 .app 包 → 生成图标(magick+iconutil)
       → 把 scan2pdf.py 复制进 Contents/Resources → 内嵌 magick/jbig2 工具链
       → 由内向外签名(codesign -s -)→ 安装。
只需 Xcode Command Line Tools,不需要完整 Xcode。编排用 python(macOS 系统 bash 3.2 不可靠)。

内嵌工具链(--no-bundle-tools 可关掉):把 homebrew 的 magick / jbig2 / jbig2topdf.py 连同
全部非系统 dylib 复制进包内,用 install_name_tool 把依赖路径改写成 @loader_path 相对引用,
装好的机器上不再需要 `brew install imagemagick jbig2enc`。**potrace 绝不内嵌**——它是 GPL-2.0,
打进包会传染整个应用;矢量模式本来也只在命令行暴露,让它继续走 PATH 找 homebrew 的。
"""
import argparse
import glob
import os
import plistlib
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = [os.path.join(os.path.dirname(HERE), "scripts", n)
           for n in ("scan2pdf.py", "merge_pdfs.py", "shrink_pdf.py", "ocr_pdf.py")]
VERSION = "2.2"
APP_NAME = f"ScanPress {VERSION}"      # 版本号进应用名,一处改全处生效
BIN_NAME = "ScanToPDF"
BUILD = os.path.join(HERE, "build")
BREW = "/opt/homebrew"

TOOL_BINS = ("magick", "jbig2")            # 要内嵌的可执行文件
TOOL_SCRIPTS = ("jbig2topdf.py",)          # 纯 python 脚本,原样复制
SYS_LIB_PREFIXES = ("/usr/lib", "/System")  # 系统自带,不搬

# 内嵌的 Python 运行时:astral-sh/python-build-standalone 的可重定位 CPython。
# 版本对齐用户原来 pdfenv 里的 3.14.6,行为不会有偏移。
PY_VERSION, PY_RELEASE = "3.14.6", "20260728"
PY_ASSET = f"cpython-{PY_VERSION}+{PY_RELEASE}-aarch64-apple-darwin-install_only_stripped.tar.gz"
PY_URL = ("https://github.com/astral-sh/python-build-standalone/releases/download/"
          f"{PY_RELEASE}/{PY_ASSET.replace('+', '%2B')}")
PY_PKGS = ["pikepdf", "img2pdf", "Pillow"]     # scan2pdf.py / merge_pdfs.py 的全部第三方依赖
CACHE = os.path.expanduser("~/.cache/scanpress")

# Python 运行时里用不上的部分(占 18 MB):包管理器、IDE、C 头文件、测试套件、tkinter。
# 注意 **lxml 不能删** —— pikepdf 导入时就要它,`--title` 写 dc:title 元数据也走它。
PY_STRIP = ["lib/python{v}/site-packages/pip", "lib/python{v}/site-packages/pip-*.dist-info",
            "lib/python{v}/site-packages/setuptools*", "lib/python{v}/site-packages/pkg_resources",
            "lib/python{v}/ensurepip", "lib/python{v}/idlelib", "lib/python{v}/turtledemo",
            "lib/python{v}/pydoc_data", "lib/python{v}/tkinter", "lib/python{v}/lib2to3",
            "lib/python{v}/test", "lib/python{v}/site-packages/*/test",
            "lib/python{v}/site-packages/*/tests", "lib/python{v}/lib-dynload/_tkinter*.so",
            "lib/python{v}/site-packages/lxml/includes",
            "lib/python{v}/site-packages/lxml/isoschematron",
            "include", "share", "lib/pkgconfig"]

# 许可证不兼容 / 用不上的库。凡依赖链碰到这些的 coder 模块一律不内嵌。
# **x265 是 GPL-2.0**,由 heic.so → libheif → x265 牵进来,打包进 MIT 应用会构成违约;
# 本工具只处理 JPEG/PNG/TIFF 扫描图,丢掉 HEIC/AVIF 一系毫无损失,顺带省 13 MB。
TAINTED_LIB_PAT = re.compile(r"x265|x264|libheif|de265|aom|rav1e|dav1d|svt-av1|vmaf|"
                             r"ghostscript|libraw", re.I)


def run(args, **kw):
    r = subprocess.run(args, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        sys.exit(f"失败: {' '.join(str(a) for a in args[:4])}...\n{r.stderr.strip()[:1500]}")
    return r.stdout


def dylib_deps(path):
    """otool -L 出一个二进制的非系统依赖(原样的路径字符串,重写时要按原样匹配)"""
    out = subprocess.run(["otool", "-L", str(path)], capture_output=True, text=True).stdout
    deps = []
    for line in out.splitlines()[1:]:
        m = re.match(r"\s+(\S+)", line)
        if not m:
            continue
        d = m.group(1)
        if d.startswith(SYS_LIB_PREFIXES) or d.endswith(":"):
            continue
        deps.append(d)
    return deps


def resolve_dep(dep, owner):
    """把 @loader_path / @rpath 之类展开成真实路径;解析不出来返回 None"""
    p = dep.replace("@loader_path", os.path.dirname(owner)) \
           .replace("@executable_path", os.path.dirname(owner))
    if p.startswith("@rpath"):
        p = p.replace("@rpath", f"{BREW}/lib")
    return os.path.realpath(p) if os.path.exists(p) else None


def closure(roots):
    """从若干二进制出发,递归收集全部非系统 dylib 的真实路径"""
    seen, stack = set(), list(roots)
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        for d in dylib_deps(cur):
            r = resolve_dep(d, cur)
            if r and r not in seen:
                stack.append(r)
    return seen


def find_modules():
    """ImageMagick 的 coder/filter 模块(这个 homebrew 版是模块化编译的,不搬则连 JPEG 都读不了)。

    返回 (可内嵌的模块路径, 因许可证被排除的模块名)。
    """
    root = os.path.join(BREW, "opt", "imagemagick", "lib", "ImageMagick")
    mods = []
    for kind in ("coders", "filters"):
        mods += sorted(glob.glob(os.path.join(root, "modules-*", kind, "*.so")))
    keep, dropped = [], []
    for m in mods:
        if any(TAINTED_LIB_PAT.search(p) for p in closure([os.path.realpath(m)])):
            dropped.append(os.path.basename(m))
        else:
            keep.append(os.path.realpath(m))
    return keep, dropped


def bundle_tools(app):
    """把 magick / jbig2 及其模块、dylib 闭包搬进 .app,改写 install name。成功返回 True。"""
    bins = {}
    for name in TOOL_BINS:
        w = shutil.which(name, path=f"{BREW}/bin:{os.environ.get('PATH', '')}")
        if not w:
            print(f"  (跳过内嵌:找不到 {name},产出的包仍需用户自备 homebrew 工具链)")
            return False
        bins[name] = os.path.realpath(w)

    frameworks = os.path.join(app, "Contents", "Frameworks")
    helpers = os.path.join(app, "Contents", "Helpers")
    modroot = os.path.join(app, "Contents", "Resources", "ImageMagick", "modules")
    os.makedirs(frameworks, exist_ok=True)
    os.makedirs(helpers, exist_ok=True)

    mods, dropped = find_modules()
    if dropped:
        print(f"  按许可证排除 {len(dropped)} 个模块: {', '.join(n[:-3] for n in dropped)}"
              f"(依赖 x265 等 GPL 库)")

    libs = sorted(closure(list(bins.values()) + mods) - set(bins.values()) - set(mods))
    bundled = {os.path.basename(p) for p in libs}

    copied = []
    for src in libs:
        dst = os.path.join(frameworks, os.path.basename(src))
        shutil.copy2(src, dst)
        copied.append(("lib", dst))
    for name, src in bins.items():
        dst = os.path.join(helpers, name)
        shutil.copy2(src, dst)
        copied.append(("bin", dst))
    for src in mods:
        kind = os.path.basename(os.path.dirname(src))       # coders / filters
        d = os.path.join(modroot, kind)
        os.makedirs(d, exist_ok=True)
        dst = os.path.join(d, os.path.basename(src))
        shutil.copy2(src, dst)
        copied.append(("mod", dst))
        # ImageMagick 经 libltdl 加载模块,认的是 .la 描述文件而不是 .so,必须一并复制。
        # 且 .la 里的 libdir 是 homebrew 的绝对路径 —— 不清空的话,包内的 .la 会把
        # homebrew 那份 .so 拽回来加载(本机看着能用,换台机器就"no decode delegate"),
        # 清空后 libltdl 退回到「.la 自己所在目录」找 .so,正是我们要的。
        la = src[:-3] + ".la"
        if os.path.exists(la):
            with open(la) as fh:
                text = re.sub(r"(?m)^libdir=.*$", "libdir=''", fh.read())
            with open(os.path.join(d, os.path.basename(la)), "w") as fh:
                fh.write(text)
    # homebrew 的文件是 r-xr-xr-x,不给写权限 install_name_tool 会失败
    for _, dst in copied:
        os.chmod(dst, 0o755)

    # 纯 python 脚本放 Resources 而不是 Helpers:Helpers 属于「嵌套代码」位置,带执行位的脚本
    # 会被 codesign 要求单独签名(签名存在扩展属性里,打包时容易丢)。放 Resources 则被正常封进
    # CodeResources。scan2pdf.py 按「与自己同目录」优先查找,命令行下再回落到 PATH。
    res_dir = os.path.join(app, "Contents", "Resources")
    for name in TOOL_SCRIPTS:
        w = shutil.which(name, path=f"{BREW}/bin:{os.environ.get('PATH', '')}")
        if w:
            dst = os.path.join(res_dir, name)
            shutil.copy2(os.path.realpath(w), dst)
            os.chmod(dst, 0o644)

    # 各类文件到 Contents/Frameworks 的相对深度不同,用 @loader_path 表达(与谁加载它无关)
    HOP = {"lib": "",                                   # Frameworks/ 内部,同级
           "bin": "../Frameworks/",                     # Helpers/ → Contents/
           "mod": "../../../../Frameworks/"}            # Resources/ImageMagick/modules/<kind>/
    for kind, dst in copied:
        # dylib 自报家门也要改,否则依赖它的二进制仍按旧的绝对路径去找
        if kind == "lib":
            run(["install_name_tool", "-id", f"@loader_path/{os.path.basename(dst)}", dst])
        for dep in dylib_deps(dst):
            base = os.path.basename(resolve_dep(dep, dst) or dep)
            if base not in bundled:
                continue
            run(["install_name_tool", "-change", dep, f"@loader_path/{HOP[kind]}{base}", dst])

    # ImageMagick 的配置 XML(policy/delegates/type 等)。运行时靠 MAGICK_CONFIGURE_PATH 指过来
    res = os.path.join(app, "Contents", "Resources")
    for sub in ("etc", "share"):
        src = os.path.join(BREW, sub, "ImageMagick-7")
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(res, "ImageMagick", sub), dirs_exist_ok=True)
    for src in glob.glob(os.path.join(BREW, "opt", "imagemagick", "lib",
                                      "ImageMagick", "config-*")):
        shutil.copytree(src, os.path.join(res, "ImageMagick", "config"), dirs_exist_ok=True)

    write_licenses(res, libs, bins.values())
    total = sum(os.path.getsize(d) for _, d in copied)
    print(f"  内嵌 {len(bins)} 个可执行 + {len(libs)} 个 dylib + {len(mods)} 个模块,"
          f"共 {total / 1048576:.1f} MB")
    return True


def prepare_python():
    """备好一份装了 pikepdf/img2pdf/Pillow 并瘦过身的可重定位 CPython,缓存在 ~/.cache/scanpress。

    第一次要联网(下 25 MB 运行时 + pip 装包),之后重建直接用缓存。
    拿不到(离线且无缓存)就返回 None —— 构建照常进行,产出的包回落到用户的 ~/.venvs/pdfenv。
    """
    ready = os.path.join(CACHE, f"python-{PY_VERSION}-{PY_RELEASE}")
    if os.path.isdir(ready):
        return ready
    os.makedirs(CACHE, exist_ok=True)
    tarball = os.path.join(CACHE, PY_ASSET)
    if not os.path.exists(tarball):
        print(f"  下载 Python 运行时 {PY_VERSION}({PY_ASSET.split('-')[0]}, 约 25 MB)…")
        r = subprocess.run(["curl", "-fsSL", "-o", tarball + ".part", PY_URL])
        if r.returncode != 0:
            print("  (下载失败,跳过内嵌 Python;产出的包仍需用户自备 ~/.venvs/pdfenv)")
            return None
        os.replace(tarball + ".part", tarball)

    staging = ready + ".tmp"
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging)
    run(["tar", "-xzf", tarball, "-C", staging])
    root = os.path.join(staging, "python")          # 压缩包内固定是 python/ 这一层
    py = os.path.join(root, "bin", "python3")

    print(f"  安装 {', '.join(PY_PKGS)}…")
    r = subprocess.run([py, "-m", "pip", "install", "-q", "--no-cache-dir",
                        "--no-warn-script-location"] + PY_PKGS)
    if r.returncode != 0:
        shutil.rmtree(staging, ignore_errors=True)
        print("  (装包失败,跳过内嵌 Python)")
        return None

    v = ".".join(PY_VERSION.split(".")[:2])
    for pat in PY_STRIP:
        for p in glob.glob(os.path.join(root, pat.format(v=v))):
            shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else os.remove(p)

    os.replace(root, ready)
    shutil.rmtree(staging, ignore_errors=True)
    return ready


def bundle_python(app):
    """把备好的 CPython 放进 Contents/Resources/python。成功返回 True。"""
    src = prepare_python()
    if not src:
        return False
    dst = os.path.join(app, "Contents", "Resources", "python")
    shutil.copytree(src, dst, symlinks=True)
    size = int(subprocess.run(["du", "-sm", dst], capture_output=True, text=True)
               .stdout.split()[0])
    print(f"  内嵌 Python {PY_VERSION} + {'/'.join(PY_PKGS)},{size} MB")
    return True


def macho_files(root):
    """递归找出 Mach-O 文件(要逐个签名)。按魔数判断,比对后缀名靠谱。"""
    out = []
    for dirpath, _, names in os.walk(root):
        for n in names:
            p = os.path.join(dirpath, n)
            if os.path.islink(p) or not os.path.isfile(p):
                continue
            try:
                with open(p, "rb") as fh:
                    magic = fh.read(4)
            except OSError:
                continue
            if magic in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe"):
                out.append(p)
    return out


def write_licenses(res, libs, bins):
    """第三方组件的许可证。ImageMagick/jbig2enc/leptonica 等都要求分发时保留声明。"""
    out = os.path.join(res, "licenses")
    os.makedirs(out, exist_ok=True)
    formulas = set()
    for p in list(libs) + list(bins):
        m = re.match(rf"{re.escape(BREW)}/Cellar/([^/]+)/", p)
        if m:
            formulas.add(m.group(1))
    lines = ["本应用内嵌了以下第三方组件,各自遵循其许可证(全文见本目录下同名文件):", ""]
    for f in sorted(formulas):
        d = os.path.join(BREW, "opt", f)
        found = [n for n in sorted(os.listdir(d))
                 if re.match(r"(LICENSE|COPYING|NOTICE)", n, re.I)] if os.path.isdir(d) else []
        for n in found:
            src = os.path.join(d, n)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(out, f"{f}-{n}"))
        lines.append(f"  {f}" + (f"  ({', '.join(found)})" if found else "  (许可证文件见其官网)"))
    lines += ["", "注:liblzma(xz)以 0BSD 分发,包内的 GPL 文本只适用于 xz 命令行工具,本应用未内嵌它们。",
              "    libltdl(libtool)为 LGPL-2.1,本应用源码公开于 GitHub,满足其重新链接要求。",
              "    potrace 为 GPL-2.0,**未内嵌**;仅命令行 --mode vector 会去 PATH 上找它。"]
    with open(os.path.join(out, "第三方组件.txt"), "w") as fh:
        fh.write("\n".join(lines) + "\n")


def sign_inside_out(app):
    """由内向外签名。--deep 已被 Apple 弃用,且将来做公证时必须逐个签。"""
    targets = []
    for sub in ("Contents/Frameworks", "Contents/Helpers"):
        d = os.path.join(app, sub)
        if os.path.isdir(d):
            targets += [os.path.join(d, n) for n in sorted(os.listdir(d))
                        if not n.endswith(".py")]
    # coder/filter 模块和 Python 的扩展模块也都是 Mach-O,同样要签
    # (漏一个,整包签名就报 "code object is not signed at all")
    for sub in ("Contents/Resources/ImageMagick/modules", "Contents/Resources/python"):
        d = os.path.join(app, sub)
        if os.path.isdir(d):
            targets += sorted(macho_files(d))
    for t in targets:
        run(["codesign", "--force", "--timestamp=none", "-s", "-", t])
    run(["codesign", "--force", "--timestamp=none", "-s", "-", app])


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
    ap.add_argument("--no-bundle-tools", action="store_true",
                    help="不内嵌 magick/jbig2,产出的包需用户自备 homebrew 工具链")
    ap.add_argument("--no-bundle-python", action="store_true",
                    help="不内嵌 Python 运行时,产出的包需用户自备 ~/.venvs/pdfenv")
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

    print("1/6 编译 Swift…")
    run(["swiftc", "-parse-as-library", "-swift-version", "5", "-O",
         "-target", "arm64-apple-macos13.0",
         os.path.join(HERE, "ScanToPDF.swift"), "-o", os.path.join(macos, BIN_NAME)])

    # sphelper:Vision 文字识别 + CoreGraphics 精确渲染的桥接工具,给 Python 引擎调用。
    # 不能带 -parse-as-library —— 那是主程序 @main 需要的,命令行工具用了会禁掉顶层语句。
    print("1.5/6 编译 sphelper(Vision 桥接)…")
    helpers = os.path.join(app, "Contents", "Helpers")
    os.makedirs(helpers, exist_ok=True)
    run(["swiftc", "-swift-version", "5", "-O", "-target", "arm64-apple-macos13.0",
         os.path.join(HERE, "SPHelper.swift"), "-o", os.path.join(helpers, "sphelper")])

    print("2/6 生成图标…")
    has_icon = make_icon(os.path.join(res, "AppIcon.icns"))

    print("3/6 组装 .app…")
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

    print("4/6 内嵌工具链…")
    bundled = False if o.no_bundle_tools else bundle_tools(app)
    py = False if o.no_bundle_python else bundle_python(app)
    info["SPBundledTools"] = bundled          # 供应用自检时判断包内有没有工具链
    info["SPBundledPython"] = py
    with open(os.path.join(app, "Contents", "Info.plist"), "wb") as fh:
        plistlib.dump(info, fh)

    print("5/6 临时签名…")
    sign_inside_out(app)

    if o.no_install:
        print(f"\n完成(未安装): {app}")
        return
    print("6/6 安装…")
    os.makedirs(o.to, exist_ok=True)
    target = os.path.join(o.to, APP_NAME + ".app")
    shutil.rmtree(target, ignore_errors=True)
    for old in os.listdir(o.to):          # 清掉改名前/旧版本的同族应用,免得启动台里堆一排
        if (old.startswith("ScanPress") or old.startswith("（自制）扫描转PDF")) \
                and old != APP_NAME + ".app":
            shutil.rmtree(os.path.join(o.to, old), ignore_errors=True)
            print(f"  (已移除旧版 {old})")
    shutil.copytree(app, target, symlinks=True)
    sign_inside_out(target)
    print(f"\n已安装: {target}")
    print("从「启动台 / 应用程序」双击即可;也可拖到 Dock 常驻。")
    print("更新方式:改完 scan2pdf.py / merge_pdfs.py / ScanToPDF.swift 后重跑本脚本。")


if __name__ == "__main__":
    main()
