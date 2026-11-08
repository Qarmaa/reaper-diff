// Иконка Reaper Diff: дорожки с серыми клипами и цветными различиями (зелёный — добавлен, жёлтый — изменён, красный — удалён).
// swiftc make_diff_icon.swift -o /tmp/mk && /tmp/mk out.png
import AppKit

let cs = CGColorSpaceCreateDeviceRGB()
let ctx = CGContext(data: nil, width: 1024, height: 1024, bitsPerComponent: 8, bytesPerRow: 0, space: cs,
                    bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
func col(_ r: CGFloat, _ g: CGFloat, _ b: CGFloat, _ a: CGFloat = 1) -> CGColor { CGColor(colorSpace: cs, components: [r/255, g/255, b/255, a])! }

let body = CGRect(x: 100, y: 100, width: 824, height: 824)
let bodyPath = CGPath(roundedRect: body, cornerWidth: 185, cornerHeight: 185, transform: nil)

ctx.saveGState()
ctx.setShadow(offset: CGSize(width: 0, height: -14), blur: 28, color: col(0, 0, 0, 0.45))
ctx.addPath(bodyPath); ctx.setFillColor(col(20, 30, 30)); ctx.fillPath()
ctx.restoreGState()

ctx.saveGState()
ctx.addPath(bodyPath); ctx.clip()
let bg = CGGradient(colorsSpace: cs, colors: [col(44, 58, 58), col(16, 22, 24)] as CFArray, locations: [0, 1])!
ctx.drawLinearGradient(bg, start: CGPoint(x: 512, y: 924), end: CGPoint(x: 512, y: 100), options: [])

enum K { case same, added, changed, removed }
let lanes: [(CGFloat, [(CGFloat, CGFloat, K)])] = [
    (618, [(170, 400, .same), (420, 600, .same), (630, 860, .added)]),
    (453, [(170, 330, .same), (360, 640, .changed), (670, 860, .same)]),
    (288, [(170, 450, .removed), (480, 700, .same), (730, 860, .same)]),
]
let laneH: CGFloat = 125
for (li, lane) in lanes.enumerated() {
    let lr = CGRect(x: 150, y: lane.0 - 8, width: 744, height: laneH + 16)
    ctx.setFillColor(col(255, 255, 255, 0.045))
    ctx.addPath(CGPath(roundedRect: lr, cornerWidth: 26, cornerHeight: 26, transform: nil)); ctx.fillPath()
    for (ci, c) in lane.1.enumerated() {
        let r = CGRect(x: c.0, y: lane.0, width: c.1 - c.0, height: laneH)
        let a: (CGFloat, CGFloat, CGFloat)
        switch c.2 {
        case .same: a = (112, 128, 134)
        case .added: a = (63, 185, 80)
        case .changed: a = (240, 170, 20)
        case .removed: a = (229, 72, 77)
        }
        let b = (a.0 - 55, a.1 - 50, a.2 - 45)
        let cp = CGPath(roundedRect: r, cornerWidth: 20, cornerHeight: 20, transform: nil)
        ctx.saveGState()
        ctx.addPath(cp); ctx.clip()
        let g = CGGradient(colorsSpace: cs, colors: [col(a.0, a.1, a.2), col(b.0, b.1, b.2)] as CFArray, locations: [0, 1])!
        ctx.drawLinearGradient(g, start: CGPoint(x: 0, y: r.maxY), end: CGPoint(x: 0, y: r.minY), options: [])
        ctx.setStrokeColor(col(255, 255, 255, c.2 == .same ? 0.35 : 0.6)); ctx.setLineWidth(5); ctx.setLineCap(.round)
        var x = r.minX + 18
        var k = li * 7 + ci * 3
        while x < r.maxX - 14 {
            let kd = Double(k)
            let w1: Double = abs(sin(kd * 1.7)) * 0.7
            let w2: Double = abs(sin(kd * 0.53)) * 0.3
            let h = CGFloat(10.0 + (w1 + w2) * 40.0)
            ctx.move(to: CGPoint(x: x, y: r.midY - h)); ctx.addLine(to: CGPoint(x: x, y: r.midY + h)); ctx.strokePath()
            x += 17; k += 1
        }
        ctx.restoreGState()
        ctx.setStrokeColor(col(255, 255, 255, 0.4)); ctx.setLineWidth(3)
        if c.2 == .removed { ctx.setLineDash(phase: 0, lengths: [14, 10]) } else { ctx.setLineDash(phase: 0, lengths: []) }
        ctx.addPath(cp); ctx.strokePath()
        ctx.setLineDash(phase: 0, lengths: [])
    }
}

// знаки +, ~, − в углах цветных клипов
func badge(_ cx: CGFloat, _ cy: CGFloat, _ c: (CGFloat, CGFloat, CGFloat), _ draw: (CGContext, CGFloat, CGFloat) -> Void) {
    ctx.saveGState()
    ctx.setShadow(offset: CGSize(width: 0, height: -5), blur: 12, color: col(0, 0, 0, 0.55))
    ctx.setFillColor(col(255, 255, 255, 0.97))
    ctx.fillEllipse(in: CGRect(x: cx - 46, y: cy - 46, width: 92, height: 92))
    ctx.restoreGState()
    ctx.setStrokeColor(col(c.0 * 0.8, c.1 * 0.8, c.2 * 0.8)); ctx.setLineWidth(14); ctx.setLineCap(.round)
    draw(ctx, cx, cy)
}
badge(860, 745, (63, 185, 80)) { c, x, y in c.move(to: CGPoint(x: x - 22, y: y)); c.addLine(to: CGPoint(x: x + 22, y: y)); c.move(to: CGPoint(x: x, y: y - 22)); c.addLine(to: CGPoint(x: x, y: y + 22)); c.strokePath() }
badge(500, 580, (240, 170, 20)) { c, x, y in
    c.move(to: CGPoint(x: x - 24, y: y - 4)); c.addCurve(to: CGPoint(x: x, y: y + 2), control1: CGPoint(x: x - 14, y: y + 22), control2: CGPoint(x: x - 8, y: y + 20))
    c.addCurve(to: CGPoint(x: x + 24, y: y + 4), control1: CGPoint(x: x + 8, y: y - 16), control2: CGPoint(x: x + 14, y: y - 22)); c.strokePath() }
badge(310, 415, (229, 72, 77)) { c, x, y in c.move(to: CGPoint(x: x - 22, y: y)); c.addLine(to: CGPoint(x: x + 22, y: y)); c.strokePath() }
ctx.restoreGState()

let rep = NSBitmapImageRep(cgImage: ctx.makeImage()!)
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
