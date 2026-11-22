// ContentView.swift — 縦向き（SC2・SC3：一覧と下部の接続状態）／横向き（SC4：波形）

import SwiftUI

enum Pal {
    static func color(_ name: String) -> Color {
        switch name {
        case "green": return Color(red: 0.18, green: 0.62, blue: 0.31)
        case "red": return Color(red: 0.85, green: 0.19, blue: 0.15)
        case "blue": return Color(red: 0.12, green: 0.37, blue: 0.66)
        case "orange": return Color(red: 0.88, green: 0.48, blue: 0.0)
        case "gray": return Color(red: 0.60, green: 0.63, blue: 0.65)
        default: return Color.primary
        }
    }
}

struct ContentView: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        GeometryReader { geo in
            Group {
                if geo.size.width > geo.size.height {
                    LandscapeView()
                } else {
                    PortraitView()
                }
            }
        }
        .overlay(alignment: .top) {
            if let b = model.banner {
                Text(b)
                    .font(.headline)
                    .padding(.horizontal, 16).padding(.vertical, 10)
                    .background(.ultraThinMaterial, in: Capsule())
                    .padding(.top, 8)
            }
        }
        .fullScreenCover(isPresented: $model.showAlert) {
            AlertView().environmentObject(model)
        }
        .alert("パッチ模擬器を検出しました", isPresented: Binding(get: { model.askEmulator != nil },
                                                          set: { if !$0 { model.askEmulator = nil } })) {
            Button("受け入れる") { model.acceptEmulator() }
            Button("キャンセル", role: .cancel) {}
        } message: {
            Text("\(model.askEmulator ?? "") は模擬器です。試験のために受け入れますか？\n（設定「模擬器を受け入れる」を ON にします）")
        }
        .alert("電極 NG", isPresented: $model.askSetupTimeout) {
            Button("続ける") { model.mgr.setupContinue() }
            Button("中止", role: .destructive) { model.mgr.userStop() }
        } message: {
            Text("電極正常の 30 秒がそろいません。貼付を確認してください。")
        }
    }
}

// MARK: - 縦向き

struct PortraitView: View {
    @EnvironmentObject var model: AppModel
    @State private var showSettings = false
    @State private var showMonitor = false
    @State private var showFiles = false
    @State private var connectPeer: String?
    @State private var connectLabel = ""
    @State private var patientID = ""

    var body: some View {
        let mgr = model.mgr
        let _ = model.tick
        VStack(spacing: 0) {
            header
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    dataCard
                    if mgr.state == .scanning {
                        patchList
                    }
                }
                .padding(12)
            }
            StatusPanelView(onLongPress: { showMonitor = true })
        }
        .sheet(isPresented: $showSettings) { SettingsView().environmentObject(model) }
        .sheet(isPresented: $showMonitor) { MonitorView().environmentObject(model) }
        .sheet(isPresented: $showFiles) { FilesView().environmentObject(model) }
        .alert("\(connectLabel) に接続", isPresented: Binding(get: { connectPeer != nil }, set: { if !$0 { connectPeer = nil } })) {
            TextField("患者 ID（任意、16 文字まで）", text: $patientID)
                .textInputAutocapitalization(.never)
            Button("接続") {
                if let p = connectPeer {
                    let pid = String(patientID.trimmingCharacters(in: .whitespaces).prefix(16))
                    model.trace.w("UI", "connect", connectLabel)
                    model.loaded = nil
                    mgr.connectTo(p, patientID: pid.isEmpty ? nil : pid)
                }
                connectPeer = nil
            }
            Button("キャンセル", role: .cancel) { connectPeer = nil }
        } message: {
            Text("パッチのラベルと照合してください。")
        }
    }

    private var header: some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text("ECG パッチ").font(.title3.bold())
                Text(APP_VERSION).font(.caption2).foregroundStyle(.secondary)
            }
            Spacer()
            Button { showFiles = true } label: { Image(systemName: "folder") }
            Button { showMonitor = true } label: { Image(systemName: "antenna.radiowaves.left.and.right") }
                .padding(.horizontal, 8)
            Button { showSettings = true } label: { Image(systemName: "gearshape") }
        }
        .font(.title3)
        .padding(.horizontal, 16).padding(.vertical, 8)
    }

    @ViewBuilder private var dataCard: some View {
        let mgr = model.mgr
        if mgr.inSession {
            VStack(alignment: .leading, spacing: 6) {
                Text(mgr.session is EventSession ? "● 異常通知を受信中" : "装着確認の受信中")
                    .font(.headline).foregroundStyle(mgr.session is EventSession ? Pal.color("red") : Pal.color("blue"))
                let prev = mgr.previewSamples
                if !prev.isEmpty {
                    WaveformView(ecg: prev, fs: P.fs, meta: [], detectIndex: -1, compact: true)
                        .frame(height: 140)
                } else {
                    Text("波形を待っています…").foregroundStyle(.secondary).frame(maxWidth: .infinity, minHeight: 80)
                }
                if mgr.session != nil {
                    Button(role: .destructive) { mgr.userStop() } label: { Text("中止").frame(maxWidth: .infinity) }
                        .buttonStyle(.bordered)
                }
                if mgr.alert != nil && !model.showAlert {
                    Button("異常通知の画面を開く") { model.showAlert = true }
                }
            }
            .padding(12)
            .background(RoundedRectangle(cornerRadius: 12).fill(Color(.secondarySystemBackground)))
        } else if let r = model.loaded {
            VStack(alignment: .leading, spacing: 6) {
                HStack {
                    Text(r.hasEvent ? "● データ検出（異常通知）" : "データ検出")
                        .font(.headline).foregroundStyle(r.hasEvent ? Pal.color("red") : Pal.color("green"))
                    Spacer()
                    Button { model.loaded = nil } label: { Image(systemName: "xmark.circle") }
                }
                Text(r.name).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                WaveformView(ecg: r.ecg, fs: r.fs, meta: r.meta, detectIndex: r.detectIndex, compact: true)
                    .frame(height: 140)
                chips(r)
                Text("iPhone を横向きにすると拡大表示します").font(.caption2).foregroundStyle(.secondary)
            }
            .padding(12)
            .background(RoundedRectangle(cornerRadius: 12).fill(r.hasEvent ? Color.red.opacity(0.06) : Color.green.opacity(0.06)))
        } else {
            VStack(spacing: 6) {
                Text("データ未検出").font(.headline).foregroundStyle(.secondary)
                Text("パッチの「接続」で装着確認を始めます").font(.caption).foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, minHeight: 120)
            .background(RoundedRectangle(cornerRadius: 12).fill(Color(.secondarySystemBackground)))
        }
    }

    private func chips(_ r: LoadedRecord) -> some View {
        let lo = !r.leadoffRanges.isEmpty
        return HStack(spacing: 8) {
            chip(lo ? "はがれ有" : "正常", "電極状態", lo ? "red" : "green")
            chip(r.hasEvent ? r.verdict : "—", "パッチの判定", r.hasEvent ? "orange" : "gray")
            chip(r.patchHR.map { "\($0)" } ?? "—", "心拍/分（パッチ）", r.patchHR == nil ? "gray" : "ink")
        }
    }

    private func chip(_ v: String, _ label: String, _ col: String) -> some View {
        VStack(spacing: 2) {
            Text(v).font(.subheadline.bold()).foregroundStyle(Pal.color(col)).lineLimit(1).minimumScaleFactor(0.6)
            Text(label).font(.caption2).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
        .padding(6)
        .background(RoundedRectangle(cornerRadius: 8).stroke(Color.gray.opacity(0.3)))
    }

    private var patchList: some View {
        let rows = model.mgr.patchRows()
        return VStack(alignment: .leading, spacing: 8) {
            Text("近くの ECG パッチ").font(.subheadline.bold())
            if rows.isEmpty {
                Text("パッチを探しています…").font(.caption).foregroundStyle(.secondary)
            }
            ForEach(rows) { row in
                HStack {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(row.label).font(.headline)
                        Text(row.kind).font(.subheadline).foregroundStyle(Pal.color(row.color))
                        Text(row.sub).font(.caption2).foregroundStyle(.secondary)
                    }
                    Spacer()
                    if !row.button.isEmpty {
                        Button(row.button) {
                            let info = model.mgr.scanner.table[row.id]
                            if info?.event == EV.setup {
                                patientID = ""
                                connectLabel = row.label
                                connectPeer = row.id
                            } else {
                                model.trace.w("UI", "connect", row.label)
                                model.mgr.connectTo(row.id, patientID: nil)
                            }
                        }
                        .buttonStyle(.borderedProminent)
                        .tint(row.color == "red" ? Pal.color("red") : Pal.color("blue"))
                    }
                }
                .padding(10)
                .background(RoundedRectangle(cornerRadius: 10).stroke(Color.gray.opacity(0.3)))
            }
        }
    }
}

// MARK: - 下部の接続状態（13.9 節）

struct StatusPanelView: View {
    @EnvironmentObject var model: AppModel
    var onLongPress: () -> Void

    var body: some View {
        let s = model.mgr.status
        let _ = model.tick
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Circle().fill(Pal.color(s.dot)).frame(width: 12, height: 12)
                Text(s.head).font(.subheadline.bold()).foregroundStyle(Pal.color(s.headColor)).lineLimit(2)
                Spacer()
            }
            if let p = s.percent {
                ProgressView(value: Double(p), total: 100).tint(Pal.color(s.barColor))
            }
            if !s.progress.isEmpty { Text(s.progress).font(.caption2).foregroundStyle(.secondary) }
            if !s.warn.isEmpty { Text(s.warn).font(.caption.bold()).foregroundStyle(Pal.color("red")) }
            if let cells = s.cells {
                HStack(spacing: 1) {
                    ForEach(0..<cells.count, id: \.self) { i in
                        Rectangle()
                            .fill(cells[i] == nil ? Color.gray.opacity(0.3) : (cells[i] == 0 ? Pal.color("green") : Pal.color("red")))
                            .frame(height: 10)
                    }
                }
            }
            Text(s.detail).font(.caption).foregroundStyle(.secondary).lineLimit(2)
            ForEach(Array(s.log.enumerated()), id: \.offset) { _, line in
                Text(line).font(.caption2.monospaced()).lineLimit(1)
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(.systemGray6))
        .onLongPressGesture(minimumDuration: 1.0) { onLongPress() }      // 1 秒長押しで BLE モニタ（SC7）
    }
}

// MARK: - 横向き（SC4）

struct LandscapeView: View {
    @EnvironmentObject var model: AppModel
    @State private var zoom = false

    var body: some View {
        let _ = model.tick
        if let r = model.loaded {
            HStack(spacing: 0) {
                VStack(alignment: .leading, spacing: 4) {
                    HStack {
                        Text(r.hasEvent ? "● データ検出（異常通知）" : "データ検出")
                            .font(.headline).foregroundStyle(r.hasEvent ? Pal.color("red") : Pal.color("green"))
                        Text(r.leadoffRanges.isEmpty ? "電極 正常" : "電極 はがれ有")
                            .font(.caption.bold()).foregroundStyle(.white).padding(.horizontal, 8).padding(.vertical, 3)
                            .background(Capsule().fill(r.leadoffRanges.isEmpty ? Pal.color("green") : Pal.color("red")))
                        Spacer()
                        Toggle("拡大", isOn: $zoom).toggleStyle(.button)
                    }
                    GeometryReader { g in
                        let secs = Double(r.ecg.count) / Double(max(1, r.fs))
                        let w = zoom ? max(g.size.width, CGFloat(secs) * 80) : g.size.width
                        ScrollView(.horizontal) {
                            WaveformView(ecg: r.ecg, fs: r.fs, meta: r.meta, detectIndex: r.detectIndex, compact: false)
                                .frame(width: w, height: g.size.height)
                        }
                    }
                }
                .padding(8)
                VStack(alignment: .leading, spacing: 8) {           // r10（本体 No.29）：アプリは判定しない
                    Text("パッチの判定").font(.caption).foregroundStyle(.secondary)
                    Text(r.verdict).font(.headline).foregroundStyle(r.hasEvent ? Pal.color("red") : .secondary)
                    Text("心拍 \(r.patchHR.map { String($0) } ?? "—")/分（パッチ）").font(.subheadline)
                    ForEach(r.notes, id: \.self) { n in Text("• " + n).font(.caption).foregroundStyle(Pal.color("red")) }
                    Spacer()
                    Text(r.name).font(.caption2).foregroundStyle(.secondary).lineLimit(3)
                }
                .padding(10)
                .frame(width: 220)
                .background(Color(.systemGray6))
            }
        } else {
            VStack {
                Text("表示する波形がありません").foregroundStyle(.secondary)
                Text("縦向きに戻して、受信するかファイルを開いてください").font(.caption).foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }
}
