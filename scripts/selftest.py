#!/usr/bin/env python3
"""一条命令的回归自测:改完代码先跑它,红了就是改坏了。

    ~/.venvs/pdfenv/bin/python scripts/selftest.py     (全绿约 1 分钟)

**不比对「金样」哈希**——那会被 ImageMagick / macOS 版本升级误伤。只查不变量:
  T1 bw     同一批图跑两遍,图像流逐字节一致(确定性);页数 / JBIG2 编码 / A4 正确
  T2 mrc    关分层内容页 = 底图+黑字蒙版恰 2 层,开分层 = 5 层,封面恒 1 层
            (必须递归穿进 Form XObject 数——get_images() 跳过 ImageMask,数不出蒙版)
  T3 shrink CCITT G4 → JBIG2 后按原生分辨率渲染,与原件逐像素一致(无损承诺)
  T4 ocr    加层后外观逐像素不变;sidecar 含预期词;对成品重跑全部跳过(幂等)
  T5 merge  页数相加正确

素材是脚本自己用 magick 画的(黑白文字页 + 彩色封面),不往仓库塞扫描图。
需要开发机环境:homebrew 的 magick/jbig2 + pdfenv + scripts/sphelper(build_app.py 会顺手编译,
或手动 `swiftc -swift-version 5 -O app/SPHelper.swift -o scripts/sphelper`)。
"""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time

import pikepdf
from PIL import Image, ImageChops

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
FIXTURE = "spselftest"          # 缓存目录名跟着源目录名走,固定它便于清理
A4 = (0, 0, 595, 842)

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  ({detail})" if detail and not ok else ""))
    passed, failed = passed + ok, failed + (not ok)


def run(args, **kw):
    r = subprocess.run(args, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        print(f"    ⚠ 命令失败: {' '.join(os.path.basename(str(a)) for a in args[:3])}…")
        print(f"      {(r.stderr or r.stdout).strip()[:300]}")
    return r


def find_font():
    for f in ("/System/Library/Fonts/HelveticaNeue.ttc", "/System/Library/Fonts/Helvetica.ttc",
              "/System/Library/Fonts/Geneva.ttf"):
        if os.path.exists(f):
            return f
    return None


def make_fixture(d, font):
    """6 页 1000×1400:1/6 是浅色彩色封面,2–5 是内容页(黑字 + 一块红色矩形喂给 mrc)"""
    os.makedirs(d, exist_ok=True)
    for i in range(1, 7):
        cover = i in (1, 6)
        args = ["magick", "-size", "1000x1400", "xc:" + ("#dbe6f4" if cover else "white")]
        if font:
            args += ["-font", font, "-fill", "black",
                     "-pointsize", "64", "-annotate", "+80+200", f"SCANPRESS TEST PAGE {i}",
                     "-pointsize", "40", "-annotate", "+80+330", "lorem ipsum dolor sit amet",
                     "-annotate", "+80+410", "consectetur adipiscing elit sed do"]
        args += ["-fill", "#c03028", "-draw", "rectangle 80,600 400,780",
                 "-stroke", "black", "-strokewidth", "3", "-fill", "none",
                 "-draw", "line 80,900 920,900", "-draw", "line 80,960 920,960",
                 os.path.join(d, f"img{i:03d}.png")]
        run(args)


def streams(path):
    return [hashlib.md5(im.read_raw_bytes()).hexdigest()
            for pg in pikepdf.open(path).pages
            for _, im in sorted(pg.get_images().items())]


def layers(path, page):
    """递归穿进 Form XObject 数真实图层(get_images 会跳过 ImageMask)"""
    pdf = pikepdf.open(path)          # 拿住引用:临时对象在遍历中会被回收(destroyed)
    out = {}

    def walk(res):
        for k, v in (res.get("/XObject") or {}).items():
            if v.get("/Subtype") == pikepdf.Name.Form:
                walk(v.get("/Resources") or {})
            else:
                out[str(k)] = str(v.get("/Filter"))
    walk(pdf.pages[page].get("/Resources") or {})
    return out


def pages_identical(helper, a, b, page, w, h, tmp):
    pa, pb = os.path.join(tmp, "_ra.png"), os.path.join(tmp, "_rb.png")
    if run([helper, "render", a, str(page), str(w), str(h), pa]).returncode != 0:
        return False
    if run([helper, "render", b, str(page), str(w), str(h), pb]).returncode != 0:
        return False
    A = Image.open(pa).convert("L").point(lambda x: 0 if x < 128 else 255)
    B = Image.open(pb).convert("L").point(lambda x: 0 if x < 128 else 255)
    same = ImageChops.difference(A, B).getbbox() is None
    os.remove(pa), os.remove(pb)
    return same


def main():
    t0 = time.time()
    sys.path.insert(0, HERE)
    import ocr_pdf as _ocr
    helper = _ocr.find_helper()
    font = find_font()
    tmp = tempfile.mkdtemp(prefix="sptest")
    src = os.path.join(tmp, FIXTURE)
    make_fixture(src, font)
    cache = os.path.expanduser("~/.cache/scan2pdf/" + FIXTURE)
    scan2 = os.path.join(HERE, "scan2pdf.py")

    print("T1 bw 确定性")
    a, b = os.path.join(tmp, "bw1.pdf"), os.path.join(tmp, "bw2.pdf")
    for out in (a, b):
        shutil.rmtree(cache, ignore_errors=True)
        run([PY, scan2, src, out, "--mode", "bw", "--enhance"])
    ok = os.path.exists(a) and os.path.exists(b)
    check("跑两遍图像流逐字节一致", ok and streams(a) == streams(b))
    if ok:
        pdf = pikepdf.open(a)
        check("页数 6", len(pdf.pages) == 6)
        boxes = {tuple(round(float(x)) for x in p.MediaBox) for p in pdf.pages}
        check("全部 A4", boxes == {A4}, str(boxes))
        filt = {str(im.get("/Filter")) for pg in pdf.pages[1:5]
                for im in pg.get_images().values()}
        check("内容页为 JBIG2", "/JBIG2Decode" in filt, str(filt))

    print("T2 mrc 图层结构")
    m1, m2 = os.path.join(tmp, "mrc1.pdf"), os.path.join(tmp, "mrc2.pdf")
    shutil.rmtree(cache, ignore_errors=True)
    run([PY, scan2, src, m1, "--mode", "mrc", "--mrc-no-color-layers",
         "--mrc-mask-width", "1000", "--mrc-bg-width", "400"])
    shutil.rmtree(cache, ignore_errors=True)
    run([PY, scan2, src, m2, "--mode", "mrc",
         "--mrc-mask-width", "1000", "--mrc-bg-width", "400"])
    if os.path.exists(m1):
        check("关分层 = Bg+M0 两层", set(layers(m1, 2)) == {"/Bg", "/M0"},
              str(sorted(layers(m1, 2))))
        check("封面恒 1 层", len(layers(m1, 0)) == 1, str(layers(m1, 0)))
    else:
        check("关分层 mrc 产出存在", False)
    if os.path.exists(m2):
        check("开分层 = Bg+M0..M3 五层",
              set(layers(m2, 2)) == {"/Bg", "/M0", "/M1", "/M2", "/M3"},
              str(sorted(layers(m2, 2))))
    else:
        check("开分层 mrc 产出存在", False)

    print("T3 shrink 无损")
    import img2pdf
    g4, slim = os.path.join(tmp, "g4.pdf"), os.path.join(tmp, "slim.pdf")
    tifs = []
    for i in (2, 3, 4):
        t = os.path.join(tmp, f"g{i}.tif")
        run(["magick", os.path.join(src, f"img{i:03d}.png"), "-threshold", "55%",
             "-compress", "Group4", t])
        tifs.append(t)
    with open(g4, "wb") as fh:
        fh.write(img2pdf.convert(tifs))
    run([PY, os.path.join(HERE, "shrink_pdf.py"), g4, "-o", slim, "--force"])
    ok = os.path.exists(slim)
    check("产出存在且不大于原件", ok and os.path.getsize(slim) <= os.path.getsize(g4))
    check("逐像素与原件一致(无损)", ok and all(
        pages_identical(helper, g4, slim, p, 1000, 1400, tmp) for p in (1, 2, 3)))

    print("T4 ocr 文字层")
    if not font or not os.path.exists(slim):
        print("  (跳过:缺字体或缺 T3 产出)")
    else:
        ocrd = os.path.join(tmp, "slim_ocr.pdf")
        run([PY, os.path.join(HERE, "ocr_pdf.py"), slim, "-o", ocrd,
             "--lang", "en-US", "--sidecar", "--force"])
        side = os.path.splitext(ocrd)[0] + ".txt"
        txt = open(side, encoding="utf-8").read() if os.path.exists(side) else ""
        check("sidecar 含预期词", "TEST PAGE" in txt or "lorem" in txt.lower())
        import pypdf
        extracted = pypdf.PdfReader(ocrd).pages[0].extract_text().upper()
        check("PDF 文字层可抽取", "TEST" in extracted or "LOREM" in extracted)
        check("外观逐像素不变", all(
            pages_identical(helper, slim, ocrd, p, 1000, 1400, tmp) for p in (1, 2)))
        r2 = run([PY, os.path.join(HERE, "ocr_pdf.py"), ocrd,
                  "-o", os.path.join(tmp, "o2.pdf"), "--lang", "en-US", "--force"])
        check("对成品重跑全部跳过(幂等)", "跳过 3 页" in r2.stdout, r2.stdout.strip()[-120:])

    print("T5 merge")
    merged = os.path.join(tmp, "merged.pdf")
    if os.path.exists(a) and os.path.exists(m1):
        run([PY, os.path.join(HERE, "merge_pdfs.py"), merged, a, m1])
        check("页数相加", os.path.exists(merged) and len(pikepdf.open(merged).pages) == 12)
    else:
        check("合并前置产出存在", False)

    shutil.rmtree(cache, ignore_errors=True)
    total = passed + failed
    if failed == 0:
        shutil.rmtree(tmp, ignore_errors=True)
        print(f"\n全部通过 ✅  ({total} 项,{time.time() - t0:.0f} 秒)")
    else:
        print(f"\n{failed}/{total} 项失败 ❌  现场保留在 {tmp}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
