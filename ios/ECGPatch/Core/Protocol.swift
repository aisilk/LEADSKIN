// Protocol.swift — 定数・データ形式（Python 版 r10 の 6.1 節・付録 A・proto・A08/A09 に対応）
//
// 本体仕様書 Rev1.2（案）7.7・7.8 節、付属 B 15・16 章（No.26 で凍結）、別紙 2 Rev0.5（案）に合わせる。
// 端末に依存しない（Foundation だけ）。

import Foundation

let APP_VERSION = "2026-11-22 Swift r10"

enum P {
    static let fs = 256, cycle = 128, pageCycles = 8, pageSamples = 1024
    static let pageBody = 2048, pageSpare = 64, pageUser = 2112
    static let fmtV1 = 0xA1
    static let setupNeedCycles = 60            // 電極正常の連続 30 秒
    static let eventHalfSamples = 3840         // 異常通知の前後 15 秒
    static let connectTimeout = 15.0
    static let pageIdleTimeout = 8.0
    static let setupTotalTimeout = 120.0
    static let eventWaveTimeout = 45.0
    static let cmdResultTimeout = 5.0
    static let ackDiscWait = 5.0               // ACK 後にパッチからの切断を待つ（本体 No.9・No.32）
    static let discTimeout = 2.0
    static let waitingLong = 20.0
    static let maxSessionPages = 40
    static let reqPagesMax = 3
    static let tiltNone = -32768               // 傾きは記録しない（0x8000。本体 No.34）
}

enum UUIDs {
    static let tail = "-6E0C-4C1B-8A5D-3E2F7C9B0D01"
    static let svcECG = "A1510001" + tail
    static let chStream = "A1510002" + tail
    static let chEventLog = "A1510003" + tail
    static let chStatus = "A1510004" + tail
    static let chControl = "A1510005" + tail
    static let chDebug = "A1510006" + tail      // Debug Info（任意）
    static let chEmu = "A15100EE" + tail        // Emulator Info（模擬器のみ）
    static let svcDIS = "180A", chSerial = "2A25"
    static let svcBAT = "180F", chBatt = "2A19"
    static let short: [String: String] = [chStream: "ECG", chEventLog: "EVT", chStatus: "STS", chControl: "CTL",
                                          chDebug: "DBG", chSerial: "SN", chBatt: "BAT", chEmu: "EMU"]
}

enum PF {                                       // ページ内フラグ
    static let arr = 1, elec = 2, notified = 4, timeUnset = 8, short = 16, reboot = 32, timeEst = 64
}

enum Cmd {
    static let setTime: UInt8 = 0x01, setPID: UInt8 = 0x02, reqSetup: UInt8 = 0x10, ackSetup: UInt8 = 0x11
    static let reqEvent: UInt8 = 0x20, ackEvent: UInt8 = 0x21, reqPages: UInt8 = 0x30, stop: UInt8 = 0x40
    static let names: [UInt8: String] = [0x01: "SET_TIME", 0x02: "SET_PATIENT_ID", 0x10: "REQ_SETUP_WAVE",
                                         0x11: "ACK_SETUP", 0x20: "REQ_EVENT_WAVE", 0x21: "ACK_EVENT",
                                         0x30: "REQ_PAGES", 0x40: "STOP"]
}

enum Res {
    static let ok = 0, badLen = 1, badState = 2, already = 3, range = 4, nand = 5, waiting = 6, unknown = 7, busy = 8
    static let names = ["OK", "BAD_LEN", "BAD_STATE", "ALREADY", "RANGE", "NAND_ERR", "WAITING", "UNKNOWN", "BUSY"]
    static func name(_ r: Int?) -> String {
        guard let r = r else { return "none" }
        return r >= 0 && r < names.count ? names[r] : String(r)
    }
}

enum ST { static let s2 = 2, s3 = 3, s5 = 5, s6 = 6 }        // Device Status の state
enum EV { static let setup = 0x01, elec = 0x02, arr = 0x03, batt = 0x04 }
// 広告の Status（付属 B 16.2 節。本体 No.26）
enum AdvST { static let elec = 0x01, unnotified = 0x02, timeSet = 0x04, rec = 0x08 }
let COMPANY_DEV = 0xFFFF

let ARR_SUB: [Int: String] = [1: "頻脈", 2: "徐脈", 3: "長いポーズ", 4: "期外収縮", 5: "心房細動", 6: "早期拍", 7: "休止"]
let ARR_SUB_EN: [Int: String] = [1: "tachy", 2: "brady", 3: "long_pause", 4: "pvc", 5: "af", 6: "early", 7: "pause"]
let ELEC_SUB: [Int: String] = [1: "リードオフ", 2: "信号飽和", 3: "フラット", 4: "ノイズ"]
let ELEC_SUB_EN: [Int: String] = [1: "lead_off", 2: "saturation", 3: "flat", 4: "noise"]

struct ProtocolError: Error, CustomStringConvertible {
    let message: String
    init(_ m: String) { message = m }
    var description: String { message }
}

// MARK: - CRC・バイト列

func crc16ccittFalse(_ data: [UInt8], count: Int? = nil) -> Int {
    var crc: UInt16 = 0xFFFF
    let n = count ?? data.count
    for i in 0..<n {
        crc ^= UInt16(data[i]) << 8
        for _ in 0..<8 {
            crc = (crc & 0x8000) != 0 ? (crc << 1) ^ 0x1021 : (crc << 1)
        }
    }
    return Int(crc)
}

extension Array where Element == UInt8 {
    func u8(_ o: Int) -> Int { Int(self[o]) }
    func i8(_ o: Int) -> Int { Int(Int8(bitPattern: self[o])) }
    func u16(_ o: Int) -> Int { Int(self[o]) | (Int(self[o + 1]) << 8) }
    func i16(_ o: Int) -> Int { Int(Int16(bitPattern: UInt16(u16(o)))) }
    func u32(_ o: Int) -> Int { u16(o) | (u16(o + 2) << 16) }
}

func le16(_ v: Int) -> [UInt8] { [UInt8(v & 0xFF), UInt8((v >> 8) & 0xFF)] }
func le32(_ v: Int) -> [UInt8] { le16(v & 0xFFFF) + le16((v >> 16) & 0xFFFF) }

func hexs(_ b: [UInt8], limit: Int? = nil) -> String {
    if let l = limit, b.count > l {
        return b.prefix(l).map { String(format: "%02X", $0) }.joined(separator: " ") + " …"
    }
    return b.map { String(format: "%02X", $0) }.joined(separator: " ")
}

func bits8(_ v: Int) -> String {               // 周期 0〜7 の順に並べた 8 ビット（Python の format(v,'08b')[::-1]）
    (0..<8).map { (v >> $0) & 1 == 1 ? "1" : "0" }.joined()
}

func eventTitle(_ event: Int, _ sub: Int) -> String {
    let motion = (sub & 0x80) != 0 ? "（体動中）" : ""
    let s = sub & 0x7F
    let t: String
    switch event {
    case EV.arr: t = "不整脈の疑い（\(ARR_SUB[s] ?? "サブ \(s)")）"
    case EV.elec: t = "電極異常（\(ELEC_SUB[s] ?? "サブ \(s)")）"
    case EV.batt: t = "電池低下"
    case EV.setup: t = "装着確認"
    default: t = String(format: "イベント %02X", event)
    }
    return t + motion
}

func kindText(_ event: Int) -> String {
    [EV.setup: "装着確認", EV.elec: "電極異常", EV.arr: "不整脈疑い", EV.batt: "電池低下"][event] ?? "—"
}

// MARK: - アドバタイズ（9.1 節・付属 B 16.2 節）

struct AdvInfo {
    var peer: String
    var deviceID: String
    var name: String
    var event: Int
    var sub: Int
    var ts: Int
    var battery: Int
    var status: Int
    var rssi: Int = 0
    var lastSeen: Double = 0
    var emulator: Bool = false
    var company: Int = COMPANY_DEV

    var unnotified: Bool { (status & AdvST.unnotified) != 0 }
    var timeSet: Bool { (status & AdvST.timeSet) != 0 || (!emulator && ts != 0) }
    var electrodeNG: Bool { (status & AdvST.elec) != 0 }
    var label: String { "ECGP-" + deviceID }
}

/// 模擬器の名前 ECGP-<ID8>~<event><sub><status><batt>（16 進 8 桁）を読む
func parseEmulatorName(_ name: String) -> (String, [UInt8])? {
    let c = Array(name)
    guard c.count == 22, name.hasPrefix("ECGP-"), c[13] == "~" else { return nil }
    let id = String(c[5..<13]), tail = String(c[14..<22])
    let hex = CharacterSet(charactersIn: "0123456789abcdefABCDEF")
    guard id.unicodeScalars.allSatisfy({ hex.contains($0) }), tail.unicodeScalars.allSatisfy({ hex.contains($0) }) else {
        return nil
    }
    var bytes: [UInt8] = []
    var i = tail.startIndex
    for _ in 0..<4 {
        let j = tail.index(i, offsetBy: 2)
        bytes.append(UInt8(tail[i..<j], radix: 16) ?? 0)
        i = j
    }
    return (id.uppercased(), bytes)
}

func isEmulatorName(_ name: String?) -> Bool { name.flatMap { parseEmulatorName($0) } != nil }

/// Manufacturer データ（本物）または名前（模擬器）から AdvInfo を作る。対象外は nil。
func parseAdv(peer: String, mfr: [UInt8]?, name: String?, rssi: Int, now: Double, acceptEmulator: Bool) throws -> AdvInfo? {
    if let m = mfr, m.count >= 14 {
        let company = m.u16(0)
        if company != COMPANY_DEV { return nil }
        let dev = String(format: "%08X", m.u32(2))
        return AdvInfo(peer: peer, deviceID: dev, name: name ?? "ECGP-" + dev, event: m.u8(6), sub: m.u8(7),
                       ts: m.u32(8), battery: m.u8(12), status: m.u8(13), rssi: rssi, lastSeen: now,
                       emulator: false, company: company)
    }
    if let m = mfr, !m.isEmpty {
        throw ProtocolError("short manufacturer data \(m.count)")
    }
    if acceptEmulator, let n = name, let em = parseEmulatorName(n) {
        let x = em.1
        return AdvInfo(peer: peer, deviceID: em.0, name: n, event: Int(x[0]), sub: Int(x[1]), ts: 0,
                       battery: Int(x[3]), status: Int(x[2]), rssi: rssi, lastSeen: now, emulator: true)
    }
    return nil
}

// MARK: - Device Status（20B）・Event Log（32B）

struct DevStatus {
    let ver, state, electrode, alarm, batt, unnotified, timeSet, lastCmd, result, rsv: Int
    let latestPageSeq, basePageSeq, fwVer: Int

    init(_ b: [UInt8]) throws {
        guard b.count == 20 else { throw ProtocolError("status length \(b.count)") }
        ver = b.u8(0); state = b.u8(1); electrode = b.u8(2); alarm = b.u8(3); batt = b.u8(4)
        unnotified = b.u8(5); timeSet = b.u8(6); lastCmd = b.u8(7); result = b.u8(8); rsv = b.u8(9)
        latestPageSeq = b.u32(10); basePageSeq = b.u32(14); fwVer = b.u16(18)
    }
}

struct EventRec {
    let recVer, event, sub, status, eventSeq, pageSeq, sampleOfs: Int
    let ts: [Int]                // 年（−2000）・月・日・時・分・秒
    let rrMs, medianMs, hrBpm, waveStart, wavePages, crc: Int
    let crcOK: Bool

    init(_ b: [UInt8]) throws {
        guard b.count == 32 else { throw ProtocolError("event length \(b.count)") }
        recVer = b.u8(0); event = b.u8(1); sub = b.u8(2); status = b.u8(3)
        eventSeq = b.u32(4); pageSeq = b.u32(8); sampleOfs = b.u16(12)
        ts = (14..<20).map { b.u8($0) }
        rrMs = b.u16(20); medianMs = b.u16(22); hrBpm = b.u8(24)
        waveStart = b.u32(25); wavePages = b.u8(29); crc = b.u16(30)
        crcOK = crc16ccittFalse(b, count: 30) == crc
    }

    var title: String { eventTitle(event, sub) }
    var timeText: String {
        if ts[1] == 0 { return "時刻未設定" }
        return String(format: "%04d-%02d-%02d %02d:%02d:%02d", 2000 + ts[0], ts[1], ts[2], ts[3], ts[4], ts[5])
    }
}

// MARK: - コマンド（Device Control）

struct Command {
    let opcode: UInt8
    var params: [UInt8] = []

    var bytes: [UInt8] { [opcode] + params }
    var name: String { Cmd.names[opcode] ?? String(format: "CMD_%02X", opcode) }

    static func setTime(_ d: Date) -> Command {
        let c = Calendar.current.dateComponents([.year, .month, .day, .hour, .minute, .second], from: d)
        return Command(opcode: Cmd.setTime, params: [UInt8((c.year ?? 2000) - 2000), UInt8(c.month ?? 1),
                                                     UInt8(c.day ?? 1), UInt8(c.hour ?? 0), UInt8(c.minute ?? 0),
                                                     UInt8(c.second ?? 0)])
    }
    static func setPatientID(_ pid: String) -> Command {
        let a = pid.unicodeScalars.map { $0.isASCII ? UInt8($0.value) : UInt8(ascii: "?") }.prefix(16)
        return Command(opcode: Cmd.setPID, params: [UInt8(a.count)] + a)
    }
    static func reqSetup() -> Command { Command(opcode: Cmd.reqSetup) }
    static func ackSetup(_ ok: Bool = true) -> Command { Command(opcode: Cmd.ackSetup, params: [ok ? 0 : 1]) }
    static func reqEvent(_ seq: Int) -> Command { Command(opcode: Cmd.reqEvent, params: le32(seq)) }
    static func ackEvent(_ seq: Int) -> Command { Command(opcode: Cmd.ackEvent, params: le32(seq)) }
    static func reqPages(_ seq: Int, _ n: Int) -> Command { Command(opcode: Cmd.reqPages, params: le32(seq) + le16(n)) }
    static func stop() -> Command { Command(opcode: Cmd.stop) }
}

// MARK: - Debug Info（32B、付属 B 16.11）

struct DebugInfo {
    let ver, state, bleSub, phy, connInterval, slaveLatency, supTimeout, attMtu, txFree, rssi, lastDisc, sec: Int
    let notifyOK, notifyBusy, cmdRx, cmdErr, connCount, uptimeS: Int

    init?(_ b: [UInt8]) {
        guard b.count >= 32 else { return nil }
        ver = b.u8(0); state = b.u8(1); bleSub = b.u8(2); phy = b.u8(3)
        connInterval = b.u16(4); slaveLatency = b.u16(6); supTimeout = b.u16(8); attMtu = b.u16(10)
        txFree = b.u8(12); rssi = b.i8(13); lastDisc = b.u8(14); sec = b.u8(15)
        notifyOK = b.u32(16); notifyBusy = b.u16(20); cmdRx = b.u16(22); cmdErr = b.u16(24)
        connCount = b.u16(26); uptimeS = b.u32(28)
    }
    var intervalMs: Double { Double(connInterval) * 1.25 }
    var timeoutS: Double { Double(supTimeout) / 100 }
    var phyText: String {
        let t = [1: "1M", 2: "2M", 3: "Coded"]
        return "\(t[phy & 15] ?? "?")/\(t[phy >> 4] ?? "?")"
    }
}

// MARK: - ページ（2,112B。本体 7.8 節 Rev1.2：fs＝offset 23〜24、CRC16＝25〜26、CRC の対象はスペア 0〜24 まで）

struct Page {
    let seq: Int
    let ts: [Int]                // 西暦・月・日・時・分・秒
    let nCycles: Int
    let flags: Int
    let elec: Int
    let accel: Int
    let tilt: [Int]
    let batt: Int
    let fs: Int
    let crcOK: Bool
    let samples: [Int16]

    init(_ b: [UInt8]) throws {
        guard b.count == P.pageUser else { throw ProtocolError("page length \(b.count)") }
        let h = P.pageBody
        guard b.u8(h) == P.fmtV1 else { throw ProtocolError(String(format: "page format %02X", b.u8(h))) }
        seq = b.u32(h + 1)
        ts = [2000 + b.u8(h + 5), b.u8(h + 6), b.u8(h + 7), b.u8(h + 8), b.u8(h + 9), b.u8(h + 10)]
        nCycles = max(1, min(P.pageCycles, b.u8(h + 11)))
        flags = b.u8(h + 12)
        elec = b.u8(h + 13)
        accel = b.u16(h + 14)
        tilt = [b.i16(h + 16), b.i16(h + 18), b.i16(h + 20)]
        batt = b.u8(h + 22)
        fs = b.u16(h + 23)
        crcOK = crc16ccittFalse(b, count: h + 25) == b.u16(h + 25)
        var s = [Int16](repeating: 0, count: P.pageSamples)
        for i in 0..<P.pageSamples {
            s[i] = Int16(bitPattern: UInt16(b[2 * i]) | (UInt16(b[2 * i + 1]) << 8))
        }
        samples = s
    }

    func cycleElectrode(_ k: Int) -> Int { (elec >> k) & 1 }
    func cycleAccel(_ k: Int) -> Int { (accel >> (2 * k)) & 3 }
    var hasTime: Bool { (flags & PF.timeUnset) == 0 && (1...12).contains(ts[1]) }
    var battText: String {              // offset 22：bit0＝90% 以上・bit1＝30% 未満・0xFF＝未測定
        if batt == 0xFF { return "未測定" }
        return (batt & 1) != 0 ? "90%以上" : ((batt & 2) != 0 ? "30%未満" : "30〜90%")
    }
}

/// 断片（xfer_id, page_idx, frag_idx, flags）を連結して 2,112B のページを作る
final class PageAssembler {
    private var key: (Int, Int)? = nil
    private var buf: [UInt8] = []
    private var nextFrag = 0
    private var xfer: Int? = nil
    var stats: [String: Int] = [:]     // frags, pages, gaps, dups, xfer_change, bad_len

    var partialBytes: Int { key != nil ? buf.count : 0 }
    var gapsAndDups: Int { (stats["gaps"] ?? 0) + (stats["dups"] ?? 0) }

    private func bump(_ k: String) { stats[k, default: 0] += 1 }

    func reset() { key = nil; buf = []; nextFrag = 0 }

    func feed(_ frag: [UInt8]) throws -> [UInt8]? {
        guard frag.count >= 4 else { throw ProtocolError("fragment length \(frag.count)") }
        let x = Int(frag[0]), pidx = Int(frag[1]), fidx = Int(frag[2]), flags = Int(frag[3])
        bump("frags")
        if let old = xfer, old != x {
            bump("xfer_change")
            reset()                      // 前の再構成を破棄
        }
        xfer = x
        if key == nil || key! != (x, pidx) {
            if key != nil { bump("gaps") }
            key = (x, pidx); buf = []; nextFrag = 0
        }
        if fidx != nextFrag {            // 飛び・重複 → このページは破棄（後で REQ_PAGES）
            bump(fidx < nextFrag ? "dups" : "gaps")
            key = nil; buf = []
            return nil
        }
        buf += frag[4...]
        nextFrag += 1
        if (flags & 0x01) != 0 {         // ページ最終断片
            let page = buf
            key = nil; buf = []
            if page.count == P.pageUser { bump("pages"); return page }
            bump("bad_len")
        }
        return nil
    }
}
