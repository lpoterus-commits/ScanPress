# ScanPress

把扫描仪（CZUR 等）拍的整本书图片，做成**高清晰、小体积**的 PDF。

转换全程确定性——不联网、不调用 AI，同一批图片跑多少次结果都一样。
提供命令行引擎和一个原生 macOS 图形界面（图片转 PDF ／ PDF 工具 ／ 合并 PDF）。

> **转换出的是纯图片 PDF：不可搜索、不可选取复制、没有目录书签。**
> 需要可搜索的话，转完再用「PDF 工具 → 加文字层」补一层看不见的文字（macOS Vision，离线）。
> 注意那一步用的是系统的识别模型，会随 macOS 更新变化，**不像其余部分那样跨机器可复现**。

## 实测效果

| 材料 | 档位 | 结果 |
|---|---|---|
| 1506 页韩语书（纯文字） | 黑白 + 提白锐化 | **32 MB**（约 22 KB/页） |
| 198 页彩色漫画 | MRC 混合分层 | **14.2 MB**，文字层 2200px；同等锐度的普通彩色要 35.7 MB |
| 180 页彩色教材 | MRC（关彩色分层） | **17.4 MB**，全彩增强版为 38 MB |
| 116 页文字书 | 黑白 + 提白锐化 + 超采样 | 0.63 MB / 32–40 秒 |

## 四种画质模式

- **黑白 · 最小** —— 二值化 + JBIG2。文字书首选，体积最小。
- **彩色 · 文字锐化（MRC 混合分层）** —— 一页拆两层：文字/线条走高分辨率 1-bit 蒙版，色块走低分辨率彩色 JPEG，叠加成页。彩色书、漫画、绘本推荐。
- **灰度** —— 保留灰阶，边缘平滑，体积约为黑白的 10 倍。
- **原样彩色** —— 整页彩色 JPEG，不分层。

命令行另有 `smooth`（平滑黑白）与 `vector`（potrace 矢量描摹）两个实验模式，界面里没放。

## 运行环境

**图形界面版是自包含的，装上就能用，不需要装任何东西。** ImageMagick、jbig2enc、
以及一个装好 pikepdf/img2pdf/Pillow 的 Python 3.14 运行时都在 `.app` 里（约 128 MB）。

命令行用法（直接跑 `scripts/scan2pdf.py`）走 PATH 找工具，所以仍需自备：

```bash
brew install imagemagick jbig2enc
python3 -m venv ~/.venvs/pdfenv && ~/.venvs/pdfenv/bin/pip install pikepdf img2pdf Pillow
```

`--mode vector` 另需 `brew install potrace` —— 它是 GPL-2.0，**刻意不内嵌**，以免传染本项目的 MIT 许可。
目前只在 Apple Silicon 的 macOS 上验证过。
Windows 移植的交接文档见 [`app/WINDOWS_移植交接.md`](app/WINDOWS_移植交接.md)，尚未实现。

## 已有 PDF 瘦身 / 加文字层

图形界面里是「PDF 工具」标签页；命令行等价物：

```bash
python scripts/shrink_pdf.py <PDF或目录> --analyze   # 体检：能省多少
python scripts/shrink_pdf.py <PDF或目录>             # 无损重压
python scripts/ocr_pdf.py <PDF> --lang ko-KR,en-US   # 加可搜索的隐形文字层
```

`shrink_pdf.py` 把 1 位黑白图的 CCITT G4 编码换成 JBIG2 通用编码 —— **位图一个比特都不变**，
不是画质换体积。实测四本自扫韩语书 **354.8 MB → 148.6 MB（省 58%）**，全书逐像素比对零差异。

`ocr_pdf.py` 用 macOS 自带的 Vision 框架加不可见文字层，**页面外观逐像素不变**，
每页只增加约 1.2 KB。韩/中/日等 30 种语言，完全离线。

## 命令行用法

```bash
~/.venvs/pdfenv/bin/python scripts/scan2pdf.py <源图目录> <输出.pdf> --mode bw --enhance
```

零配置、自动自然排序、支持断点续做（中间产物在 `~/.cache/scan2pdf/`）。
调参时用 `--probe`：抽 8 页 × 多个档位出样张，**定档不必全书重跑**。
`scripts/merge_pdfs.py` 可无损合并多个 PDF（pikepdf 搬运页面对象，压缩流原样带过去）。

完整选项见 `--help`。

## 图形界面：下载或自行构建

打包好的 macOS 应用在 [Releases](https://github.com/lpoterus-commits/ScanPress/releases/latest)（`.dmg` / `.zip`）。
也可以从源码构建：

```bash
python3 app/build_app.py
```

编译 Swift → 组装 `.app` → 生成图标 → 临时签名 → 安装到 `~/Applications`。
只需 **Xcode Command Line Tools**，不需要完整 Xcode。

> ⚠ **构建出的 `.app` 只有 ad-hoc 临时签名，未经 Apple 公证。** 首次打开需在图标上右键 →
> 打开 → 再点「打开」，否则会被 Gatekeeper 拦下。

## 文档

- [`维护交接.md`](维护交接.md) —— 代码地图、构建验证闭环、哪些参数不能随手改、已否掉的路线
- [`CLAUDE.md`](CLAUDE.md) —— 全部实测数据与踩坑记录（为什么是现在这套参数）

这两份是本项目最有价值的部分：JBIG2 切段带来的 11.6× 提速、MRC 分层六轮排错的完整教训、
以及一串「试过但没用」的死路，都记在里面。

## 许可

代码以 [MIT](LICENSE) 发布。

ImageMagick、jbig2enc、potrace 都是**以子进程方式调用的外部工具，不随本仓库分发**，
各自遵循其自身许可（potrace 为 GPL-2.0，若你要再分发含 potrace 的构建物请注意）。

---

**English:** ScanPress turns scanned book images into small, sharp PDFs — fully deterministic,
no OCR, no AI, no network. Bilevel + JBIG2 for text, MRC layering for color books
(198-page comic → 14.2 MB with 2200px text). CLI engine plus a native macOS GUI.
Requires ImageMagick, jbig2enc and a Python venv with pikepdf; docs are in Chinese.
