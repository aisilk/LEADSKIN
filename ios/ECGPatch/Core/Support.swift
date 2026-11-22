// Support.swift — 時刻・保存先・動作記録・設定（Python 版の Clock・DocumentsStore・Trace・Settings に対応）

import Foundation

// MARK: - 時刻

protocol Clock: AnyObject {
    func now() -> Double            // 単調増加の秒
    func localDate() -> Date        // iPhone のローカル時刻
}

final class SystemClock: Clock {
    func now() -> Double { ProcessInfo.processInfo.systemUptime }
    func localDate() -> Date { Date() }
}

/// 試験用：advance(秒) で進める
final class FakeClock: Clock {
    var t = 0.0
    var base: Date
    init(base: Date) { self.base = base }
    func advance(_ s: Double) { t += s }
    func now() -> Double { t }
    func localDate() -> Date { base.addingTimeInterval(t) }
}

enum DF {
    private static var cache: [String: DateFormatter] = [:]
    static func fmt(_ pattern: String) -> DateFormatter {
        if let f = cache[pattern] { return f }
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.calendar = Calendar(identifier: .gregorian)
        f.timeZone = TimeZone.current
        f.dateFormat = pattern
        cache[pattern] = f
        return f
    }
    static func s(_ d: Date, _ pattern: String = "yyyy-MM-dd HH:mm:ss") -> String { fmt(pattern).string(from: d) }
    static func microsecond(_ d: Date) -> Int {
        let ns = Calendar.current.component(.nanosecond, from: d)
        return ns / 1000
    }
    /// 西暦・月・日・時・分・秒（ローカル）から Date。不正な日付は nil
    static func date(_ ts: [Int]) -> Date? {
        var c = DateComponents()
        c.year = ts[0]; c.month = ts[1]; c.day = ts[2]; c.hour = ts[3]; c.minute = ts[4]; c.second = ts[5]
        let cal = Calendar(identifier: .gregorian)
        c.calendar = cal
        c.timeZone = TimeZone.current
        guard c.isValidDate(in: cal) else { return nil }
        return cal.date(from: c)
    }
}

// MARK: - 保存先（Documents。ファイル App の「この iPhone 内 > ECG Patch」に見える）

struct StorageError: Error, CustomStringConvertible {
    let message: String
    var description: String { message }
}

final class FileStore {
    let root: URL
    var failNext = false            // 試験用：書込みの異常

    init(root: URL) { self.root = root }

    static func documents() -> FileStore {
        FileStore(root: FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0])
    }

    func url(_ rel: String) -> URL { root.appendingPathComponent(rel) }
    func exists(_ rel: String) -> Bool { FileManager.default.fileExists(atPath: url(rel).path) }

    @discardableResult
    func writeAtomic(_ rel: String, _ data: Data) throws -> URL {
        let u = url(rel)
        do {
            try FileManager.default.createDirectory(at: u.deletingLastPathComponent(), withIntermediateDirectories: true)
            if failNext {
                failNext = false
                throw StorageError(message: "injected write failure")
            }
            try data.write(to: u, options: .atomic)      // 一時名で書いてから改名（16.3 節）
        } catch let e as StorageError {
            throw e
        } catch {
            throw StorageError(message: error.localizedDescription)
        }
        return u
    }

    func appendLine(_ rel: String, _ line: String) {
        appendText(rel, line + "\n")
    }

    func appendText(_ rel: String, _ text: String) {
        let u = url(rel)
        let fm = FileManager.default
        try? fm.createDirectory(at: u.deletingLastPathComponent(), withIntermediateDirectories: true)
        guard let d = text.data(using: .utf8) else { return }
        if !fm.fileExists(atPath: u.path) {
            fm.createFile(atPath: u.path, contents: d)
            return
        }
        if let h = try? FileHandle(forWritingTo: u) {
            defer { try? h.close() }
            _ = try? h.seekToEnd()
            try? h.write(contentsOf: d)
        }
    }
}

// MARK: - 動作記録（ecg_patch_trace.txt。前回分は _prev に移す）

final class Trace {
    private(set) var lines: [String] = []
    var verbose = true
    private var handle: FileHandle?

    init(store: FileStore?, name: String = "ecg_patch_trace.txt") {
        guard let store = store else { return }
        let fm = FileManager.default
        let u = store.url(name)
        let prev = store.url(name.replacingOccurrences(of: ".txt", with: "_prev.txt"))
        if fm.fileExists(atPath: u.path) {
            try? fm.removeItem(at: prev)
            try? fm.moveItem(at: u, to: prev)
        }
        fm.createFile(atPath: u.path, contents: nil)
        handle = try? FileHandle(forWritingTo: u)
    }

    func w(_ parts: Any...) {
        let line = DF.s(Date(), "HH:mm:ss.SSS") + " " + parts.map { "\($0)" }.joined(separator: " ")
        lines.append(line)
        if lines.count > 400 { lines.removeFirst(lines.count - 400) }
        if let h = handle, let d = (line + "\n").data(using: .utf8) {
            try? h.write(contentsOf: d)
        }
    }

    func v(_ parts: Any...) {         // 詳細記録（verbose_trace が OFF なら書かない）
        guard verbose else { return }
        let line = parts.map { "\($0)" }.joined(separator: " ")
        w(line)
    }

    func close() { try? handle?.close(); handle = nil }
}

// MARK: - 設定（ecg_patch_settings.json。Python 版と同じキー）

struct Registered: Codable, Equatable {
    var device_id: String
    var setup_at: String
    var setup_result: String
    var last_seen: String
}

final class Settings {
    var registered: [Registered] = []
    var autoConnectAlert = true
    var autoFetchUnnotified = true
    var savePagesBin = true
    var verboseTrace = true
    var listExpireS = 10.0
    var devDisplay = false
    var bleTrace = true
    var acceptEmulator = false
    var loadError: String?

    private let store: FileStore?
    private let rel = "ecg_patch_settings.json"

    init(store: FileStore?) { self.store = store }

    @discardableResult
    func load() -> Settings {
        guard let store = store else { return self }
        guard store.exists(rel), let d = try? Data(contentsOf: store.url(rel)) else {
            save()
            return self
        }
        guard let o = (try? JSONSerialization.jsonObject(with: d)) as? [String: Any] else {
            loadError = "broken settings file"            // A01-02：壊れていたら既定値で起動
            return self
        }
        if let r = o["registered"] as? [[String: Any]] {
            registered = r.map {
                Registered(device_id: $0["device_id"] as? String ?? "", setup_at: $0["setup_at"] as? String ?? "",
                           setup_result: $0["setup_result"] as? String ?? "ok", last_seen: $0["last_seen"] as? String ?? "")
            }
        }
        autoConnectAlert = o["auto_connect_alert"] as? Bool ?? autoConnectAlert
        autoFetchUnnotified = o["auto_fetch_unnotified"] as? Bool ?? autoFetchUnnotified
        savePagesBin = o["save_pages_bin"] as? Bool ?? savePagesBin
        verboseTrace = o["verbose_trace"] as? Bool ?? verboseTrace
        listExpireS = (o["list_expire_s"] as? NSNumber)?.doubleValue ?? listExpireS
        devDisplay = o["dev_display"] as? Bool ?? devDisplay
        bleTrace = o["ble_trace"] as? Bool ?? bleTrace
        acceptEmulator = o["accept_emulator"] as? Bool ?? acceptEmulator
        return self
    }

    func save() {
        guard let store = store else { return }
        let o: [String: Any] = [
            "version": 1,
            "registered": registered.map { ["device_id": $0.device_id, "setup_at": $0.setup_at,
                                            "setup_result": $0.setup_result, "last_seen": $0.last_seen] },
            "auto_connect_alert": autoConnectAlert, "auto_fetch_unnotified": autoFetchUnnotified,
            "save_pages_bin": savePagesBin, "verbose_trace": verboseTrace, "list_expire_s": listExpireS,
            "dev_display": devDisplay, "ble_trace": bleTrace, "accept_emulator": acceptEmulator,
        ]
        if let d = try? JSONSerialization.data(withJSONObject: o, options: [.prettyPrinted, .sortedKeys]) {
            try? store.writeAtomic(rel, d)
        }
    }

    func isRegistered(_ id: String) -> Bool { registered.contains { $0.device_id == id } }

    func register(_ id: String, result: String?, now: Date) {
        let ts = DF.s(now)
        if let i = registered.firstIndex(where: { $0.device_id == id }) {
            if let r = result {
                registered[i].setup_at = ts
                registered[i].setup_result = r
            }
            registered[i].last_seen = ts
        } else {
            registered.append(Registered(device_id: id, setup_at: ts, setup_result: result ?? "ok", last_seen: ts))
        }
        save()
    }
}
