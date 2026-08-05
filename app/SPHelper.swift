// SPHelper —— 给 Python 引擎用的 macOS 系统能力桥接工具(纯命令行,无界面)。
//
// 为什么要它:Vision 的文字识别和 CoreGraphics 的 PDF 渲染都只能从 Swift 调,
// 而本项目的编排逻辑全在 Python。于是把这两件事做成一个小可执行文件,
// 由 build_app.py 编译进 Contents/Helpers/,scan2pdf.py / ocr_pdf.py 按需调用。
//
// 用法:
//   sphelper ocr <图片> [语言,逗号分隔]     → 每行一条 JSON:{text,x,y,w,h,conf}
//                                              坐标已归一化,原点左下(Vision 的约定)
//   sphelper render <PDF> <页码> <宽> <高> <输出.png>   → 按指定像素尺寸精确渲染
//   sphelper langs                          → 本机 Vision 支持的识别语言
//
// OCR 的语言处理沿用作者另一个取词工具的实测经验:**不能把拉丁语和 CJK 塞进同一个
// recognitionLanguages 列表**,那样 Vision 对 CJK 会直接返回空;正确做法是先开
// automaticallyDetectsLanguage 让它自己选模型,落空了再逐语言重试取置信度最高的。

import CoreGraphics
import Foundation
import ImageIO
import UniformTypeIdentifiers
import Vision

func fail(_ msg: String) -> Never {
    FileHandle.standardError.write((msg + "\n").data(using: .utf8)!)
    exit(1)
}

// MARK: - OCR

struct Pass {
    var lines: [[String: Any]]
    var confidence: Float
}

func recognize(_ image: CGImage, languages: [String], auto: Bool) -> Pass {
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.usesLanguageCorrection = true
    req.automaticallyDetectsLanguage = auto
    if !languages.isEmpty, let ok = try? req.supportedRecognitionLanguages() {
        let want = languages.filter { ok.contains($0) }
        if !want.isEmpty { req.recognitionLanguages = want }
    }
    do {
        try VNImageRequestHandler(cgImage: image, options: [:]).perform([req])
    } catch {
        return Pass(lines: [], confidence: 0)
    }
    // Vision 不保证返回顺序,按「先上后下、再左到右」排成阅读顺序
    let obs = (req.results ?? []).sorted { a, b in
        abs(a.boundingBox.midY - b.boundingBox.midY) > 0.02
            ? a.boundingBox.midY > b.boundingBox.midY
            : a.boundingBox.minX < b.boundingBox.minX
    }
    var out: [[String: Any]] = []
    var confs: [Float] = []
    for o in obs {
        guard let c = o.topCandidates(1).first, !c.string.isEmpty else { continue }
        let b = o.boundingBox
        out.append(["text": c.string, "x": b.minX, "y": b.minY,
                    "w": b.width, "h": b.height, "conf": c.confidence])
        confs.append(c.confidence)
    }
    let mean = confs.isEmpty ? 0 : confs.reduce(0, +) / Float(confs.count)
    return Pass(lines: out, confidence: mean)
}

func cmdOCR(_ args: [String]) {
    guard args.count >= 1 else { fail("用法: sphelper ocr <图片> [语言]") }
    let langs = args.count > 1 && !args[1].isEmpty
        ? args[1].split(separator: ",").map(String.init) : []
    guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: args[0]) as CFURL, nil),
          let img = CGImageSourceCreateImageAtIndex(src, 0, nil)
    else { fail("读不了图片: \(args[0])") }

    var pass = recognize(img, languages: langs, auto: true)
    if pass.lines.isEmpty {                       // 自动识别落空,逐语言重试取最高置信度
        var best: Pass?
        for l in langs {
            let p = recognize(img, languages: [l], auto: false)
            if !p.lines.isEmpty, best == nil || p.confidence > best!.confidence { best = p }
        }
        pass = best ?? recognize(img, languages: [], auto: false)
    }
    for line in pass.lines {
        if let d = try? JSONSerialization.data(withJSONObject: line),
           let s = String(data: d, encoding: .utf8) {
            print(s)
        }
    }
}

// MARK: - 渲染 + 识别一步到位

/// 把 PDF 某页渲染成位图后直接喂给 Vision,**不落地成 PNG**。
/// 分成 render+ocr 两个进程时,每页要多付一次 PNG 编码、一次写盘读盘、一次进程启动
/// (实测单页 1 MB 的 PNG);合起来省掉这些,千页书省下的就不是零头了。
func cmdOCRPage(_ args: [String]) {
    guard args.count >= 3 else { fail("用法: sphelper ocrpage <PDF> <页码> <宽> [语言]") }
    guard let doc = CGPDFDocument(URL(fileURLWithPath: args[0]) as CFURL),
          let page = doc.page(at: Int(args[1]) ?? 1) else { fail("打不开 PDF 或页码越界") }
    guard let W = Int(args[2]), W > 0 else { fail("宽度不合法") }
    let langs = args.count > 3 && !args[3].isEmpty
        ? args[3].split(separator: ",").map(String.init) : []

    let box = page.getBoxRect(.mediaBox)
    let H = max(1, Int((Double(W) * box.height / box.width).rounded()))
    guard let ctx = CGContext(data: nil, width: W, height: H, bitsPerComponent: 8,
                              bytesPerRow: W * 4, space: CGColorSpaceCreateDeviceRGB(),
                              bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)
    else { fail("建不了画布") }
    ctx.setFillColor(gray: 1, alpha: 1)
    ctx.fill(CGRect(x: 0, y: 0, width: W, height: H))
    ctx.scaleBy(x: CGFloat(W) / box.width, y: CGFloat(H) / box.height)
    ctx.drawPDFPage(page)
    guard let img = ctx.makeImage() else { fail("取不到位图") }

    var pass = recognize(img, languages: langs, auto: true)
    if pass.lines.isEmpty {
        var best: Pass?
        for l in langs {
            let p = recognize(img, languages: [l], auto: false)
            if !p.lines.isEmpty, best == nil || p.confidence > best!.confidence { best = p }
        }
        pass = best ?? recognize(img, languages: [], auto: false)
    }
    for line in pass.lines {
        if let d = try? JSONSerialization.data(withJSONObject: line),
           let s = String(data: d, encoding: .utf8) { print(s) }
    }
}

// MARK: - 渲染(供无损重压后的逐像素校验用)

func cmdRender(_ args: [String]) {
    guard args.count >= 5 else {
        fail("用法: sphelper render <PDF> <页码> <宽> <高> <输出.png> [rgb]")
    }
    guard let doc = CGPDFDocument(URL(fileURLWithPath: args[0]) as CFURL),
          let page = doc.page(at: Int(args[1]) ?? 1) else { fail("打不开 PDF 或页码越界") }
    guard let W = Int(args[2]), let H = Int(args[3]), W > 0, H > 0 else { fail("尺寸不合法") }
    // 默认灰度(逐像素校验用,省内存);看画质样张时给 rgb
    let rgb = args.count > 5 && args[5].lowercased() == "rgb"
    let ctx: CGContext? = rgb
        ? CGContext(data: nil, width: W, height: H, bitsPerComponent: 8, bytesPerRow: W * 4,
                    space: CGColorSpaceCreateDeviceRGB(),
                    bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)
        : CGContext(data: nil, width: W, height: H, bitsPerComponent: 8, bytesPerRow: W,
                    space: CGColorSpaceCreateDeviceGray(),
                    bitmapInfo: CGImageAlphaInfo.none.rawValue)
    guard let ctx else { fail("建不了画布") }
    ctx.setFillColor(gray: 1, alpha: 1)
    ctx.fill(CGRect(x: 0, y: 0, width: W, height: H))
    if !rgb {
        ctx.interpolationQuality = .none        // 逐像素校验:不要任何插值/抗锯齿
        ctx.setShouldAntialias(false)
    }
    let box = page.getBoxRect(.mediaBox)
    ctx.scaleBy(x: CGFloat(W) / box.width, y: CGFloat(H) / box.height)
    ctx.drawPDFPage(page)
    guard let img = ctx.makeImage(),
          let dst = CGImageDestinationCreateWithURL(URL(fileURLWithPath: args[4]) as CFURL,
                                                    UTType.png.identifier as CFString, 1, nil)
    else { fail("写不出 PNG") }
    CGImageDestinationAddImage(dst, img, nil)
    if !CGImageDestinationFinalize(dst) { fail("PNG 写入失败") }
}

// MARK: -

let argv = Array(CommandLine.arguments.dropFirst())
guard let cmd = argv.first else { fail("用法: sphelper ocr|render|langs …") }
switch cmd {
case "ocr":     cmdOCR(Array(argv.dropFirst()))
case "ocrpage": cmdOCRPage(Array(argv.dropFirst()))
case "render":  cmdRender(Array(argv.dropFirst()))
case "langs":
    let r = VNRecognizeTextRequest()
    r.recognitionLevel = .accurate
    print(((try? r.supportedRecognitionLanguages()) ?? []).joined(separator: " "))
default: fail("未知子命令: \(cmd)")
}
