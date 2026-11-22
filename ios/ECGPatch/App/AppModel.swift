// AppModel.swift — アプリ全体の組立て（Python 版 A01 AppInit）と画面への橋渡し

import AudioToolbox
import Combine
import SwiftUI
import UIKit

final class AppModel: ObservableObject {
    let store: FileStore
    let clock: Clock = SystemClock()
    let settings: Settings
    let trace: Trace
    let monitor: BleMonitor
    let recorder: BleTraceRecorder
    let transport: CoreBluetoothTransport
    let mgr: SessionManager

    @Published var tick = 0
    @Published var loaded: LoadedRecord?
    @Published var showAlert = false
    @Published var askEmulator: String?
    @Published var askSetupTimeout = false
    @Published var banner: String?
    private var timer: Timer?
    private var flushAt = 0.0

    init() {
        store = FileStore.documents()
        trace = Trace(store: store)
        settings = Settings(store: store).load()
        trace.verbose = settings.verboseTrace
        monitor = BleMonitor(clock: clock)
        recorder = BleTraceRecorder(store: store, clock: clock, settings: settings)
        transport = CoreBluetoothTransport()
        transport.clock = clock
        transport.observers = [monitor, recorder]
        mgr = SessionManager(transport: transport, clock: clock, store: store, settings: settings, trace: trace,
                             monitor: monitor, recorder: recorder)
        trace.w("START", APP_VERSION, UIDevice.current.model, UIDevice.current.systemVersion)
        if let e = settings.loadError { trace.w("SETTINGS", "broken, use defaults:", e) }
        if settings.bleTrace { recorder.start() }
        trace.w("BLE-1", "start")
        mgr.start()
        UIApplication.shared.isIdleTimerDisabled = true      // 受信中に画面が消えないように
        timer = Timer.scheduledTimer(withTimeInterval: 0.2, repeats: true) { [weak self] _ in self?.onTick() }
    }

    private func onTick() {
        mgr.tick()
        let now = clock.now()
        if now - flushAt >= 0.5 {
            flushAt = now
            recorder.flush()
        }
        // SessionManager からの合図を画面に渡す
        if mgr.ui.showAlert {
            mgr.ui.showAlert = false
            showAlert = true
        }
        if mgr.ui.alertSound {
            mgr.ui.alertSound = false
            AudioServicesPlaySystemSound(1005)
            UINotificationFeedbackGenerator().notificationOccurred(.warning)
            showBanner("異常通知を受信しました")
        }
        if let n = mgr.ui.askEmulator {
            mgr.ui.askEmulator = nil
            askEmulator = n
        }
        if mgr.ui.setupTimeoutAsk {
            mgr.ui.setupTimeoutAsk = false
            askSetupTimeout = true
        }
        if mgr.ui.setupDone {
            mgr.ui.setupDone = false
            showBanner("装着確認が完了しました")
        }
        if let s = mgr.ui.lastSaved {
            mgr.ui.lastSaved = nil
            if s.kind == "setup30s" || s.kind == "partial" { load(s.csv, source: "ble") }
        }
        tick &+= 1
    }

    func showBanner(_ s: String) {
        banner = s
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) { [weak self] in
            if self?.banner == s { self?.banner = nil }
        }
    }

    func load(_ url: URL, source: String = "file") {
        do {
            var r = try loadECGFile(url)
            r.source = source
            loaded = r
        } catch {
            showBanner("読み込めません: \(error)")
        }
    }

    func acceptEmulator() {
        settings.acceptEmulator = true
        settings.save()
        trace.w("SETTINGS", "accept_emulator=True")
        mgr.status.add("模擬器を受け入れます（設定を ON にしました）")
    }

    func saveSettings() {
        settings.save()
        trace.verbose = settings.verboseTrace
    }

    func closeAlert() {
        showAlert = false
        if !mgr.inSession { mgr.alert = nil }
    }

    func viewAlertWave() {
        if let s = mgr.alert?.saved {
            load(s.event ?? s.csv, source: "ble")
        }
        showAlert = false
        if !mgr.inSession { mgr.alert = nil }
        showBanner("iPhone を横向きにしてください")
    }

    /// 保存済みの ECG ファイル（新しい順）
    func savedFiles() -> [URL] {
        let ecg = store.url("ECG")
        guard let days = try? FileManager.default.contentsOfDirectory(at: ecg, includingPropertiesForKeys: nil) else { return [] }
        var out: [URL] = []
        for d in days {
            let files = (try? FileManager.default.contentsOfDirectory(at: d, includingPropertiesForKeys: nil)) ?? []
            out += files.filter { $0.pathExtension == "csv" && !$0.lastPathComponent.hasSuffix("_meta.csv") }
        }
        return out.sorted { $0.lastPathComponent > $1.lastPathComponent }
    }
}
