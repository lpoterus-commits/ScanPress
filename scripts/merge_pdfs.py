#!/usr/bin/env python3
"""合并多个 PDF 为一个(无损:保留原有压缩与文字层,不重新光栅化)

用法:
  ~/.venvs/pdfenv/bin/python merge_pdfs.py <输出.pdf> <输入1.pdf> <输入2.pdf> ... [选项]

选项:
  --a4            把非 A4 的页统一为 A4(等比缩放居中;已是 A4 的页直接原样收录)
  --bookmarks     为每个输入文件建一个书签(书签名=文件名,跳到该文件第一页)
  --title T       PDF 文档标题(默认取输出文件名)
  --progress      机器可读进度行(供 GUI):@@P merge i n / @@S save / @@DONE bytes pages path

要点:pikepdf 是「搬运」页面对象,JBIG2/JPEG 流原样带过去,体积≈各输入之和,画质零损失。
     不要用「打印成 PDF」或重新光栅化的工具合并——那会毁掉压缩和文字层。
"""
import os
import sys

import pikepdf

A4 = (595.28, 841.89)
PROG = False


def emit(*a):
    if PROG:
        print("@@" + " ".join(str(x) for x in a), flush=True)


def main():
    argv = sys.argv[1:]
    a4 = "--a4" in argv
    marks = "--bookmarks" in argv
    global PROG
    PROG = "--progress" in argv
    title = None
    if "--title" in argv:
        i = argv.index("--title")
        title = argv[i + 1]
        del argv[i:i + 2]
    paths = [x for x in argv if not x.startswith("--")]
    if len(paths) < 3:
        sys.exit("用法: merge_pdfs.py <输出.pdf> <输入1.pdf> <输入2.pdf> ...  (至少两个输入)")
    dest, srcs = os.path.abspath(paths[0]), [os.path.abspath(p) for p in paths[1:]]
    for p in srcs:
        if not os.path.exists(p):
            sys.exit(f"找不到输入文件: {p}")
    if os.path.abspath(dest) in (os.path.abspath(p) for p in srcs):
        sys.exit("输出文件不能与输入文件相同")

    out = pikepdf.new()
    opened = []                       # 必须保持打开到 save 之后(页面对象是引用)
    starts = []                       # 每个输入的起始页序号(建书签用)
    total = 0
    for p in srcs:
        src = pikepdf.open(p)
        opened.append(src)
        starts.append(len(out.pages))
        for page in src.pages:
            box = page.trimbox
            w, h = float(box[2]) - float(box[0]), float(box[3]) - float(box[1])
            if a4 and not (abs(w - A4[0]) < 1.5 and abs(h - A4[1]) < 1.5):
                s = min(A4[0] / w, A4[1] / h)
                nw, nh = w * s, h * s
                blank = out.add_blank_page(page_size=A4)
                blank.add_overlay(page, pikepdf.Rectangle(
                    (A4[0] - nw) / 2, (A4[1] - nh) / 2,
                    (A4[0] - nw) / 2 + nw, (A4[1] - nh) / 2 + nh))
            else:
                out.pages.append(page)   # 原样收录,零处理
            total += 1
            emit("P", "merge", total, "0")
        print(f"  + {os.path.basename(p)}: {len(src.pages)} 页 "
              f"({os.path.getsize(p) / 1e6:.1f} MB)", flush=True)

    if marks:
        with out.open_outline() as ol:
            for p, i in zip(srcs, starts):
                name = os.path.splitext(os.path.basename(p))[0]
                ol.root.append(pikepdf.OutlineItem(name, i))

    with out.open_metadata() as m:
        m["dc:title"] = title or os.path.splitext(os.path.basename(dest))[0]

    emit("S", "save")
    out.save(dest, compress_streams=True,
             object_stream_mode=pikepdf.ObjectStreamMode.generate)
    for s in opened:
        s.close()
    size = os.path.getsize(dest)
    emit("DONE", size, len(out.pages), dest)
    print(f"\n完成: {dest}")
    print(f"  {len(out.pages)} 页, {size / 1e6:.1f} MB"
          + ("(已统一 A4)" if a4 else "") + ("(含书签)" if marks else ""))


if __name__ == "__main__":
    main()
