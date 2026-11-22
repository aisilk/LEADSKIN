// OtherViews.swift — 異常通知（SC5）・設定（SC6）・BLE モニタ（SC7）・保存ファイル

import SwiftUI

// MARK: - SC5 異常通知

struct AlertView: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        let _ = model.tick
        let a = model.mgr.alert
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text("● 異常通知").font(.title2.bold()).foregroundStyle(Pal.color("red"))
                Spacer()
                Text(a?.device ?? "").font(.subheadline).foregroundStyle(.secondary)
            }
            Text(a?.title ?? "").font(.title3.bold())
            HStack {
                Text(a?.time ?? "").font(.subheadline)
                Spacer()
                Text(a?.counter ?? "").font(.caption).foregroundStyle(.secondary)
            }
            if let chips = a?.chips, !chips.isEmpty {
                HStack(spacing: 8) {
                    ForEach(Array(chips.enumerated()), id: \.offset) { _, c in
                        VStack(spacing: 2) {
                            Text(c.0).font(.headline)
                            Text(c.1).font(.caption2).foregroundStyle(.secondary)
                        }
                        .frame(maxWidth: .infinity)
                        .padding(8)
                        .background(RoundedRectangle(cornerRadius: 8).fill(Color.red.opacity(0.07)))
                    }
                }
            }
            VStack(alignment: .leading, spacing: 10) {
                ForEach(Array((a?.steps ?? []).enumerated()), id: \.offset) { i, s in
                    HStack(spacing: 10) {
                        stepIcon(s.0)
                        Text("\(["①", "②", "③", "④", "⑤"][min(i, 4)]) \(s.1)")
                            .foregroundStyle(s.0 == "warn" ? Pal.color("orange") : (s.0 == "todo" ? .secondary : .primary))
                    }
                }
            }
            .padding(12)
            .background(RoundedRectangle(cornerRadius: 12).fill(Color(.secondarySystemBackground)))
            if let e = a?.error, !e.isEmpty {
                Text(e).font(.subheadline.bold()).foregroundStyle(Pal.color("orange"))
            }
            Spacer()
            HStack(spacing: 12) {
                Button { model.viewAlertWave() } label: { Text("波形を見る").frame(maxWidth: .infinity) }
                    .buttonStyle(.borderedProminent)
                    .disabled(!(a?.canView ?? false))
                Button { model.closeAlert() } label: { Text("閉じる").frame(maxWidth: .infinity) }
                    .buttonStyle(.bordered)
            }
            Text("「閉じる」を押しても受信は続きます").font(.caption2).foregroundStyle(.secondary)
        }
        .padding(20)
    }

    @ViewBuilder private func stepIcon(_ s: String) -> some View {
        switch s {
        case "done": Image(systemName: "checkmark.circle.fill").foregroundStyle(Pal.color("green"))
        case "active": ProgressView()
        case "warn": Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(Pal.color("orange"))
        case "error": Image(systemName: "xmark.octagon.fill").foregroundStyle(Pal.color("red"))
        default: Image(systemName: "circle").foregroundStyle(.secondary)
        }
    }
}

// MARK: - SC6 設定

struct SettingsView: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var refresh = 0

    var body: some View {
        let s = model.settings
        let _ = refresh
        NavigationStack {
            Form {
                Section("通知") {
                    Toggle("登録済みパッチの異常通知に自動接続", isOn: bind(\.autoConnectAlert))
                    Toggle("未通知ありに自動接続", isOn: bind(\.autoFetchUnnotified))
                }
                Section("記録") {
                    Toggle("_pages.bin を保存", isOn: bind(\.savePagesBin))
                    Toggle("詳細な動作記録", isOn: bind(\.verboseTrace))
                    Toggle("BLE トレース（ble_trace_*.jsonl）", isOn: bind(\.bleTrace))
                    Toggle("開発用の表示（RSSI・接続間隔）", isOn: bind(\.devDisplay))
                }
                Section("開発・試験") {
                    Toggle("模擬器を受け入れる", isOn: bind(\.acceptEmulator))
                    Stepper("一覧の保持 \(Int(s.listExpireS)) 秒", value: Binding(get: { s.listExpireS },
                                                                          set: { s.listExpireS = $0; model.saveSettings(); refresh += 1 }),
                            in: 5...60, step: 5)
                }
                Section("登録済みパッチ") {
                    if s.registered.isEmpty { Text("なし").foregroundStyle(.secondary) }
                    ForEach(s.registered, id: \.device_id) { r in
                        VStack(alignment: .leading) {
                            Text("ECGP-" + r.device_id)
                            Text("装着確認 \(r.setup_at)（\(r.setup_result)）・最終 \(r.last_seen)").font(.caption2).foregroundStyle(.secondary)
                        }
                    }
                    if !s.registered.isEmpty {
                        Button("登録を消去する", role: .destructive) {
                            s.registered = []
                            model.saveSettings()
                            model.trace.w("SETTINGS", "registered cleared")
                            refresh += 1
                        }
                    }
                }
                Section("この版") {
                    Text(APP_VERSION)
                    Text("ファイルは「ファイル」App の「この iPhone 内 > ECGPatch」に保存されます").font(.caption).foregroundStyle(.secondary)
                }
            }
            .navigationTitle("設定")
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("閉じる") { dismiss() } } }
        }
    }

    private func bind(_ kp: ReferenceWritableKeyPath<Settings, Bool>) -> Binding<Bool> {
        Binding(get: { model.settings[keyPath: kp] },
                set: { model.settings[keyPath: kp] = $0; model.saveSettings(); refresh += 1 })
    }
}

// MARK: - SC7 BLE モニタ

struct MonitorView: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var tab = 0
    private let tabs = ["概要", "スキャン", "接続", "通信量", "ログ"]

    var body: some View {
        let _ = model.tick
        NavigationStack {
            VStack(spacing: 8) {
                Picker("", selection: $tab) {
                    ForEach(0..<tabs.count, id: \.self) { Text(tabs[$0]).tag($0) }
                }
                .pickerStyle(.segmented)
                ScrollView {
                    Text(text())
                        .font(.caption.monospaced())
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .textSelection(.enabled)
                }
            }
            .padding(.horizontal, 12)
            .navigationTitle("BLE モニタ")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    let urls = model.recorder.exportURLs()
                    if let u = urls.first {
                        ShareLink("書出し", item: u)
                    }
                }
                ToolbarItem(placement: .confirmationAction) { Button("閉じる") { dismiss() } }
            }
        }
    }

    private func text() -> String {
        let m = model.monitor
        let mgr = model.mgr
        var L: [String] = []
        switch tab {
        case 0:
            L.append("版          : \(APP_VERSION)")
            L.append("Bluetooth   : \(m.centralState)")
            L.append("状態        : \(mgr.state.rawValue)")
            if let c = mgr.conn {
                L.append("パッチ      : \(c.info.label)\(c.info.emulator ? "（模擬器）" : "")")
                L.append("ATT MTU     : \(c.mtu.map { String($0) } ?? "—")")
                L.append("RSSI        : \(c.rssi.last.map { String($0) } ?? "—") dBm")
                if let st = c.status {
                    L.append("状態 S\(st.state)  電極 \(st.electrode == 0 ? "OK" : "NG")  未通知 \(st.unnotified)  時刻 \(st.timeSet != 0 ? "設定済み" : "未設定")")
                    L.append("ページ      : 最新 \(st.latestPageSeq)  基準 \(st.basePageSeq)  FW \(String(format: "%04X", st.fwVer))")
                }
                if let d = c.dbg {
                    L.append(String(format: "Debug Info  : 接続間隔 %.1f ms  PHY %@  MTU %d  送信枠 %d  BUSY %d  RSSI %d",
                                    d.intervalMs, d.phyText, d.attMtu, d.txFree, d.notifyBusy, d.rssi))
                } else {
                    L.append("Debug Info  : —")
                }
            }
            L.append(String(format: "受信速度    : %.1f KB/s", m.rateKBs))
            L.append("接続時間    : \(m.connectMs.map { "\($0) ms" } ?? "—")")
            L.append("結果コード  : " + m.results.sorted { $0.key < $1.key }.map { "\(Res.name($0.key))=\($0.value)" }.joined(separator: ", "))
            if !m.cmdMs.isEmpty {
                L.append("応答時間    : 最小 \(m.cmdMs.min()!)  平均 \(m.cmdMs.reduce(0, +) / m.cmdMs.count)  最大 \(m.cmdMs.max()!) ms")
            }
            L.append("断片の欠落  : \(m.fragGaps)")
            L.append("")
            L.append("警告:")
            for w in m.warnings.suffix(8) { L.append("  \(w.2)") }
            L.append("切断:")
            for d in m.disconnects.suffix(5) { L.append("  \(d.1)  \(d.2)") }
        case 1:
            for (peer, s) in m.scan.sorted(by: { $0.value.last > $1.value.last }) {
                L.append("\(s.name)  RSSI \(s.rssi)  間隔 \(m.advIntervalMs(peer).map { "\($0) ms" } ?? "—")  "
                         + String(format: "event %02X  status %02X", s.event, s.status))
            }
        case 2:
            for line in mgr.status.console.suffix(80) { L.append(line) }
        case 3:
            L.append("受信 \(m.bytesRx) バイト  送信 \(m.bytesTx) バイト")
            for (sec, b) in m.rate.suffix(30) { L.append("\(sec)  \(String(repeating: "█", count: min(40, b / 1000)))  \(b)") }
        default:
            for d in model.recorder.ring.suffix(200) {
                L.append([str(d["wall"]), str(d["k"]), str(d["ev"]), str(d["ch"]), String(str(d["hex"]).prefix(48))]
                    .joined(separator: " "))
            }
        }
        return L.joined(separator: "\n")
    }

    private func str(_ x: Any?) -> String { x.map { "\($0)" } ?? "" }
}

// MARK: - 保存ファイル

struct FilesView: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            List {
                let files = model.savedFiles()
                if files.isEmpty { Text("保存したファイルはありません").foregroundStyle(.secondary) }
                ForEach(files, id: \.self) { u in
                    Button {
                        model.load(u)
                        dismiss()
                    } label: {
                        VStack(alignment: .leading) {
                            Text(u.lastPathComponent).font(.subheadline)
                            Text(u.deletingLastPathComponent().lastPathComponent).font(.caption2).foregroundStyle(.secondary)
                        }
                    }
                }
            }
            .navigationTitle("保存ファイル")
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("閉じる") { dismiss() } } }
        }
    }
}
