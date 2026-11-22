// Sessions.swift — コマンド送信・装着確認・異常通知（Python 版 A05・A13 に対応）

import Foundation

// MARK: - CommandSender（Write の結果と Device Status の結果コードの両方を待つ。9.4 節）

final class CommandSender {
    unowned let m: SessionManager

    private struct Cur {
        var cmd: Command
        var cb: (Int?) -> Void
        var make: (() -> Command)?
        var tries = 0
        var busy = 0
        var result: Int? = nil
        var writeOK = false
        var t = 0.0
        var waiting = false
        var retryAt: Double? = nil
    }
    private var cur: Cur?
    private var pending: [(Command, (Int?) -> Void, (() -> Command)?)] = []
    private(set) var sent: [UInt8] = []

    init(_ m: SessionManager) { self.m = m }

    /// make：送る直前にコマンドを作り直す関数（SET_TIME は送る時刻で作る）
    func send(_ cmd: Command, make: (() -> Command)? = nil, _ cb: @escaping (Int?) -> Void) {
        if cur != nil {
            pending.append((cmd, cb, make))
            return
        }
        cur = Cur(cmd: cmd, cb: cb, make: make)
        write()
    }

    var busy: Bool { cur != nil }

    private func write() {
        guard var c = cur else { return }
        c.t = m.clock.now()
        c.writeOK = false
        c.result = nil
        c.tries += 1
        if let mk = c.make { c.cmd = mk() }
        cur = c
        let data = c.cmd.bytes
        sent.append(c.cmd.opcode)
        let shown = c.cmd.opcode == Cmd.setPID ? String(format: "%02X **", Cmd.setPID) : hexs(data)
        m.slog("CMD", "\(c.cmd.name) \(shown) (try \(c.tries))")
        m.t.write(UUIDs.chControl, data) { [weak self] err in self?.onWriteDone(err) }
    }

    private func onWriteDone(_ err: BleErr?) {
        guard var c = cur else { return }
        if let e = err {
            m.trace.w("WRITE_ERR", c.cmd.name, e)
            if c.tries < 2 { write() } else { finish(nil) }
            return
        }
        c.writeOK = true
        cur = c
        maybeComplete()
    }

    func onStatus(_ st: DevStatus) {
        guard var c = cur, st.lastCmd == Int(c.cmd.opcode) else { return }
        c.result = st.result
        cur = c
        maybeComplete()
    }

    private func maybeComplete() {
        guard var c = cur, let r = c.result, c.writeOK else { return }
        m.slog("RES", "\(c.cmd.name) -> result=\(r) \(Res.name(r))")
        if r == Res.busy {
            if c.busy < 3 {
                c.busy += 1
                c.tries = 0
                var at = m.clock.now() + 1.0
                if c.make != nil {           // SET_TIME の再送も秒の変わり目に合わせる
                    let us = Double(DF.microsecond(m.clock.localDate()))
                    at += (1.0 - us / 1e6).truncatingRemainder(dividingBy: 1.0)
                }
                c.retryAt = at
                c.result = nil
                cur = c
                return
            }
            finish(r)
            return
        }
        if r == Res.waiting {
            c.result = nil
            let first = !c.waiting
            c.waiting = true
            cur = c
            if first { c.cb(Res.waiting) }
            return
        }
        finish(r)
    }

    func tick() {
        guard var c = cur else { return }
        let now = m.clock.now()
        if let at = c.retryAt {
            if now >= at {
                c.retryAt = nil
                cur = c
                write()
            }
            return
        }
        if c.waiting { return }
        if now - c.t > P.cmdResultTimeout {
            m.emitApp("warn", "timeout", ["what": "CMD_RESULT", "cmd": c.cmd.name])
            if c.tries < 2 { write() } else { finish(nil) }
        }
    }

    private func finish(_ r: Int?) {
        guard let c = cur else { return }
        cur = nil
        c.cb(r)
        if cur == nil, !pending.isEmpty {
            let (cmd, cb, mk) = pending.removeFirst()
            send(cmd, make: mk, cb)
        }
    }

    /// WAITING 中のコマンドを完了扱いにする（波形が届き始めたとき）
    func releaseWaiting() {
        if let c = cur, c.waiting {
            cur = nil
            if !pending.isEmpty {
                let (cmd, cb, mk) = pending.removeFirst()
                send(cmd, make: mk, cb)
            }
        }
    }

    func cancelAll() {
        cur = nil
        pending = []
    }
}

// MARK: - セッションの共通部分

class BaseSession {
    unowned let m: SessionManager
    var mode: String { "" }
    var asm = PageAssembler()
    var pages: [Int: Page] = [:]
    var raw: [Int: [UInt8]] = [:]
    var firstFragT: Double?
    var lastFragT: Double?
    var reqCount: [Int: Int] = [:]
    var unavailable = Set<Int>()        // REQ_PAGES が RANGE だったページ（飛ばす）
    var crcNG = 0
    var reqPagesSent = 0
    var done = false
    var ackedAll = false
    var saved: SavedItem?
    var receiving = false
    var waiting = false
    private var fsSeen: Int?

    init(_ m: SessionManager) { self.m = m }

    func onFragment(_ data: [UInt8]) {
        let now = m.clock.now()
        if firstFragT == nil { firstFragT = now }
        lastFragT = now
        let before = asm.gapsAndDups
        var b: [UInt8]? = nil
        do { b = try asm.feed(data) } catch { m.err(error, "fragment"); return }
        if asm.gapsAndDups != before {
            m.emitApp("warn", "frag_gap", ["frag": Int(data[2]), "page": Int(data[1])])
        }
        if let b = b { onPageBytes(b) }
        if (data[3] & 0x02) != 0 { checkMissing() }     // 転送最終
        onProgress()
    }

    private func onPageBytes(_ b: [UInt8]) {
        let p: Page
        do { p = try Page(b) } catch { m.err(error, "page"); return }
        if !p.crcOK {
            crcNG += 1
            m.slog("RX", "page \(p.seq) crc=NG")
            m.emitApp("warn", "crc_ng", ["page": p.seq])
            requestPages([p.seq])
            return
        }
        if p.fs != fsSeen {                          // ページヘッダのサンプリング周波数を記録（変わったときだけ）
            fsSeen = p.fs
            m.slog("RX", "page fs=\(p.fs)")
        }
        pages[p.seq] = p
        raw[p.seq] = b
        while pages.count > P.maxSessionPages, let s = pages.keys.min() {
            pages[s] = nil
            raw[s] = nil
        }
        m.slog("RX", String(format: "page %d crc=ok elec=%@ accel=%04X", p.seq, bits8(p.elec), p.accel))
        onPage(p)
    }

    func expected() -> [Int] { [] }
    func missing() -> [Int] { expected().filter { pages[$0] == nil && !unavailable.contains($0) } }

    func checkMissing() {
        let miss = missing()
        if !miss.isEmpty { requestPages(miss) }
    }

    func requestPages(_ seqs: [Int]) {
        let s = Array(Set(seqs)).sorted().filter { (reqCount[$0] ?? 0) < P.reqPagesMax }
        guard !s.isEmpty else { return }
        for seg in segments(s) {
            for x in seg { reqCount[x, default: 0] += 1 }
            reqPagesSent += 1
            m.status.onEvent("resend", ["seq": seg[0]])
            m.sender.send(Command.reqPages(seg[0], seg.count)) { [weak self] r in self?.afterReqPages(r, seg) }
        }
    }

    private func afterReqPages(_ r: Int?, _ seg: [Int]) {
        if r == Res.range {                          // 9.4 節：記録して当該ページを飛ばす
            m.slog("RESULT", "REQ_PAGES \(seg[0])+\(seg.count) -> RANGE（飛ばす）")
            unavailable.formUnion(seg)
            onRangeSkipped()
        }
    }

    func onRangeSkipped() {}
    func onPage(_ p: Page) {}
    func onProgress() {}
    func onStatus(_ st: DevStatus) {}
    func onEvent(_ ev: EventRec) {}
    func start() {}
    func onLinkLost() {}

    func tick() {
        let now = m.clock.now()
        if receiving && !waiting, let lf = lastFragT, now - lf > P.pageIdleTimeout {
            m.emitApp("warn", "timeout", ["what": "PAGE_IDLE"])
            lastFragT = now
            checkMissing()
        }
    }

    func times() -> PageTimes {
        let c = m.conn
        let st = c?.status
        let base = st?.basePageSeq ?? 0
        var anchor: (Int, Date)? = nil
        if let c = c, let a = c.timeAnchor, let st = st, st.timeSet != 0 { anchor = (base, a) }
        return fixTimes(pages, baseSeq: base, now: m.clock.localDate(), latestSeq: st?.latestPageSeq, anchor: anchor)
    }

    @discardableResult
    func savePartial(_ reason: String) -> SavedItem? {
        guard !pages.isEmpty, saved == nil, let rec = Cutter.partial(pages, times()) else { return nil }
        saved = try? m.saveRecord(rec, raw: raw, event: nil, why: reason)
        return saved
    }
}

// MARK: - 装着確認（12 章）

final class SetupSession: BaseSession {
    override var mode: String { "setup" }
    let patientID: String?
    var tReq: Double?
    var ngPrompted = false
    var saving = false
    var ackDeadline: Double?
    var window: Record?
    var electrodeOK: Bool?
    var refWaitUntil: Double?

    init(_ m: SessionManager, patientID: String?) {
        self.patientID = patientID
        super.init(m)
    }

    override func start() {
        guard let st = m.conn?.status else { return }
        if st.state != ST.s2 {
            m.slog("SETUP", "state S\(st.state) is not S2")
            m.disconnect("state")
            return
        }
        m.state = .setup
        m.emitApp("app", "session", ["mode": "setup"])
        if st.timeSet == 0 {
            m.sendSetTime { [weak self] r, d in self?.afterTime(r, d) }     // 秒の変わり目に送る
        } else {
            m.status.onEvent("time_set", ["already": true])
            patient()
        }
    }

    private func afterTime(_ r: Int?, _ d: Date) {
        if r == Res.ok || r == Res.already {
            if r == Res.ok { m.conn?.timeAnchor = d }
            m.status.onEvent("time_set", ["already": r == Res.already, "when": DF.s(d)])
            patient()
        } else if r == nil {
            m.fail("時刻を設定できませんでした（応答なし）", "cmd_timeout")
        } else {
            m.resultError(r, "SET_TIME") { [weak self] in self?.start() }
        }
    }

    private func patient() {
        if let pid = patientID, !pid.isEmpty {
            m.sender.send(Command.setPatientID(pid)) { [weak self] _ in self?.req() }
        } else {
            req()
        }
    }

    private func req() {
        m.sender.send(Command.reqSetup()) { [weak self] r in self?.afterReq(r) }
    }

    private func afterReq(_ r: Int?) {
        if r == Res.ok {
            tReq = m.clock.now()
            receiving = true
            m.status.onEvent("rx_start", ["mode": "setup", "pages": 8])
        } else if r == nil {
            m.fail("パッチから応答がありません", "cmd_timeout")
        } else {
            m.resultError(r, "REQ_SETUP_WAVE") { [weak self] in self?.req() }
        }
    }

    override func expected() -> [Int] {
        guard let lo = pages.keys.min(), let hi = pages.keys.max() else { return [] }
        return Array(lo...hi)
    }

    override func onStatus(_ st: DevStatus) {
        let ok = st.electrode == 0
        if ok != electrodeOK {
            if electrodeOK != nil || !ok { m.status.onEvent("electrode", ["ok": ok]) }
            electrodeOK = ok
        }
    }

    /// 直近 60 周期（30 秒）の電極状態。nil は未受信
    func cells() -> [Int?] {
        guard let hi = pages.keys.max() else { return Array(repeating: nil, count: 60) }
        var out: [Int?] = []
        for seq in (hi - 7)...hi {
            let p = pages[seq]
            for k in 0..<P.pageCycles {
                if let p = p, k < p.nCycles { out.append(p.cycleElectrode(k)) } else { out.append(nil) }
            }
        }
        return Array(out.suffix(60))
    }

    func normalSecs() -> Double { Double(min(P.setupNeedCycles, Cutter.normalRunCycles(pages))) / 2.0 }

    override func onProgress() {
        let secs = normalSecs()
        let pct = window != nil ? 100 : Int(secs / 30.0 * 100)
        m.status.onEvent("progress", ["pct": pct, "cells": cells(),
                                      "text": String(format: "電極正常 %.1f／30 秒・受信 %d ページ", secs, pages.count)])
    }

    override func onPage(_ p: Page) {
        if saving || done { return }
        window = Cutter.setupWindow(pages, times())
        if window != nil {
            if !pages.values.contains(where: { $0.hasTime }) && m.conn?.timeAnchor != nil {
                // 10.3 節：基準ページ（SET_TIME 後の最初のページ）の時刻を使うため、届くまで待つ
                if refWaitUntil == nil {
                    refWaitUntil = m.clock.now() + 9.0
                    m.slog("SETUP", "30 秒そろいました。時刻の基準ページを待ちます")
                }
                if m.clock.now() < refWaitUntil! { return }
            }
            complete()
        }
    }

    private func complete() {
        guard let w = window else { return }
        saving = true
        m.status.onEvent("progress", ["pct": 100, "cells": cells(), "text": "電極正常 30.0／30 秒・受信 \(pages.count) ページ"])
        m.status.onEvent("saving")
        do {
            saved = try m.saveRecord(w, raw: raw, event: nil, why: "setup")
        } catch {
            m.err(error, "save_setup")
            m.fail("ファイルを保存できませんでした", "storage", sendStop: false)
            return
        }
        m.sender.send(Command.ackSetup(true)) { [weak self] r in self?.afterAck(r) }
    }

    private func afterAck(_ r: Int?) {
        if r == Res.ok {
            ackedAll = true
            done = true
            receiving = false
            ackDeadline = m.clock.now() + P.ackDiscWait     // 最大 5 秒（本体 No.9・No.32）
            m.onSetupAcked()
        } else if r == nil {
            m.fail("ACK の応答がありません（保存済み）", "cmd_timeout")
        } else {
            m.resultError(r, "ACK_SETUP") { [weak self] in
                guard let s = self else { return }
                s.m.sender.send(Command.ackSetup(true)) { r in s.afterAck(r) }
            }
        }
    }

    override func tick() {
        super.tick()
        let now = m.clock.now()
        if let d = ackDeadline, now > d {
            ackDeadline = nil
            let emu = m.conn?.info.emulator ?? false
            m.emitApp(emu ? "app" : "warn", "no_disc_after_ack")    // 模擬器は自分から切断できない
            m.disconnect("ack_done")
            return
        }
        if window != nil && !saving, let w = refWaitUntil, now >= w {
            m.slog("SETUP", "基準ページが届かないため推定時刻（~）で保存します")
            complete()
        }
        if receiving && !done && !ngPrompted, let t = tReq, now - t > P.setupTotalTimeout {
            ngPrompted = true
            m.emitApp("warn", "timeout", ["what": "SETUP_TOTAL"])
            m.status.onEvent("error", ["text": "電極 NG：30 秒そろいません（続ける／中止）"])
            m.ui.setupTimeoutAsk = true
        }
    }

    func userContinue() {
        tReq = m.clock.now()
        ngPrompted = false
    }

    override func onLinkLost() {
        if saved != nil { return }
        if let w = window {
            saved = try? m.saveRecord(w, raw: raw, event: nil, why: "setup_noack")
        } else {
            savePartial("setup_lost")
        }
    }
}

// MARK: - 異常通知（15 章）

final class EventSession: BaseSession {
    override var mode: String { "event" }
    var queue: [EventRec] = []
    var seen = Set<Int>()
    var cur: EventRec?
    var no = 0
    var total = 1
    var acked = 0
    var tReq: Double?
    var waitT0: Double?
    var longWaitShown = false
    var idleDeadline: Double?
    var steps = ["todo", "todo", "todo", "todo", "todo"]
    var stepText = ["通知を受信（Event Log）", "記録待ち（検知後 15 秒）", "波形を受信中",
                    "保存（ECG＋メタ＋イベント）", "パッチへ確認（ACK）"]
    var lastSaved: SavedItem?
    var started = false
    var timeWait = false

    override func start() {
        guard let st = m.conn?.status else { return }
        m.state = .event
        started = true
        total = max(1, st.unnotified)
        m.emitApp("app", "session", ["mode": "event"])
        if st.timeSet == 0 {                         // N-02：時刻未設定なら SET_TIME を 1 回
            timeWait = true                          // SET_TIME が終わるまで REQ_EVENT_WAVE を送らない
            m.sendSetTime { [weak self] r, d in self?.afterTime(r, d) }
        } else {
            next()
        }
    }

    private func afterTime(_ r: Int?, _ d: Date) {
        timeWait = false
        if r == Res.ok || r == Res.already {
            if r == Res.ok { m.conn?.timeAnchor = d }
            m.status.onEvent("time_set", ["already": r == Res.already, "when": DF.s(d)])
        } else {
            m.slog("EVENT", "SET_TIME result=\(Res.name(r))（続行）")
        }
        next()
    }

    override func onEvent(_ ev: EventRec) {
        if seen.contains(ev.eventSeq) { return }    // APP-Q13：event_seq で重複を除く
        seen.insert(ev.eventSeq)
        if !ev.crcOK { m.slog("EVENT", "event \(ev.eventSeq) crc NG（続行）") }
        queue.append(ev)
        total = max(total, no + queue.count)
        m.slog("EVENT", "seq=\(ev.eventSeq) \(ev.title) page=\(ev.pageSeq) ofs=\(ev.sampleOfs) wave=\(ev.waveStart)+\(ev.wavePages)")
        if started && cur == nil && !timeWait && !m.cmdBusy { next() }
    }

    private func next() {
        if cur != nil || timeWait { return }
        if !queue.isEmpty {
            idleDeadline = nil
            startEvent(queue.removeFirst())
            return
        }
        let st = m.conn?.status
        // 未通知が 0 ならパッチからの切断を最大 5 秒待つ（本体 No.9・No.32）
        let wait = (st == nil || st!.unnotified == 0) ? P.ackDiscWait : 10.0
        idleDeadline = m.clock.now() + wait
    }

    private func startEvent(_ ev: EventRec) {
        cur = ev
        no += 1
        pages = [:]; raw = [:]
        asm = PageAssembler()
        reqCount = [:]
        unavailable = []
        firstFragT = nil; lastFragT = nil
        waitT0 = nil
        longWaitShown = false
        steps = ["done", "active", "todo", "todo", "todo"]
        stepText[1] = "記録待ち（検知後 15 秒）"
        stepText[2] = "波形を受信中"
        lastSaved = nil
        m.alertUpdate(ev, self)
        tReq = m.clock.now()
        m.sender.send(Command.reqEvent(ev.eventSeq)) { [weak self] r in self?.afterReq(r) }
    }

    private func afterReq(_ r: Int?) {
        let now = m.clock.now()
        guard let ev = cur else { return }
        if r == Res.waiting {
            waiting = true
            waitT0 = now
            receiving = true
            m.status.onEvent("waiting", ["first": true, "pct": 0])
            m.alertUpdate(ev, self)
            return
        }
        waiting = false
        if r == Res.ok {
            receiving = true
            tReq = now                               // 受信開始から EVENT_WAVE_TIMEOUT
            steps[1] = "done"; steps[2] = "active"
            if firstFragT == nil {
                m.status.onEvent("rx_start", ["mode": "event", "pages": ev.wavePages, "no": ev.eventSeq])
            }
            m.alertUpdate(ev, self)
        } else if r == Res.range {
            m.slog("EVENT", "RANGE: event \(ev.eventSeq) を飛ばす")
            cur = nil
            next()
        } else if r == nil {
            m.fail("パッチから応答がありません", "cmd_timeout")
        } else {
            m.resultError(r, "REQ_EVENT_WAVE") { [weak self] in
                guard let s = self, let c = s.cur else { return }
                s.m.sender.send(Command.reqEvent(c.eventSeq)) { r in s.afterReq(r) }
            }
        }
    }

    override func expected() -> [Int] {
        guard let c = cur else { return [] }
        return Array(c.waveStart..<(c.waveStart + c.wavePages))
    }

    func recvBytes() -> Int { expected().filter { pages[$0] != nil }.count * P.pageUser + asm.partialBytes }

    override func onFragment(_ data: [UInt8]) {
        if waiting {                                 // 波形が届き始めた＝記録待ち終了
            waiting = false
            m.sender.releaseWaiting()
            steps[1] = "done"; steps[2] = "active"
            tReq = m.clock.now()
            if let c = cur { m.status.onEvent("rx_start", ["mode": "event", "pages": c.wavePages, "no": c.eventSeq]) }
        }
        super.onFragment(data)
    }

    override func onProgress() {
        guard let c = cur else { return }
        let tot = c.wavePages * P.pageUser
        let got = min(tot, recvBytes())
        let pct = tot > 0 ? got * 100 / tot : 0
        let n = expected().filter { pages[$0] != nil }.count
        stepText[2] = "波形を受信中 \(pct)%（\(n)／\(c.wavePages) ページ）"
        m.status.onEvent("progress", ["pct": pct, "no": c.eventSeq, "text": "\(n)／\(c.wavePages) ページ・\(got)／\(tot) バイト"])
        m.alertUpdate(c, self)
    }

    override func onPage(_ p: Page) {
        if cur != nil && missing().isEmpty { complete(full: true) }
    }

    override func onRangeSkipped() {
        if cur != nil && receiving && !waiting && !pages.isEmpty && missing().isEmpty { complete(full: true) }
    }

    private func complete(full: Bool) {
        guard let ev = cur else { return }
        receiving = false
        steps[2] = full ? "done" : "warn"
        m.status.onEvent("saving")
        let kind = ev.event == EV.arr ? "event_arr" : "event_elec"
        guard let rec = Cutter.eventWindow(pages, times(), pageSeq: ev.pageSeq, sampleOfs: ev.sampleOfs, kind: kind) else {
            m.slog("EVENT", "no data to save for \(ev.eventSeq)")
            steps[3] = "error"
            cur = nil
            m.alertUpdate(ev, self)
            next()
            return
        }
        let exp = Set(expected())
        var extra: [String: Any] = ["pages_received": pages.keys.filter { exp.contains($0) }.sorted(),
                                    "pages_missing": Array(Set(missing()).union(unavailable.intersection(exp))).sorted(),
                                    "received_at": DF.s(m.clock.localDate())]
        do {
            lastSaved = try m.saveRecord(rec, raw: raw, event: ev, why: "event", extra: extra)
        } catch {
            m.err(error, "save_event")
            steps[3] = "error"
            stepText[3] = "保存できませんでした"
            m.alertUpdate(ev, self)
            m.fail("ファイルを保存できませんでした", "storage", sendStop: false)
            return
        }
        steps[3] = "done"
        m.alertUpdate(ev, self)
        if !full {                                    // 一部だけ：ACK しない（次回再送される）
            m.fail("一部を受信できませんでした（ACK しません）", "event_incomplete", sendStop: false)
            return
        }
        steps[4] = "active"
        extra["acked_at"] = ""
        m.sender.send(Command.ackEvent(ev.eventSeq)) { [weak self] r in self?.afterAck(r, ev) }
    }

    private func afterAck(_ r: Int?, _ ev: EventRec) {
        if r == Res.ok {
            steps[4] = "done"
            acked += 1
            if let u = lastSaved?.event, let d = try? Data(contentsOf: u),
               var o = (try? JSONSerialization.jsonObject(with: d)) as? [String: Any] {     // acked_at を書き足す
                o["acked_at"] = DF.s(m.clock.localDate())
                if let nd = try? JSONSerialization.data(withJSONObject: o, options: [.prettyPrinted, .sortedKeys]) {
                    try? nd.write(to: u, options: .atomic)
                }
            }
            m.alertUpdate(ev, self)
            m.status.onEvent("done", ["count": acked])
            cur = nil
            next()
        } else if r == nil {
            m.fail("ACK の応答がありません（保存済み）", "cmd_timeout")
        } else {
            m.resultError(r, "ACK_EVENT") { [weak self] in
                guard let s = self else { return }
                s.m.sender.send(Command.ackEvent(ev.eventSeq)) { r in s.afterAck(r, ev) }
            }
        }
    }

    override func tick() {
        super.tick()
        let now = m.clock.now()
        if let c = cur {
            if waiting, let t0 = waitT0 {
                let w = now - t0
                let left = max(0, Int(15 - w + 0.999))
                if w > P.waitingLong {
                    stepText[1] = "記録済みの範囲だけ受信します（待ち \(Int(w)) 秒）"
                    if !longWaitShown {
                        longWaitShown = true
                        steps[1] = "warn"
                        m.status.onEvent("message", ["text": "記録待ちが 20 秒を超えました"])
                    }
                } else {
                    stepText[1] = left > 0 ? "記録待ち（残り \(left) 秒）" : "記録待ち（まもなく）"
                }
                m.status.onEvent("waiting", ["pct": min(100, Int(w / 15 * 100)), "text": stepText[1]])
                m.alertUpdate(c, self)
            } else if receiving, let t = tReq, now - t > P.eventWaveTimeout {
                m.emitApp("warn", "timeout", ["what": "EVENT_WAVE"])
                if !pages.isEmpty { complete(full: false) } else { m.fail("波形を受信できませんでした", "event_timeout") }
            }
        } else if !queue.isEmpty && !m.cmdBusy {
            next()
        } else if let d = idleDeadline, now > d, !m.cmdBusy {
            idleDeadline = nil
            ackedAll = acked > 0 && queue.isEmpty
            done = true
            if ackedAll { m.slog("EVENT", "全 \(acked) 件の ACK が完了") }
            m.emitApp("app", ackedAll ? "no_disc_after_ack" : "nothing_to_do")
            m.disconnect(ackedAll ? "ack_done" : "nothing")
        }
    }

    override func onLinkLost() {
        guard let ev = cur, !pages.isEmpty, lastSaved == nil else { return }
        let kind = ev.event == EV.arr ? "event_arr" : "event_elec"
        if let rec = Cutter.eventWindow(pages, times(), pageSeq: ev.pageSeq, sampleOfs: ev.sampleOfs, kind: kind) {
            let extra: [String: Any] = ["pages_received": pages.keys.sorted(), "pages_missing": missing(),
                                        "received_at": DF.s(m.clock.localDate())]
            do { lastSaved = try m.saveRecord(rec, raw: raw, event: ev, why: "event_lost", extra: extra) } catch {
                m.err(error, "save_event_lost")
            }
            saved = lastSaved
        }
        let n = expected().filter { pages[$0] != nil }.count
        steps[2] = "warn"
        stepText[2] = "中断（\(n)／\(ev.wavePages) ページ）"
        m.alertUpdate(ev, self)
    }
}
