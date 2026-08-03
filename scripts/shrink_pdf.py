#!/usr/bin/env python3
"""给已有的 PDF 瘦身 —— 不改变可见内容,只换更好的图像编码。

与 scan2pdf.py 的分工:那边是「源图 → PDF」,这边是「PDF → 更小的 PDF」。

两条路,按页面图像的性质自动分流:
  ① 无损重压(bilevel):1 位黑白图(CCITT G4 / Flate)改用 **JBIG2 通用编码**。
     位图一个比特都不变,放大到任何倍数都与原件相同,**没有画质取舍可谈**。
     实测自扫韩语书 354.8 MB → 155.2 MB(0.44×),逐像素比对 1742 万像素零差异。
     刻意不用 JBIG2 的符号模式:那个靠「字形聚类」省体积,是有损的,存在字符替换
     风险(著名的施乐复印机数字调包事故);而且实测在带扫描噪声的原始扫描上,
     符号模式反而比通用编码大 19%——噪声让同一个字每次的像素都不同,词典失效。
  ② MRC 重制(彩色/灰度):复用 scan2pdf.py 的 mrc 分层重做。**这条有画质取舍**。
     ⚠ **用户 2026-08-03 看过样张后否掉了:「还是觉得原图好看」。** 技术上是成功的
     (167 MB 教材 → 约 21 MB,文字层反而从 4960px JPEG 升成 2600px 黑白蒙版,
     原有 OCR 文字层也完整保留),但对**已经存在、看着就挺好**的彩色书来说,
     用户心里的基准线是「我已经有它了」,那个门槛比「从散图新做一本」高得多。
     故此路**只留命令行(--mrc),不进 GUI、不做默认**,与 smooth/vector 同等待遇。
     注意这不影响 scan2pdf.py 从源图新建时的 mrc 档 —— 那个用户是认可并在用的。

文字层、书签、注释、元数据一律原样保留:①只替换图像流,PDF 的其余结构不动。

用法:
  shrink_pdf.py <PDF或目录> ... --analyze        # 只体检,报告能省多少,不写文件
  shrink_pdf.py 书.pdf                           # 无损重压,写成 书_slim.pdf
  shrink_pdf.py 书.pdf -o 输出.pdf
  shrink_pdf.py <目录> --out-dir <目录>           # 批量
"""
import argparse
import contextlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

import pikepdf
from PIL import Image

PROG = False
SUFFIX = "_slim"


def emit(*a):
    if PROG:
        print("@@" + " ".join(str(x) for x in a), flush=True)


def die(msg):
    sys.exit(f"错误: {msg}")


def human(n):
    return f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{n / 1024:.0f} KB"


def list_pdfs(paths):
    out = []
    for p in paths:
        if os.path.isdir(p):
            out += [os.path.join(p, f) for f in sorted(os.listdir(p))
                    if f.lower().endswith(".pdf") and not f.startswith(".")]
        elif p.lower().endswith(".pdf"):
            out.append(p)
        else:
            die(f"不是 PDF 也不是目录: {p}")
    if not out:
        die("没找到 PDF")
    return out


def page_images(pdf):
    """遍历全部图像 XObject,按对象去重(同一张图可能被多页引用)"""
    seen = set()
    for i, page in enumerate(pdf.pages):
        for _, im in page.get_images().items():
            key = im.objgen
            if key in seen:
                continue
            seen.add(key)
            yield i, im


def is_bilevel(im):
    """1 位黑白图 —— 走无损重压这条路的判据"""
    try:
        if int(im.get("/BitsPerComponent", 8)) != 1:
            return False
        # ImageMask 是蒙版而非图像,替换它要连 /Decode 语义一起管,第一版不碰
        return not bool(im.get("/ImageMask", False))
    except Exception:
        return False


@contextlib.contextmanager
def capture_cstderr():
    """接住 C 层(libtiff)写到 fd 2 的抱怨。

    **这是本脚本最要紧的一道保险。** 实测某本自扫书里有两页的 CCITT 流是损坏的
    (libtiff 报 `Fax4Decode: Bad code word`),Pillow 不抛异常、照样返回一张图,
    但那张图与系统渲染器解出来的内容差了 8% 的像素 —— 若照此重编码,就等于
    悄悄改掉了两页内容,而「无损」的承诺是这个功能唯一的立身之本。
    Python 层的 warnings 捕获不到它,只能在文件描述符层面接。
    """
    fd = sys.stderr.fileno()
    saved = os.dup(fd)
    tmp = tempfile.TemporaryFile()
    try:
        sys.stderr.flush()
        os.dup2(tmp.fileno(), fd)
        yield tmp
    finally:
        sys.stderr.flush()
        os.dup2(saved, fd)
        os.close(saved)


def decode_to_png(im, work, name):
    """解码一张 1 位图到 PNG。解码器有任何抱怨就返回 None —— 宁可不压,也不能压错。

    **必须串行调用**:capture_cstderr 重定向的是进程全局的 fd 2,多线程并发会互相串音。
    好在解码只占小头,耗时的 jbig2 编码仍可并行。
    """
    png = name + ".png"
    path = os.path.join(work, png)
    with capture_cstderr() as err:
        try:
            pil = pikepdf.PdfImage(im).as_pil_image()
            pil.load()                        # 强制真正解码,不能停在惰性状态
            pil.save(path)
        except Exception:
            err.seek(0)
            _ = err.read()
            return None
        err.seek(0)
        noise = err.read().strip()
    if noise:                                 # 源流损坏,原样保留这张图
        if os.path.exists(path):
            os.remove(path)
        return None
    return png


def encode_png(png, work):
    """PNG → JBIG2 通用编码(无损)。jbig2/leptonica 会重映射 /tmp 开头的绝对路径,
    所以一律 cwd=工作目录 + 相对文件名。"""
    r = subprocess.run(["jbig2", "-p", png], capture_output=True, cwd=work)
    os.remove(os.path.join(work, png))
    return r.stdout if r.returncode == 0 and r.stdout else None


def to_jbig2(im, work, name):
    """体检用:解码 + 编码一步到位(单线程场景)"""
    png = decode_to_png(im, work, name)
    return encode_png(png, work) if png else None


def probe(path, sample=10):
    """体检:看构成 + 抽样实压估算能省多少。不写任何文件。"""
    try:
        pdf = pikepdf.open(path)
    except Exception as e:
        return {"path": path, "error": str(e)[:60]}
    size = os.path.getsize(path)
    n = len(pdf.pages)
    bi = color = 0
    bi_bytes = color_bytes = 0
    for _, im in page_images(pdf):
        try:
            raw = len(im.read_raw_bytes())
        except Exception:
            continue
        if is_bilevel(im):
            bi += 1
            bi_bytes += raw
        else:
            color += 1
            color_bytes += raw
    has_text = any("/Font" in (p.get("/Resources") or {}) for p in pdf.pages[:30])

    # 抽样真压,不靠经验系数
    ratio = None
    if bi:
        with tempfile.TemporaryDirectory() as work:
            old = new = 0
            k = 0
            for _, im in page_images(pdf):
                if k >= sample:
                    break
                if not is_bilevel(im):
                    continue
                try:
                    data = to_jbig2(im, work, f"s{k}")
                except Exception:
                    continue
                if data:
                    old += len(im.read_raw_bytes())
                    new += len(data)
                    k += 1
            if old:
                ratio = new / old
    return {"path": path, "size": size, "pages": n, "bilevel": bi, "color": color,
            "bi_bytes": bi_bytes, "color_bytes": color_bytes,
            "has_text": has_text, "ratio": ratio}


def report(rows):
    print(f"{'文件':40s} {'现状':>9s} {'页数':>6s} {'类型':>10s} {'文字层':>6s} {'预计':>9s}")
    print("-" * 88)
    tot_now = tot_new = 0
    for r in rows:
        name = os.path.basename(r["path"])
        if r.get("error"):
            print(f"{name[:38]:40s} 打不开: {r['error']}")
            continue
        kind = ("黑白" if r["bilevel"] and not r["color"] else
                "彩色/灰度" if r["color"] and not r["bilevel"] else
                "混合" if r["bilevel"] else "无图像")
        if r["ratio"] is not None:
            saved = r["bi_bytes"] * (1 - r["ratio"])
            est = r["size"] - saved
            note = f"{est / 1048576:.1f} MB"
        else:
            est = r["size"]
            note = "需 MRC" if r["color"] else "—"
        tot_now += r["size"]
        tot_new += est
        print(f"{name[:38]:40s} {human(r['size']):>9s} {r['pages']:>6d} {kind:>10s} "
              f"{'有' if r['has_text'] else '无':>6s} {note:>9s}")
    print("-" * 88)
    print(f"{'合计':40s} {human(tot_now):>9s} → {human(tot_new)}  "
          f"省下 {human(tot_now - tot_new)}({(1 - tot_new / tot_now) * 100:.0f}%)"
          if tot_now else "")


def shrink(src, dst, jobs=4):
    """无损重压:只替换 1 位图像的编码,PDF 其余结构(文字层/书签/注释)原样保留"""
    pdf = pikepdf.open(src)
    targets = [(i, im) for i, im in page_images(pdf) if is_bilevel(im)]
    if not targets:
        return None
    results = {}
    skipped = 0
    with tempfile.TemporaryDirectory() as work:
        emit("S", "解码")
        pngs = {}
        for k, (_, im) in enumerate(targets):          # 串行:见 decode_to_png 的说明
            try:
                png = decode_to_png(im, work, f"w{k}")
            except Exception:
                png = None
            if png:
                pngs[k] = png
            else:
                skipped += 1
            emit("P", "解码", k + 1, len(targets))

        emit("S", "压缩")
        done = [0]

        def one(item):
            k, png = item
            try:
                data = encode_png(png, work)
            except Exception:
                data = None
            done[0] += 1
            emit("P", "压缩", done[0], len(pngs))
            return k, data

        with ThreadPoolExecutor(max_workers=jobs) as ex:
            for k, data in ex.map(one, list(pngs.items())):
                results[k] = data

        for k, (_, im) in enumerate(targets):
            data = results.get(k)
            if not data or len(data) >= len(im.read_raw_bytes()):
                continue                      # 压不动就别动它
            for key in ("/DecodeParms", "/Decode"):
                if key in im:
                    del im[key]               # CCITT 的解码参数/极性已在解码时消化掉
            im.write(data, filter=pikepdf.Name.JBIG2Decode)
            im.ColorSpace = pikepdf.Name.DeviceGray
            im.BitsPerComponent = 1
    emit("S", "写出")
    pdf.save(dst, compress_streams=True, object_stream_mode=pikepdf.ObjectStreamMode.generate)
    return os.path.getsize(dst), skipped, len(targets)


# ---------------------------------------------------------------- ② MRC 重制
def load_engine():
    """借用 scan2pdf.py 的 MRC 实现。mrc_layers 是全项目最难的函数(六轮排错的成果),
    绝不在这里重写一遍 —— 两处分层逻辑一旦分家,以后改一边忘一边必出事。"""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import scan2pdf
    return scan2pdf


def text_ops(page):
    """把一页里「画文字」的操作挑出来(BT…ET 之间的部分),其余(画图像等)全部丢弃。

    MRC 重制会把一张图换成「底图 + 若干蒙版」,页面内容流必须重建;而原页可能带着
    OCR 过的文字层(实测那本 167 MB 的韩语教材就有)——**丢了它等于把可搜索的书压成
    不可搜索的**,那是用户眼里的倒退,比省下的体积重要得多。
    """
    try:
        ops = pikepdf.parse_content_stream(page)
    except Exception:
        return None
    keep, inside = [], False
    for operands, operator in ops:
        o = str(operator)
        if o == "BT":
            inside = True
        if inside:
            keep.append((operands, operator))
        if o == "ET":
            inside = False
    if not keep:
        return None
    try:
        return pikepdf.unparse_content_stream(keep)
    except Exception:
        return None


def page_single_image(page):
    """只处理「整页就是一张图」的页面(扫描书的常态)。多图/矢量页原样保留,不冒险。"""
    ims = list(page.get_images().values())
    return ims[0] if len(ims) == 1 else None


def remake_mrc(src, dst, opt):
    """把彩色/灰度扫描页用 MRC 分层重做。**这条有画质取舍**,先用 --pages 出样张再全书跑。"""
    sp = load_engine()
    pdf = pikepdf.open(src)
    n = len(pdf.pages)
    idx = [i for i in range(n) if not opt.pages or (i + 1) in opt.pages]
    todo = [i for i in idx if page_single_image(pdf.pages[i]) is not None]
    if not todo:
        die("没有「整页一张图」的页面可重制(多图或矢量页不处理)")

    work = tempfile.mkdtemp(prefix="spmrc")
    emit("S", "导出")
    srcs = {}
    for k, i in enumerate(todo):
        im = page_single_image(pdf.pages[i])
        srcs[i] = pikepdf.PdfImage(im).extract_to(fileprefix=os.path.join(work, f"src{k:05d}"))
        emit("P", "导出", k + 1, len(todo))

    emit("S", "分层")
    colors = {}

    def one(i):
        c = sp.mrc_layers(srcs[i], work, f"p{i:05d}", opt.mrc_dark, opt.mrc_chroma,
                          opt.mrc_mask_width, opt.mrc_bg_width, opt.mrc_bg_quality,
                          color_layers=opt.mrc_color_layers)
        colors[i] = c or [(0.0, 0.0, 0.0)] * sp.MRC_SLOTS
        return True

    sp.pmap(one, todo, opt.jobs, "分层", "分层")

    emit("S", "压缩")
    groups = [[f"p{i:05d}_black.png" for i in todo]]
    for c in range(sp.MRC_SLOTS):
        groups.append([f"p{i:05d}_c{c}.png" for i in todo])
    bgs = [os.path.join(work, f"p{i:05d}_bg.jpg") for i in todo]
    new = sp.mrc_compose(work, "r", groups, bgs, [colors[i] for i in todo], opt.jbig2_slots)

    emit("S", "写出")
    out = pikepdf.new()
    for i in range(n):
        orig = pdf.pages[i]
        if i not in todo:
            out.pages.append(orig)                      # 没重制的页原样搬过去
            continue
        out.pages.append(new.pages[todo.index(i)])
        page = out.pages[-1]
        box = [float(v) for v in orig.MediaBox]
        pw, ph = box[2] - box[0], box[3] - box[1]
        # 跨文档不能直接把对象搬过来,取成纯数值再写
        page.MediaBox = [box[0], box[1], box[2], box[3]]
        if "/Rotate" in orig:
            page.Rotate = int(orig.Rotate)              # 不带过来的话页面会转向
        # 重建内容流:把各图层铺满原页面的尺寸(而不是 mrc_compose 默认的「像素即点」)
        body = bytes(page.Contents.read_bytes()).decode("latin-1")
        body = re.sub(r"\d+(?:\.\d+)? 0 0 \d+(?:\.\d+)? 0 0 cm",
                      f"{pw:.3f} 0 0 {ph:.3f} {box[0]:.3f} {box[1]:.3f} cm", body)
        txt = text_ops(orig)
        if txt:                                          # 原有文字层原样接回去
            body += "\n" + txt.decode("latin-1")
            res = orig.get("/Resources")
            if res is not None and "/Font" in res:
                # copy_foreign 只收间接对象;源里的 /Font 常是直接写在页面上的,先提为间接
                fonts = res["/Font"]
                if not fonts.is_indirect:
                    fonts = pdf.make_indirect(fonts)
                page.Resources["/Font"] = out.copy_foreign(fonts)
        page.Contents = out.make_stream(body.encode("latin-1"))
    out.save(dst)
    shutil.rmtree(work, ignore_errors=True)
    return os.path.getsize(dst), len(todo), n


def main():
    ap = argparse.ArgumentParser(description="给已有 PDF 瘦身(无损重压 1 位图像为 JBIG2)")
    ap.add_argument("inputs", nargs="+", help="PDF 文件或目录")
    ap.add_argument("-o", "--output", help="输出文件(仅单个输入时有效)")
    ap.add_argument("--out-dir", help="批量输出目录(默认与原文件同目录,加 _slim 后缀)")
    ap.add_argument("--analyze", action="store_true", help="只体检不写文件")
    ap.add_argument("--sample", type=int, default=10, help="体检抽样页数(默认 10)")
    ap.add_argument("--jobs", type=int, default=max(2, (os.cpu_count() or 4) // 2))
    ap.add_argument("--progress", action="store_true", help="输出 @@ 进度行供 GUI 解析")
    g = ap.add_argument_group("MRC 重制(彩色/灰度页,有画质取舍)")
    g.add_argument("--mrc", action="store_true", help="用 MRC 分层重做彩色页")
    g.add_argument("--pages", help="只处理这些页(如 20-30),先出样张再全书跑")
    g.add_argument("--mrc-dark", default="50%")
    g.add_argument("--mrc-chroma", default="35%")
    g.add_argument("--mrc-mask-width", type=int, default=2600)   # 教材档(GUI 默认)
    g.add_argument("--mrc-bg-width", type=int, default=1500)
    g.add_argument("--mrc-bg-quality", type=int, default=50)
    g.add_argument("--mrc-color-layers", action="store_true",
                   help="开彩色文字分层(漫画/绘本合适;彩字密集的教材开了会「像被虫啃」)")
    g.add_argument("--jbig2-slots", type=int, default=8)
    o = ap.parse_args()
    if o.pages:
        o.pages = {int(x) for part in o.pages.split(",") for x in
                   (range(int(part.split("-")[0]), int(part.split("-")[1]) + 1)
                    if "-" in part else [int(part)])}

    global PROG
    PROG = o.progress
    files = list_pdfs(o.inputs)

    if o.analyze:
        rows = []
        for i, f in enumerate(files, 1):
            emit("P", "体检", i, len(files))
            rows.append(probe(f, o.sample))
        report(rows)
        return

    if o.output and len(files) > 1:
        die("-o 只能配单个 PDF;批量请用 --out-dir")

    tot_before = tot_after = 0
    for i, f in enumerate(files, 1):
        emit("P", "文件", i, len(files))
        if o.output:
            dst = o.output
        else:
            d = o.out_dir or os.path.dirname(os.path.abspath(f))
            os.makedirs(d, exist_ok=True)
            base, ext = os.path.splitext(os.path.basename(f))
            dst = os.path.join(d, base + SUFFIX + ext)
        if os.path.abspath(dst) == os.path.abspath(f):
            die(f"输出会覆盖原文件: {dst}")
        before = os.path.getsize(f)
        print(f"[{i}/{len(files)}] {os.path.basename(f)}  {human(before)}")
        if o.mrc:
            after, made, total = remake_mrc(f, dst, o)
            scope = f"{made}/{total} 页重制" + ("(样张)" if o.pages else "")
            print(f"    → {human(after)}  {scope}  {dst}")
            continue
        try:
            res = shrink(f, dst, o.jobs)
        except Exception as e:
            print(f"    失败: {str(e)[:120]}")
            continue
        if res is None:
            print("    没有可无损重压的 1 位图像,跳过(彩色/灰度请用 scan2pdf.py 的 mrc 档)")
            continue
        after, skipped, total = res
        tot_before += before
        tot_after += after
        note = f"  ⚠ {skipped}/{total} 张原图解码异常,已原样保留" if skipped else ""
        print(f"    → {human(after)}  ({after / before:.3f}×,省 {human(before - after)})  {dst}{note}")
    if tot_before:
        print(f"\n合计 {human(tot_before)} → {human(tot_after)}  "
              f"省下 {human(tot_before - tot_after)}({(1 - tot_after / tot_before) * 100:.0f}%)")
        emit("DONE", tot_after, len(files), "")


if __name__ == "__main__":
    main()
