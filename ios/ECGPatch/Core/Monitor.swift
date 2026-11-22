// Monitor.swift — BLE モニタ・BLE トレース記録（Python 版 A18・A19、17 章に対応）

import Foundation

struct BleErr: CustomStringConvertible {
    let domain: String
    let code: Int
    let name: String
    var description: String { "(\(domain), \(code), \(name))" }
}

let CB_ERRORS: [Int: String] = [0: "unknown", 1: "invalidParameters", 2: "invalidHandle", 3: "notConnected",
                                4: "outOfSpace", 5: "operationCancelled", 6: "connectionTimeout",
                                7: "peripheralDisconnected", 8: "uuidNotAllowed", 9: "alreadyAdvertising",
                                10: "connectionFailed", 11: "connectionLimitReached", 12: "unknownDevice",
                                13: "operationNotSupported", 14: "peerRemovedPairingInformation",
                                15: "encryptionTimedOut", 16: "tooManyLEPairedDevices"]

struct BleEvent {
    let t: Double
    let kind: String            // cb / op / rx / tx / app / dbg / warn
    let ev: String
    var peer: String = ""
    var ch: String = ""
    var data: [UInt8] = []
    var err: BleErr? = nil
    var v: [String: Any] = [:]
}

protocol BleObserver: AnyObject {
    func onBleEvent(_ e: BleEvent)
}

/// 計数・統計・警告（17.2・17.6 節）
final class BleMonitor: BleObserver {
    let clock: Clock
    var counts: [String: Int] = [:]
    var bytesRx = 0, bytesTx = 0
    var scan: [String: (name: String, rssi: Int, times: [Double], last: Double, event: Int, status: Int)] = [:]
    var connectMs: Int?
    private var tConnect: Double?
    var rssi: [(Double, Int)] = []
    var rate: [(Int, Int)] = []
    private var rxSec: Int?
    private var rxAcc = 0
    var results: [Int: Int] = [:]
    var cmdMs: [Int] = []
    private var cmdT: Double?
    var disconnects: [(Double, String, String)] = []
    var warnings: [(Double, String, String)] = []
    var dbg: [String: Any]?
    var mtu: Int?
    var centralState = "unknown"
    var emulator = false
    var fragGaps = 0
    private var weakSince: Double?

    init(clock: Clock) { self.clock = clock }

    func reset() {
        counts = [:]; bytesRx = 0; bytesTx = 0; scan = [:]; connectMs = nil; rssi = []; rate = []
        results = [:]; cmdMs = []; disconnects = []; warnings = []; dbg = nil; fragGaps = 0
    }

    private func warn(_ ev: String, _ text: String) {
        warnings.append((clock.now(), ev, text))
        if warnings.count > 50 { warnings.removeFirst() }
        counts["warn", default: 0] += 1
    }

    func onBleEvent(_ e: BleEvent) {
        counts["\(e.kind)/\(e.ch.isEmpty ? e.ev : e.ch)", default: 0] += 1
        if !e.data.isEmpty {
            if e.kind == "rx" { bytesRx += e.data.count } else if e.kind == "tx" { bytesTx += e.data.count }
        }
        switch e.ev {
        case "state":
            centralState = e.v["state"] as? String ?? "?"
        case "adv":
            var s = scan[e.peer] ?? (name: "", rssi: 0, times: [], last: 0, event: 0, status: 0)
            s.name = e.v["name"] as? String ?? ""
            s.rssi = e.v["rssi"] as? Int ?? 0
            s.last = e.t
            s.event = e.v["event"] as? Int ?? 0
            s.status = e.v["status"] as? Int ?? 0
            if s.times.isEmpty || e.t - s.times.last! >= 0.02 {      // 広告とスキャン応答の組は 1 回と数える
                s.times.append(e.t)
                if s.times.count > 21 { s.times.removeFirst() }
            }
            scan[e.peer] = s
        case "connect" where e.kind == "op":
            tConnect = e.t
            fragGaps = 0
        case "didConnect":
            if let t0 = tConnect {
                connectMs = Int(((e.t - t0) * 1000).rounded())
                if connectMs! > 3000 { warn("slow_connect", "接続が遅い（\(connectMs!) ms）") }
            }
        case "rssi":
            if let r = e.v["rssi"] as? Int, r != 127 {
                rssi.append((e.t, r))
                if rssi.count > 120 { rssi.removeFirst() }
                let recent = rssi.filter { $0.0 >= e.t - 5 }.map { $0.1 }
                if !recent.isEmpty && Double(recent.reduce(0, +)) / Double(recent.count) < -85 {
                    if weakSince == nil { weakSince = e.t } else if e.t - weakSince! >= 5 && counts["warn_weak"] == nil {
                        counts["warn_weak"] = 1
                        warn("weak_rssi", "電波が弱い：パッチに近づけてください")
                    }
                } else {
                    weakSince = nil
                }
            }
        case "mtu":
            mtu = e.v["mtu"] as? Int
        case "frag_gap":
            fragGaps += 1
            if fragGaps == 3 { warn("frag_gap", "断片の欠落が 3 回以上") }
        case "didDisconnect":
            let name = e.err?.name ?? "normal"
            disconnects.append((e.t, name, e.v["judge"] as? String ?? ""))
            if let c = e.err?.code, c == 6 || c == 7 { warn("unexpected_disc", "想定外の切断（\(name)）") } else if (e.v["unexpected"] as? Bool) == true {
                warn("unexpected_disc", "想定外の切断（\(name)）")
            }
        case "emulator":
            emulator = true
        default:
            break
        }
        if e.kind == "rx" && e.ch == "ECG" {
            let sec = Int(e.t)
            if rxSec == nil { rxSec = sec }
            if sec != rxSec! {
                rate.append((rxSec!, rxAcc))
                checkSlow()
                if rxSec! + 1 < sec { for g in (rxSec! + 1)..<sec { rate.append((g, 0)) } }
                rxSec = sec
                rxAcc = 0
                if rate.count > 120 { rate.removeFirst(rate.count - 120) }
            }
            rxAcc += e.data.count
        } else if e.kind == "tx" && e.ch == "CTL" {
            cmdT = e.t
        } else if e.kind == "rx" && e.ch == "STS", let r = e.v["result"] as? Int {
            results[r, default: 0] += 1
            if let t = cmdT {
                cmdMs.append(Int((e.t - t) * 1000))
                cmdT = nil
            }
        } else if e.kind == "dbg" {
            dbg = e.v
        }
    }

    private func checkSlow() {
        let last3 = rate.suffix(3)
        if last3.count == 3 && last3.allSatisfy({ $0.1 > 0 }) && Double(last3.map { $0.1 }.reduce(0, +)) / 3 < 5000 {
            counts["warn_slow", default: 0] += 1
            if counts["warn_slow"] == 1 {
                warn("slow_rate", String(format: "速度が出ない（%.1f KB/s）", Double(last3.map { $0.1 }.reduce(0, +)) / 3000))
            }
        }
    }

    var rateKBs: Double { Double(rate.last?.1 ?? 0) / 1000 }

    func advIntervalMs(_ peer: String) -> Int? {
        guard let s = scan[peer], s.times.count >= 3 else { return nil }
        let d = zip(s.times, s.times.dropFirst()).map { Int(($1 - $0) * 1000) }.sorted()
        return d[d.count / 2]
    }
}

/// ble_trace_*.jsonl（17.4 節）。tick ごとにまとめて書く
final class BleTraceRecorder: BleObserver {
    let store: FileStore, clock: Clock, settings: Settings
    let reldir = "ECG_patch/trace"
    var ring: [[String: Any]] = []
    private var queue: [String] = []
    private let t0: Double
    private let wall0: Date
    private(set) var relpath: String?
    private var size = 0
    private var advLast: [String: (String, Double)] = [:]
    var summaryRel: String { reldir + "/ble_summary.csv" }
    let fileMax = 5 * 1024 * 1024

    init(store: FileStore, clock: Clock, settings: Settings) {
        self.store = store; self.clock = clock; self.settings = settings
        t0 = clock.now()
        wall0 = clock.localDate()
    }

    func start() { newFile() }

    private func newFile() {
        let stamp = DF.s(clock.localDate(), "yyyyMMdd_HHmmss")
        var name = "ble_trace_\(stamp).jsonl", n = 1
        while store.exists(reldir + "/" + name) {
            n += 1
            name = "ble_trace_\(stamp)_\(n).jsonl"
        }
        relpath = reldir + "/" + name
        size = 0
    }

    func onBleEvent(_ e: BleEvent) {
        if e.ev == "adv" {               // 広告は内容が変わったときと 1 秒に 1 回だけ記録
            let sig = "\(e.v["name"] ?? "")/\(e.v["event"] ?? "")/\(e.v["status"] ?? "")"
            if let last = advLast[e.peer], last.0 == sig, e.t - last.1 < 1.0 { return }
            advLast[e.peer] = (sig, e.t)
        }
        let wall = wall0.addingTimeInterval(e.t - t0)
        var d: [String: Any] = ["t": ((e.t - t0) * 1000).rounded() / 1000,
                                "wall": DF.s(wall, "yyyy-MM-dd HH:mm:ss.SSS"), "k": e.kind, "ev": e.ev]
        if !e.peer.isEmpty { d["peer"] = e.peer }
        if !e.ch.isEmpty { d["ch"] = e.ch }
        if !e.data.isEmpty {
            d["len"] = e.data.count
            if e.ch == "CTL" && e.data.first == Cmd.setPID {
                d["hex"] = String(format: "%02X **", Cmd.setPID)              // 患者 ID を伏せる
            } else if e.ch == "ECG" && !settings.verboseTrace {
                d["hex"] = hexs(Array(e.data.prefix(8)))
            } else {
                d["hex"] = hexs(e.data)
            }
        }
        if let er = e.err { d["err"] = ["domain": er.domain, "code": er.code, "name": er.name] }
        if !e.v.isEmpty { d["v"] = jsonSafe(e.v) }
        ring.append(d)
        if ring.count > 5000 { ring.removeFirst(ring.count - 5000) }
        guard settings.bleTrace, let data = try? JSONSerialization.data(withJSONObject: d, options: [.sortedKeys]),
              let s = String(data: data, encoding: .utf8) else { return }
        queue.append(s)
    }

    private func jsonSafe(_ v: [String: Any]) -> [String: Any] {
        var out: [String: Any] = [:]
        for (k, x) in v {
            switch x {
            case let b as BleErr: out[k] = ["domain": b.domain, "code": b.code, "name": b.name]
            case is String, is Int, is Double, is Bool: out[k] = x
            default: out[k] = "\(x)"
            }
        }
        return out
    }

    func flush() {
        guard !queue.isEmpty, let rel = relpath else { return }
        let text = queue.joined(separator: "\n") + "\n"
        queue = []
        store.appendText(rel, text)
        size += text.utf8.count
        if size > fileMax { newFile() }
    }

    func appendSummary(_ row: [Any]) {
        let hdr = "connected_at,device_id,connect_ms,mtu,rssi_avg,rx_bytes,rate_kbs,frag_gaps,crc_ng,req_pages,disc_reason"
        if !store.exists(summaryRel) { store.appendLine(summaryRel, hdr) }
        store.appendLine(summaryRel, row.map { "\($0)" }.joined(separator: ","))
    }

    func exportURLs() -> [URL] {
        flush()
        var out: [URL] = []
        if let r = relpath, store.exists(r) { out.append(store.url(r)) }
        if store.exists(summaryRel) { out.append(store.url(summaryRel)) }
        return out
    }
}
