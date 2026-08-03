#!/usr/bin/env python3
"""扫描图 → 高清晰 / 最小体积 PDF(纯影印本)

与本项目主流程的区别:**不转录、不 OCR、无文字层、不生成 Word、不做色彩增强**,
因此全程确定性、无需 Claude 参与,一条命令跑完,可反复重跑/断点续做。
代价:成品不可搜索、不可选取复制、无目录书签。

用法:
  ~/.venvs/pdfenv/bin/python scan2pdf.py <源图目录> [输出.pdf] [选项]

常用选项:
  --mode bw|smooth|mrc|gray|color
                         bw=二值化+JBIG2(默认,文字书最小) smooth=平滑黑白(抗锯齿)
                         **mrc=彩色+锐利文字(推荐给彩色书):文字层 1-bit 高分辨率 + 彩色底图低分辨率,
                         198 页漫画实测 14.2 MB 而文字 2200px,普通彩色同等锐度要 3 倍体积**
                         gray=灰度JPEG color=原样彩色JPEG
  --width N              内容页缩放目标宽 px(只缩不放);0=保持源图原生宽
                         默认:bw 1800 / gray·color 1800。DPI@A4 ≈ 宽px ÷ 8.27
  --quality N            JPEG 质量,仅 gray/color(默认 45)
  --threshold P          二值化阈值,仅 bw(默认 55%,开 --enhance 时默认 58%;低=更白更细,高=更黑更粗)
  --enhance              提白+锐化预处理。黑白模式=激进提白+强锐化(让阈值切得准);
                         彩色/灰度模式=温和提白+S曲线+微增饱和+弱锐化(保住浅色细节)
  --supersample N        黑白边缘精细化(默认 1):2=阈值前放大 2 倍,放大看台阶细一半,体积约 ×2
  --enhance-level        提白区间(默认 15%,88%)
  --enhance-unsharp      锐化参数(默认 0x1.2+1.2+0.01)
  --range A-B            只取该编号区间(源图名带编号时按编号,否则按 1 起的序号)
  --include 3,7,10-12    只处理这些编号(选中若干单个文件时用)
  --exclude 3,7,10-12    再排除若干页(书尾混入的杂散扫描等)
  --covers first,last    保留彩色的页(默认首末两张);none=全书统一处理
  --cover-width N        彩色封面缩放宽(默认 1400)
  --probe                只抽样几页试跑多个档位,打印体积/页与全书预估 + 导出样张,不做全书
  --probe-pages N        抽样页数(默认 8)
  --no-a4                不统一 A4(默认统一:等比缩放居中)
  --title / --author     PDF 元数据
  --work DIR             中间产物目录(默认 ~/.cache/scan2pdf/<源目录名>)
  --keep-cache           成功后保留中间产物(默认自动清理;失败/中断时一律保留以便续做)
  --jbig2-slots N        全局同时压几段 jbig2(默认 8,所有并行任务共抢,自动分摊)
  --clean                开跑前清空中间产物
  --jobs N               并行度(默认 CPU-1)

依赖:magick / jbig2 / jbig2topdf.py(homebrew)+ pikepdf / img2pdf(~/.venvs/pdfenv)。
"""
import argparse
import contextlib
import fcntl
import io
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import img2pdf
import pikepdf
from PIL import Image

A4 = (595.28, 841.89)
EXTS = (".jpg", ".jpeg", ".png", ".tif", ".tiff")

# --progress:额外输出机器可读进度行,供 GUI(ScanToPDF.app)解析;默认关闭,不影响人读输出。
#   @@P <stage> <done> <total> / @@S <stage> / @@DONE <bytes> <pages> <path>
PROG = False


def emit(*a):
    if PROG:
        print("@@" + " ".join(str(x) for x in a), flush=True)


def die(msg):
    sys.exit(f"错误: {msg}")


def run(args, **kw):
    r = subprocess.run(args, capture_output=True, **kw)
    if r.returncode != 0:
        err = (r.stderr or b"").decode("utf-8", "replace").strip()[:300]
        die(f"{' '.join(str(a) for a in args[:3])}... 失败: {err}")
    return r.stdout


def fresh(path):
    """产物存在且非空才算完成(0 字节残留一律重做)"""
    return os.path.exists(path) and os.path.getsize(path) > 0


def parse_ranges(spec):
    """'3,7,10-12' → {3,7,10,11,12}"""
    out = set()
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part.lstrip("-"):
            a, b = part.split("-", 1)
            out |= set(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return out


def list_images(src):
    """(路径, 编号) 列表,按编号(无编号则按名)自然排序"""
    files = [f for f in os.listdir(src) if f.lower().endswith(EXTS) and not f.startswith(".")]
    if not files:
        die(f"{src} 里没有图片文件({'/'.join(EXTS)})")
    items = []
    for f in sorted(files):
        m = re.search(r"(\d+)(?=\D*$)", os.path.splitext(f)[0])
        items.append((os.path.join(src, f), int(m.group(1)) if m else None))
    numbered = all(n is not None for _, n in items)
    if numbered:
        items.sort(key=lambda t: t[1])
        if len({n for _, n in items}) != len(items):     # 编号重复 → 退回序号
            numbered = False
    if not numbered:
        items = [(p, i) for i, (p, _) in enumerate(sorted(items), 1)]
    return items, numbered



def img_size(path):
    """读图像宽高。用 Pillow 直接解文件头(约 1ms),不再为每页多起一个 magick identify 进程
    (实测 43ms/页 —— 千页书就是 45 秒纯开销)。"""
    with Image.open(path) as im:
        return im.size


# ---------------- 单页处理 ----------------
def binarize(src, dst, width, threshold, enh=(), ss=1):
    """二值化:灰度 →(可选提白+锐化)→ 阈值 → 边缘清白(左右各 1.5% 宽、上下各 1% 高,清书脊阴影/黑边)。
    顺序:缩放 → 提白/锐化 → 阈值。缩放后再阈值,笔画比"阈值后缩放"干净得多;
    锐化放在缩放之后、阈值之前,让阈值切在已经锐利的边缘上(实测边缘更利、体积几乎不变)。"""
    if fresh(dst):
        return False
    w, h = img_size(src)
    resize = ["-resize", f"{width}x>"] if width else []
    if width and width < w:                              # 边缘涂白按输出尺寸算
        h = round(h * width / w)
        w = width
    # 超采样:阈值前把灰度图等比放大 ss 倍,阈值仍按平滑插值后的轮廓切,
    # 于是台阶粒度细 ss 倍(不增加真实细节,只是量化更细)。体积约 ×2。
    supersample = ["-resize", f"{ss * 100}%"] if ss > 1 else []
    if ss > 1:
        w, h = w * ss, h * ss
    sw, sh = w * 15 // 1000, h * 10 // 1000
    run(["magick", src, "-colorspace", "Gray"] + resize + list(enh) + supersample
        + ["-threshold", threshold,
           "-shave", f"{sw}x{sh}", "-bordercolor", "white", "-border", f"{sw}x{sh}", dst])
    return True


def smooth_bw(src, dst, width, threshold, enh=(), levels=3, depth=2):
    """平滑黑白(Photoshop「建立选区 → 调整边缘」的等价做法):

      ① 放大 4 倍后取阈值  = 在 4 倍精度上"抠"出文字选区
      ② 形态学平滑          = 对选区轮廓本身做平滑,削掉锯齿缺口(相当于「选择 › 修改 › 平滑」)
      ③ 缩回目标尺寸        = 4→1 的降采样天然产生抗锯齿灰边(相当于消除锯齿的选区渲染)
      ④ 量化到 5 级灰       = 边缘只需几级过渡就足够平滑,却让 PNG 无损压缩极其高效

    产出:背景纯白、笔画纯黑、只有边缘一圈过渡灰。**抗锯齿会补偿分辨率**,故可大幅降分辨率
    而观感不塌——1200px/3级灰/2bit 只要 22 KB/页(接近纯黑白的 13 KB),放大却无锯齿。
    用 PNG(Flate)无损编码,不引入 JPEG 伪影。
    """
    if fresh(dst):
        return False
    w, h = img_size(src)
    tw = width if (width and width < w) else w            # 目标输出宽
    pct = tw / w * 25.0                                   # 先 400%,再缩到目标 → 净 tw/w
    th = round(h * tw / w)
    sw, sh = tw * 15 // 1000, th * 10 // 1000
    run(["magick", src, "-colorspace", "Gray"] + list(enh)
        + ["-resize", "400%", "-threshold", threshold,
           "-morphology", "Smooth", "Diamond:2",
           "-resize", f"{pct:.4f}%",
           "-posterize", str(levels), "-depth", str(depth),
           "-shave", f"{sw}x{sh}", "-bordercolor", "white", "-border", f"{sw}x{sh}",
           "-strip", dst])
    return True


def vectorize(src, dst, threshold, enh=(), zoom=1, opt="1.0", turd=10):
    """矢量描摹(potrace):把文字轮廓追踪成贝塞尔曲线,存曲线而非像素。

    放大到任意倍数边缘都光滑(分辨率无关),字形仍是原书的样子(从扫描件描出来的)。
    代价:一页密排 CJK 文字约 136 KB —— 约为纯黑白的 10 倍、平滑栅格的 1.5~4 倍。
    适合几十页、要反复放大看的资料;几百页的大书不划算。
    zoom=2:先放大 2 倍再取阈值并平滑轮廓。**不可降到 1**——形态学平滑按像素计,
    在原生位图上会啃断笔画(实测字形碎裂不可读)。-t 10 丢弃 ≤10 像素杂点。
    """
    if fresh(dst):
        return False
    pbm = dst + ".pbm"
    # 形态学平滑的强度按像素计:zoom=1 时 Diamond:2 相当于 2x 图上的两倍强度,会把笔画啃断
    # (2026-07-31 实测事故)。故只在放大后的位图上平滑,原生位图直接描摹。
    morph = ["-morphology", "Smooth", "Diamond:2"] if zoom >= 2 else []
    run(["magick", src, "-colorspace", "Gray"] + list(enh)
        + ["-resize", f"{zoom * 100}%", "-threshold", threshold] + morph + [pbm])
    run(["potrace", "-b", "pdf", "-O", opt, "-t", str(turd), "-o", dst, pbm])
    os.remove(pbm)
    return True


# ---------------- MRC 混合分层(彩色 + 锐利文字)----------------
# 一页拆两层:①彩色底图大幅降分辨率只管颜色 ②1-bit 文字蒙版保持高分辨率只管"哪里是墨"。
# PDF 里底图打底,再用纯黑透过 /ImageMask 蒙版画上文字 → 文字锐度=黑白模式,色彩=彩色模式,
# 体积却比整页高分辨率彩色小得多(198 页漫画实测:普通彩色 35.7 MB → MRC 14.2 MB,
# 而文字分辨率从 900px 提到 2200px)。
#
# 六轮踩坑的结论,改参数前务必读:
#   (1) 判"是不是墨"必须是 (够暗) AND (色度够低) 两个条件。只看明暗会把红头发、深色衣服涂黑。
#   (2) 色度必须用 HCL 的 **C 通道(绝对色度)**,不能用 HSL/HSV 的饱和度——近黑像素的饱和度
#       会因分母趋零而虚高,导致黑笔画内部被误排除,只剩空心轮廓。
#   (3) 色度门不能太严:本书标题是**深蓝色**,门限 20% 会把笔画切碎、露出底图的模糊蓝字;
#       35% 时标题完整而彩色人物不受影响;55% 就开始把人物涂黑。
#   (4) 暗度门不能太松:65% 会把中性灰画面(路面)当成墨,出现黑斑;50% 干净。
#   (5) 底图必须**先挖掉墨再降分辨率**,否则锐字周围会有模糊鬼影和 JPEG 振铃噪点。
#   (6) 挖洞要按蒙版**原样**挖(设为透明、alpha 加权缩放),不能用"整幅模糊"填(模糊图含墨,
#       糊出脏色被色度采样拉成蓝紫拖影),也不能把洞放大(黑字盖不住,彩色区露白边)。
#   (7) 封面封底不做 MRC(纯彩色高分辨率),否则深色画面被蒙版涂黑。


MRC_SLOTS = 3          # 每页固定预留的彩色层数(不足的用空蒙版占位,几乎不占体积)


def mrc_layers(src, outdir, tag, dark, chroma, mask_w, bg_w, bg_q, det_w=1600, color_layers=True):
    """产出一页的全部图层:black.png + c0..c2.png(彩色层)+ bg.jpg,返回各彩色层的 RGB。

    判定"是不是墨":够暗 AND 色度低 → 黑墨;够暗 AND 色度高 AND 细结构 → 彩墨。
    彩墨再按**色相聚类**成最多 3 组,每组一层、填该组的代表色。

    踩过的坑(改参数前必读):
      · 色度用 HCL 的 C 通道(绝对色度),不能用 HSL 饱和度——近黑像素饱和度虚高,
        会把黑笔画内部挖空只剩轮廓。
      · 黑墨门与彩墨门必须是**同一个数值**,否则色度正好卡在中间的字(实测某绿色人名 25.4%)
        两边各收一半,笔画断续发虚。
      · "细结构"= 原图 − 开运算,用来把彩色文字与彩色插图/照片分开;必须在与笔画粗细
        相称的分辨率上做(det_w),在原生分辨率上做慢十几倍,在过低分辨率上做会失效。
      · ImageMagick 的 minus 是「目标减源」,两图顺序反了结果全空。
      · 代表色只取笔画核心(较暗的像素);把抗锯齿边缘一起平均会明显偏浅,字画出来发虚。
      · 底图必须按蒙版**原样**挖洞(设透明 + alpha 加权缩放),不能用整幅模糊填(会糊出
        脏色被色度采样拉成蓝紫拖影),也不能把洞放大(黑字盖不住,彩色区露白边)。
    """
    import colorsys
    blk = os.path.join(outdir, f"{tag}_black.png")
    bgp = os.path.join(outdir, f"{tag}_bg.jpg")
    cols = [os.path.join(outdir, f"{tag}_c{i}.png") for i in range(MRC_SLOTS)]
    if fresh(blk) and fresh(bgp) and all(fresh(c) for c in cols):
        return None                                   # 断点续做:已完成
    tmp = os.path.join(outdir, f"{tag}_tmp")

    # ① 黑墨层
    run(["magick", src, "-resize", f"{mask_w}x",
         "(", "-clone", "0", "-colorspace", "Gray", "-level", "15%,88%",
         "-unsharp", "0x1.2+1.2+0.01", "-threshold", dark, "-negate", ")",
         "(", "-clone", "0", "-colorspace", "HCL", "-channel", "G", "-separate",
         "+channel", "-threshold", chroma, "-negate", ")",
         "-delete", "0", "-compose", "multiply", "-composite", "-negate", blk])

    if not color_layers:                      # 只做黑字层,彩字交给底图(观感均匀,无拼接感)
        for c in cols:
            run(["magick", blk, "-fill", "white", "-colorize", "100", c])
        allm = f"{tmp}_all.png"
        run(["magick", blk, allm])
        w0, h0 = img_size(src)
        run(["magick", src, "(", allm, "-resize", f"{w0}x{h0}!", ")", "-alpha", "off",
             "-compose", "CopyOpacity", "-composite",
             "-resize", f"{bg_w}x>", "-background", "white", "-alpha", "remove", "-alpha", "off",
             "-level", "6%,94%", "-sigmoidal-contrast", "3.5,50%", "-modulate", "102,107,100",
             "-colorspace", "sRGB", "-strip", "-sampling-factor", "4:2:0",
             "-quality", str(bg_q), "-define", "jpeg:optimize-coding=true", bgp])
        os.remove(allm)
        return [(0.0, 0.0, 0.0)] * MRC_SLOTS

    # ② 彩墨(不分色):够暗 AND 色度高 AND 细结构
    run(["magick", src, "-resize", f"{det_w}x", tmp + "_s.png"])
    run(["magick", tmp + "_s.png",
         "(", "-clone", "0", "-colorspace", "HCL", "-channel", "G", "-separate",
         "+channel", "-threshold", chroma, ")",
         "(", "-clone", "0", "-colorspace", "Gray", "-threshold", "72%", "-negate", ")",
         "-delete", "0", "-compose", "multiply", "-composite", tmp + "_ci.png"])
    run(["magick", tmp + "_ci.png", "-morphology", "Open", "Disk:6", tmp + "_big.png"])
    run(["magick", tmp + "_big.png", tmp + "_ci.png", "-compose", "minus", "-composite",
         "-morphology", "Open", "Diamond:1", tmp + "_ink.png"])

    # ③ 色相聚类(只用笔画核心算代表色)
    im = Image.open(tmp + "_s.png").convert("RGB")
    mi = Image.open(tmp + "_ink.png").convert("L")
    if mi.size != im.size:
        mi = mi.resize(im.size)
    ip, mp = im.load(), mi.load()
    BINS = 36
    hist = [0] * BINS
    acc = [[0, 0, 0] for _ in range(BINS)]
    accn = [0] * BINS
    for y in range(0, im.size[1], 2):
        for x in range(0, im.size[0], 2):
            if mp[x, y] > 128:
                c = ip[x, y]
                h, l, sa = colorsys.rgb_to_hls(c[0] / 255, c[1] / 255, c[2] / 255)
                if sa < 0.15:
                    continue
                b = int(h * BINS) % BINS
                hist[b] += 1
                if l < 0.55:
                    accn[b] += 1
                    acc[b][0] += c[0]; acc[b][1] += c[1]; acc[b][2] += c[2]
    total = sum(hist)
    clusters, used = [], set()
    while total and len(clusters) < MRC_SLOTS:
        cand = [(h, i) for i, h in enumerate(hist) if i not in used]
        if not cand or max(cand)[0] == 0:
            break
        peak = max(cand)
        span = 4                                        # ±40°
        bins = [b for b in ((peak[1] + d) % BINS for d in range(-span, span + 1))
                if b not in used]
        n = sum(hist[b] for b in bins)
        used.update(bins)
        if n < total * 0.08:
            continue
        cn = sum(accn[b] for b in bins) or 1
        clusters.append(((sum(acc[b][0] for b in bins) / cn,
                          sum(acc[b][1] for b in bins) / cn,
                          sum(acc[b][2] for b in bins) / cn), sorted(bins)))

    # ④ 每簇一层:彩墨 ∩ 该色相带;不足的槽位写空蒙版
    rgbs = []
    for i in range(MRC_SLOTS):
        if i < len(clusters):
            rgb, bins = clusters[i]
            segs, cur = [], [bins[0]]
            for b in bins[1:]:
                if b == cur[-1] + 1:
                    cur.append(b)
                else:
                    segs.append(cur); cur = [b]
            segs.append(cur)
            parts = []
            for j, seg in enumerate(segs):
                lo, hi = seg[0] * 100 / BINS, (seg[-1] + 1) * 100 / BINS
                pth = f"{tmp}_b{j}.png"
                run(["magick", tmp + "_s.png", "-colorspace", "HSL",
                     "-channel", "R", "-separate", "+channel",
                     "(", "-clone", "0", "-threshold", f"{lo:.2f}%", ")",
                     "(", "-clone", "0", "-threshold", f"{hi:.2f}%", "-negate", ")",
                     "-delete", "0", "-compose", "multiply", "-composite", pth])
                parts.append(pth)
            band = f"{tmp}_band.png"
            if len(parts) == 1:
                run(["magick", parts[0], band])
            else:
                run(["magick"] + parts + ["-compose", "lighten", "-layers", "flatten", band])
            # 注意 "{mask_w}x" 不能写成 "{mask_w}x>":后者只缩不放,会让彩色层停在 det_w
            # 彩色层经过"细结构过滤"和"色相带求交"两道减法,笔画会被削出缺口
            #(用户实测:彩字像被虫啃)。这里补回来:
            #   Close 填掉笔画内部的小洞 → Dilate 补上被削掉的边缘 → 笔画重新实心
            # 稍微加粗一点点无妨,总比"实心块与模糊残影交错"好看得多。
            run(["magick", tmp + "_ink.png", band, "-compose", "multiply", "-composite",
                 "-resize", f"{mask_w}x", "-threshold", "50%",
                 "-morphology", "Close", "Disk:2",
                 "-morphology", "Dilate", "Diamond:1",
                 "-negate", cols[i]])
            rgbs.append(tuple(v / 255 for v in rgb))
        else:
            run(["magick", blk, "-fill", "white", "-colorize", "100", cols[i]])   # 空层
            rgbs.append((0.0, 0.0, 0.0))

    # ⑤ 底图:挖掉全部墨(黑 + 各彩色层),alpha 加权缩放
    allm = f"{tmp}_all.png"
    run(["magick", blk, allm])
    for c in cols:
        run(["magick", allm, "(", c, "-resize", f"{mask_w}x!", ")",
             "-compose", "darken", "-composite", allm])
    w0, h0 = img_size(src)
    run(["magick", src, "(", allm, "-resize", f"{w0}x{h0}!", ")", "-alpha", "off",
         "-compose", "CopyOpacity", "-composite",
         "-resize", f"{bg_w}x>", "-background", "white", "-alpha", "remove", "-alpha", "off",
         "-level", "6%,94%", "-sigmoidal-contrast", "3.5,50%", "-modulate", "102,107,100",
         "-colorspace", "sRGB", "-strip", "-sampling-factor", "4:2:0",
         "-quality", str(bg_q), "-define", "jpeg:optimize-coding=true", bgp])
    for f in os.listdir(outdir):                       # 清掉本页临时文件
        if f.startswith(os.path.basename(tmp)):
            os.remove(os.path.join(outdir, f))
    return rgbs


def mrc_compose(work, tag, groups, bg_paths, colors, slots):
    """groups: [[黑蒙版相对路径...], [c0...], [c1...], [c2...]];每组一次 JBIG2(共享词典)。"""
    docs = []
    for gi, rels in enumerate(groups):
        pre = f"jbm{tag}_{gi}"
        for f in os.listdir(work):
            if f.startswith(pre + "."):
                os.remove(os.path.join(work, f))
        with jbig2_slot(slots):
            run(["jbig2", "-s", "-p", "-t", "0.95", "-b", pre] + rels, cwd=work)
        r = subprocess.run(["jbig2topdf.py", pre], cwd=work, capture_output=True)
        if r.returncode != 0 or not r.stdout:
            die(f"jbig2topdf.py 失败: {r.stderr.decode('utf-8', 'replace')[:300]}")
        for f in os.listdir(work):
            if f.startswith(pre + "."):
                os.remove(os.path.join(work, f))
        docs.append(pikepdf.open(io.BytesIO(r.stdout)))

    out = pikepdf.new()
    for i, bgp in enumerate(bg_paths):
        res, ops = {}, []
        mw = mh = None
        for gi, d in enumerate(docs):
            im = next(iter(d.pages[i].get_images().values()))
            if mw is None:
                mw, mh = int(im.Width), int(im.Height)
            st = out.copy_foreign(im)
            st.ImageMask = True
            if "/ColorSpace" in st:
                del st["/ColorSpace"]
            key = f"M{gi}"
            res[key] = st
            ops.append((key, (0.0, 0.0, 0.0) if gi == 0 else colors[i][gi - 1]))
        bw, bh = img_size(bgp)
        bg = pikepdf.Stream(out, open(bgp, "rb").read())
        bg.Type, bg.Subtype = pikepdf.Name.XObject, pikepdf.Name.Image
        bg.Width, bg.Height = bw, bh
        bg.ColorSpace, bg.BitsPerComponent = pikepdf.Name.DeviceRGB, 8
        bg.Filter = pikepdf.Name.DCTDecode
        res["Bg"] = bg
        page = out.add_blank_page(page_size=(mw, mh))
        page.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(**res))
        cm = f"{mw} 0 0 {mh} 0 0 cm"
        page.Contents = pikepdf.Stream(out, (
            f"q {cm} /Bg Do Q\n" + "".join(
                f"q {r:.3f} {g:.3f} {b:.3f} rg {cm} /{k} Do Q\n"
                for k, (r, g, b) in ops)).encode())
    return out


def to_jpeg(src, dst, width, quality, gray, sampling="4:2:0", enh=()):
    """灰度/彩色内容页:等比缩放 +(可选提白+锐化)+ JPEG 重压。
    不给 enh 时不改动任何像素值。optimize-coding 优化 Huffman 表,无损再省 5-8%。"""
    if fresh(dst):
        return False
    cs = ["-colorspace", "Gray"] if gray else ["-colorspace", "sRGB"]
    resize = ["-resize", f"{width}x>"] if width else []
    samp = [] if gray else ["-sampling-factor", sampling]
    run(["magick", src] + resize + list(enh) + cs + samp
        + ["-strip", "-quality", str(quality),
           "-define", "jpeg:optimize-coding=true", dst])
    return True


def pmap(fn, items, jobs, label, stage="work"):
    t0 = time.time()
    done = 0
    with ThreadPoolExecutor(max_workers=jobs) as ex:
        for i, made in enumerate(ex.map(fn, items), 1):
            done += bool(made)
            emit("P", stage, i, len(items))
            if i % max(1, len(items) // 20) == 0 or i == len(items):
                print(f"  {label} {i}/{len(items)}  ({time.time() - t0:.0f}s)", flush=True)
    return done


# ---------------- 组装 ----------------
MIN_CHUNK_PAGES = 16   # 每段最少页数(再碎则符号词典太零散,体积代价上升)
MAX_CHUNKS = 8         # 段数上限:8 段已把大部头压到 30 秒内,再切收益微乎其微
SLOT_DIR = os.path.expanduser("~/.cache/scan2pdf/.slots")


@contextlib.contextmanager
def jbig2_slot(total):
    """全局名额(跨进程信号量):所有 scan2pdf 进程共抢同一批 <total> 个 jbig2 名额。

    这样并行度是**动态自适应**的:队列里只有一个任务时它独占 8 个名额;中途再加两个任务,
    新任务的段自然排队等名额,三者自动分摊——不需要任何进程间协商,也不必在启动时算死。
    用 flock:进程崩溃/被杀时锁由内核自动释放,不会留下死锁。
    """
    os.makedirs(SLOT_DIR, exist_ok=True)
    fh = None
    try:
        while fh is None:
            for i in range(total):
                f = open(os.path.join(SLOT_DIR, f"slot{i}"), "a+")
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fh = f
                    break
                except OSError:
                    f.close()
            if fh is None:
                time.sleep(0.3)
        yield
    finally:
        if fh is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            fh.close()


def jbig2_pdf(work, rel_pngs, out_pdf, tag="", slots=MAX_CHUNKS):
    """切段并行 jbig2 + 无损拼接 → content.pdf。

    为什么切段(848 页《교회법 해설 1》实测):jbig2 的符号词典随页数膨胀,每页成本**超线性**增长
    —— 整本一次压 888s(1.05 s/页),切 8 段每段 106 页:**顺序**跑只要 77s(11.6x,零并行),
    8 段并行 30s(29.5x);体积仅 +2.0%(15.7→16.0 MB)。
    即「切段」本身贡献 11.6 倍、并行再贡献 2.6 倍,故**任何情况下都切段**,
    只有「同时压几段」跟着可用核数走(workers)。
    拼接用 pikepdf 搬运页面对象,二值化像素一位不改。
    """
    if fresh(out_pdf):
        print(f"  jbig2: 复用已有 {os.path.basename(out_pdf)}"
              f" ({os.path.getsize(out_pdf) / 1e6:.1f} MB)")
        return
    n = len(rel_pngs)
    k = max(1, min(MAX_CHUNKS, n // MIN_CHUNK_PAGES))     # 页数够就尽量切满 8 段
    per = (n + k - 1) // k
    chunks = [rel_pngs[i * per:(i + 1) * per] for i in range(k)]
    chunks = [c for c in chunks if c]
    print(f"  jbig2: {n} 页 → 切 {len(chunks)} 段(每段约 {per} 页),"
          f"全局名额 {slots} 个(与其他任务共享,自动分摊)...", flush=True)
    t0 = time.time()
    done = [0]

    def one(idx_files):
        i, files = idx_files
        pre = f"jb{tag}_{i}"
        for f in os.listdir(work):
            if f.startswith(pre + "."):
                os.remove(os.path.join(work, f))
        with jbig2_slot(slots):          # 抢到全局名额才真正开压(跨进程自适应)
            run(["jbig2", "-s", "-p", "-t", "0.95", "-b", pre] + files, cwd=work)
        r = subprocess.run(["jbig2topdf.py", pre], cwd=work, capture_output=True)
        if r.returncode != 0 or not r.stdout:
            die(f"jbig2topdf.py 失败: {r.stderr.decode('utf-8', 'replace')[:300]}")
        part = os.path.join(work, f"{pre}.pdf")
        with open(part, "wb") as fh:                      # 输出是二进制 PDF,不能走文本管道
            fh.write(r.stdout)
        for f in os.listdir(work):                        # 段内中间流文件即刻删掉(千页书可达数百 MB)
            if f.startswith(pre + ".") and not f.endswith(".pdf"):
                os.remove(os.path.join(work, f))
        done[0] += 1
        emit("P", "jbig2", done[0], len(chunks))
        print(f"    第 {i + 1}/{len(chunks)} 段完成 ({len(files)} 页, {time.time() - t0:.0f}s)", flush=True)
        return part

    with ThreadPoolExecutor(max_workers=len(chunks)) as ex:
        parts = list(ex.map(one, enumerate(chunks)))

    out = pikepdf.new()
    opened = []
    for p in parts:                                       # 按段序拼回,页序即原页序
        d = pikepdf.open(p)
        opened.append(d)
        out.pages.extend(d.pages)
    out.save(out_pdf)
    for d in opened:
        d.close()
    for p in parts:
        os.remove(p)
    print(f"  jbig2: {os.path.getsize(out_pdf) / 1e6:.1f} MB,{time.time() - t0:.0f}s")


def assemble(page_sources, dest, a4=True, title=None, author=None):
    """page_sources: [(pdf对象, 页索引), ...] 按最终页序;a4=True 时每页等比缩放居中到 A4。
    统一 A4 只能这样矢量缩放——重新光栅化会毁掉 JBIG2 压缩。"""
    out = pikepdf.new()
    for doc, idx in page_sources:
        page = doc.pages[idx]
        if not a4:
            out.pages.append(page)
            continue
        box = page.trimbox
        w, h = float(box[2]) - float(box[0]), float(box[3]) - float(box[1])
        s = min(A4[0] / w, A4[1] / h)
        nw, nh = w * s, h * s
        x0, y0 = (A4[0] - nw) / 2, (A4[1] - nh) / 2
        blank = out.add_blank_page(page_size=A4)
        blank.add_overlay(page, pikepdf.Rectangle(x0, y0, x0 + nw, y0 + nh))
    if title or author:
        with out.open_metadata() as m:
            if title:
                m["dc:title"] = title
            if author:
                m["dc:creator"] = [author]
    out.save(dest, compress_streams=True,
             object_stream_mode=pikepdf.ObjectStreamMode.generate)
    return len(out.pages)


def build(pages, covers, work, dest, o, width, quality, threshold, jobs, quiet=False):
    """pages: [(路径, 编号)] 全部页(含封面页);covers: 保留彩色的编号集合"""
    os.makedirs(work, exist_ok=True)
    content = [(p, n) for p, n in pages if n not in covers]
    # 提白锐化配方按模式分开:黑白是为"阈值切得准"调的(激进提白+强锐化),
    # 彩色/灰度要保住浅色细节,用主线流程验证过的温和配方(提白+S曲线+微增饱和+弱锐化)。
    if not o.enhance:
        enh = []
    elif o.mode in ("bw", "smooth"):
        enh = ["-level", o.enhance_level, "-unsharp", o.enhance_unsharp]
    else:
        enh = ["-level", o.color_level, "-sigmoidal-contrast", o.color_sigmoidal]
        if o.mode == "color":
            enh += ["-modulate", o.color_modulate]        # 灰度图谈不上饱和度,跳过
        enh += ["-unsharp", o.color_unsharp]
    if o.mode == "mrc":
        tag = f"mrc_{o.mrc_mask_width}_{o.mrc_bg_width}q{o.mrc_bg_quality}" \
              f"_d{o.mrc_dark.rstrip('%')}c{o.mrc_chroma.rstrip('%')}"
    else:
        tag = f"{o.mode}_{width}_{quality if o.mode not in ('bw', 'smooth') else threshold.rstrip('%')}" \
          + (f"_l{o.smooth_levels}d{o.smooth_depth}" if o.mode == "smooth" else "") \
          + ("_e" if o.enhance else "") + (f"_ss{o.supersample}" if o.supersample > 1 else "")   # 加进缓存目录名,开关不同的产物不会互相顶掉
    sub = os.path.join(work, tag)
    os.makedirs(sub, exist_ok=True)

    # 内容页
    if o.mode == "bw":
        pmap(lambda t: binarize(t[0], os.path.join(sub, f"p{t[1]:05d}.png"),
                                width, threshold, enh, o.supersample),
             content, jobs, "二值化", "binarize")
        rel = [os.path.join(tag, f"p{n:05d}.png") for _, n in content]
        miss = [f for f in rel if not fresh(os.path.join(work, f))]
        if miss:
            die(f"二值化缺 {len(miss)} 页(如 {miss[0]}),重跑本命令即可续做")
        content_pdf = os.path.join(work, f"content_{tag}.pdf")
        emit("S", "jbig2")
        jbig2_pdf(work, rel, content_pdf, tag, o.jbig2_slots)
        cdoc = pikepdf.open(content_pdf)
    elif o.mode == "mrc":
        mdir = os.path.join(sub, "L")
        os.makedirs(mdir, exist_ok=True)
        colors = {}

        def one_mrc(t):
            rgbs = mrc_layers(t[0], mdir, f"p{t[1]:05d}", o.mrc_dark, o.mrc_chroma,
                              o.mrc_mask_width, o.mrc_bg_width, o.mrc_bg_quality,
                              color_layers=not o.mrc_no_color_layers)
            if rgbs is not None:
                colors[t[1]] = rgbs
            return rgbs is not None

        pmap(one_mrc, content, jobs, "分层(文字+色彩)", "binarize")
        # 续做时缓存已在但内存里没有颜色 → 从颜色记录文件恢复
        cfile = os.path.join(sub, "colors.json")
        import json
        if os.path.exists(cfile):
            old = {int(k): [tuple(v) for v in vv] for k, vv in
                   json.load(open(cfile, encoding="utf-8")).items()}
            old.update(colors)
            colors = old
        json.dump({str(k): [list(v) for v in vv] for k, vv in colors.items()},
                  open(cfile, "w", encoding="utf-8"))
        missing = [n for _, n in content if n not in colors]
        if missing:
            die(f"分层缺 {len(missing)} 页(如 {missing[0]}),重跑本命令即可续做")

        groups = [[os.path.join(tag, "L", f"p{n:05d}_black.png") for _, n in content]]
        for i in range(MRC_SLOTS):
            groups.append([os.path.join(tag, "L", f"p{n:05d}_c{i}.png") for _, n in content])
        bgs = [os.path.join(mdir, f"p{n:05d}_bg.jpg") for _, n in content]
        emit("S", "jbig2")
        cdoc = mrc_compose(work, tag, groups, bgs,
                           [colors[n] for _, n in content], o.jbig2_slots)
    elif o.mode == "vector":
        pmap(lambda t: vectorize(t[0], os.path.join(sub, f"p{t[1]:05d}.pdf"),
                                 threshold, enh, o.vector_zoom),
             content, jobs, "矢量描摹", "compress")
        paths = [os.path.join(sub, f"p{n:05d}.pdf") for _, n in content]
        miss = [p_ for p_ in paths if not fresh(p_)]
        if miss:
            die(f"描摹缺 {len(miss)} 页,重跑本命令即可续做")
        cdoc = pikepdf.new()
        _keep = []
        for p_ in paths:                                  # 单页矢量 PDF 按序合成内容 PDF
            d = pikepdf.open(p_)
            _keep.append(d)
            cdoc.pages.extend(d.pages)
    else:
        if o.mode == "smooth":                            # 平滑黑白:PNG(Flate)无损
            pmap(lambda t: smooth_bw(t[0], os.path.join(sub, f"p{t[1]:05d}.png"),
                                     width, threshold, enh,
                                     o.smooth_levels, o.smooth_depth),
                 content, jobs, "平滑黑白", "compress")
            paths = [os.path.join(sub, f"p{n:05d}.png") for _, n in content]
        else:                                             # 灰度 / 彩色:JPEG
            gray = o.mode == "gray"
            pmap(lambda t: to_jpeg(t[0], os.path.join(sub, f"p{t[1]:05d}.jpg"),
                                   width, quality, gray, "4:2:0", enh),
                 content, jobs, "灰度化" if gray else "彩色压缩", "compress")
            paths = [os.path.join(sub, f"p{n:05d}.jpg") for _, n in content]
        miss = [p for p in paths if not fresh(p)]
        if miss:
            die(f"压缩缺 {len(miss)} 页(如 {os.path.basename(miss[0])}),重跑本命令即可续做")
        cdoc = pikepdf.open(io.BytesIO(img2pdf.convert(paths)))
    if len(cdoc.pages) != len(content):
        die(f"内容 PDF {len(cdoc.pages)} 页 != 应有 {len(content)} 页")

    # 彩色封面(不做增强,只缩放+压缩)
    cov_doc, cov_idx = {}, 0
    if covers:
        cpaths = []
        for p, n in pages:
            if n in covers:
                dst = os.path.join(sub, f"cover{n:05d}.jpg")
                to_jpeg(p, dst, o.cover_width, 85, False, "4:4:4")
                cpaths.append((n, dst))
        cov = pikepdf.open(io.BytesIO(img2pdf.convert([d for _, d in cpaths])))
        cov_doc = {n: i for i, (n, _) in enumerate(cpaths)}
        cov_idx = cov

    order, ci = [], 0
    for _, n in pages:
        if n in covers:
            order.append((cov_idx, cov_doc[n]))
        else:
            order.append((cdoc, ci))
            ci += 1
    emit("S", "assemble")
    npages = assemble(order, dest, not o.no_a4, o.title, o.author)
    size = os.path.getsize(dest)
    # 成品已落盘,中间产物就是垃圾:默认立刻清掉(失败/中断时不会走到这里,故断点续做不受影响)
    if not o.keep_cache:
        shutil.rmtree(sub, ignore_errors=True)
        for f in os.listdir(work):
            if f.startswith(f"content_{tag}") or f.startswith(f"jb{tag}_"):
                try:
                    os.remove(os.path.join(work, f))
                except OSError:
                    pass
    emit("DONE", size, npages, dest)
    if not quiet:
        print(f"\n完成: {dest}")
        print(f"  {npages} 页, {size / 1e6:.1f} MB, 平均 {size / npages / 1024:.0f} KB/页"
              + (f", 内容页 {width}px ≈ {width / 8.27:.0f} DPI@A4" if width else ", 内容页原生分辨率"))
    return size, npages


# ---------------- 主流程 ----------------
def main():
    t_start = time.time()
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("src")
    ap.add_argument("dest", nargs="?")
    ap.add_argument("--mode", choices=["bw", "smooth", "mrc", "vector", "gray", "color"], default="bw")
    ap.add_argument("--width", type=int, default=None)
    ap.add_argument("--quality", type=int, default=45)
    ap.add_argument("--threshold", default=None)      # 默认 55%;开 --enhance 时默认 58%
    ap.add_argument("--enhance", action="store_true")  # 提白+锐化预处理(墨淡/发灰的扫描)
    # MRC 默认值 = 用户实测选定的 v7 档(198 页漫画 14.2 MB,文字 2200px)
    ap.add_argument("--mrc-dark", default="50%")          # 墨的暗度门(松了会把中性灰画面当墨)
    ap.add_argument("--mrc-chroma", default="35%")        # 墨的色度门(严了会切碎深色文字)
    ap.add_argument("--mrc-mask-width", type=int, default=2200)   # 文字层分辨率
    ap.add_argument("--mrc-bg-width", type=int, default=500)      # 彩色底图分辨率
    ap.add_argument("--mrc-bg-quality", type=int, default=35)
    ap.add_argument("--mrc-no-color-layers", action="store_true")  # 彩字不做蒙版,统一交给底图
    ap.add_argument("--vector-zoom", type=int, default=2)    # 矢量描摹精度(位图放大倍数)
    ap.add_argument("--smooth-levels", type=int, default=3)  # 平滑黑白:边缘灰阶数(3=最省)
    ap.add_argument("--smooth-depth", type=int, default=2)   # 平滑黑白:PNG 位深(2bit=4值)
    ap.add_argument("--supersample", type=int, default=1)  # 黑白边缘精细化:阈值前放大 N 倍(2=台阶细一半)
    ap.add_argument("--enhance-level", default="15%,88%")
    ap.add_argument("--enhance-unsharp", default="0x1.2+1.2+0.01")
    ap.add_argument("--color-level", default="6%,94%")            # 彩色/灰度的提白区间(温和)
    ap.add_argument("--color-sigmoidal", default="3.5,50%")       # S 曲线对比,不糊高光
    ap.add_argument("--color-modulate", default="102,107,100")    # 亮度%,饱和%,色相%
    ap.add_argument("--color-unsharp", default="0x0.9+1.0+0.008")
    ap.add_argument("--range", dest="rng")
    ap.add_argument("--include")   # 只处理这些编号(与 --range 可叠加)
    ap.add_argument("--exclude")
    ap.add_argument("--covers", default="first,last")
    ap.add_argument("--cover-width", type=int, default=1400)
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--probe-pages", type=int, default=8)
    ap.add_argument("--no-a4", action="store_true")
    ap.add_argument("--title")
    ap.add_argument("--author")
    ap.add_argument("--work")
    ap.add_argument("--clean", action="store_true")
    ap.add_argument("--jobs", type=int, default=max(2, (os.cpu_count() or 4) - 1))
    ap.add_argument("--jbig2-slots", type=int, default=MAX_CHUNKS)  # 全局同时压几段(所有任务共享)
    ap.add_argument("--keep-cache", action="store_true")  # 保留中间产物(默认成功后自动清理)
    ap.add_argument("--progress", action="store_true")   # 机器可读进度行(供 GUI)
    ap.add_argument("-h", "--help", action="store_true")
    o = ap.parse_args()
    if o.help:
        print(__doc__)
        return
    global PROG
    PROG = o.progress
    src = os.path.abspath(os.path.expanduser(o.src))
    if not os.path.isdir(src):
        die(f"源图目录不存在: {src}")
    if o.width is None:
        o.width = 1400 if o.mode == "smooth" else 1800
    if o.width and o.width < 200:
        die("--width 太小(建议 ≥1200;0 表示不缩放)")
    if o.threshold is None:
        # 提白会把整体推亮,阈值同步上调一档才不会把细笔画切细
        o.threshold = "58%" if o.enhance else "55%"
    if not o.threshold.endswith("%"):
        o.threshold += "%"

    items, numbered = list_images(src)
    print(f"源图: {src}")
    print(f"  {len(items)} 张,编号 {items[0][1]}–{items[-1][1]}"
          f"({'按文件名编号' if numbered else '按文件序号'})")
    fmts = {}
    for p, _ in items[:: max(1, len(items) // 12)]:      # 抽查真实格式(有的 .jpg 实为 TIFF)
        with open(p, "rb") as fh:
            head = fh.read(4)
        k = ("JPEG" if head[:2] == b"\xff\xd8" else
             "TIFF" if head[:2] in (b"II", b"MM") else
             "PNG" if head[:4] == b"\x89PNG" else head.hex())
        fmts[k] = fmts.get(k, 0) + 1
    print(f"  抽查格式: {', '.join(f'{k}×{v}' for k, v in fmts.items())}"
          "(magick 全部可读,无需预转换)")

    keep = {n for _, n in items}
    if o.include:                                        # 只保留指定编号(GUI 选中若干单个文件时用)
        keep &= parse_ranges(o.include)
    if o.rng:
        a, b = (int(x) for x in o.rng.split("-"))
        keep &= set(range(a, b + 1))
    if o.exclude:
        keep -= parse_ranges(o.exclude)
    pages = [(p, n) for p, n in items if n in keep]
    if not pages:
        die("筛选后没有剩下任何页")

    covers = set()
    if o.covers.lower() not in ("none", "no", ""):
        for tok in o.covers.split(","):
            tok = tok.strip().lower()
            covers.add(pages[0][1] if tok == "first" else
                       pages[-1][1] if tok == "last" else int(tok))
        covers &= {n for _, n in pages}
    print(f"  输出 {len(pages)} 页;彩色页 {sorted(covers) or '无'};"
          f"内容页 {len(pages) - len(covers)} 页 [{o.mode}]")

    work = os.path.abspath(os.path.expanduser(o.work)) if o.work else \
        os.path.expanduser(f"~/.cache/scan2pdf/{os.path.basename(src)}")
    if o.clean and os.path.isdir(work):
        shutil.rmtree(work)
    os.makedirs(work, exist_ok=True)
    print(f"  中间产物: {work}(可重跑续做;确认成品后可整个删除)")

    dest = o.dest or os.path.join(os.getcwd(), os.path.basename(src) + ".pdf")
    dest = os.path.abspath(os.path.expanduser(dest))

    if not o.probe:
        build(pages, covers, work, dest, o, o.width, o.quality, o.threshold, o.jobs)
        if not o.keep_cache:
            try:
                if not os.listdir(work):
                    os.rmdir(work)
                print(f"  临时文件已清理({work})")
            except OSError:
                pass
        print(f"  总耗时 {time.time() - t_start:.0f} 秒")
        print("\n提示:抽查几页放大看清晰度;不满意就换档位重跑(改 --width/--threshold/--quality)。")
        return

    # ---- probe:抽样几页 × 多档位,给出体积/页与全书预估 + 样张 PDF ----
    n_all = len(pages) - len(covers)
    step = max(1, n_all // o.probe_pages)
    sample = [(p, n) for p, n in pages if n not in covers][::step][:o.probe_pages]
    grid = ([(w, o.threshold) for w in (0, 2400, 1800, 1400)] if o.mode == "bw" else
            [(w, q) for w in (2400, 1800, 1400) for q in (45, 35)])
    print(f"\nprobe: 抽样 {len(sample)} 页 × {len(grid)} 档;样张 PDF 输出到 {os.path.dirname(dest)}")
    rows = []
    for w, x in grid:
        tag = f"probe_{o.mode}_{w or 'native'}_{str(x).rstrip('%')}"
        sd = os.path.join(os.path.dirname(dest), tag + ".pdf")
        print(f"\n[{tag}]")
        size, np_ = build(sample, set(), os.path.join(work, "_probe"), sd, o,
                          w, x if o.mode != "bw" else o.quality,
                          x if o.mode == "bw" else o.threshold, o.jobs, quiet=True)
        per = size / np_
        rows.append((tag, w, x, per, per * n_all))
        print(f"  {size / 1e6:.2f} MB / {np_} 页 = {per / 1024:.0f} KB/页"
              f"  → 全书 {n_all} 页约 {per * n_all / 1e6:.0f} MB  ({os.path.basename(sd)})")
    print("\n档位对比(DPI@A4 = 宽px ÷ 8.27):")
    print(f"  {'档位':<26}{'KB/页':>8}{'全书预估':>12}")
    for tag, w, x, per, tot in rows:
        print(f"  {tag:<26}{per / 1024:>8.0f}{tot / 1e6:>10.0f} MB")
    print("\n注:bw 模式的样张预估偏高——JBIG2 符号词典由全书共享,页数越多摊薄越多,"
          "实际全书通常比预估小。请打开样张放大对比清晰度后定档。")


if __name__ == "__main__":
    main()
