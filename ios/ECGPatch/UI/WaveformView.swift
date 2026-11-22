// WaveformView.swift — 心電図の描画（格子・波形・電極はがれ・不整脈の帯・検知位置）

import SwiftUI

struct WaveformView: View {
    let ecg: [Int16]
    let fs: Int
    let meta: [MetaRow]
    let detectIndex: Int
    var compact: Bool

    var body: some View {
        Canvas { ctx, size in
            let n = ecg.count
            guard n > 1 else { return }
            let w = size.width, h = size.height
            let pps = w / CGFloat(n)                            // 1 サンプルあたりの幅
            // 背景（方眼紙）
            ctx.fill(Path(CGRect(origin: .zero, size: size)), with: .color(Color(red: 1.0, green: 0.97, blue: 0.97)))
            let small = CGFloat(fs) * 0.04 * pps, big = small * 5
            if !compact && small >= 3 {
                var p = Path()
                var x: CGFloat = 0
                while x <= w { p.move(to: CGPoint(x: x, y: 0)); p.addLine(to: CGPoint(x: x, y: h)); x += small }
                var y: CGFloat = 0
                while y <= h { p.move(to: CGPoint(x: 0, y: y)); p.addLine(to: CGPoint(x: w, y: y)); y += small }
                ctx.stroke(p, with: .color(Color(red: 0.96, green: 0.79, blue: 0.79)), lineWidth: 0.5)
            }
            if big >= 6 {
                var p = Path()
                var x: CGFloat = 0
                while x <= w { p.move(to: CGPoint(x: x, y: 0)); p.addLine(to: CGPoint(x: x, y: h)); x += big }
                var y: CGFloat = 0
                while y <= h { p.move(to: CGPoint(x: 0, y: y)); p.addLine(to: CGPoint(x: w, y: y)); y += big }
                ctx.stroke(p, with: .color(Color(red: 0.90, green: 0.56, blue: 0.56)), lineWidth: 0.8)
            }
            // 電極はがれ（パッチの記録）・不整脈の帯
            for (k, row) in meta.enumerated() {
                let x0 = CGFloat(k * P.cycle) * pps, x1 = CGFloat((k + 1) * P.cycle) * pps
                if row.electrode != 0 {
                    ctx.fill(Path(CGRect(x: x0, y: 0, width: x1 - x0, height: h)), with: .color(Color.gray.opacity(0.35)))
                }
                if row.arr != 0 {
                    ctx.fill(Path(CGRect(x: x0, y: 0, width: x1 - x0, height: 4)), with: .color(Pal.color("red")))
                }
            }
            // 縮尺：電極正常の部分の 1〜99% 点で決める
            var vals = ecg.enumerated().filter { i, _ in
                meta.isEmpty || meta.count <= i / P.cycle || meta[i / P.cycle].electrode == 0
            }.map { Int($0.1) }
            if vals.count < 10 { vals = ecg.map { Int($0) } }
            vals.sort()
            let lo = Double(vals[vals.count / 100]), hi = Double(vals[vals.count * 99 / 100])
            let mid = (lo + hi) / 2
            let span = max(hi - lo, 200) * 1.3
            func yOf(_ v: Int16) -> CGFloat { CGFloat(0.5 - (Double(v) - mid) / span) * h }
            // 波形（1 ピクセルごとの最小・最大で間引く）
            var path = Path()
            let cols = max(1, Int(w))
            if n > cols * 2 {
                for c in 0..<cols {
                    let a = c * n / cols, b = max(a + 1, (c + 1) * n / cols)
                    var mn = Int16.max, mx = Int16.min
                    for i in a..<min(b, n) { mn = min(mn, ecg[i]); mx = max(mx, ecg[i]) }
                    let x = CGFloat(c)
                    if c == 0 { path.move(to: CGPoint(x: x, y: yOf(mx))) } else { path.addLine(to: CGPoint(x: x, y: yOf(mx))) }
                    path.addLine(to: CGPoint(x: x, y: yOf(mn)))
                }
            } else {
                for i in 0..<n {
                    let pt = CGPoint(x: CGFloat(i) * pps, y: yOf(ecg[i]))
                    if i == 0 { path.move(to: pt) } else { path.addLine(to: pt) }
                }
            }
            ctx.stroke(path, with: .color(Color(red: 0.10, green: 0.14, blue: 0.20)), lineWidth: compact ? 1 : 1.2)
            // 検知位置（異常通知）
            if detectIndex >= 0 && detectIndex < n {
                let x = CGFloat(detectIndex) * pps
                var p = Path()
                p.move(to: CGPoint(x: x, y: 0)); p.addLine(to: CGPoint(x: x, y: h))
                ctx.stroke(p, with: .color(Pal.color("blue")), style: StrokeStyle(lineWidth: 1.5, dash: [4, 3]))
            }
            // 時間軸
            if !compact {
                let secs = Double(n) / Double(fs)
                let step = secs <= 10 ? 1.0 : (secs <= 30 ? 2.0 : 5.0)
                var s = 0.0
                while s <= secs + 0.01 {
                    ctx.draw(Text("\(Int(s))s").font(.system(size: 10)).foregroundColor(.gray),
                             at: CGPoint(x: CGFloat(s * Double(fs)) * pps + 8, y: h - 8))
                    s += step
                }
            }
        }
    }
}
