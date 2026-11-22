// Records.swift — 時刻補完・切り出し・ファイル保存（Python 版 A10・A11・A12 に対応）

import Foundation

struct MetaRow {
    var page: Int
    var time: String
    var cycle: Int
    var electrode: Int
    var accel: Int
    var tiltX: Int
    var tiltY: Int
    var tiltZ: Int
    var arr: Int
    var csv: String { "\(page),\(time),\(cycle),\(electrode),\(accel),\(tiltX),\(tiltY),\(tiltZ),\(arr)" }
}

struct Record {
    var kind: String                 // setup30s / event_elec / event_arr / partial
    var ecg: [Int16]
    var meta: [MetaRow]
    var firstTime: Date
    var estimated: Bool
    var pages: [Int]
    var detectIndex: Int = -1        // 異常通知：ecg 内の検知位置（サンプル）
}

typealias PageTimes = [Int: (Date, Bool)]       // page_seq → (ページ先頭時刻, 推定か)

/// 10.3 節。anchor＝(base_seq, SET_TIME で送った時刻)：時刻のあるページを受信していないときに使う
func fixTimes(_ pages: [Int: Page], baseSeq: Int, now: Date, latestSeq: Int?, anchor: (Int, Date)?) -> PageTimes {
    var ref: [Int: Date] = [:]
    for (seq, p) in pages where p.hasTime {
        if let d = DF.date(p.ts) { ref[seq] = d }
    }
    let latest = latestSeq ?? (pages.keys.max() ?? 0)
    var out: PageTimes = [:]
    for (seq, p) in pages {
        var est = (p.flags & PF.timeEst) != 0
        let dt: Date
        if let d = ref[seq] {
            dt = d
        } else if let b = ref[baseSeq] {
            dt = b.addingTimeInterval(-Double(baseSeq - seq) * 4)
        } else if let near = ref.keys.min(by: { abs($0 - seq) < abs($1 - seq) }) {
            dt = ref[near]!.addingTimeInterval(-Double(near - seq) * 4)
        } else if let a = anchor {           // SET_TIME の時刻から推定（±4 秒）
            dt = a.1.addingTimeInterval(-Double(a.0 - seq) * 4)
            est = true
        } else {                             // iPhone の時計から推定
            dt = now.addingTimeInterval(-Double(latest - seq + 1) * 4)
            est = true
        }
        out[seq] = (dt, est)
    }
    return out
}

/// 連続する通番ごとに区切る
func segments<S: Sequence>(_ seqs: S) -> [[Int]] where S.Element == Int {
    var out: [[Int]] = []
    var seg: [Int] = []
    for s in seqs.sorted() {
        if let last = seg.last, s != last + 1 {
            out.append(seg)
            seg = []
        }
        seg.append(s)
    }
    if !seg.isEmpty { out.append(seg) }
    return out
}

func timeStr(_ d: Date, _ est: Bool) -> String {
    (est ? "~" : "") + DF.s(d) + ".\(DF.microsecond(d) / 100000)"
}

func buildRecord(_ kind: String, _ pages: [Int: Page], _ times: PageTimes, _ cycles: [(Int, Int)],
                 detectIndex: Int = -1) -> Record {
    var ecg: [Int16] = []
    ecg.reserveCapacity(cycles.count * P.cycle)
    var meta: [MetaRow] = []
    var estAny = false
    for (seq, k) in cycles {
        let p = pages[seq]!
        ecg.append(contentsOf: p.samples[(k * P.cycle)..<((k + 1) * P.cycle)])
        let (dt, est) = times[seq]!
        estAny = estAny || est
        let t = dt.addingTimeInterval(0.5 * Double(k))
        meta.append(MetaRow(page: seq, time: timeStr(t, est), cycle: k, electrode: p.cycleElectrode(k),
                            accel: p.cycleAccel(k), tiltX: p.tilt[0], tiltY: p.tilt[1], tiltZ: p.tilt[2],
                            arr: (p.flags & PF.arr) != 0 ? 1 : 0))
    }
    let (s0, k0) = cycles[0]
    let first = times[s0]!.0.addingTimeInterval(0.5 * Double(k0))
    return Record(kind: kind, ecg: ecg, meta: meta, firstTime: first, estimated: estAny,
                  pages: Array(Set(cycles.map { $0.0 })).sorted(), detectIndex: detectIndex)
}

enum Cutter {
    /// 電極正常の周期が need 個以上続く区間のうち、最新の区間の末尾 need 周期（10.4 節）
    static func setupWindow(_ pages: [Int: Page], _ times: PageTimes, need: Int = P.setupNeedCycles) -> Record? {
        var best: [(Int, Int)]? = nil
        for seg in segments(pages.keys) {
            var run: [(Int, Int)] = []
            for seq in seg {
                let p = pages[seq]!
                for k in 0..<p.nCycles {
                    if p.cycleElectrode(k) == 0 {
                        run.append((seq, k))
                        if run.count >= need { best = Array(run.suffix(need)) }
                    } else {
                        run = []
                    }
                }
                if p.nCycles < P.pageCycles { run = [] }     // 途中の短いページで区間を切る
            }
        }
        guard let b = best else { return nil }
        return buildRecord("setup30s", pages, times, b)
    }

    /// 最新ページから遡った電極正常の連続周期数（表示用）
    static func normalRunCycles(_ pages: [Int: Page]) -> Int {
        guard let seg = segments(pages.keys).last else { return 0 }
        var n = 0
        for seq in seg.reversed() {
            let p = pages[seq]!
            for k in (0..<p.nCycles).reversed() {
                if p.cycleElectrode(k) != 0 { return n }
                n += 1
            }
        }
        return n
    }

    /// 検知位置 t を中心に [t−3840, t+3840) を切り出す。周期境界にそろえる
    static func eventWindow(_ pages: [Int: Page], _ times: PageTimes, pageSeq: Int, sampleOfs: Int,
                            kind: String = "event_arr", half: Int = P.eventHalfSamples) -> Record? {
        let t = pageSeq * P.pageSamples + sampleOfs
        var lo = t - half
        lo -= ((lo % P.cycle) + P.cycle) % P.cycle
        let hi = lo + 2 * half
        let segs = segments(pages.keys)
        guard !segs.isEmpty else { return nil }
        let seg = segs.first(where: { $0.contains(pageSeq) })
            ?? segs.min(by: { a, b in a.map { abs($0 - pageSeq) }.min()! < b.map { abs($0 - pageSeq) }.min()! })!
        var cycles: [(Int, Int)] = []
        for seq in seg {
            let p = pages[seq]!
            for k in 0..<p.nCycles {
                let g = seq * P.pageSamples + k * P.cycle
                if lo <= g && g + P.cycle <= hi { cycles.append((seq, k)) }
            }
        }
        guard let c0 = cycles.first else { return nil }
        let g0 = c0.0 * P.pageSamples + c0.1 * P.cycle
        return buildRecord(kind, pages, times, cycles, detectIndex: t - g0)
    }

    /// 受信済みの最新の連続区間（_partial 保存用）
    static func partial(_ pages: [Int: Page], _ times: PageTimes) -> Record? {
        guard let seg = segments(pages.keys).last else { return nil }
        let cycles = seg.flatMap { s in (0..<pages[s]!.nCycles).map { (s, $0) } }
        return buildRecord("partial", pages, times, cycles)
    }
}

// MARK: - セッションログ（ECG/日付/session_*.log。時刻 TAB 種別 TAB 内容）

final class SessionLog {
    let store: FileStore, clock: Clock, rel: String
    private(set) var lines: [String] = []

    init(store: FileStore, clock: Clock, rel: String) {
        self.store = store; self.clock = clock; self.rel = rel
    }

    func write(_ kind: String, _ text: String) {
        let d = clock.localDate()
        let line = DF.s(d) + String(format: ".%03d", DF.microsecond(d) / 1000) + "\t\(kind)\t\(text)"
        lines.append(line)
        store.appendLine(rel, line)
    }
}

// MARK: - ファイル保存（10.5 節）

struct SavedItem {
    var name: String
    var dir: URL
    var csv: URL
    var meta: URL
    var event: URL?
    var pagesBin: URL?
    var samples: Int
    var kind: String
    var device: String
    var time: String
    var detectIndex: Int
    var leadoff: Bool
}

final class Exporter {
    let store: FileStore, clock: Clock, settings: Settings

    init(store: FileStore, clock: Clock, settings: Settings) {
        self.store = store; self.clock = clock; self.settings = settings
    }

    func dayDir() -> String { "ECG/" + DF.s(clock.localDate(), "yyyyMMdd") }

    func fileName(_ deviceID: String, _ d: Date, _ kind: String) -> String {
        let base = "ECG_\(deviceID)_\(DF.s(d, "yyyyMMdd-HHmmss"))_\(kind)"
        let dir = dayDir()
        var name = base, n = 1
        while store.exists(dir + "/" + name + ".csv") {
            n += 1
            name = "\(base)_\(n)"
        }
        return name
    }

    static func ecgCSV(_ r: Record) -> Data {
        var s = "ECG\n\(P.fs)\n"
        s.reserveCapacity(r.ecg.count * 6 + 16)
        for v in r.ecg { s += "\(v)\n" }
        return s.data(using: .utf8)!
    }

    static func metaCSV(_ r: Record) -> Data {
        let rows = ["page,time,cycle,electrode,accel,tilt_x,tilt_y,tilt_z,arr"] + r.meta.map { $0.csv }
        return (rows.joined(separator: "\n") + "\n").data(using: .utf8)!
    }

    func save(_ r: Record, deviceID: String, raw: [Int: [UInt8]]?, event: EventRec?, extra: [String: Any]) throws -> SavedItem {
        let dir = dayDir()
        let name = fileName(deviceID, r.firstTime, r.kind)
        let csv = try store.writeAtomic("\(dir)/\(name).csv", Exporter.ecgCSV(r))
        let meta = try store.writeAtomic("\(dir)/\(name)_meta.csv", Exporter.metaCSV(r))
        var ev: URL? = nil
        if let e = event {
            let j = eventJSON(r, deviceID: deviceID, ev: e, name: name, extra: extra)
            let d = try JSONSerialization.data(withJSONObject: j, options: [.prettyPrinted, .sortedKeys])
            ev = try store.writeAtomic("\(dir)/\(name)_event.json", d)
        }
        var pb: URL? = nil
        if let raw = raw, !raw.isEmpty, settings.savePagesBin {
            var blob = Data()
            for s in raw.keys.sorted() { blob.append(contentsOf: raw[s]!) }
            pb = try store.writeAtomic("\(dir)/\(name)_pages.bin", blob)
        }
        return SavedItem(name: name, dir: store.url(dir), csv: csv, meta: meta, event: ev, pagesBin: pb,
                         samples: r.ecg.count, kind: r.kind, device: deviceID, time: DF.s(r.firstTime),
                         detectIndex: r.detectIndex, leadoff: r.meta.contains { $0.electrode != 0 })
    }

    func eventJSON(_ r: Record, deviceID: String, ev: EventRec, name: String, extra: [String: Any]) -> [String: Any] {
        let isArr = ev.event == EV.arr
        let sub = ev.sub & 0x7F
        return [
            "format": "ecgpatch-event/1",
            "device_id": deviceID, "event_seq": ev.eventSeq,
            "event": isArr ? "arrhythmia" : (ev.event == EV.elec ? "electrode" : "other"),
            "event_code": ev.event,
            "sub": (isArr ? ARR_SUB_EN : ELEC_SUB_EN)[sub] ?? String(sub), "sub_code": sub,
            "during_motion": (ev.sub & 0x80) != 0,
            "time": ev.timeText, "page_seq": ev.pageSeq, "sample_ofs": ev.sampleOfs,
            "rr_ms": ev.rrMs, "median_ms": ev.medianMs, "hr_bpm": ev.hrBpm,
            "wave_start": ev.waveStart, "wave_pages": ev.wavePages,
            "pages_received": extra["pages_received"] ?? [Int](),
            "pages_missing": extra["pages_missing"] ?? [Int](),
            "ecg_csv": name + ".csv", "samples": r.ecg.count,
            "detect_index": r.detectIndex,
            "received_at": extra["received_at"] ?? "", "acked_at": extra["acked_at"] ?? "",
        ]
    }
}

// MARK: - 読込み・表示用のまとめ（本体 No.29：受信アプリは R 波検出・判定をしない）

struct LoadedRecord {
    var name: String
    var url: URL?
    var ecg: [Int16]
    var fs: Int
    var meta: [MetaRow]
    var event: [String: Any]?
    var detectIndex: Int = -1
    var source: String = "file"

    /// 電極はがれ区間（パッチの記録 _meta.csv の electrode だけを使う）
    var leadoffRanges: [(Int, Int)] {
        var out: [(Int, Int)] = []
        for (k, row) in meta.enumerated() where row.electrode != 0 {
            let s = k * P.cycle, e = (k + 1) * P.cycle
            if let last = out.last, last.1 == s { out[out.count - 1] = (last.0, e) } else { out.append((s, e)) }
        }
        return out
    }
    var arrRanges: [(Int, Int)] {
        var out: [(Int, Int)] = []
        for (k, row) in meta.enumerated() where row.arr != 0 {
            if let last = out.last, last.1 == k { out[out.count - 1] = (last.0, k + 1) } else { out.append((k, k + 1)) }
        }
        return out
    }
    var hasEvent: Bool { event != nil }
    /// パッチの判定（異常通知）。装着確認は「—」
    var verdict: String {
        guard let e = event, let code = (e["event_code"] as? NSNumber)?.intValue else { return "—" }
        let sub = ((e["sub_code"] as? NSNumber)?.intValue ?? 0) | (((e["during_motion"] as? Bool) ?? false) ? 0x80 : 0)
        return eventTitle(code, sub)
    }
    var patchHR: Int? {
        guard let h = (event?["hr_bpm"] as? NSNumber)?.intValue, h > 0 else { return nil }
        return h
    }
    var notes: [String] {
        guard let e = event else { return [] }
        var out: [String] = []
        if let rr = (e["rr_ms"] as? NSNumber)?.intValue, rr > 0 {
            out.append("R-R \(rr) ms・中央値 \((e["median_ms"] as? NSNumber)?.intValue ?? 0) ms（パッチ）")
        }
        if let t = e["time"] as? String, !t.isEmpty { out.append("検知 \(t)") }
        return out
    }
}

/// CSV（ヘッダ 2 行／4 行／なし）・_event.json を読む
func loadECGFile(_ url: URL) throws -> LoadedRecord {
    if url.pathExtension.lowercased() == "json" {
        let d = try Data(contentsOf: url)
        let o = (try JSONSerialization.jsonObject(with: d)) as? [String: Any] ?? [:]
        if let csv = o["ecg_csv"] as? String {
            var r = try loadECGFile(url.deletingLastPathComponent().appendingPathComponent(csv))
            r.event = o
            return r
        }
        throw ProtocolError("ECG ではない JSON です")
    }
    let text = try String(contentsOf: url, encoding: .utf8)
    let lines = text.split(whereSeparator: \.isNewline).map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
    var hdr: [String] = []
    var i = 0
    while i < lines.count && i < 4 {
        let first = lines[i].split(separator: ",").first.map(String.init) ?? ""
        if Double(first) != nil {
            if i == 1, hdr.first?.uppercased() == "ECG" { hdr.append(lines[i]); i += 1; continue }
            break
        }
        hdr.append(lines[i]); i += 1
    }
    var fs = P.fs
    if hdr.count >= 2, hdr[0].uppercased() == "ECG", let f = Double(hdr[1]) { fs = Int(f) }
    var x: [Int16] = []
    x.reserveCapacity(lines.count)
    for ln in lines[i...] {
        if let v = Double(ln.split(separator: ",").first.map(String.init) ?? "") {
            x.append(Int16(max(-32768, min(32767, Int(v)))))
        }
    }
    var meta: [MetaRow] = []
    let base = url.deletingPathExtension().path
    let metaURL = URL(fileURLWithPath: base + "_meta.csv")
    if FileManager.default.fileExists(atPath: metaURL.path), let mt = try? String(contentsOf: metaURL, encoding: .utf8) {
        for ln in mt.split(whereSeparator: \.isNewline).dropFirst() {
            let p = ln.split(separator: ",", omittingEmptySubsequences: false).map(String.init)
            if p.count >= 9 {
                meta.append(MetaRow(page: Int(p[0]) ?? 0, time: p[1], cycle: Int(p[2]) ?? 0, electrode: Int(p[3]) ?? 0,
                                    accel: Int(p[4]) ?? 0, tiltX: Int(p[5]) ?? 0, tiltY: Int(p[6]) ?? 0,
                                    tiltZ: Int(p[7]) ?? 0, arr: Int(p[8]) ?? 0))
            }
        }
    }
    var r = LoadedRecord(name: url.lastPathComponent, url: url, ecg: x, fs: fs, meta: meta, event: nil)
    let evURL = URL(fileURLWithPath: base + "_event.json")
    if let d = try? Data(contentsOf: evURL), let o = (try? JSONSerialization.jsonObject(with: d)) as? [String: Any] {
        r.event = o
        if let di = (o["detect_index"] as? NSNumber)?.intValue { r.detectIndex = di }
    }
    return r
}
