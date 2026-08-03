# ScanPress —— Windows 版移植交接文档

> 给在 Windows 电脑上接手这个任务的 Claude 会话。**这份文档是自足的**,不需要看之前的对话。
> 交接日期:2026-07-31 ｜ Mac 版本:ScanPress 2.0

---

## 0. 一句话说明

把扫描仪拍的整本书图片(`image00001.jpg` … `imageNNNNN.jpg`)转成**高清晰、小体积的 PDF**。
纯图像处理,**不做 OCR、不生成文字层、不生成 Word**,全程确定性,无需 AI 参与。

---

## 1. 现状:Mac 版已完成

- **转换引擎** `scan2pdf.py`(727 行 Python)—— 全部功能在这里,命令行可独立运行
- **合并工具** `merge_pdfs.py`(98 行)—— 多个 PDF 无损合并
- **图形界面** `ScanToPDF.swift`(1307 行 SwiftUI)—— **仅 macOS 可用,Windows 需重写**

**用户已选定方案 C:在 Windows 上用原生技术(.NET / WinUI / WPF 皆可)单独实现界面,引擎沿用 Python 脚本。**

---

## 2. 你要做的事

1. **移植引擎**:`scan2pdf.py` 有 3 处 Unix 专有代码要改(见 §4)
2. **实现界面**:按 §6 的规格书做一个 Windows 原生 GUI,通过子进程调用引擎
3. **验收**:按 §8 的清单逐项测

界面**不必和 macOS 版长得一样**,但功能与默认值必须一致 —— 那些默认值是几十轮实测定下来的。

---

## 3. Windows 端依赖

| 组件 | 用途 | 获取方式 |
|---|---|---|
| **Python 3.10+** | 引擎本体 | python.org 官方安装包 |
| **pikepdf / img2pdf / Pillow** | PDF 组装 | `pip install pikepdf img2pdf pillow` |
| **ImageMagick** | 所有图像处理 | imagemagick.org 官方 Windows 安装包,**装时勾选"Add to PATH"** |
| ~~jbig2enc~~ | 黑白压缩 | **Windows 无官方版,改用 CCITT G4 后备(见 §5)** |
| potrace(可选) | 矢量模式 | 有 Windows 版,但**是 GPL,商用需注意**;不做矢量模式可完全不装 |

装完自检:
```
magick -version
python -c "import pikepdf, img2pdf, PIL; print('ok')"
```

---

## 4. 引擎必改的 3 处

### 4.1 `fcntl` → `msvcrt`(必改,否则 import 就失败)

`scan2pdf.py` 顶部 `import fcntl`,用于 `jbig2_slot()` 的跨进程名额锁。Windows 用 `msvcrt.locking` 替代:

```python
import os, sys, time, contextlib
IS_WIN = sys.platform.startswith("win")
if IS_WIN:
    import msvcrt
else:
    import fcntl

def _try_lock(fh):
    """非阻塞地尝试加锁,成功返回 True"""
    try:
        if IS_WIN:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False

def _unlock(fh):
    try:
        if IS_WIN:
            fh.seek(0); msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
```
把 `jbig2_slot()` 里的两处 `fcntl.flock(...)` 换成上面两个函数即可。
注意 Windows 的 `msvcrt.locking` 要求文件**至少 1 字节**,打开后先 `fh.write(b"x"); fh.flush(); fh.seek(0)`。

### 4.2 缓存目录路径

现在写死 `~/.cache/scan2pdf`。Windows 应改到 `%LOCALAPPDATA%\ScanPress\cache`:
```python
if IS_WIN:
    CACHE_ROOT = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "ScanPress", "cache")
else:
    CACHE_ROOT = os.path.expanduser("~/.cache/scan2pdf")
```
涉及 `SLOT_DIR` 和 `main()` 里 `--work` 的默认值两处。

### 4.3 外部程序名

Mac 上是 `magick`。Windows 装完 ImageMagick 后同样是 `magick`(在 PATH 里),**通常无需改**。
但要注意:Windows 上有个同名的系统命令 `convert.exe`(磁盘转换工具),**绝不要用 `convert`,一律用 `magick`** —— 现有代码已经全部用 `magick`,保持即可。

---

## 5. jbig2enc 的替代方案(重要)

黑白模式和 MRC 模式的文字层都靠 `jbig2` 压缩。Windows 上没有现成的,**用 CCITT Group 4 替代**:

- G4 是传真标准,**PDF 原生支持**,ImageMagick 直接能输出,零额外依赖
- 体积代价:比 JBIG2 大 2–3 倍(实测 22 KB/页 → 约 50–60 KB/页),但仍远小于灰度/彩色

### 实现思路

```python
def g4_pdf(pngs, out_pdf):
    """把一批 1-bit PNG 压成 CCITT G4 并组成 PDF(替代 jbig2 路线)"""
    tiffs = []
    for p in pngs:
        t = p[:-4] + ".tif"
        run(["magick", p, "-monochrome", "-compress", "Group4", t])
        tiffs.append(t)
    # img2pdf 原生支持 G4 TIFF 直通(不重新编码)
    with open(out_pdf, "wb") as fh:
        fh.write(img2pdf.convert(tiffs))
    for t in tiffs:
        os.remove(t)
```

### MRC 模式的蒙版层同理

现在的流程是「jbig2 → jbig2topdf.py → 取出图像对象 → 设 `ImageMask=True`」。
换成 G4 后流程完全一样,只是前两步改为「magick 输出 G4 TIFF → img2pdf 转 PDF」,
之后**取图像对象、设 `ImageMask=True`、删掉 `ColorSpace` 的代码原样保留**。

⚠️ **极性**:JBIG2 里 0=黑;G4 TIFF 经 img2pdf 后可能相反。若成品整页涂黑或文字反白,
在图像对象上设 `Decode = [1, 0]` 翻转。**这一点必须实测确认**,两种情况都遇到过。

### 建议做成自动降级
```python
HAS_JBIG2 = shutil.which("jbig2") is not None
```
有就用 JBIG2(体积最优),没有就用 G4。这样同一份代码在 Mac 和 Windows 都能跑。

---

## 6. 界面规格书

以下是 macOS 版的完整功能,Windows 版照此实现。**默认值都是实测定下来的,不要随意改。**

### 6.1 两个标签页:`图片转 PDF` / `合并 PDF`

### 6.2 图片转 PDF 页

**① 源选择区**(拖放框)
- 支持拖入**文件夹**,也支持拖入/多选**若干张图片**
- 选中后显示:文件夹名、完整路径、`N 张图片 · 编号 A–B`
- 右侧按钮:`选择…`(选中后变`换一个…`)、`✕`(清除并重置全部)

**② 输出设置**

| 控件 | 选项 | 默认 |
|---|---|---|
| 色彩 | 黑白·最小 ／ **彩色·文字锐化** ／ 灰度 ／ 原样彩色 | 黑白 |
| 页面范围 | 两个输入框「从 __ 到 __」,预填检测到的实际范围 + 一个"恢复全部"按钮 | 全部 |
| 清晰度 | 原生分辨率 ／ 2400px ／ 1800px ／ 1400px<br>(彩色·文字锐化模式下改为只读文字「文字 2200px · 色彩 500px」) | 原生 |
| 封面封底 | 保留原样:首末两页 ／ 保留原样:第1、2页 ／ **无封面:全部转黑白** ／ 自定义 | 首末两页 |
| 输出文件名 | 可编辑文本框 + `.pdf` 后缀 + 「还原」「换位置…」两个按钮 | 源文件夹名 |

**③ 高级选项**(可折叠;**整行都要能点开,不能只有小三角能点**)

黑白模式下:
- 二值化阈值滑杆 40–70%,默认 **55%**(开提白后自动变 58%)
- ☐ 提白 + 锐化 —— 副标题「阈值前去灰底、锐边缘,体积 +2%,墨淡的扫描提升明显」
- ☐ 边缘精细化 —— 副标题「放大 2 倍再二值化,台阶细一半,体积约 ×2」
  - **⚠ 图片数 ≥300 且勾选时,必须在「输出设置」顶部显示橙色提醒 + 「关掉」按钮**
    (文案:「这本有 N 张,「边缘精细化」会让体积翻倍。通读为主的大部头建议关掉」)

彩色·文字锐化(MRC)模式下:
- 文字层分辨率:原生 ／ 2600 ／ **2200(推荐)** ／ 1800 px
- 色彩层:400 ／ **500(推荐)** ／ 700 ／ 900 px + 质量 30/**35**/45/60

灰度/彩色模式下:
- JPEG 质量滑杆 25–85,默认 **45**

通用:
- 排除编号(文本框,如 `3,7,10-12`)
- PDF 标题(只写元数据,**不影响文件名** —— 界面上要写明这句)
- ☐ 先抽样试跑各档位

**④ 任务队列**
- 按钮:`加入队列` ／ `▶ 开始` ／ `■ 取消`
- **加入队列不自动开工**,必须点「开始」;一批跑完自动回到暂停态
- `同时进行` 数字选择器 1–4,**默认 1**
- 队列每行显示:文件名、状态胶囊(等待中/进行中/已完成/失败/已取消)、档位摘要、
  进度条 + 阶段文字、**实时耗时**(运行中「已用 X」/ 完成后「用时 X」)
- 每行操作:在资源管理器中显示、展开日志、移除
- 全部完成时提示音
- 底部:临时文件残留大小 + 「清理」按钮

**⑤ 依赖自检**:启动时检查 `magick` 和 Python 包,缺失则在顶部显示橙色面板 + 安装命令

### 6.3 合并 PDF 页
- 拖入多个 PDF → 列表显示序号、文件名、页数、体积
- 每行可上移/下移/移除;按列表顺序合并
- 选项:☑ 统一为 A4 ☑ 为每个文件加书签(书签名=文件名)
- 输出文件名可编辑

### 6.4 关键交互规则(都是踩过坑才加的)
1. **同名文件不许静默覆盖** —— 弹窗「自动改名 / 覆盖 / 取消」
2. **文件名输入必须净化** —— 去掉换行、控制字符、`/ \ :` 等非法字符,截断 120 字
   (真实事故:用户误把剪贴板里的报错文本粘进文件名)
3. **所有档位设置跨启动记忆**(Windows 用注册表或配置文件均可)

---

## 7. 引擎调用方式

界面通过子进程调用,加 `--progress` 参数可得到机器可读的进度行:

```
python scan2pdf.py <源图目录> <输出.pdf> --mode bw --width 0 --covers first,last --progress
```

**进度协议**(每行一条,直接解析即可):
```
@@P <阶段> <已完成> <总数>     阶段 = binarize | compress | jbig2
@@S <阶段>                     阶段 = jbig2 | assemble
@@DONE <字节数> <页数> <路径>
```
非 `@@` 开头的行是给人看的日志,原样显示即可。

**常用参数**(完整列表运行 `python scan2pdf.py --help`):
```
--mode bw|smooth|mrc|gray|color
--width N            0=原生分辨率
--quality N          灰度/彩色的 JPEG 质量
--threshold 55%      黑白阈值
--enhance            提白+锐化(黑白与彩色自动用不同配方)
--supersample 2      边缘精细化
--mrc-mask-width 2200 --mrc-bg-width 500 --mrc-bg-quality 35
--range A-B  --include 3,7  --exclude 10-12
--covers first,last | none | 1,2
--jobs N             并行度
--keep-cache         保留中间产物(默认成功后自动清理)
```

---

## 8. 验收清单

- [ ] `magick`、Python 包自检通过
- [ ] 黑白模式:15 页文稿 → 约 0.3 MB(用 G4 则约 0.6 MB)
- [ ] 黑白 + 提白 + 边缘精细化:同一批 → 约 0.6 MB(G4 约 1.2 MB)
- [ ] **MRC 模式**:彩色漫画 → 文字锐利、色块正常、**红色/彩色区域不能被涂黑**
- [ ] MRC 蒙版极性正确(不是整页黑、也不是文字反白)
- [ ] 合并:两个 PDF → 页数相加、书签正确、体积≈之和
- [ ] 队列:加入不自动跑;点开始才跑;取消能停
- [ ] 同名文件弹出覆盖确认
- [ ] 成品统一 A4,用 Acrobat/Edge 打开正常

---

## 9. 关键调参常数(**改之前先读原因**)

这些数字都是反复实测定下来的,不是随手填的:

| 常数 | 值 | 为什么 |
|---|---|---|
| 二值化阈值 | 55% | 20+ 本书验证;墨淡的书可降到 50 |
| 边缘涂白 | 左右 1.5%、上下 1% | 清掉书脊阴影和黑边,否则进压缩又脏又大 |
| 提白锐化(黑白) | `-level 15%,88% -unsharp 0x1.2+1.2+0.01` | 为"阈值切得准"调的 |
| 提白锐化(彩色) | `-level 6%,94% -sigmoidal-contrast 3.5,50% -modulate 102,107,100 -unsharp 0x0.9+1.0+0.008` | **不可与黑白档混用** —— 黑白档施于彩图会把浅色压掉 |
| MRC 暗度门 | 50% | 65% 会把中性灰画面(路面)当成墨,出现黑斑 |
| MRC 色度门 | 35% | 20% 会把**深蓝色文字**切碎;55% 开始把彩色人物涂黑 |
| MRC 文字层 | 2200px | 再高收益很小,再低开始看得出 |
| MRC 底图 | 500px / q35 | 文字已由蒙版承担,底图可以很低 |
| JBIG2 切段 | 每段约 106 页,最多 8 段 | 词典随页数膨胀,切段后 848 页从 888 秒降到 30 秒 |

---

## 10. MRC 模式的实现要点(**最容易做错的部分**)

一页拆两层:①彩色底图(低分辨率,只管颜色) ②1-bit 文字蒙版(高分辨率,只管"哪里是墨")。
PDF 里先画底图,再用**纯黑**透过 `/ImageMask` 蒙版画上文字。

### 六个必须避开的坑(按当初踩到的顺序)

1. **判"是不是墨"必须两个条件:够暗 AND 色度够低。** 只看明暗会把红头发、深色衣服涂黑。
2. **色度必须用 HCL 的 C 通道(绝对色度),不能用 HSL/HSV 的饱和度。**
   近黑像素的饱和度会因分母趋零而虚高 → 黑笔画内部被误排除,只剩空心轮廓。
   ImageMagick 写法:`-colorspace HCL -channel G -separate +channel -threshold 35% -negate`
3. **暗度门不能太松**(65% 会把中性灰画面当墨 → 黑斑),50% 干净。
4. **色度门不能太严**(20% 会把深蓝色标题切碎 → 露出底图的模糊蓝字),35% 合适。
5. **底图必须先挖掉墨再降分辨率。** 否则锐字周围会有模糊鬼影 + JPEG 振铃噪点。
6. **挖洞要按蒙版原样挖**:把墨设为**透明**,缩放时 alpha 加权(只让非墨像素参与平均),
   剩余空洞填白。
   - ✗ 用"整幅模糊"去填 —— 模糊图含墨,糊出脏色被色度采样拉成**蓝紫横条拖影**
   - ✗ 把墨区放大几像素再挖 —— 洞比字大,黑字盖不住,彩色区露白边

**封面封底不做 MRC**(纯彩色高分辨率),否则深色画面会被蒙版涂黑。

**已知副作用**:深色彩色文字会被画成纯黑(蒙版只能单色填充)。用户已接受。

### 实测收益(198 页彩色漫画)
| 方案 | 体积 | 文字分辨率 |
|---|---|---|
| 普通彩色 900px | 35.7 MB | 900px |
| **MRC** | **14.2 MB** | **2200px** |

---

## 11. 其他已知坑

- **ImageMagick 的 `-shave`+`-border` 用于边缘涂白**,顺序不能反
- **磁盘中间产物**:每页约 0.1 MB,千页书约 130 MB;**成功后自动删除,失败时保留以便续做**
- **同一本书同一档位不许同时排两次** —— 缓存目录按「源目录+档位」分,并发会互相踩产物
- **PDF 合并绝不能用"打印成 PDF"** —— 会重新光栅化,毁掉压缩;必须用 pikepdf 搬运页面对象
- 成品是纯图片 PDF:**不可搜索、不可选取文字、无目录书签**,要向用户说明

---

## 12. 需要带到 Windows 的文件

```
scan2pdf.py            引擎(必需)
merge_pdfs.py          合并工具(必需)
WINDOWS_移植交接.md    本文档
ScanToPDF.swift        macOS 界面源码 —— 仅作界面规格参照,不能编译
```

前三个已打包成 `dist/ScanPress-Windows移植包.zip`。

---

## 13. 给接手会话的建议顺序

1. 先让引擎在 Windows 上**命令行跑通**(改 §4 三处 + §5 的 G4 后备),用一小批图验证
2. 确认黑白、MRC 两个模式的产出正确(尤其 MRC 的蒙版极性和彩色区域)
3. 再做界面,先做"单个任务能跑通"的最小版本
4. 最后补队列、设置记忆、覆盖确认这些交互细节

**不要一上来就写界面** —— 引擎跑不通的话界面全是空壳。
