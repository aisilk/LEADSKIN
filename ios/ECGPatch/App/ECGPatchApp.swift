// ECGPatchApp.swift — ECG パッチ受信アプリ（Swift 版。Python／Pythonista 版 r10 の移植）

import SwiftUI

@main
struct ECGPatchApp: App {
    @StateObject private var model = AppModel()
    @Environment(\.scenePhase) private var phase

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(model)
        }
        .onChange(of: phase) { _, p in
            if p == .background { model.recorder.flush() }
        }
    }
}
