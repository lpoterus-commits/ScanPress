#!/usr/bin/env python3
"""给影印 PDF 加一层**看不见的文字**,让它可以被搜索 —— 页面外观一个像素都不变。

用的是 macOS 自带的 Vision 框架(经 sphelper 桥接),不是 Tesseract:
同一页韩语实测 Vision 0.56 秒、标题正文全对,Tesseract 1.39 秒且漏标题、错字、吐垃圾。
Vision 还原生支持 ko/zh/ja 等 30 种语言,零依赖零体积(系统框架),完全离线。

**准确率在这里的要求比「转录」低一个数量级**:读者看到的永远是原图,文字层只服务于
Ctrl+F,错了也看不见。所以 OCR 不需要完美,只需要比「零」好。

代价与边界:
  · 仅 macOS。Vision 的模型会随系统更新变化,故输出不像本项目其余部分那样跨机器可复现。
  · 不改变页面外观,但会让文件变大一点(每页几 KB 的文字层)。

用法:
  ocr_pdf.py 书.pdf                      # 写成 书_ocr.pdf
  ocr_pdf.py 书.pdf -o 输出.pdf --lang ko-KR,en-US
  ocr_pdf.py <目录> --out-dir <目录>
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

import pikepdf

PROG = False
SUFFIX = "_ocr"
DEFAULT_LANGS = "ko-KR,en-US,zh-Hans"


def emit(*a):
    if PROG:
        print("@@" + " ".join(str(x) for x in a), flush=True)


def die(msg):
    sys.exit(f"错误: {msg}")


def find_helper():
    """sphelper:应用包里在 Contents/Helpers/,命令行下看 PATH 和本脚本旁边"""
    here = os.path.dirname(os.path.abspath(__file__))
    cands = [os.path.join(here, "sphelper"),
             os.path.join(here, "..", "Helpers", "sphelper"),
             os.path.join(here, "..", "app", "build", "sphelper")]
    for c in cands:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return os.path.abspath(c)
    import shutil
    w = shutil.which("sphelper")
    if w:
        return w
    die("找不到 sphelper(Vision 桥接工具)。命令行下先编译:\n"
        "  swiftc -swift-version 5 -O app/SPHelper.swift -o scripts/sphelper")


# ---------------------------------------------------------------- 隐形字体
# 不可见文字层要能被搜索,就得让阅读器知道每个字对应哪个 Unicode。做法是:
# Type0 + Identity-H 编码,把「CID 直接等于 Unicode 码位」,再配一份 ToUnicode
# CMap 做恒等映射。字形文件不嵌入 —— 反正渲染模式是 3(不绘制),没有字形要画。
def tounicode_cmap():
    head = ("/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
            "/CMapName /Identity-UTF16 def\n/CMapType 2 def\n"
            "1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n")
    body = []
    # bfrange 每块最多 100 条,且只有末字节递增,所以按高字节切成 256 段
    rows = [f"<{h:02X}00> <{h:02X}FF> <{h:02X}00>" for h in range(256)]
    for i in range(0, 256, 100):
        chunk = rows[i:i + 100]
        body.append(f"{len(chunk)} beginbfrange\n" + "\n".join(chunk) + "\nendbfrange\n")
    tail = "endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n"
    return (head + "".join(body) + tail).encode("ascii")


def make_font(pdf):
    desc = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.FontDescriptor, FontName=pikepdf.Name("/SPInvisible"),
        Flags=4, FontBBox=[0, -200, 1000, 900], ItalicAngle=0,
        Ascent=900, Descent=-200, CapHeight=700, StemV=80))
    cid = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.Font, Subtype=pikepdf.Name.CIDFontType2,
        BaseFont=pikepdf.Name("/SPInvisible"),
        CIDSystemInfo=pikepdf.Dictionary(Registry="Adobe", Ordering="Identity", Supplement=0),
        # 不写 CIDToGIDMap:字形没有嵌入,它没有意义,写了 CoreGraphics 反而会抱怨
        FontDescriptor=desc, DW=1000))
    return pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type0,
        BaseFont=pikepdf.Name("/SPInvisible"), Encoding=pikepdf.Name("/Identity-H"),
        DescendantFonts=[cid], ToUnicode=pdf.make_stream(tounicode_cmap())))


def text_layer(lines, pw, ph):
    """把识别结果拼成一段内容流。`3 Tr` = 既不描边也不填充,即完全不可见。"""
    out = ["q", "BT", "3 Tr"]
    for ln in lines:
        s = ln["text"]
        # Identity-H 下字符串就是 2 字节 CID 序列,我们让 CID 等于 UTF-16BE 码元
        try:
            hexs = s.encode("utf-16-be").hex().upper()
        except Exception:
            continue
        n = max(1, len(s))
        w = ln["w"] * pw
        h = ln["h"] * ph
        x = ln["x"] * pw
        y = ln["y"] * ph
        size = max(1.0, h)
        natural = n * size                      # DW=1000 ⇒ 每字宽度恰为一个字号
        tz = max(1.0, min(1000.0, 100.0 * w / natural)) if natural else 100.0
        out.append(f"/SPF {size:.2f} Tf {tz:.1f} Tz 1 0 0 1 {x:.2f} {y:.2f} Tm <{hexs}> Tj")
    out += ["ET", "Q"]
    return "\n".join(out).encode("latin-1", "replace")


# ---------------------------------------------------------------- 主流程
def page_px(page, target_w):
    box = [float(v) for v in page.MediaBox]
    pw, ph = box[2] - box[0], box[3] - box[1]
    w = int(target_w)
    return w, max(1, int(round(w * ph / pw))), pw, ph


def ocr_pdf(src, dst, helper, langs, target_w, jobs):
    pdf = pikepdf.open(src)
    n = len(pdf.pages)
    work = tempfile.mkdtemp()
    emit("S", "识别")
    done = [0]

    def one(i):
        page = pdf.pages[i]
        w, h, pw, ph = page_px(page, target_w)
        png = os.path.join(work, f"p{i}.png")
        lines = []
        try:
            r = subprocess.run([helper, "render", src, str(i + 1), str(w), str(h), png],
                               capture_output=True)
            if r.returncode == 0:
                r2 = subprocess.run([helper, "ocr", png, langs], capture_output=True, text=True)
                for row in r2.stdout.splitlines():
                    try:
                        lines.append(json.loads(row))
                    except Exception:
                        pass
        finally:
            if os.path.exists(png):
                os.remove(png)
        done[0] += 1
        emit("P", "识别", done[0], n)
        return i, lines, pw, ph

    with ThreadPoolExecutor(max_workers=jobs) as ex:
        results = list(ex.map(one, range(n)))
    os.rmdir(work) if not os.listdir(work) else None

    emit("S", "写出")
    font = make_font(pdf)
    total = 0
    for i, lines, pw, ph in sorted(results):
        if not lines:
            continue
        total += len(lines)
        page = pdf.pages[i]
        res = page.get("/Resources")
        if res is None:
            page.Resources = pikepdf.Dictionary()
            res = page.Resources
        if "/Font" not in res:
            res.Font = pikepdf.Dictionary()
        res.Font["/SPF"] = font
        page.contents_add(pdf.make_stream(text_layer(lines, pw, ph)), prepend=False)
    pdf.save(dst)
    return total, n


def main():
    ap = argparse.ArgumentParser(description="给影印 PDF 加不可见文字层(macOS Vision)")
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("-o", "--output")
    ap.add_argument("--out-dir")
    ap.add_argument("--lang", default=DEFAULT_LANGS, help=f"识别语言(默认 {DEFAULT_LANGS})")
    ap.add_argument("--width", type=int, default=2000, help="送进 OCR 的渲染宽度 px(默认 2000)")
    ap.add_argument("--jobs", type=int, default=max(2, (os.cpu_count() or 4) // 2))
    ap.add_argument("--progress", action="store_true")
    o = ap.parse_args()

    global PROG
    PROG = o.progress
    helper = find_helper()

    files = []
    for p in o.inputs:
        if os.path.isdir(p):
            files += [os.path.join(p, f) for f in sorted(os.listdir(p))
                      if f.lower().endswith(".pdf") and not f.startswith(".")]
        else:
            files.append(p)
    if not files:
        die("没找到 PDF")
    if o.output and len(files) > 1:
        die("-o 只能配单个 PDF;批量请用 --out-dir")

    for i, f in enumerate(files, 1):
        if o.output:
            dst = o.output
        else:
            d = o.out_dir or os.path.dirname(os.path.abspath(f))
            os.makedirs(d, exist_ok=True)
            base, ext = os.path.splitext(os.path.basename(f))
            dst = os.path.join(d, base + SUFFIX + ext)
        if os.path.abspath(dst) == os.path.abspath(f):
            die(f"输出会覆盖原文件: {dst}")
        print(f"[{i}/{len(files)}] {os.path.basename(f)}")
        lines, pages = ocr_pdf(f, dst, helper, o.lang, o.width, o.jobs)
        before, after = os.path.getsize(f), os.path.getsize(dst)
        print(f"    {pages} 页,识别 {lines} 行  |  {before / 1048576:.1f} → "
              f"{after / 1048576:.1f} MB(+{(after - before) / 1024 / max(1, pages):.1f} KB/页)")
        print(f"    → {dst}")
    emit("DONE", 0, len(files), "")


if __name__ == "__main__":
    main()
