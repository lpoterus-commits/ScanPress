// ScanPress.app —— scan2pdf.py 的原生 macOS 图形外壳(SwiftUI)
// 编译:见同目录 build_app.py(swiftc -parse-as-library -swift-version 5)
// 职责:选源图文件夹 → 选档位 → 调 ~/.venvs/pdfenv/bin/python <Resources/scan2pdf.py> → 解析
//       @@P/@@S/@@DONE 进度行 → 进度条 + 日志 + 完成后在 Finder 中显示。
// 真正的转换逻辑全在 scan2pdf.py 里,本文件不含任何图像/PDF 处理。

import SwiftUI
import AppKit
import UniformTypeIdentifiers
import PDFKit

let IMG_EXTS: Set<String> = ["jpg", "jpeg", "png", "tif", "tiff"]
let BREW = "/opt/homebrew/bin"
let BIG_BOOK = 300      // 超过这个张数即视为"大部头",提醒关闭会让体积翻倍的选项

func homeFile(_ rel: String) -> String {
    FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(rel).path
}

/// 包内自带的工具链(build_app.py 把 magick / jbig2 / jbig2topdf.py 连同 dylib 搬了进来)。
/// 有就用包内的,没有(用 --no-bundle-tools 构建的瘦身包)再回落到 homebrew。
enum Tools {
    static let helpers = Bundle.main.bundleURL
        .appendingPathComponent("Contents/Helpers").path
    static let magickConfig = Bundle.main.bundleURL
        .appendingPathComponent("Contents/Resources/ImageMagick").path

    /// 包内工具链是否齐备(jbig2topdf.py 是纯脚本,放在 Resources 里,由 scan2pdf.py 自己就近找)
    static var bundled: Bool {
        let fm = FileManager.default
        return ["magick", "jbig2"].allSatisfy { fm.isExecutableFile(atPath: "\(helpers)/\($0)") }
            && Bundle.main.url(forResource: "jbig2topdf", withExtension: "py") != nil
    }

    /// 包内自带的 Python(装好了 pikepdf/img2pdf/Pillow);没有则回落到用户的 pdfenv
    static var python: String {
        let inside = Bundle.main.bundleURL
            .appendingPathComponent("Contents/Resources/python/bin/python3").path
        return FileManager.default.isExecutableFile(atPath: inside)
            ? inside : homeFile(".venvs/pdfenv/bin/python")
    }

    /// 子进程的 PATH:包内优先,homebrew 兜底(potrace 等未内嵌的工具还得靠它)
    static var searchPath: String {
        (bundled ? "\(helpers):" : "") + "\(BREW):/usr/bin:/bin:/usr/sbin:/sbin"
    }

    /// 给子进程加上工具链相关的环境变量
    static func apply(to env: inout [String: String]) {
        env["PATH"] = searchPath
        guard bundled else { return }
        // 这个 ImageMagick 是模块化编译的:coder 都是单独的 .so,不指路连 JPEG 都读不出来
        env["MAGICK_CONFIGURE_PATH"] = "\(magickConfig)/etc:\(magickConfig)/share:\(magickConfig)/config"
        env["MAGICK_CODER_MODULE_PATH"] = "\(magickConfig)/modules/coders"
        env["MAGICK_FILTER_MODULE_PATH"] = "\(magickConfig)/modules/filters"
    }
}

/// 统一的行标签:固定宽度右对齐,让「基本」与「高级」两组 Grid 的标签列对齐
func L(_ s: String) -> some View {
    Text(s).frame(width: 78, alignment: .trailing)
}

/// 文件名净化:粘贴进来的东西什么都可能有(换行、制表符、控制字符、路径分隔符)。
/// 真实案例:某次「截图到剪贴板」失败,剪贴板里存的是 screencapture 的报错文本,
/// 用户 Cmd+V 到文件名栏,那段报错就成了文件名的一部分。输入框一律先过这一道。
func sanitizeName(_ s: String) -> String {
    var out = s.unicodeScalars.filter { !CharacterSet.controlCharacters.contains($0) }
        .map(String.init).joined()
    for bad in ["/", ":", "\\", "\n", "\r", "\t"] { out = out.replacingOccurrences(of: bad, with: "") }
    out = out.trimmingCharacters(in: .whitespaces)
    if out.count > 120 { out = String(out.prefix(120)) }   // HFS/APFS 上限 255 字节,留足余量
    return out
}

/// 输出文件名输入框(两个标签页共用):净化 + 空值不覆盖 + 保留原目录
func nameBinding(_ url: Binding<URL?>) -> Binding<String> {
    Binding(
        get: { url.wrappedValue?.deletingPathExtension().lastPathComponent ?? "" },
        set: { raw in
            guard let d = url.wrappedValue else { return }
            let n = sanitizeName(raw)
            guard !n.isEmpty else { return }
            url.wrappedValue = d.deletingLastPathComponent().appendingPathComponent(n + ".pdf")
        })
}

/// 临时文件(~/.cache/scan2pdf):正常完成后 scan2pdf.py 会自动删除,
/// 这里只兜底「失败/中断」留下的残留,让用户能看见并一键清掉。
enum Cache {
    static let dir = homeFile(".cache/scan2pdf")

    static func size() -> Int64 {
        let fm = FileManager.default
        guard let en = fm.enumerator(at: URL(fileURLWithPath: dir),
                                     includingPropertiesForKeys: [.fileSizeKey]) else { return 0 }
        var total: Int64 = 0
        for case let u as URL in en {
            total += Int64((try? u.resourceValues(forKeys: [.fileSizeKey]))?.fileSize ?? 0)
        }
        return total
    }

    static func clear() { try? FileManager.default.removeItem(atPath: dir) }
}

/// 秒 → 人读时长(队列行显示用)
func fmtDuration(_ t: TimeInterval) -> String {
    let s = Int(t.rounded())
    if s < 60 { return "\(s) 秒" }
    if s < 3600 { return s % 60 == 0 ? "\(s / 60) 分" : "\(s / 60) 分 \(s % 60) 秒" }
    return "\(s / 3600) 小时 \(s % 3600 / 60) 分"
}

final class Model: ObservableObject {
    @Published var src: URL?
    @Published var imgCount = 0
    @Published var imgRange = ""
    @Published var dest: URL?

    @Published var mode = "bw" { didSet { save() } }
    @Published var width = 0 { didSet { save() } }          // 0 = 原生分辨率
    @Published var quality = 45 { didSet { save() } }
    @Published var threshold = 55 { didSet { save() } }
    @Published var enhance = false {          // 提白会推亮整体,阈值同步上调一档(用户自己动过则不干预)
        didSet {
            if enhance, threshold == 55 { threshold = 58 }
            if !enhance, threshold == 58 { threshold = 55 }
            save()
        }
    }
    @Published var coverPreset = "first,last" { didSet { save() } }
    @Published var coverCustom = ""
    @Published var rangeFrom = ""          // 页面范围(主界面),默认=检测到的全书范围
    @Published var rangeTo = ""
    @Published var fullFrom = 0            // 检测到的实际范围,用于判断用户是否改过
    @Published var fullTo = 0
    @Published var includeList = ""        // 选中若干单个文件时:只处理这些编号
    @Published var pickedFiles = 0         // >0 表示当前是"选了 N 个文件"而非整个文件夹
    @Published var excludeText = ""
    @Published var title = ""
    /// MRC 材料预设。两类材料的最优参数差别很大,直接给预设比让用户猜数字友好:
    ///   text  = 彩色教材/杂志(彩色文字多)→ 不做彩色分层,底图给足;实测 180 页 17.4 MB
    ///   comic = 漫画/绘本(黑字为主)     → 开彩色分层,底图可以很低;实测 198 页 14.2 MB
    @Published var mrcPreset = "text" {
        didSet {
            guard loaded else { return }   // 启动恢复设置时不要用预设值盖掉用户微调过的数值
            if mrcPreset == "text" {
                mrcMaskWidth = 2600; mrcBgWidth = 1500; mrcBgQuality = 50; mrcColorLayers = false
            } else {
                mrcMaskWidth = 2200; mrcBgWidth = 500; mrcBgQuality = 35; mrcColorLayers = true
            }
            save()
        }
    }
    @Published var mrcColorLayers = false { didSet { save() } }
    @Published var mrcMaskWidth = 2600 { didSet { save() } }
    @Published var mrcBgWidth = 1500 { didSet { save() } }
    @Published var mrcBgQuality = 50 { didSet { save() } }
    @Published var supersample = false { didSet { save() } }   // 黑白:阈值前放大 2 倍,台阶细一半
    @Published var probe = false

    @Published var running = false
    @Published var phase = ""
    @Published var progress = 0.0
    @Published var log = ""
    @Published var doneMsg: String?
    @Published var failMsg: String?
    @Published var missing: [String] = []

    private var task: Process?
    private var buf = Data()


    // 档位设置跨启动记忆:用户调好的组合(如「提白锐化 + 边缘精细化」)下次打开仍在
    private var loaded = false

    init() {
        let d = UserDefaults.standard
        if d.object(forKey: "mode") != nil {
            mode = d.string(forKey: "mode") ?? "bw"
            width = d.integer(forKey: "width")
            quality = d.integer(forKey: "quality")
            threshold = d.integer(forKey: "threshold")
            enhance = d.bool(forKey: "enhance")
            supersample = d.bool(forKey: "supersample")
            // 以 mrcPreset 键是否存在为准:2.0 升上来的旧设置没有它,直接采用新版默认(教材档)
            if let p = d.string(forKey: "mrcPreset") {
                mrcMaskWidth = d.integer(forKey: "mrcMaskWidth")
                mrcBgWidth = d.integer(forKey: "mrcBgWidth")
                mrcBgQuality = d.integer(forKey: "mrcBgQuality")
                mrcColorLayers = d.bool(forKey: "mrcColorLayers")
                mrcPreset = p
            }
            coverPreset = d.string(forKey: "coverPreset") ?? "first,last"
        }
        loaded = true
    }

    private func save() {
        guard loaded else { return }
        let d = UserDefaults.standard
        d.set(mode, forKey: "mode")
        d.set(width, forKey: "width")
        d.set(quality, forKey: "quality")
        d.set(threshold, forKey: "threshold")
        d.set(enhance, forKey: "enhance")
        d.set(supersample, forKey: "supersample")
        d.set(mrcMaskWidth, forKey: "mrcMaskWidth")
        d.set(mrcBgWidth, forKey: "mrcBgWidth")
        d.set(mrcBgQuality, forKey: "mrcBgQuality")
        d.set(mrcColorLayers, forKey: "mrcColorLayers")
        d.set(mrcPreset, forKey: "mrcPreset")
        d.set(coverPreset, forKey: "coverPreset")
    }

    var scriptPath: String? {
        Bundle.main.url(forResource: "scan2pdf", withExtension: "py")?.path
    }
    var pythonPath: String { Tools.python }

    /// 启动时自检外部依赖,缺什么直接告诉用户,不要等跑一半才失败
    func checkDeps() {
        let fm = FileManager.default
        var miss: [String] = []
        if !fm.isExecutableFile(atPath: pythonPath) {
            miss.append("Python 环境(应用包内未内嵌,且没有 ~/.venvs/pdfenv)—— 终端执行:python3 -m venv ~/.venvs/pdfenv && ~/.venvs/pdfenv/bin/pip install pikepdf img2pdf Pillow")
        }
        if !Tools.bundled {          // 包内自带工具链时无需检查 homebrew
            for (bin, hint) in [("magick", "brew install imagemagick"),
                                ("jbig2", "brew install jbig2enc"),
                                ("jbig2topdf.py", "brew install jbig2enc")] {
                if !fm.isExecutableFile(atPath: "\(BREW)/\(bin)") { miss.append("\(bin) —— 终端执行:\(hint)") }
            }
        }
        if scriptPath == nil { miss.append("scan2pdf.py(应用包内资源缺失,请重新构建)") }
        missing = miss
    }

    func pick() {
        let p = NSOpenPanel()
        p.canChooseDirectories = true
        p.canChooseFiles = true                 // 也允许直接挑若干张图
        p.allowsMultipleSelection = true
        p.allowedContentTypes = [.folder, .jpeg, .png, .tiff]
        p.prompt = "选择"
        p.message = "选择扫描图所在的文件夹,或直接选中若干张图片"
        if p.runModal() == .OK, !p.urls.isEmpty { adopt(p.urls) }
    }

    /// 清空当前选择,回到初始状态(换素材时不必重开应用)
    func clear() {
        src = nil; dest = nil; imgCount = 0; imgRange = ""
        rangeFrom = ""; rangeTo = ""; fullFrom = 0; fullTo = 0
        includeList = ""; pickedFiles = 0
        excludeText = ""; title = ""; failMsg = nil
    }

    /// 从文件名末尾的数字取编号
    private func num(_ u: URL) -> Int? {
        let stem = u.deletingPathExtension().lastPathComponent
        let digits = stem.reversed().prefix(while: { $0.isNumber }).reversed()
        return Int(String(digits))
    }

    func adopt(_ u: URL) { adopt([u]) }

    func adopt(_ urls: [URL]) {
        guard let first = urls.first else { return }
        let firstIsDir = (try? first.resourceValues(forKeys: [.isDirectoryKey]))?.isDirectory ?? false
        // 选了若干张图片 → 取其所在文件夹为源,并只处理这些编号
        let picked = firstIsDir ? [] : urls.filter {
            IMG_EXTS.contains($0.pathExtension.lowercased())
        }
        let dir = firstIsDir ? first : first.deletingLastPathComponent()
        src = dir
        let files = (try? FileManager.default.contentsOfDirectory(atPath: dir.path)) ?? []
        let imgs = files.filter { IMG_EXTS.contains(($0 as NSString).pathExtension.lowercased())
                                  && !$0.hasPrefix(".") }
        imgCount = imgs.count
        let nums = imgs.compactMap { f -> Int? in
            let stem = (f as NSString).deletingPathExtension
            let digits = stem.reversed().prefix(while: { $0.isNumber }).reversed()
            return Int(String(digits))
        }.sorted()
        imgRange = nums.isEmpty ? "(文件名无编号,按名称排序)" : "编号 \(nums.first!)–\(nums.last!)"
        fullFrom = nums.first ?? 1
        fullTo = nums.last ?? imgCount
        rangeFrom = String(fullFrom)
        rangeTo = String(fullTo)

        if picked.count > 0 {                    // 选中的是若干张图
            let ns = picked.compactMap { num($0) }.sorted()
            pickedFiles = picked.count
            includeList = ns.map(String.init).joined(separator: ",")
            imgRange = ns.isEmpty ? "已选 \(picked.count) 张"
                                  : "已选 \(picked.count) 张,编号 \(ns.first!)–\(ns.last!)"
            if let a = ns.first, let b = ns.last { rangeFrom = String(a); rangeTo = String(b) }
        } else {
            pickedFiles = 0
            includeList = ""
        }

        if title.isEmpty { title = dir.lastPathComponent }
        dest = dir.deletingLastPathComponent()
            .appendingPathComponent(dir.lastPathComponent + ".pdf")
        doneMsg = nil; failMsg = nil; log = ""; progress = 0
    }

    func chooseDest() {
        let p = NSSavePanel()
        p.allowedContentTypes = [.pdf]
        p.nameFieldStringValue = dest?.lastPathComponent ?? "输出.pdf"
        p.directoryURL = dest?.deletingLastPathComponent()
        if p.runModal() == .OK, let u = p.url { dest = u }
    }

    var covers: String {
        coverPreset == "custom" ? (coverCustom.isEmpty ? "none" : coverCustom) : coverPreset
    }

    func args() -> [String] {
        var a = [scriptPath!, src!.path, dest!.path,
                 "--mode", mode, "--width", String(width),
                 "--covers", covers, "--progress"]
        if mode == "mrc" {
            a += ["--mrc-mask-width", String(mrcMaskWidth),
                  "--mrc-bg-width", String(mrcBgWidth),
                  "--mrc-bg-quality", String(mrcBgQuality)]
            if !mrcColorLayers { a.append("--mrc-no-color-layers") }
        } else if mode == "bw" {
            a += ["--threshold", "\(threshold)%"]
            if enhance { a.append("--enhance") }
            if supersample { a += ["--supersample", "2"] }
        }
        else { a += ["--quality", String(quality)] }
        if !includeList.isEmpty { a += ["--include", includeList] }
        let f = Int(rangeFrom) ?? fullFrom, t = Int(rangeTo) ?? fullTo
        if f != fullFrom || t != fullTo { a += ["--range", "\(f)-\(t)"] }   // 用户改过才传
        if !excludeText.trimmingCharacters(in: .whitespaces).isEmpty {
            a += ["--exclude", excludeText.trimmingCharacters(in: .whitespaces)]
        }
        if !title.isEmpty { a += ["--title", title] }
        if probe { a += ["--probe"] }
        return a
    }

    /// 同名文件已存在时:绝不静默覆盖(旧版本的 bug——改了「PDF 标题」不改文件名,第二次跑把第一份盖掉了)
    /// 返回 false 表示用户选择取消
    private func resolveOverwrite() -> Bool {
        guard let d = dest, FileManager.default.fileExists(atPath: d.path) else { return true }
        let a = NSAlert()
        a.messageText = "「\(d.lastPathComponent)」已经存在"
        a.informativeText = "覆盖会丢掉上一次的结果。想保留两份就选「自动改名」。\n"
            + "(提示:「PDF 标题」只写进文档属性,不会改文件名。)"
        a.addButton(withTitle: "自动改名")
        a.addButton(withTitle: "覆盖")
        a.addButton(withTitle: "取消")
        switch a.runModal() {
        case .alertFirstButtonReturn:
            var n = 2
            let base = d.deletingPathExtension().path
            var cand = URL(fileURLWithPath: "\(base) \(n).pdf")
            while FileManager.default.fileExists(atPath: cand.path) {
                n += 1
                cand = URL(fileURLWithPath: "\(base) \(n).pdf")
            }
            dest = cand
            return true
        case .alertSecondButtonReturn:
            return true
        default:
            return false
        }
    }

    /// 档位摘要(队列行显示用)
    var summary: String {
        if mode == "mrc" {
            var t = ["彩色·文字锐化", mrcPreset == "text" ? "教材档" : "漫画档",
                     "文字 \(mrcMaskWidth == 0 ? "原生" : "\(mrcMaskWidth)px")",
                     "色彩 \(mrcBgWidth)px/q\(mrcBgQuality)"]
            if mrcColorLayers { t.append("彩字分层") }
            t.append(covers == "none" ? "无封面" : "封面 \(covers)")
            if probe { t.append("试跑档位") }
            return t.joined(separator: " · ")
        }
        var s = [mode == "bw" ? "黑白" : (mode == "gray" ? "灰度" : "彩色"),
                 width == 0 ? "原生" : "\(width)px"]
        if mode == "bw" {
            s.append("阈值\(threshold)%")
            if enhance { s.append("提白锐化") }
            if supersample { s.append("精细边缘") }
        } else {
            s.append("q\(quality)")
        }
        s.append(covers == "none" ? "无封面" : "封面 \(covers)")
        if probe { s.append("试跑档位") }
        return s.joined(separator: " · ")
    }

    /// 把当前设置冻结成一条队列任务(之后再改设置不影响已入队的任务)
    func makeJob() -> Job? {
        failMsg = nil
        guard src != nil, dest != nil, scriptPath != nil else { return nil }
        if !probe && !resolveOverwrite() { return nil }
        let a = args()                       // resolveOverwrite 可能改过 dest,故在其后取参数
        if JobQueue.shared.contains(args: a) {
            failMsg = "同一本书的同一档位已经在队列里了(换个档位或等它跑完)"
            return nil
        }
        return Job(src: src!, dest: dest!, args: a, summary: summary,
                   isProbe: probe, python: pythonPath)
    }
}

// ==================== 任务队列 ====================
// 实测(M5/10 核):二值化占 10% 且已吃满多核,JBIG2 占 85% 但**单线程**——
// 所以并行做多本书几乎线性提速(一本在 jbig2 时其余 9 核闲着)。默认并发 3。

final class Job: ObservableObject, Identifiable {
    enum State: String { case waiting = "等待中", running = "进行中",
                              done = "已完成", failed = "失败", cancelled = "已取消" }
    let id = UUID()
    let src: URL
    let args: [String]
    let summary: String
    let isProbe: Bool
    let python: String
    @Published var dest: URL
    @Published var state: State = .waiting
    @Published var progress = 0.0
    @Published var phase = "排队中"
    @Published var result = ""
    @Published var log = ""
    @Published var startedAt: Date?
    @Published var elapsed: TimeInterval = 0     // 完成后固定为总耗时
    private var proc: Process?
    private var buf = Data()

    init(src: URL, dest: URL, args: [String], summary: String, isProbe: Bool, python: String) {
        self.src = src; self.dest = dest; self.args = args
        self.summary = summary; self.isProbe = isProbe; self.python = python
    }

    var name: String { dest.deletingPathExtension().lastPathComponent }
    var isActive: Bool { state == .waiting || state == .running }

    func start(jobs: Int, jbig2Workers: Int, onFinish: @escaping () -> Void) {
        guard state == .waiting else { return }
        state = .running; phase = "准备…"; progress = 0
        startedAt = Date()
        let p = Process()
        p.executableURL = URL(fileURLWithPath: python)
        p.arguments = args + ["--jobs", String(jobs), "--jbig2-slots", String(jbig2Workers)]
        var env = ProcessInfo.processInfo.environment
        Tools.apply(to: &env)
        env["PYTHONUNBUFFERED"] = "1"
        p.environment = env
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe
        buf = Data()
        pipe.fileHandleForReading.readabilityHandler = { [weak self] h in
            let d = h.availableData
            guard !d.isEmpty, let self = self else { return }
            DispatchQueue.main.async { self.consume(d) }
        }
        p.terminationHandler = { [weak self] pr in
            DispatchQueue.main.async {
                guard let self = self else { return }
                pipe.fileHandleForReading.readabilityHandler = nil
                self.proc = nil
                self.elapsed = self.startedAt.map { Date().timeIntervalSince($0) } ?? 0
                if pr.terminationReason == .uncaughtSignal {
                    self.state = .cancelled; self.phase = "已取消"
                } else if pr.terminationStatus != 0 {
                    self.state = .failed; self.phase = "失败(展开日志看末尾)"
                } else {
                    self.state = .done; self.phase = "完成"; self.progress = 1
                    if self.result.isEmpty { self.result = self.isProbe ? "试跑完成,见日志" : "完成" }
                }
                onFinish()
            }
        }
        do { try p.run(); proc = p } catch {
            state = .failed; phase = "无法启动:\(error.localizedDescription)"
            onFinish()
        }
    }

    func cancel() {
        if state == .waiting { state = .cancelled; phase = "已取消" } else { proc?.terminate() }
    }

    private func consume(_ d: Data) {
        buf.append(d)
        while let nl = buf.firstIndex(of: 0x0a) {
            let line = String(decoding: buf[buf.startIndex..<nl], as: UTF8.self)
            buf.removeSubrange(buf.startIndex...nl)
            handle(line)
        }
    }

    private func handle(_ line: String) {
        if line.hasPrefix("@@") {
            let f = line.dropFirst(2).split(separator: " ").map(String.init)
            switch f.first {
            case "P":
                if f.count >= 4, let i = Double(f[2]), let n = Double(f[3]), n > 0 {
                    if f[1] == "jbig2" {
                        progress = 0.74 + 0.19 * (i / n)
                        phase = "JBIG2 压缩 \(Int(i))/\(Int(n)) 段"
                    } else {
                        progress = (i / n) * (f[1] == "binarize" ? 0.72 : 0.85)
                        phase = (f[1] == "binarize" ? "二值化 " : "图像压缩 ") + "\(Int(i))/\(Int(n)) 页"
                    }
                }
            case "S":
                if f.count >= 2 {
                    if f[1] == "jbig2" { progress = 0.74; phase = "JBIG2 压缩(切段并行)…" }
                    if f[1] == "assemble" { progress = 0.93; phase = "组装 PDF + 统一 A4…" }
                }
            case "DONE":
                if f.count >= 4, let bytes = Double(f[1]), let pages = Double(f[2]) {
                    progress = 1
                    if !isProbe {
                        result = String(format: "%.0f 页,%.1f MB(平均 %.0f KB/页)",
                                        pages, bytes / 1e6, bytes / pages / 1024)
                        dest = URL(fileURLWithPath: f[3...].joined(separator: " "))
                    }
                }
            default: break
            }
            return
        }
        log += line + "\n"
        if log.count > 40000 { log = String(log.suffix(30000)) }
    }
}

final class JobQueue: ObservableObject {
    static let shared = JobQueue()
    @Published var jobs: [Job] = []
    /// 暂停态:加入队列不自动开工,等用户点「开始」。每批跑完自动回到暂停,
    /// 这样下一批仍需显式确认,不会有"手一滑就开跑"的意外。
    @Published var paused = true
    @Published var concurrency = 1 { didSet { pump() } }   // 默认一次只做一本
    private let cores = ProcessInfo.processInfo.activeProcessorCount

    var activeCount: Int { jobs.filter { $0.isActive }.count }

    /// 同一本书的同一档位不许排两次:缓存目录按「源目录+档位」分,同档位并发会互相踩产物
    func contains(args: [String]) -> Bool {
        jobs.contains { $0.isActive && $0.args == args }
    }

    func add(_ j: Job) { jobs.append(j); pump() }

    var waitingCount: Int { jobs.filter { $0.state == .waiting }.count }

    /// 开工:解除暂停并把等待中的任务按并发数放出去
    func begin() { paused = false; pump() }

    /// 全部取消:运行中的终止,等待中的标记取消
    func cancelAll() {
        paused = true
        for j in jobs where j.isActive { j.cancel() }
    }

    func pump() {
        guard !paused else { return }          // 暂停中:只排队不开工
        var slots = max(1, concurrency) - jobs.filter { $0.state == .running }.count
        guard slots > 0 else { return }
        // 二值化阶段的并行度按并发数分摊,避免过度订阅;
        // jbig2 不在这里分摊——脚本用**跨进程全局名额**(8 个)自动平衡:
        // 队列里只有一个任务时它独占 8 个名额,中途再加任务则自动分摊,无需重启已跑的任务。
        let per = max(2, cores / max(1, concurrency))
        let jw = 8
        for j in jobs where j.state == .waiting {
            guard slots > 0 else { break }
            slots -= 1
            j.start(jobs: per, jbig2Workers: jw) { [weak self] in
                guard let self = self else { return }
                self.pump()
                if self.activeCount == 0 {
                    self.paused = true                        // 一批跑完回到暂停,等下次确认
                    NSSound.beep()
                }
                self.objectWillChange.send()
            }
        }
        objectWillChange.send()
    }

    func remove(_ j: Job) {
        j.cancel()
        jobs.removeAll { $0.id == j.id }
        pump()
    }

    func clearFinished() { jobs.removeAll { !$0.isActive } }
}

struct ConvertView: View {
    @StateObject var m = Model()
    @ObservedObject var q = JobQueue.shared
    @State private var dropping = false
    @State private var showAdv = false
    @State private var cacheMB: Double = 0

    func refreshCache() {
        DispatchQueue.global(qos: .utility).async {
            let mb = Double(Cache.size()) / 1e6
            DispatchQueue.main.async { cacheMB = mb }
        }
    }

    var body: some View {
        Form {
            if !m.missing.isEmpty {
                Section { depWarning }
            }

            // ---------- 源图文件夹 ----------
            Section {
                dropZone
            } header: {
                Label("扫描图文件夹", systemImage: "photo.stack")
            } footer: {
                if m.src != nil {
                    Text("\(m.imgCount) 张图片 · \(m.imgRange)")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }

            // ---------- 输出设置 ----------
            Section {
                if m.supersample && m.imgCount >= BIG_BOOK {
                    HStack(alignment: .top, spacing: 8) {
                        Image(systemName: "exclamationmark.circle.fill")
                            .foregroundStyle(.orange)
                        VStack(alignment: .leading, spacing: 2) {
                            Text("这本有 \(m.imgCount) 张,「边缘精细化」会让体积翻倍")
                                .font(.callout)
                            Text("放大 2 倍再二值化 → 像素 4 倍。通读为主的大部头建议关掉;"
                                 + "几十页、要放大细看的资料再开。")
                                .font(.caption2).foregroundStyle(.secondary)
                        }
                        Spacer()
                        Button("关掉") { m.supersample = false }
                    }
                    .padding(.vertical, 4)
                }

                if m.src != nil {
                    LabeledContent {
                        HStack(spacing: 6) {
                            TextField("", text: $m.rangeFrom).frame(width: 60)
                            Text("到").foregroundStyle(.secondary)
                            TextField("", text: $m.rangeTo).frame(width: 60)
                            Button {
                                m.rangeFrom = String(m.fullFrom); m.rangeTo = String(m.fullTo)
                            } label: { Image(systemName: "arrow.uturn.backward") }
                                .help("恢复全部页").buttonStyle(.borderless)
                            if m.pickedFiles > 0 {
                                Text("· 已选 \(m.pickedFiles) 张")
                                    .font(.caption2).foregroundStyle(.secondary)
                            }
                        }
                    } label: {
                        Label("页面范围", systemImage: "arrow.left.and.right")
                    }
                    .help("填源图文件名里的编号(如 image00060.jpg 就是 60),不是书上印的页码")
                }

                Picker(selection: $m.mode) {
                    Text("黑白 · 最小").tag("bw")
                    Text("彩色 · 文字锐化").tag("mrc")
                    Text("灰度").tag("gray")
                    Text("原样彩色").tag("color")
                } label: {
                    Label("色彩", systemImage: "circle.lefthalf.filled")
                }
                .pickerStyle(.segmented)
                .help("彩色 · 文字锐化 = 文字/线条按高分辨率黑白处理,色块用低分辨率彩色,"
                      + "两层叠加。彩色书的推荐档:文字比普通彩色锐利得多,体积反而更小。")

                if m.mode == "mrc" {
                    LabeledContent {
                        Text("文字 \(m.mrcMaskWidth)px · 色彩 \(m.mrcBgWidth)px")
                            .foregroundStyle(.secondary)
                    } label: {
                        Label("清晰度", systemImage: "square.grid.3x3")
                    }
                    .help("MRC 两层各有分辨率,在高级选项里可调")
                } else {
                Picker(selection: $m.width) {
                    Text("原生分辨率(推荐)").tag(0)
                    Text("2400 px · 290 DPI").tag(2400)
                    Text("1800 px · 218 DPI").tag(1800)
                    Text("1400 px · 169 DPI").tag(1400)
                } label: {
                    Label("清晰度", systemImage: "square.grid.3x3")
                }
                }

                Picker(selection: $m.coverPreset) {
                    Text("保留原样:首末两页").tag("first,last")
                    Text("保留原样:第 1、2 页").tag("1,2")
                    Text("无封面:全部转黑白").tag("none")
                    Text("自定义…").tag("custom")
                } label: {
                    Label("封面封底", systemImage: "book.closed")
                }
                .help("封面页保留原图(会显出纸张色调),其余页转纯黑白。没有封面的文稿请选「无封面」。")

                if m.coverPreset == "custom" {
                    LabeledContent("自定义页号") {
                        TextField("如 1,2", text: $m.coverCustom).frame(width: 140)
                    }
                }

                LabeledContent {
                    HStack(spacing: 6) {
                        TextField("", text: nameBinding($m.dest))
                            .frame(width: 170).disabled(m.src == nil)
                        Text(".pdf").foregroundStyle(.secondary)
                        Button {
                            if let s = m.src {
                                m.dest = s.deletingLastPathComponent()
                                    .appendingPathComponent(s.lastPathComponent + ".pdf")
                            }
                        } label: { Image(systemName: "arrow.uturn.backward") }
                            .help("还原为源文件夹名").disabled(m.src == nil)
                        Button { m.chooseDest() } label: { Image(systemName: "folder") }
                            .help("换个保存位置").disabled(m.src == nil)
                    }
                } label: {
                    Label("输出文件名", systemImage: "square.and.arrow.down")
                }

                if let d = m.dest {
                    Text("将写入 " + d.deletingLastPathComponent().path)
                        .font(.caption2).foregroundStyle(.tertiary)
                        .lineLimit(1).truncationMode(.head)
                }
            } header: {
                Label("输出设置", systemImage: "slider.horizontal.3")
            }

            // ---------- 高级 ----------
            Section {
                DisclosureGroup(isExpanded: $showAdv) {
                    advancedRows
                } label: {
                    Label("高级选项", systemImage: "gearshape")
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .contentShape(Rectangle())          // 让整行都是点击热区
                        .onTapGesture { withAnimation { showAdv.toggle() } }
                }
            }

            // ---------- 队列 ----------
            Section {
                HStack(spacing: 8) {
                    Button {
                        if let j = m.makeJob() { q.add(j) }
                    } label: {
                        Label(m.probe ? "加入队列(试跑)" : "加入队列", systemImage: "plus.circle")
                    }
                    .disabled(m.src == nil || m.imgCount == 0 || !m.missing.isEmpty)

                    Button {
                        q.begin()
                    } label: {
                        Label("开始", systemImage: "play.fill")
                    }
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut(.defaultAction)
                    .disabled(q.waitingCount == 0)

                    Button {
                        q.cancelAll()
                    } label: {
                        Label("取消", systemImage: "stop.fill")
                    }
                    .disabled(q.activeCount == 0)

                    Spacer()
                    Text("同时进行").foregroundStyle(.secondary).font(.callout)
                    Picker("", selection: $q.concurrency) {
                        ForEach(1...4, id: \.self) { Text("\($0)").tag($0) }
                    }.labelsHidden().frame(width: 60)
                    if q.jobs.contains(where: { !$0.isActive }) {
                        Button("清除已完成") { q.clearFinished() }
                    }
                }

                if let f = m.failMsg {
                    Label(f, systemImage: "exclamationmark.triangle.fill")
                        .foregroundStyle(.orange).font(.callout)
                }

                if q.jobs.isEmpty {
                    HStack(spacing: 8) {
                        Image(systemName: "tray").foregroundStyle(.tertiary)
                        Text("队列为空。先「加入队列」(可连加好几本),再点「开始」。")
                            .font(.caption).foregroundStyle(.secondary)
                    }.padding(.vertical, 6)
                } else {
                    ForEach(q.jobs) { job in
                        JobRow(job: job, q: q)
                    }
                }
            } header: {
                HStack {
                    Label("任务队列", systemImage: "list.bullet.rectangle")
                    if q.waitingCount > 0 && q.activeCount == q.waitingCount {
                        Text("· \(q.waitingCount) 个等待中,点「开始」开工")
                            .font(.caption).foregroundStyle(.orange)
                    }
                }
            } footer: {
                HStack(spacing: 6) {
                    Image(systemName: cacheMB < 1 ? "checkmark.circle" : "internaldrive")
                        .foregroundStyle(.tertiary)
                    Text(cacheMB < 1 ? "临时文件:无残留(任务成功后自动清除)"
                         : String(format: "临时文件残留 %.0f MB(来自失败或中断的任务)", cacheMB))
                        .font(.caption2).foregroundStyle(.secondary)
                    if cacheMB >= 1 {
                        Button("清理") { Cache.clear(); refreshCache() }
                            .buttonStyle(.link).font(.caption2)
                    }
                    Spacer()
                }
            }
        }
        .formStyle(.grouped)
        .frame(width: 620, height: 600)
        .onAppear { m.checkDeps(); refreshCache() }
        .onChange(of: q.activeCount) { _ in refreshCache() }
    }

    // ---------- 子视图 ----------
    var dropZone: some View {
        HStack(spacing: 12) {
            Image(systemName: m.src == nil ? "square.and.arrow.down.on.square" : "folder.fill")
                .font(.system(size: 26))
                .foregroundStyle(m.src == nil ? Color.secondary : Color.accentColor)
            VStack(alignment: .leading, spacing: 2) {
                Text(m.src?.lastPathComponent ?? "把文件夹(或若干张图片)拖到这里")
                    .fontWeight(m.src == nil ? .regular : .medium)
                    .lineLimit(1).truncationMode(.middle)
                    .foregroundStyle(m.src == nil ? .secondary : .primary)
                Text(m.src?.deletingLastPathComponent().path ?? "或点右侧「选择…」,文件夹和单张图片都能选")
                    .font(.caption2).foregroundStyle(.tertiary)
                    .lineLimit(1).truncationMode(.head)
            }
            Spacer()
            if m.src != nil {
                Button { m.clear() } label: { Image(systemName: "xmark.circle.fill") }
                    .buttonStyle(.borderless).foregroundStyle(.tertiary)
                    .help("清除当前选择,重新挑素材")
            }
            Button(m.src == nil ? "选择…" : "换一个…") { m.pick() }
        }
        .padding(12)
        .frame(maxWidth: .infinity)
        .background(RoundedRectangle(cornerRadius: 10)
            .fill(dropping ? Color.accentColor.opacity(0.15) : Color.secondary.opacity(0.08)))
        .overlay(RoundedRectangle(cornerRadius: 10)
            .strokeBorder(dropping ? Color.accentColor : Color.secondary.opacity(0.3),
                          style: StrokeStyle(lineWidth: dropping ? 2 : 1,
                                             dash: m.src == nil ? [6, 4] : [])))
        .animation(.easeInOut(duration: 0.15), value: dropping)
        .dropDestination(for: URL.self) { urls, _ in
            guard !urls.isEmpty else { return false }
            m.adopt(urls)
            return true
        } isTargeted: { dropping = $0 }
    }

    @ViewBuilder
    var advancedRows: some View {
        if m.mode == "mrc" {
            Picker(selection: $m.mrcPreset) {
                Text("彩色教材 / 杂志(彩色文字多)").tag("text")
                Text("漫画 / 绘本(黑字为主)").tag("comic")
            } label: {
                Label("材料类型", systemImage: "books.vertical")
            }
            .help("两类材料的最优参数差别很大。选好类型后下面三项会自动设成对应的推荐值。")

            Toggle(isOn: $m.mrcColorLayers) {
                Text("彩色文字也做分层")
                Text("漫画绘本适用;彩色文字密集的教材请关闭,否则彩字会出现啃噬状缺口")
            }

            LabeledContent {
                HStack {
                    Picker("", selection: $m.mrcMaskWidth) {
                        Text("原生(最锐)").tag(0)
                        Text("2600 px").tag(2600)
                        Text("2200 px(推荐)").tag(2200)
                        Text("1800 px").tag(1800)
                    }.labelsHidden().frame(width: 160)
                }
            } label: { Label("文字层分辨率", systemImage: "textformat.size") }
            .help("文字和线条的清晰度由这一层决定。2200px 已远高于普通彩色档,再高收益很小。")

            LabeledContent {
                HStack {
                    Picker("", selection: $m.mrcBgWidth) {
                        Text("500 px").tag(500)
                        Text("900 px").tag(900)
                        Text("1200 px").tag(1200)
                        Text("1500 px").tag(1500)
                        Text("1800 px").tag(1800)
                    }.labelsHidden().frame(width: 130)
                    Text("质量").foregroundStyle(.secondary)
                    Picker("", selection: $m.mrcBgQuality) {
                        Text("35").tag(35); Text("45").tag(45)
                        Text("50").tag(50); Text("60").tag(60)
                    }.labelsHidden().frame(width: 70)
                }
            } label: { Label("色彩层", systemImage: "paintpalette") }
            .help("色块的分辨率。因为文字已由上一层承担,这里可以压得很低。")
        } else if m.mode == "bw" {
            LabeledContent {
                HStack {
                    Slider(value: .init(get: { Double(m.threshold) },
                                        set: { m.threshold = Int($0) }), in: 40...70, step: 1)
                        .frame(width: 150)
                    Text("\(m.threshold)%").monospacedDigit().frame(width: 42, alignment: .leading)
                    if m.threshold != 55 && m.threshold != 58 {
                        Button("复位") { m.threshold = m.enhance ? 58 : 55 }
                            .buttonStyle(.link).font(.caption)
                    }
                }
            } label: {
                Label("二值化阈值", systemImage: "circle.righthalf.filled")
            }
            .help("多深的灰算字、多浅的灰算纸。调低更白净但细笔画可能变淡,调高笔画更实但易留纸黄。默认 55%。")

            Toggle(isOn: $m.enhance) {
                Text("提白 + 锐化")
                Text("阈值前去灰底、锐边缘,体积 +2%,墨淡的扫描提升明显")
            }
            Toggle(isOn: $m.supersample) {
                Text("边缘精细化")
                Text(m.imgCount >= BIG_BOOK
                     ? "放大 2 倍再二值化,台阶细一半,体积约 ×2 —— 这本有 \(m.imgCount) 张,建议关闭"
                     : "放大 2 倍再二值化,放大看时台阶细一半,体积约 ×2")
            }
        } else {
            LabeledContent {
                HStack {
                    Slider(value: .init(get: { Double(m.quality) },
                                        set: { m.quality = Int($0) }), in: 25...85, step: 1)
                        .frame(width: 150)
                    Text("q\(m.quality)").monospacedDigit()
                }
            } label: {
                Label("JPEG 质量", systemImage: "photo")
            }
            .help("分辨率给足时 q35–45 与 q65 肉眼无差,却省近四成体积。")
        }

        LabeledContent {
            TextField("如 3,7,10-12", text: $m.excludeText).frame(width: 170)
        } label: {
            Label("排除编号", systemImage: "minus.circle")
        }
        .help("填源图文件名里的编号,不是书上印的页码")

        LabeledContent {
            TextField("", text: $m.title).frame(width: 170)
        } label: {
            Label("PDF 标题", systemImage: "textformat")
        }
        .help("只写进 PDF 文档属性,不会改文件名")

        Toggle(isOn: $m.probe) {
            Text("先抽样试跑各档位")
            Text("只处理几页并导出样张,用来比清晰度和体积")
        }
    }

    var depWarning: some View {
        VStack(alignment: .leading, spacing: 4) {
            Label("缺少运行所需的工具", systemImage: "wrench.and.screwdriver.fill")
                .foregroundStyle(.orange).bold()
            ForEach(m.missing, id: \.self) {
                Text("• " + $0).font(.caption).foregroundStyle(.secondary)
            }
        }
    }
}

final class MergeModel: ObservableObject {
    @Published var files: [URL] = []
    @Published var dest: URL?
    @Published var a4 = true
    @Published var bookmarks = true
    @Published var running = false
    @Published var phase = ""
    @Published var log = ""
    @Published var doneMsg: String?
    @Published var failMsg: String?

    private var task: Process?
    private var buf = Data()

    var scriptPath: String? {
        Bundle.main.url(forResource: "merge_pdfs", withExtension: "py")?.path
    }
    var pythonPath: String { Tools.python }

    func info(_ u: URL) -> String {
        let attrs = try? FileManager.default.attributesOfItem(atPath: u.path)
        let size = (attrs?[.size] as? Int) ?? 0
        let pages = PDFDocument(url: u)?.pageCount ?? 0
        return String(format: "%d 页 · %.1f MB", pages, Double(size) / 1e6)
    }

    func pick() {
        let p = NSOpenPanel()
        p.allowedContentTypes = [.pdf]
        p.allowsMultipleSelection = true
        p.prompt = "添加"
        p.message = "选择要合并的 PDF(可多选;顺序之后可调)"
        if p.runModal() == .OK { add(p.urls) }
    }

    func add(_ urls: [URL]) {
        for u in urls where u.pathExtension.lowercased() == "pdf" {
            if !files.contains(u) { files.append(u) }
        }
        if dest == nil, let f = files.first {
            dest = f.deletingLastPathComponent().appendingPathComponent("合并.pdf")
        }
        doneMsg = nil; failMsg = nil
    }

    func move(_ i: Int, _ by: Int) {
        let j = i + by
        guard files.indices.contains(i), files.indices.contains(j) else { return }
        files.swapAt(i, j)
    }

    func chooseDest() {
        let p = NSSavePanel()
        p.allowedContentTypes = [.pdf]
        p.nameFieldStringValue = dest?.lastPathComponent ?? "合并.pdf"
        p.directoryURL = dest?.deletingLastPathComponent()
        if p.runModal() == .OK, let u = p.url { dest = u }
    }

    func start() {
        guard files.count >= 2, let d = dest, let sp = scriptPath, !running else { return }
        if files.contains(d) {
            failMsg = "输出文件不能是列表里的某个输入文件,请改输出文件名"
            return
        }
        if FileManager.default.fileExists(atPath: d.path) {
            let a = NSAlert()
            a.messageText = "「\(d.lastPathComponent)」已经存在"
            a.informativeText = "继续会覆盖它。"
            a.addButton(withTitle: "覆盖")
            a.addButton(withTitle: "取消")
            if a.runModal() != .alertFirstButtonReturn { return }
        }
        running = true; log = ""; doneMsg = nil; failMsg = nil; phase = "合并中…"
        var args = [sp, d.path] + files.map { $0.path } + ["--progress"]
        if a4 { args.append("--a4") }
        if bookmarks { args.append("--bookmarks") }
        args += ["--title", d.deletingPathExtension().lastPathComponent]

        let p = Process()
        p.executableURL = URL(fileURLWithPath: pythonPath)
        p.arguments = args
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe
        buf = Data()
        pipe.fileHandleForReading.readabilityHandler = { [weak self] h in
            let dd = h.availableData
            guard !dd.isEmpty, let self = self else { return }
            DispatchQueue.main.async { self.consume(dd) }
        }
        p.terminationHandler = { [weak self] proc in
            DispatchQueue.main.async {
                guard let self = self else { return }
                pipe.fileHandleForReading.readabilityHandler = nil
                self.running = false
                self.task = nil
                self.phase = ""
                if proc.terminationStatus != 0 && self.doneMsg == nil {
                    self.failMsg = "合并失败(见下方日志末尾)"
                }
            }
        }
        do { try p.run(); task = p } catch {
            running = false; failMsg = "无法启动:\(error.localizedDescription)"
        }
    }

    func cancel() { task?.terminate() }

    private func consume(_ d: Data) {
        buf.append(d)
        while let nl = buf.firstIndex(of: 0x0a) {
            let line = String(decoding: buf[buf.startIndex..<nl], as: UTF8.self)
            buf.removeSubrange(buf.startIndex...nl)
            if line.hasPrefix("@@") {
                let f = line.dropFirst(2).split(separator: " ").map(String.init)
                if f.first == "P", f.count >= 3 { phase = "搬运第 \(f[2]) 页…" }
                if f.first == "S" { phase = "写出文件(压缩对象流)…" }
                if f.first == "DONE", f.count >= 4, let b = Double(f[1]), let n = Double(f[2]) {
                    doneMsg = String(format: "%.0f 页,%.1f MB", n, b / 1e6)
                    dest = URL(fileURLWithPath: f[3...].joined(separator: " "))
                }
            } else {
                log += line + "\n"
                if log.count > 40000 { log = String(log.suffix(30000)) }
            }
        }
    }
}

/// 队列里的一行:名称 + 档位摘要 + 进度/状态 + 展开日志 + 取消/移除 + 完成后定位文件
struct StatusPill: View {
    let state: Job.State
    var color: Color {
        switch state {
        case .done: return .green
        case .failed: return .orange
        case .cancelled: return .secondary
        case .running: return .accentColor
        case .waiting: return .secondary
        }
    }
    var icon: String {
        switch state {
        case .done: return "checkmark.circle.fill"
        case .failed: return "exclamationmark.triangle.fill"
        case .cancelled: return "minus.circle"
        case .running: return "arrow.triangle.2.circlepath"
        case .waiting: return "clock"
        }
    }
    var body: some View {
        Label(state.rawValue, systemImage: icon)
            .font(.caption2).foregroundStyle(color)
            .padding(.horizontal, 7).padding(.vertical, 2)
            .background(Capsule().fill(color.opacity(0.14)))
    }
}

/// 队列里的一行:名称 + 状态胶囊 + 档位摘要 + 进度/耗时 + 展开日志 + 操作
struct JobRow: View {
    @ObservedObject var job: Job
    @ObservedObject var q: JobQueue
    @State private var showLog = false
    @State private var now = Date()      // 每秒刷新,驱动运行中的实时计时

    /// 运行中=已用时间(实时);结束后=总耗时(固定)
    var timeText: String {
        if job.state == .running, let t0 = job.startedAt {
            return "已用 " + fmtDuration(now.timeIntervalSince(t0))
        }
        return job.elapsed > 0 ? "用时 " + fmtDuration(job.elapsed) : ""
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 8) {
                Text(job.name).lineLimit(1).truncationMode(.middle).fontWeight(.medium)
                StatusPill(state: job.state)
                Spacer()
                if job.state == .done, !job.isProbe {
                    Button { NSWorkspace.shared.activateFileViewerSelecting([job.dest]) }
                        label: { Image(systemName: "magnifyingglass") }
                        .buttonStyle(.borderless).help("在访达中显示")
                }
                Button { showLog.toggle() }
                    label: { Image(systemName: showLog ? "chevron.up" : "text.alignleft") }
                    .buttonStyle(.borderless).help("日志")
                Button { q.remove(job) } label: { Image(systemName: "xmark.circle.fill") }
                    .buttonStyle(.borderless).foregroundStyle(.tertiary).help("移除")
            }

            Text(job.summary + (timeText.isEmpty ? "" : " · " + timeText))
                .font(.caption2).foregroundStyle(.secondary)

            if job.state == .running {
                ProgressView(value: job.progress)
                Text(job.phase).font(.caption2).foregroundStyle(.tertiary)
            } else if !job.result.isEmpty {
                Text(job.result).font(.caption2).foregroundStyle(.secondary)
            }

            if showLog {
                ScrollView {
                    Text(job.log.isEmpty ? "(暂无输出)" : job.log)
                        .font(.system(size: 10, design: .monospaced))
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .frame(height: 110)
                .background(RoundedRectangle(cornerRadius: 6).fill(Color.secondary.opacity(0.08)))
            }
        }
        .padding(.vertical, 3)
        .onReceive(Timer.publish(every: 1, on: .main, in: .common).autoconnect()) { t in
            if job.state == .running { now = t }
        }
    }
}

struct MergeView: View {
    @StateObject var m = MergeModel()
    @State private var dropping = false
    @State private var showLog = false

    var body: some View {
        Form {
            Section {
                if m.files.isEmpty {
                    HStack(spacing: 12) {
                        Image(systemName: "doc.on.doc")
                            .font(.system(size: 26)).foregroundStyle(.secondary)
                        VStack(alignment: .leading, spacing: 2) {
                            Text("把多个 PDF 拖到这里").foregroundStyle(.secondary)
                            Text("或点右侧「添加…」;顺序之后可调")
                                .font(.caption2).foregroundStyle(.tertiary)
                        }
                        Spacer()
                        Button("添加…") { m.pick() }
                    }
                    .padding(12)
                    .frame(maxWidth: .infinity)
                    .background(RoundedRectangle(cornerRadius: 10)
                        .fill(dropping ? Color.accentColor.opacity(0.15) : Color.secondary.opacity(0.08)))
                    .overlay(RoundedRectangle(cornerRadius: 10)
                        .strokeBorder(dropping ? Color.accentColor : Color.secondary.opacity(0.3),
                                      style: StrokeStyle(lineWidth: dropping ? 2 : 1, dash: [6, 4])))
                    .dropDestination(for: URL.self) { urls, _ in m.add(urls); return true }
                        isTargeted: { dropping = $0 }
                } else {
                    ForEach(Array(m.files.enumerated()), id: \.element) { i, u in
                        HStack(spacing: 8) {
                            Text("\(i + 1)").font(.caption).monospacedDigit()
                                .foregroundStyle(.secondary)
                                .frame(width: 22, height: 22)
                                .background(Circle().fill(Color.secondary.opacity(0.15)))
                            VStack(alignment: .leading, spacing: 1) {
                                Text(u.lastPathComponent).lineLimit(1).truncationMode(.middle)
                                Text(m.info(u)).font(.caption2).foregroundStyle(.secondary)
                            }
                            Spacer()
                            Button { m.move(i, -1) } label: { Image(systemName: "arrow.up") }
                                .disabled(i == 0).buttonStyle(.borderless)
                            Button { m.move(i, 1) } label: { Image(systemName: "arrow.down") }
                                .disabled(i == m.files.count - 1).buttonStyle(.borderless)
                            Button { m.files.remove(at: i) } label: {
                                Image(systemName: "xmark.circle.fill")
                            }.buttonStyle(.borderless).foregroundStyle(.tertiary)
                        }
                        .padding(.vertical, 2)
                    }
                    HStack {
                        Button("添加…") { m.pick() }
                        Button("清空") { m.files.removeAll(); m.doneMsg = nil }
                        Spacer()
                        Text("共 \(m.files.count) 个文件").font(.caption).foregroundStyle(.secondary)
                    }
                }
            } header: {
                Label("要合并的 PDF", systemImage: "doc.on.doc")
            } footer: {
                Text("按列表顺序合并,画质与压缩零损失(不重新光栅化)。")
                    .font(.caption2).foregroundStyle(.secondary)
            }

            Section {
                LabeledContent {
                    HStack(spacing: 6) {
                        TextField("", text: nameBinding($m.dest))
                            .frame(width: 170).disabled(m.dest == nil)
                        Text(".pdf").foregroundStyle(.secondary)
                        Button {
                            if let f = m.files.first {
                                m.dest = f.deletingLastPathComponent()
                                    .appendingPathComponent("合并.pdf")
                            }
                        } label: { Image(systemName: "arrow.uturn.backward") }
                            .help("还原默认名").disabled(m.files.isEmpty)
                        Button { m.chooseDest() } label: { Image(systemName: "folder") }
                            .help("换个保存位置").disabled(m.files.isEmpty)
                    }
                } label: {
                    Label("输出文件名", systemImage: "square.and.arrow.down")
                }
                Toggle(isOn: $m.a4) {
                    Text("统一为 A4")
                    Text("非 A4 的页等比缩放居中;已是 A4 的原样收录")
                }
                Toggle(isOn: $m.bookmarks) {
                    Text("为每个文件加书签")
                    Text("书签名 = 文件名,合完可在侧边栏直接跳转")
                }
            } header: {
                Label("输出设置", systemImage: "slider.horizontal.3")
            }

            Section {
                HStack(spacing: 10) {
                    Button {
                        m.start()
                    } label: {
                        Label(m.running ? "合并中…" : "开始合并", systemImage: "arrow.triangle.merge")
                    }
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut(.defaultAction)
                    .disabled(m.running || m.files.count < 2 || m.dest == nil)
                    if m.running { Button("取消") { m.cancel() } }
                    Spacer()
                    Button(showLog ? "隐藏日志" : "显示日志") { showLog.toggle() }
                        .buttonStyle(.link).font(.callout)
                }
                if m.running {
                    ProgressView().progressViewStyle(.linear)
                    Text(m.phase).font(.caption2).foregroundStyle(.secondary)
                }
                if let d = m.doneMsg {
                    HStack {
                        Label(d, systemImage: "checkmark.circle.fill").foregroundStyle(.green)
                        Spacer()
                        if let u = m.dest {
                            Button("在访达中显示") { NSWorkspace.shared.activateFileViewerSelecting([u]) }
                        }
                    }
                }
                if let f = m.failMsg {
                    Label(f, systemImage: "exclamationmark.triangle.fill").foregroundStyle(.orange)
                }
                if showLog {
                    ScrollView {
                        Text(m.log.isEmpty ? "(暂无输出)" : m.log)
                            .font(.system(size: 10, design: .monospaced))
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    .frame(height: 110)
                    .background(RoundedRectangle(cornerRadius: 6).fill(Color.secondary.opacity(0.08)))
                }
            }
        }
        .formStyle(.grouped)
        .frame(width: 620, height: 600)
    }
}

// MARK: - PDF 工具(体检 / 无损瘦身 / 加文字层 / MRC 重制)

/// 体检结果的一行。由 shrink_pdf.py --analyze --json 逐行吐出的 @@J 解析而来。
struct ProbeRow: Identifiable {
    let id = UUID()
    var path: String
    var name: String
    var size: Int
    var pages: Int
    var bilevel: Int
    var color: Int
    var hasText: Bool
    var est: Int
    var selected = true

    var kind: String {
        if bilevel > 0 && color == 0 { return "黑白" }
        if color > 0 && bilevel == 0 { return "彩色/灰度" }
        return bilevel > 0 ? "混合" : "无图像"
    }
    /// 无损瘦身能省多少;彩色页这条路不适用,返回 nil
    var saving: Double? {
        guard bilevel > 0, size > 0, est < size else { return nil }
        return 1 - Double(est) / Double(size)
    }
}

final class PdfToolsModel: ObservableObject {
    @Published var files: [URL] = []
    @Published var rows: [ProbeRow] = []
    @Published var mode = "shrink" { didSet { save() } }     // shrink / ocr / mrc
    @Published var outDir: URL?
    @Published var langs = "ko-KR,en-US,zh-Hans" { didSet { save() } }
    // MRC 是备用路线(用户看过样张后否掉过),参数照搬教材档
    @Published var mrcMask = 2600 { didSet { save() } }
    @Published var mrcBg = 1500 { didSet { save() } }
    @Published var mrcQuality = 50 { didSet { save() } }
    @Published var mrcColorLayers = false { didSet { save() } }
    @Published var mrcPages = ""

    @Published var probing = false
    @Published var running = false
    @Published var phase = ""
    @Published var progress = 0.0
    @Published var log = ""
    @Published var doneMsg: String?
    @Published var failMsg: String?

    private var task: Process?
    private var buf = Data()

    init() {
        let d = UserDefaults.standard
        if let m = d.string(forKey: "pdfMode") { mode = m }
        if let l = d.string(forKey: "pdfLangs") { langs = l }
        if d.object(forKey: "pdfMrcMask") != nil {
            mrcMask = d.integer(forKey: "pdfMrcMask")
            mrcBg = d.integer(forKey: "pdfMrcBg")
            mrcQuality = d.integer(forKey: "pdfMrcQuality")
            mrcColorLayers = d.bool(forKey: "pdfMrcColorLayers")
        }
    }

    private func save() {
        let d = UserDefaults.standard
        d.set(mode, forKey: "pdfMode")
        d.set(langs, forKey: "pdfLangs")
        d.set(mrcMask, forKey: "pdfMrcMask")
        d.set(mrcBg, forKey: "pdfMrcBg")
        d.set(mrcQuality, forKey: "pdfMrcQuality")
        d.set(mrcColorLayers, forKey: "pdfMrcColorLayers")
    }

    var script: String? {
        Bundle.main.url(forResource: mode == "ocr" ? "ocr_pdf" : "shrink_pdf",
                        withExtension: "py")?.path
    }
    var suffix: String { mode == "ocr" ? "_ocr" : (mode == "mrc" ? "_mrc" : "_slim") }

    var selectedFiles: [URL] {
        rows.isEmpty ? files
            : rows.filter { $0.selected }.map { URL(fileURLWithPath: $0.path) }
    }

    func pick() {
        let p = NSOpenPanel()
        p.allowedContentTypes = [.pdf]
        p.allowsMultipleSelection = true
        p.canChooseDirectories = true
        p.prompt = "添加"
        p.message = "选择 PDF 或包含 PDF 的文件夹"
        if p.runModal() == .OK { add(p.urls) }
    }

    func add(_ urls: [URL]) {
        let fm = FileManager.default
        for u in urls {
            var isDir: ObjCBool = false
            guard fm.fileExists(atPath: u.path, isDirectory: &isDir) else { continue }
            if isDir.boolValue {
                let inner = (try? fm.contentsOfDirectory(at: u, includingPropertiesForKeys: nil)) ?? []
                for f in inner where f.pathExtension.lowercased() == "pdf" {
                    if !files.contains(f) { files.append(f) }
                }
            } else if u.pathExtension.lowercased() == "pdf", !files.contains(u) {
                files.append(u)
            }
        }
        if outDir == nil, let f = files.first { outDir = f.deletingLastPathComponent() }
        rows = []; doneMsg = nil; failMsg = nil
    }

    func chooseOutDir() {
        let p = NSOpenPanel()
        p.canChooseFiles = false
        p.canChooseDirectories = true
        p.canCreateDirectories = true
        p.prompt = "选择"
        p.message = "成品放到哪个文件夹(原文件不会被改动)"
        if p.runModal() == .OK, let u = p.urls.first { outDir = u }
    }

    /// 体检:只读不写,抽样真压来估,不用经验系数
    func probe() {
        guard !files.isEmpty, !probing, !running,
              let sp = Bundle.main.url(forResource: "shrink_pdf", withExtension: "py")?.path
        else { return }
        probing = true; rows = []; log = ""; doneMsg = nil; failMsg = nil
        phase = "体检中…"; progress = 0
        launch(script: sp, args: files.map { $0.path } + ["--analyze", "--json", "--progress"]) {
            [weak self] ok in
            self?.probing = false
            self?.phase = ""
            if !ok { self?.failMsg = "体检失败(见日志)" }
        }
    }

    /// 返回将要写出的文件里已经存在的那些 —— 项目铁律:写文件前先查存在性再问
    func existingOutputs() -> [URL] {
        guard let d = outDir else { return [] }
        return selectedFiles.compactMap { f in
            let u = d.appendingPathComponent(
                f.deletingPathExtension().lastPathComponent + suffix + ".pdf")
            return FileManager.default.fileExists(atPath: u.path) ? u : nil
        }
    }

    func start() {
        guard !running, !probing, let sp = script, let d = outDir else { return }
        let inputs = selectedFiles
        guard !inputs.isEmpty else { failMsg = "没有选中任何文件"; return }

        let clash = existingOutputs()
        if !clash.isEmpty {
            let a = NSAlert()
            a.messageText = "有 \(clash.count) 个成品已经存在"
            a.informativeText = clash.prefix(5).map { $0.lastPathComponent }
                .joined(separator: "\n") + (clash.count > 5 ? "\n…" : "")
            a.addButton(withTitle: "覆盖")
            a.addButton(withTitle: "取消")
            if a.runModal() != .alertFirstButtonReturn { return }
        }

        running = true; log = ""; doneMsg = nil; failMsg = nil; progress = 0
        phase = "准备…"
        var args = inputs.map { $0.path } + ["--out-dir", d.path, "--force", "--progress"]
        if mode == "ocr" {
            args += ["--lang", langs]
        } else if mode == "mrc" {
            args += ["--mrc",
                     "--mrc-mask-width", String(mrcMask),
                     "--mrc-bg-width", String(mrcBg),
                     "--mrc-bg-quality", String(mrcQuality)]
            if mrcColorLayers { args.append("--mrc-color-layers") }
            let pg = mrcPages.trimmingCharacters(in: .whitespaces)
            if !pg.isEmpty { args += ["--pages", pg] }
        }
        launch(script: sp, args: args) { [weak self] ok in
            guard let self = self else { return }
            self.running = false
            self.phase = ""
            if ok {
                if self.doneMsg == nil { self.doneMsg = "完成" }
                NSSound.beep()
            } else {
                self.failMsg = "失败(见下方日志末尾)"
            }
        }
    }

    func cancel() { task?.terminate() }

    private func launch(script: String, args: [String], done: @escaping (Bool) -> Void) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: Tools.python)
        p.arguments = [script] + args
        var env = ProcessInfo.processInfo.environment
        Tools.apply(to: &env)
        env["PYTHONUNBUFFERED"] = "1"
        p.environment = env
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe
        buf = Data()
        pipe.fileHandleForReading.readabilityHandler = { [weak self] h in
            let d = h.availableData
            guard !d.isEmpty, let self = self else { return }
            DispatchQueue.main.async { self.consume(d) }
        }
        p.terminationHandler = { [weak self] proc in
            DispatchQueue.main.async {
                pipe.fileHandleForReading.readabilityHandler = nil
                self?.task = nil
                done(proc.terminationStatus == 0)
            }
        }
        do { try p.run(); task = p } catch {
            running = false; probing = false
            failMsg = "无法启动:\(error.localizedDescription)"
        }
    }

    private func consume(_ d: Data) {
        buf.append(d)
        while let nl = buf.firstIndex(of: 0x0a) {
            let line = String(decoding: buf[buf.startIndex..<nl], as: UTF8.self)
            buf.removeSubrange(buf.startIndex...nl)
            if line.hasPrefix("@@J") {                       // 体检结果:一行一个 JSON
                if let data = line.dropFirst(3).data(using: .utf8),
                   let o = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                    rows.append(ProbeRow(
                        path: o["path"] as? String ?? "",
                        name: o["name"] as? String ?? "?",
                        size: o["size"] as? Int ?? 0,
                        pages: o["pages"] as? Int ?? 0,
                        bilevel: o["bilevel"] as? Int ?? 0,
                        color: o["color"] as? Int ?? 0,
                        hasText: o["has_text"] as? Bool ?? false,
                        est: Int((o["est"] as? Double) ?? Double(o["size"] as? Int ?? 0))))
                }
            } else if line.hasPrefix("@@") {
                let f = line.dropFirst(2).split(separator: " ").map(String.init)
                if f.first == "S", f.count >= 2 { phase = f[1] + "…" }
                if f.first == "P", f.count >= 4, let a = Double(f[2]), let b = Double(f[3]), b > 0 {
                    phase = "\(f[1]) \(f[2])/\(f[3])"
                    progress = a / b
                }
                if f.first == "DONE" { progress = 1 }
            } else {
                log += line + "\n"
                if log.count > 40000 { log = String(log.suffix(30000)) }
            }
        }
    }
}

struct PdfToolsView: View {
    @StateObject var m = PdfToolsModel()
    @State private var dropping = false
    @State private var showLog = false

    private func mb(_ n: Int) -> String { String(format: "%.1f MB", Double(n) / 1048576) }

    var body: some View {
        Form {
            Section {
                if m.files.isEmpty {
                    HStack(spacing: 12) {
                        Image(systemName: "doc.badge.gearshape")
                            .font(.system(size: 26)).foregroundStyle(.secondary)
                        VStack(alignment: .leading, spacing: 2) {
                            Text("把 PDF 或整个文件夹拖到这里").foregroundStyle(.secondary)
                            Text("原文件永远不会被改动,成品另存为新文件")
                                .font(.caption2).foregroundStyle(.tertiary)
                        }
                        Spacer()
                        Button("添加…") { m.pick() }
                    }
                    .padding(12)
                    .frame(maxWidth: .infinity)
                    .background(RoundedRectangle(cornerRadius: 10)
                        .fill(dropping ? Color.accentColor.opacity(0.15) : Color.secondary.opacity(0.08)))
                    .overlay(RoundedRectangle(cornerRadius: 10)
                        .strokeBorder(dropping ? Color.accentColor : Color.secondary.opacity(0.3),
                                      style: StrokeStyle(lineWidth: dropping ? 2 : 1, dash: [6, 4])))
                    .dropDestination(for: URL.self) { urls, _ in m.add(urls); return true }
                        isTargeted: { dropping = $0 }
                } else if m.rows.isEmpty {
                    ForEach(m.files, id: \.self) { u in
                        Text(u.lastPathComponent).lineLimit(1).truncationMode(.middle)
                            .font(.callout)
                    }
                    HStack {
                        Button("添加…") { m.pick() }
                        Button("清空") { m.files.removeAll(); m.rows = [] }
                        Spacer()
                        Text("\(m.files.count) 个文件").font(.caption).foregroundStyle(.secondary)
                    }
                } else {
                    pdfTable
                }
            } header: {
                Label("要处理的 PDF", systemImage: "doc.badge.gearshape")
            } footer: {
                if !m.files.isEmpty && m.rows.isEmpty {
                    Text("先点「体检」看看每本什么构成、能省多少 —— 抽样实压估算,不是拍脑袋的系数。")
                        .font(.caption2).foregroundStyle(.secondary)
                }
            }

            Section {
                Picker("处理方式", selection: $m.mode) {
                    Text("无损瘦身(黑白页换 JBIG2 编码)").tag("shrink")
                    Text("加可搜索的文字层(不改外观)").tag("ocr")
                    Text("MRC 重制(彩色页,有画质取舍)").tag("mrc")
                }
                .pickerStyle(.radioGroup)

                if m.mode == "shrink" {
                    Text("位图一个比特都不变,放大到任何倍数都和原件相同 —— 没有画质取舍可谈。"
                         + "实测四本自扫韩语书 354.8 MB → 148.6 MB。彩色页这条路帮不上忙。")
                        .font(.caption2).foregroundStyle(.secondary)
                } else if m.mode == "ocr" {
                    LabeledContent("识别语言") {
                        TextField("", text: $m.langs).frame(width: 220)
                            .help("逗号分隔,如 ko-KR,en-US,zh-Hans。用 macOS 自带的 Vision,完全离线")
                    }
                    Text("页面外观逐像素不变,每页只增加约 1 KB。读者看到的永远是原图,"
                         + "文字层只服务于 Cmd+F —— 所以偶有错字也看不见。")
                        .font(.caption2).foregroundStyle(.secondary)
                } else {
                    mrcOptions
                }
            } header: {
                Label("要做什么", systemImage: "slider.horizontal.3")
            }

            Section {
                LabeledContent("成品放到") {
                    HStack(spacing: 6) {
                        Text(m.outDir?.lastPathComponent ?? "(未选)")
                            .foregroundStyle(m.outDir == nil ? .tertiary : .primary)
                            .lineLimit(1).truncationMode(.middle)
                        Button("换位置…") { m.chooseOutDir() }
                    }
                }
                Text("文件名会自动加 「\(m.suffix)」 后缀,原文件保持不动。")
                    .font(.caption2).foregroundStyle(.secondary)
            } header: {
                Label("输出", systemImage: "folder")
            }

            Section {
                HStack(spacing: 10) {
                    Button("体检") { m.probe() }
                        .disabled(m.files.isEmpty || m.probing || m.running)
                    Button(m.mode == "shrink" ? "开始瘦身"
                           : (m.mode == "ocr" ? "开始加文字层" : "开始重制")) { m.start() }
                        .keyboardShortcut(.defaultAction)
                        .disabled(m.files.isEmpty || m.running || m.probing || m.outDir == nil)
                    if m.running || m.probing {
                        Button("取消") { m.cancel() }
                        ProgressView(value: m.progress).frame(width: 120)
                    }
                    Text(m.phase).font(.caption).foregroundStyle(.secondary)
                    Spacer()
                }
                if let d = m.doneMsg {
                    Label(d, systemImage: "checkmark.circle.fill").foregroundStyle(.green)
                }
                if let f = m.failMsg {
                    Label(f, systemImage: "exclamationmark.triangle.fill").foregroundStyle(.orange)
                }
                DisclosureGroup("日志", isExpanded: $showLog) {
                    ScrollView {
                        Text(m.log).font(.system(size: 11, design: .monospaced))
                            .frame(maxWidth: .infinity, alignment: .leading).textSelection(.enabled)
                    }.frame(height: 150)
                }
            }
        }
        .formStyle(.grouped)
        .dropDestination(for: URL.self) { urls, _ in m.add(urls); return true }
    }

    private var pdfTable: some View {
        VStack(spacing: 4) {
            ForEach($m.rows) { $r in
                HStack(spacing: 8) {
                    Toggle("", isOn: $r.selected).labelsHidden()
                    VStack(alignment: .leading, spacing: 1) {
                        Text(r.name).lineLimit(1).truncationMode(.middle).font(.callout)
                        HStack(spacing: 6) {
                            Text("\(r.pages) 页").font(.caption2).foregroundStyle(.secondary)
                            Text(r.kind).font(.caption2).foregroundStyle(.secondary)
                            if r.hasText {
                                Text("有文字层").font(.caption2).foregroundStyle(.blue)
                            }
                        }
                    }
                    Spacer()
                    if let s = r.saving {
                        Text("\(mb(r.size)) → \(mb(r.est))")
                            .font(.caption).monospacedDigit()
                        Text(String(format: "省 %.0f%%", s * 100))
                            .font(.caption2).foregroundStyle(.green)
                            .frame(width: 46, alignment: .trailing)
                    } else {
                        Text(mb(r.size)).font(.caption).monospacedDigit()
                        Text(r.color > 0 ? "彩色" : "—")
                            .font(.caption2).foregroundStyle(.tertiary)
                            .frame(width: 46, alignment: .trailing)
                    }
                }
                .padding(.vertical, 1)
            }
            let sel = m.rows.filter { $0.selected }
            let now = sel.reduce(0) { $0 + $1.size }
            let est = sel.reduce(0) { $0 + $1.est }
            Divider()
            HStack {
                Button("全选") { for i in m.rows.indices { m.rows[i].selected = true } }
                    .buttonStyle(.borderless).font(.caption)
                Button("只选黑白") {
                    for i in m.rows.indices { m.rows[i].selected = m.rows[i].saving != nil }
                }.buttonStyle(.borderless).font(.caption)
                Button("重新体检") { m.probe() }.buttonStyle(.borderless).font(.caption)
                Spacer()
                if now > 0 {
                    Text("选中 \(sel.count) 个:\(mb(now)) → \(mb(est))")
                        .font(.caption).monospacedDigit()
                    if now > est {
                        Text(String(format: "省 %.0f%%", (1 - Double(est) / Double(now)) * 100))
                            .font(.caption).foregroundStyle(.green)
                    }
                }
            }
        }
    }

    /// MRC 是备用路线:用户 2026-08-03 看过样张后否掉过(「还是觉得原图好看」),
    /// 所以这里把话说明白,并默认建议先用「样张页」试几页,别一上来跑全书。
    private var mrcOptions: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("这条路会改变画质。先填「样张页」试几页,满意了再跑全书",
                  systemImage: "exclamationmark.triangle.fill")
                .font(.caption).foregroundStyle(.orange)
            LabeledContent("样张页") {
                TextField("如 41-46", text: $m.mrcPages).frame(width: 190)
                    .help("只重制这些页,用来看样张。留空 = 整本都重制")
            }
            LabeledContent("文字层宽") {
                HStack(spacing: 4) {
                    TextField("", value: $m.mrcMask, format: .number).frame(width: 70)
                    Text("px").foregroundStyle(.secondary)
                }
            }.help("文字和线条走这个分辨率的黑白蒙版 —— MRC 省体积的关键就在这:它可以比彩色底图高好几倍")
            LabeledContent("底图宽") {
                HStack(spacing: 4) {
                    TextField("", value: $m.mrcBg, format: .number).frame(width: 70)
                    Text("px").foregroundStyle(.secondary)
                }
            }.help("色块/插图走这个分辨率的彩色 JPEG。糊的就是它")
            LabeledContent("底图质量") {
                TextField("", value: $m.mrcQuality, format: .number).frame(width: 70)
            }
            Toggle("彩色文字分层", isOn: $m.mrcColorLayers)
                .help("漫画/绘本适合开;彩色文字密集的教材开了会「像被虫啃」——同一笔画被劈在锐利蒙版层和模糊底图之间")
        }
    }
}

struct RootView: View {
    @State private var tab = 0
    var body: some View {
        TabView(selection: $tab) {
            ConvertView()
                .tabItem { Label("图片转 PDF", systemImage: "photo.on.rectangle.angled") }.tag(0)
            PdfToolsView()
                .tabItem { Label("PDF 工具", systemImage: "doc.badge.gearshape") }.tag(2)
            MergeView()
                .tabItem { Label("合并 PDF", systemImage: "doc.on.doc") }.tag(1)
        }
        .padding(.top, 6)
    }
}

@main
struct ScanToPDFApp: App {
    var body: some Scene {
        WindowGroup("ScanPress") { RootView() }
            .windowResizability(.contentSize)
    }
}
