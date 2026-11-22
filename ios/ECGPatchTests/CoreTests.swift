// CoreTests.swift — 端末に依存しない部分の試験（Python 版 tests/test_ecg_patch.py の主な項目に対応）

import XCTest
@testable import ECGPatch

// MARK: - 試験用のデータ作り

func makePage(seq: Int, ts: [Int], elec: Int = 0, flags: Int = 0, fs: Int = 256) -> [UInt8] {
    var b = [UInt8](repeating: 0, count: P.pageUser)
    for i in 0..<P.pageSamples {
        let v = Int16(500.0 * sin(Double(seq * P.pageSamples + i) / 40.0))
        let u = UInt16(bitPattern: v)
        b[2 * i] = UInt8(u & 0xFF); b[2 * i + 1] = UInt8(u >> 8)
    }
    let h = P.pageBody
    b[h] = UInt8(P.fmtV1)
    for (k, x) in le32(seq).enumerated() { b[h + 1 + k] = x }
    b[h + 5] = UInt8(ts[0] - 2000); b[h + 6] = UInt8(ts[1]); b[h + 7] = UInt8(ts[2])
    b[h + 8] = UInt8(ts[3]); b[h + 9] = UInt8(ts[4]); b[h + 10] = UInt8(ts[5])
    b[h + 11] = 8; b[h + 12] = UInt8(flags); b[h + 13] = UInt8(elec)
    for k in 0..<3 { b[h + 16 + 2 * k] = 0x00; b[h + 17 + 2 * k] = 0x80 }      // 傾き 0x8000
    b[h + 22] = 0x01
    b[h + 23] = UInt8(fs & 0xFF); b[h + 24] = UInt8(fs >> 8)
    let crc = crc16ccittFalse(b, count: h + 25)
    b[h + 25] = UInt8(crc & 0xFF); b[h + 26] = UInt8(crc >> 8)
    for i in (h + 27)..<P.pageUser { b[i] = 0xFF }
    return b
}

func tsOf(_ d: Date) -> [Int] {
    let c = Calendar(identifier: .gregorian).dateComponents(in: TimeZone.current, from: d)
    return [c.year!, c.month!, c.day!, c.hour!, c.minute!, c.second!]
}

final class CoreTests: XCTestCase {

    func testCRC() {
        XCTAssertEqual(crc16ccittFalse(Array("123456789".utf8)), 0x29B1)
    }

    func testPageHeaderRev12() throws {
        let b = makePage(seq: 5000, ts: [2026, 11, 21, 10, 15, 0])
        let p = try Page(b)
        XCTAssertTrue(p.crcOK)
        XCTAssertEqual(p.seq, 5000)
        XCTAssertEqual(p.fs, 256)
        XCTAssertEqual(p.tilt, [P.tiltNone, P.tiltNone, P.tiltNone])
        XCTAssertEqual(p.battText, "90%以上")
        var bad = b
        bad[P.pageBody + 23] ^= 1                    // fs を壊すと CRC NG（対象はスペア 0〜24）
        XCTAssertFalse(try Page(bad).crcOK)
        var old = b                                  // 旧形式（CRC が offset 23）は受け付けない
        let c = crc16ccittFalse(old, count: P.pageBody + 23)
        old[P.pageBody + 23] = UInt8(c & 0xFF); old[P.pageBody + 24] = UInt8(c >> 8)
        XCTAssertFalse(try Page(old).crcOK)
    }

    func testAssemblerAndGap() throws {
        let page = makePage(seq: 7, ts: [2026, 11, 21, 10, 15, 0])
        let asm = PageAssembler()
        var out: [UInt8]? = nil
        let n = (P.pageUser + 239) / 240
        for i in 0..<n {
            let part = Array(page[(i * 240)..<min(P.pageUser, (i + 1) * 240)])
            out = try asm.feed([1, 0, UInt8(i), i == n - 1 ? 0x03 : 0x00] + part)
        }
        XCTAssertEqual(out, page)
        let asm2 = PageAssembler()                    // 断片の飛び → 破棄
        _ = try asm2.feed([2, 0, 0, 0] + Array(page[0..<240]))
        XCTAssertNil(try asm2.feed([2, 0, 2, 0] + Array(page[240..<480])))
        XCTAssertEqual(asm2.gapsAndDups, 1)
    }

    func testAdvStatusBitsAppendixB() throws {
        var mfr = le16(0xFFFF) + le32(0xA1B2C3D6)
        mfr += [1, 0] + le32(0) + [90, 0x09]           // キット：電極 NG・記録中
        let a = try XCTUnwrap(try parseAdv(peer: "p", mfr: mfr, name: "ECGP-A1B2C3D6", rssi: -40, now: 0, acceptEmulator: false))
        XCTAssertEqual(a.deviceID, "A1B2C3D6")
        XCTAssertTrue(a.electrodeNG)
        XCTAssertFalse(a.timeSet)
        let e = try XCTUnwrap(try parseAdv(peer: "p", mfr: nil, name: "ECGP-A1B2C3D4~0307065A", rssi: -40, now: 0, acceptEmulator: true))
        XCTAssertTrue(e.emulator)
        XCTAssertEqual(e.event, 3); XCTAssertEqual(e.sub, 7); XCTAssertEqual(e.battery, 0x5A)
        XCTAssertTrue(e.timeSet && e.unnotified && !e.electrodeNG)
        XCTAssertNil(try parseAdv(peer: "p", mfr: nil, name: "ECGP-A1B2C3D4~0307065A", rssi: -40, now: 0, acceptEmulator: false))
    }

    func testCommands() {
        XCTAssertEqual(Command.reqPages(5000, 2).bytes, [0x30, 0x88, 0x13, 0, 0, 2, 0])
        XCTAssertEqual(Command.setPatientID("ABC").bytes, [0x02, 3, 65, 66, 67])
        XCTAssertEqual(Command.ackSetup(true).bytes, [0x11, 0])
    }

    func testSetupWindowAndTimes() throws {
        let base = DF.date([2026, 11, 21, 10, 15, 0])!
        var pages: [Int: Page] = [:]
        for i in 0..<9 {                              // 5000 は電極 NG、5001〜5008 は正常（64 周期）
            pages[5000 + i] = try Page(makePage(seq: 5000 + i, ts: tsOf(base.addingTimeInterval(Double(i) * 4)),
                                                elec: i == 0 ? 0xFF : 0))
        }
        let times = fixTimes(pages, baseSeq: 5000, now: Date(), latestSeq: 5008, anchor: nil)
        let rec = try XCTUnwrap(Cutter.setupWindow(pages, times))
        XCTAssertEqual(rec.ecg.count, 7680)
        XCTAssertEqual(rec.meta.count, 60)
        XCTAssertEqual(rec.meta.first?.page, 5001)    // 最新の 60 周期＝5001 の周期 4 から
        XCTAssertEqual(rec.meta.first?.cycle, 4)
        XCTAssertTrue(rec.meta.allSatisfy { $0.electrode == 0 })
        XCTAssertEqual(Exporter.metaCSV(rec).split(separator: UInt8(ascii: "\n")).count, 61)
        XCTAssertEqual(Exporter.ecgCSV(rec).split(separator: UInt8(ascii: "\n")).count, 7682)
    }

    func testEventWindowCenteredOnDetection() throws {
        let base = DF.date([2026, 11, 21, 10, 15, 0])!
        var pages: [Int: Page] = [:]
        for i in 0..<9 { pages[5000 + i] = try Page(makePage(seq: 5000 + i, ts: tsOf(base.addingTimeInterval(Double(i) * 4)))) }
        let times = fixTimes(pages, baseSeq: 5000, now: Date(), latestSeq: 5008, anchor: nil)
        let rec = try XCTUnwrap(Cutter.eventWindow(pages, times, pageSeq: 5004, sampleOfs: 300))
        XCTAssertEqual(rec.ecg.count, 7680)
        XCTAssertTrue(rec.detectIndex >= 3840 && rec.detectIndex < 3840 + 128, "\(rec.detectIndex)")
    }

    func testNoAppJudgement() {                      // 本体 No.29：パッチの判定だけを表示する
        var r = LoadedRecord(name: "x", url: nil, ecg: [Int16](repeating: 0, count: 7680), fs: 256, meta: [], event: nil)
        XCTAssertEqual(r.verdict, "—")
        XCTAssertNil(r.patchHR)
        r.event = ["event_code": 3, "sub_code": 7, "during_motion": false, "hr_bpm": 70, "rr_ms": 1650, "median_ms": 830]
        XCTAssertEqual(r.verdict, "不整脈の疑い（休止）")
        XCTAssertEqual(r.patchHR, 70)
    }
}

// MARK: - 模擬パッチ（装着確認の通し試験）

final class MockPatchTransport: BleTransport {
    var observers: [BleObserver] = []
    var clock: Clock
    var state = ST.s2
    var timeSet = 0
    var commands: [UInt8] = []
    var pages: [Int: [UInt8]] = [:]
    var connected = false
    private var queue: [(Double, () -> Void)] = []
    private var onAdv: ((String, String?, [UInt8]?, Int) -> Void)?
    private var subs: [String: (String, [UInt8]) -> Void] = [:]
    private var onDisc: ((BleErr?) -> Void)?
    private var lastCmd = 0, result = 0
    let peer = "00000000-0000-0000-0000-0000A1B2C3D4"

    init(clock: Clock) {
        self.clock = clock
        let base = clock.localDate()
        for i in 0..<8 { pages[5000 + i] = makePage(seq: 5000 + i, ts: tsOf(base.addingTimeInterval(Double(i - 8) * 4))) }
    }

    private func post(_ delay: Double = 0, _ f: @escaping () -> Void) { queue.append((clock.now() + delay, f)) }

    func pump() {
        let now = clock.now()
        let due = queue.filter { $0.0 <= now }
        queue.removeAll { $0.0 <= now }
        for (_, f) in due { f() }
    }

    func advertise() {
        guard !connected, state == ST.s2 else { return }
        let mfr = le16(0xFFFF) + le32(0xA1B2C3D4) + [1, 0] + le32(0) + [90, UInt8(AdvST.rec | (timeSet != 0 ? AdvST.timeSet : 0))]
        onAdv?(peer, "ECGP-A1B2C3D4", mfr, -50)
    }

    func statusBytes() -> [UInt8] {
        [1, UInt8(state), 0, 0, 90, 0, UInt8(timeSet), UInt8(lastCmd), UInt8(result), 0] + le32(5007) + le32(5000) + le16(0x0102)
    }

    func start(onState: @escaping (String) -> Void) { post { onState("poweredOn") } }
    func scanStart(onAdv: @escaping (String, String?, [UInt8]?, Int) -> Void) { self.onAdv = onAdv }
    func scanStop() {}
    func connect(_ peer: String, onConnected: @escaping () -> Void, onFailed: @escaping (BleErr?) -> Void,
                 onDisconnected: @escaping (BleErr?) -> Void, onInvalidated: @escaping () -> Void) {
        onDisc = onDisconnected
        connected = true
        post(0.1) { onConnected() }
    }
    func discover(onDone: @escaping (Set<String>) -> Void) {
        post { onDone([UUIDs.chStream, UUIDs.chEventLog, UUIDs.chStatus, UUIDs.chControl, UUIDs.chSerial]) }
    }
    func subscribe(_ ch: String, onValue: @escaping (String, [UInt8]) -> Void, onState: ((String, BleErr?) -> Void)?) {
        subs[ch] = onValue
        post { onState?(ch, nil) }
    }
    func read(_ ch: String, onValue: @escaping (String, [UInt8]) -> Void) {
        let d: [UInt8] = ch == UUIDs.chSerial ? Array("A1B2C3D4".utf8) : (ch == UUIDs.chStatus ? statusBytes() : [])
        post { onValue(ch, d) }
    }
    func write(_ ch: String, _ data: [UInt8], onDone: @escaping (BleErr?) -> Void) {
        let op = data[0]
        commands.append(op)
        post(0.02) { onDone(nil) }
        lastCmd = Int(op)
        result = Res.ok
        if op == Cmd.setTime { timeSet = 1 }
        if op == Cmd.ackSetup { state = ST.s3 }
        let st = statusBytes()
        post(0.03) { self.subs[UUIDs.chStatus]?(UUIDs.chStatus, st) }
        if op == Cmd.reqSetup {
            var t = 0.05
            for (pi, seq) in pages.keys.sorted().enumerated() {
                let p = pages[seq]!
                let n = (P.pageUser + 239) / 240
                for i in 0..<n {
                    let last = i == n - 1
                    let fl: UInt8 = (last ? 1 : 0) | (last && pi == 7 ? 2 : 0)
                    let frag = [UInt8(1), UInt8(pi), UInt8(i), fl] + Array(p[(i * 240)..<min(P.pageUser, (i + 1) * 240)])
                    post(t) { self.subs[UUIDs.chStream]?(UUIDs.chStream, frag) }
                    t += 0.005
                }
            }
        }
        if op == Cmd.ackSetup {                       // パッチは ACK を受けたら切断する
            post(0.2) { self.disconnect() }
        }
    }
    func disconnect() {
        guard connected else { return }
        connected = false
        let cb = onDisc
        post { cb?(nil) }
    }
    func maxWriteLen() -> Int { 244 }
    func readRSSI(onRSSI: @escaping (Int) -> Void) { post { onRSSI(-50) } }
}

final class SessionFlowTests: XCTestCase {

    func testSetupOverMockPatch() throws {
        let tmp = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        let store = FileStore(root: tmp)
        let clock = FakeClock(base: DF.date([2026, 11, 21, 10, 15, 0])!.addingTimeInterval(0.35))
        let settings = Settings(store: store).load()
        let mock = MockPatchTransport(clock: clock)
        let mgr = SessionManager(transport: mock, clock: clock, store: store, settings: settings, trace: Trace(store: nil),
                                 monitor: nil, recorder: nil)
        mgr.start()
        func run(_ secs: Double, until: () -> Bool = { false }) -> Bool {
            for _ in 0..<Int(secs / 0.05) {
                clock.advance(0.05)
                mock.advertise()
                mock.pump()
                mgr.tick()
                if until() { return true }
            }
            return false
        }
        XCTAssertTrue(run(2) { mgr.scanner.table[mock.peer] != nil })
        mgr.connectTo(mock.peer, patientID: "P001")
        XCTAssertTrue(run(30) { mock.state == ST.s3 && mgr.state == .scanning })
        XCTAssertEqual(mock.commands, [Cmd.setTime, Cmd.setPID, Cmd.reqSetup, Cmd.ackSetup])
        XCTAssertTrue(settings.isRegistered("A1B2C3D4"))
        let item = try XCTUnwrap(mgr.savedList.first)
        XCTAssertEqual(item.kind, "setup30s")
        let csv = try String(contentsOf: item.csv, encoding: .utf8)
        XCTAssertEqual(csv.split(separator: "\n").count, 7682)
        let meta = try String(contentsOf: item.meta, encoding: .utf8)
        XCTAssertEqual(meta.split(separator: "\n").count, 61)
        try? FileManager.default.removeItem(at: tmp)
    }
}
