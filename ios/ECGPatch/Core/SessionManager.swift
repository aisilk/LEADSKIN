// SessionManager.swift — 状態機械・スキャン・接続・受信の振り分け・切断（Python 版 A03・A04・A06・A07・A13）
//
// Pythonista 版は作業スレッドと画面のスレッドを分けていたが、Swift 版は CoreBluetooth の通知をメインキューで受け、
// 状態機械・画面の更新もメインスレッドで行う（スレッドをまたぐ受け渡しがない）。

import Foundation

enum MgrState: String {
    case idle = "IDLE", scanning = "SCANNING", connecting = "CONNECTING", discovering = "DISCOVERING"
    case setup = "SETUP", event = "EVENT", disconnecting = "DISCONNECTING"
}

// MARK: - 下部の接続状態表示（13.9.5 節）

final class StatusPanel {
    let clock: Clock
    var log: [String] = []              // 直近 4 件
    var console: [String] = []
    var dot = "gray", head = "起動しました", headColor = "ink", barColor = "gray"
    var percent: Int? = nil
    var detail = "BLE を準備しています…", progress = ""
    var cells: [Int?]? = nil
    var mode: String? = nil
    var warn = ""
    var bt = "unknown"
    private var lastPctLogged = 0
    private var doneAt: Double?

    init(clock: Clock) {
        self.clock = clock
        add("起動しました ／ BLE を準備しています…")
    }

    func add(_ m: String) {
        log.append(m)
        if log.count > 4 { log.removeFirst() }
        console.append(m)
        if console.count > 500 { console.removeFirst() }
    }

    func onEvent(_ ev: String, _ d: [String: Any] = [:]) {
        switch ev {
        case "bt_state":
            let s = d["state"] as? String ?? "?"
            bt = s
            if s == "poweredOn" {
                dot = "gray"; head = "待機中（パッチを探しています）"; percent = 0; barColor = "gray"; detail = "Bluetooth: オン"
                add("BLE 受信待機中（ECG パッチを待っています）")
            } else if s == "unauthorized" {
                dot = "gray"; head = "Bluetooth が許可されていません"; percent = nil
                detail = "設定 > ECG Patch > Bluetooth を許可してください"
                add(head)
            } else {
                dot = "gray"; percent = nil
                head = "Bluetooth: オフ   設定アプリで Bluetooth をオンにしてください"
                detail = "Bluetooth: \(s)"
                add(head)
            }
        case "scan":
            dot = "gray"; head = "待機中（パッチを探しています）"; percent = 0; barColor = "gray"
            headColor = "ink"; mode = nil; cells = nil; progress = ""; warn = ""; detail = "Bluetooth: オン"
        case "found":
            add("\(d["name"] ?? "") を見つけました（\(d["kind"] ?? "")）")
        case "alert":
            add("\(d["name"] ?? "") の異常通知を受信しました（自動接続）")
        case "connecting":
            dot = "blue"; head = "接続中（\(d["name"] ?? "")）"; percent = 0; barColor = "blue"; headColor = "ink"
            detail = d["name"] as? String ?? ""
        case "connected":
            head = "接続しました（MTU \(d["mtu"] ?? "")）"
            add("\(d["name"] ?? "") に接続しました（MTU \(d["mtu"] ?? "")）")
            if let t = d["detail"] as? String { detail = t }
        case "detail":
            detail = d["text"] as? String ?? detail
        case "time_set":
            add((d["already"] as? Bool ?? false) ? "時刻は設定済みです" : "時刻を設定しました（\(d["when"] ?? "")）")
        case "rx_start":
            mode = d["mode"] as? String
            lastPctLogged = 0
            let pages = d["pages"] as? Int ?? 0
            if mode == "setup" {
                dot = "blue"; barColor = "blue"; head = "受信中 0%（装着確認）"
                add("受信開始: 装着確認（\(pages) ページ / \(pages * P.pageUser) バイト）")
            } else {
                dot = "red"; barColor = "red"; head = "受信中 0%（異常通知 #\(d["no"] ?? 0)）"
                add("受信開始: 異常通知 #\(d["no"] ?? 0)（\(pages) ページ / \(pages * P.pageUser) バイト）")
            }
            percent = 0
        case "progress":
            let pct = max(0, min(100, d["pct"] as? Int ?? 0))
            percent = pct
            progress = d["text"] as? String ?? ""
            if mode == "setup" { head = "受信中 \(pct)%（装着確認）" }
            if mode == "event" { head = "受信中 \(pct)%（異常通知 #\(d["no"] ?? 0)）" }
            if let c = d["cells"] as? [Int?] { cells = c }
            let step = (pct / 20) * 20
            if step >= 20 && step < 100 && step > lastPctLogged {
                lastPctLogged = step
                add(" 受信中… \(step)%")
            }
        case "electrode":
            if d["ok"] as? Bool ?? false {
                warn = ""
                add("電極 OK")
            } else {
                warn = "電極 NG：貼付を確認してください"
                add(warn)
            }
        case "waiting":
            dot = "red"; barColor = "red"; headColor = "ink"
            head = (d["text"] as? String) ?? "記録待ち（検知後 15 秒）"
            percent = d["pct"] as? Int ?? 0
            if d["first"] as? Bool ?? false { add("記録待ち（検知後 15 秒）…") }
        case "resend":
            add("再送を要求しました（ページ \(d["seq"] ?? 0)）")
        case "saving":
            head = "保存中…"; percent = 100
        case "saved":
            add("受信 \(d["name"] ?? "").csv（\(d["samples"] ?? 0) 点、BLE）＋ _meta.csv")
        case "done":
            dot = "green"; barColor = "green"; percent = 100; headColor = "ink"
            head = "受信完了（\(d["count"] ?? 1) 件目）"
            add("受信完了（\(d["count"] ?? 1) 件目）。次の通知を待っています")
            doneAt = clock.now()
        case "message":
            add(d["text"] as? String ?? "")
            if let h = d["head"] as? String { head = h }
        case "disconnected":
            dot = "orange"; barColor = "gray"; headColor = "orange"
            head = (d["text"] as? String) ?? "受信の途中で ECG パッチとの接続が切れました"
            add(head)
            doneAt = clock.now()
        case "error":
            dot = "orange"; headColor = "orange"
            head = d["text"] as? String ?? ""
            add(head)
        default:
            break
        }
    }

    func tick(scanning: Bool) {
        if let d = doneAt, clock.now() - d >= 3, scanning {
            doneAt = nil
            onEvent("scan")
        }
    }
}

// MARK: - スキャン（A03）

final class Scanner {
    let settings: Settings, clock: Clock
    var table: [String: AdvInfo] = [:]
    var cooldown: [String: Double] = [:]         // この時刻まで自動接続しない
    var seen = Set<String>()
    var advLogged: [String: String] = [:]
    var alertLogged = Set<String>()
    var emulatorHint = false

    init(settings: Settings, clock: Clock) { self.settings = settings; self.clock = clock }

    func onAdv(_ info: AdvInfo) -> Bool {
        let new = !seen.contains(info.peer)
        seen.insert(info.peer)
        table[info.peer] = info
        return new
    }

    func devices() -> [AdvInfo] {
        let now = clock.now()
        for (p, a) in table where now - a.lastSeen > settings.listExpireS {
            table[p] = nil
            seen.remove(p)
        }
        return table.values.sorted { $0.lastSeen > $1.lastSeen }
    }

    func autoTarget() -> AdvInfo? {
        let now = clock.now()
        for a in devices() {
            if (cooldown[a.peer] ?? 0) > now { continue }
            if !settings.isRegistered(a.deviceID) { continue }
            if (a.event == EV.elec || a.event == EV.arr) && settings.autoConnectAlert { return a }
            if a.unnotified && settings.autoFetchUnnotified { return a }
        }
        return nil
    }
}

// MARK: - 接続の状態

final class ConnCtx {
    let info: AdvInfo
    let t0: Double
    let patientID: String?
    let auto: Bool
    var chars = Set<String>()
    var subPending = 0
    var status: DevStatus?
    var ready = false
    var powerOff = false
    var invalidated = false
    var tConn: Double?                  // didConnect の時刻（Debug Info は 1 秒以降の値を使う：別紙 2 未決 8）
    var dbgRereadAt: Double?
    var discReason: String?
    var discDeadline: Double?
    var mtu: Int?
    var timeAnchor: Date?               // SET_TIME で送った時刻
    var dbg: DebugInfo?
    var battery: Int?
    var rssi: [Int] = []
    var rxBytes = 0
    var tWall: Date?
    var ecgFirst: Double?, ecgLast: Double?
    var ecgActive = 0.0                 // 受信していた時間（0.5 秒以上の空きは含めない）
    var earlyEvents: [EventRec] = []

    init(info: AdvInfo, t0: Double, patientID: String?, auto: Bool) {
        self.info = info; self.t0 = t0; self.patientID = patientID; self.auto = auto
    }
}

struct PatchRow: Identifiable {
    let id: String          // peer
    let label: String
    let kind: String
    let sub: String
    let button: String
    let color: String
}

/// SC5（異常通知画面）の表示元
struct AlertModel {
    var device: String
    var title: String = ""
    var time: String = ""
    var counter: String = ""
    var chips: [(String, String)] = []
    var steps: [(String, String)] = []
    var canView = false
    var saved: SavedItem?
    var error = ""
    var t0: Double
}

/// 画面への合図（UI がまとめて読む）
struct UISignals {
    var showAlert = false               // SC5 を出す
    var alertSound = false              // 効果音・HUD
    var askEmulator: String? = nil      // 模擬器を受け入れるかの確認
    var setupTimeoutAsk = false         // 電極 NG：続ける／中止
    var setupDone = false
    var lastSaved: SavedItem? = nil
}

// MARK: - SessionManager

final class SessionManager {
    let t: BleTransport
    let clock: Clock
    let store: FileStore
    let settings: Settings
    let trace: Trace
    let monitor: BleMonitor?
    let recorder: BleTraceRecorder?
    var state: MgrState = .idle
    var btState = "unknown"
    let scanner: Scanner
    lazy var sender = CommandSender(self)
    let status: StatusPanel
    let exporter: Exporter
    var conn: ConnCtx?
    var session: BaseSession?
    var savedList: [SavedItem] = []
    var alert: AlertModel?
    var ui = UISignals()
    private var slogObj: SessionLog?
    private var lastRSSIt = 0.0
    private var timePending: (Double, (Int?, Date) -> Void)?
    private var nandRetried = false

    init(transport: BleTransport, clock: Clock, store: FileStore, settings: Settings, trace: Trace,
         monitor: BleMonitor?, recorder: BleTraceRecorder?) {
        t = transport; self.clock = clock; self.store = store; self.settings = settings; self.trace = trace
        self.monitor = monitor; self.recorder = recorder
        scanner = Scanner(settings: settings, clock: clock)
        status = StatusPanel(clock: clock)
        exporter = Exporter(store: store, clock: clock, settings: settings)
        transport.clock = clock
    }

    // MARK: 記録

    func err(_ e: Error, _ whr: String) {
        trace.w("ERR", whr, "\(type(of: e))", "\(e)")
    }

    func slog(_ kind: String, _ text: String) {
        trace.v(kind, text)
        slogObj?.write(kind, text)
    }

    func emitApp(_ kind: String, _ ev: String, _ v: [String: Any] = [:]) {
        t.emit(kind, ev, peer: conn?.info.deviceID ?? "", v)
    }

    // MARK: 起動・BLE 状態

    func start() {
        t.start { [weak self] s in self?.onBTState(s) }
    }

    private func onBTState(_ name: String) {
        btState = name
        trace.w("BT", name)
        status.onEvent("bt_state", ["state": name])
        if name == "poweredOn" {
            if state == .idle || state == .scanning { startScan() }
        } else {
            if conn != nil {                     // 16.2：Bluetooth OFF → 中止して保存
                session?.onLinkLost()
                cleanupConn("bt_off")
            }
            state = .idle
        }
    }

    func startScan() {
        state = .scanning
        t.scanStart { [weak self] peer, name, mfr, rssi in self?.onAdv(peer, name, mfr, rssi) }
        status.onEvent("scan")
        trace.w("SCAN", "start")
    }

    private func onAdv(_ peer: String, _ name: String?, _ mfr: [UInt8]?, _ rssi: Int) {
        let now = clock.now()
        var info: AdvInfo?
        do {
            info = try parseAdv(peer: peer, mfr: mfr, name: name, rssi: rssi, now: now, acceptEmulator: settings.acceptEmulator)
        } catch {
            trace.v("ADV", "invalid", peer, error)
            return
        }
        // 広告の内容（名前・Event・状態・受け入れ）が変わったときに 1 行記録
        let sig = "\(name ?? "-")/\(info != nil)/\(info?.event ?? -1)/\(info?.sub ?? -1)/\(info?.status ?? -1)"
        if scanner.advLogged[peer] != sig {
            scanner.advLogged[peer] = sig
            var line = "name=\(name ?? "None") mfr=\(mfr == nil ? "none" : String(mfr!.count)) rssi=\(rssi) accepted=\(info != nil)"
            if let i = info {
                line += String(format: " event=%02X sub=%02X status=%02X reg=%@", i.event, i.sub, i.status,
                               settings.isRegistered(i.deviceID) ? "True" : "False")
            }
            trace.w("ADV", line)
        }
        guard let a = info else {
            if isEmulatorName(name) && !scanner.emulatorHint {
                scanner.emulatorHint = true
                status.onEvent("message", ["text": "模擬器を検出しました（設定「模擬器を受け入れる」を ON に）"])
                ui.askEmulator = name
            }
            return
        }
        t.emit("cb", "adv", peer: a.deviceID, ["name": a.label, "rssi": rssi, "event": a.event, "status": a.status])
        if scanner.onAdv(a) {
            status.onEvent("found", ["name": a.label, "kind": kindText(a.event) + (a.emulator ? "・模擬器" : "")])
            trace.v("SCAN", String(format: "%@ event=%02X batt=%d rssi=%d%@", a.deviceID, a.event, a.battery, rssi,
                                   a.emulator ? " emulator" : ""))
        }
        if state == .scanning, let tgt = scanner.autoTarget() {
            if tgt.event == EV.elec || tgt.event == EV.arr {
                status.onEvent("alert", ["name": tgt.label])
                ui.alertSound = true
            }
            beginConnect(tgt, patientID: nil, auto: true)
        }
    }

    func patchRows() -> [PatchRow] {
        var rows: [PatchRow] = []
        for a in scanner.devices().prefix(3) {
            let reg = settings.isRegistered(a.deviceID)
            let kind: String, btn: String, color: String
            if a.event == EV.elec || a.event == EV.arr {
                kind = reg ? eventTitle(a.event, a.sub) : "未登録パッチの異常通知"
                let key = "\(a.peer)/\(a.event)/\(a.sub)/\(reg)"
                if !scanner.alertLogged.contains(key) {          // 異常通知の表示を記録
                    scanner.alertLogged.insert(key)
                    trace.w("LIST", "\(a.label) \(kind)（\(reg ? "登録済み" : "未登録")）")
                }
                btn = reg && settings.autoConnectAlert ? "自動接続" : (reg ? "接続" : "")
                color = "red"
            } else {
                kind = kindText(a.event)
                btn = "接続"
                color = a.event == EV.setup ? "blue" : "ink"
            }
            let batt = a.battery == 0xFF ? "—" : "\(a.battery)"
            let sub = "電池 \(batt)%・RSSI \(a.rssi)・\(a.timeSet ? "時刻設定済み" : "時刻未設定")"
                + (a.electrodeNG ? "・電極 NG" : "") + (a.emulator ? "・模擬器" : "")
            rows.append(PatchRow(id: a.peer, label: a.label, kind: kind, sub: sub, button: btn, color: color))
        }
        return rows
    }

    // MARK: 利用者の操作

    func connectTo(_ peer: String, patientID: String?) {
        if state != .scanning {
            trace.w("CONNECT", "ignored in", state.rawValue)
            if state == .idle && btState == "poweredOn" { startScan() }     // 探索が止まっていたら再開する
            return
        }
        guard let info = scanner.table[peer] else {
            trace.w("CONNECT", "peer not in list", peer)
            return
        }
        scanner.cooldown[peer] = nil         // 「接続」は待ち時間（直前の切断から 15 秒）より優先
        if (info.event == EV.elec || info.event == EV.arr) && !settings.isRegistered(info.deviceID) {
            status.onEvent("message", ["text": "未登録パッチの異常通知は表示のみです"])
            return
        }
        beginConnect(info, patientID: patientID, auto: false)
    }

    /// 「中止」：STOP を送り、受信分を _partial で保存して切断
    func userStop() {
        guard let s = session, conn != nil else { return }
        sender.cancelAll()
        sender.send(Command.stop()) { [weak self] _ in
            guard let self = self else { return }
            if s.saved == nil {
                if let ss = s as? SetupSession, let w = ss.window {
                    ss.saved = try? self.saveRecord(w, raw: ss.raw, event: nil, why: "setup_stop")
                } else {
                    s.savePartial("stop")
                }
            }
            self.disconnect("user_stop")
        }
    }

    func setupContinue() { (session as? SetupSession)?.userContinue() }

    // MARK: 接続（9.2 節の手順 1〜10）

    private func beginConnect(_ info: AdvInfo, patientID: String?, auto: Bool) {
        t.scanStop()
        state = .connecting
        let c = ConnCtx(info: info, t0: clock.now(), patientID: patientID, auto: auto)
        c.tWall = clock.localDate()
        conn = c
        sender = CommandSender(self)
        nandRetried = false
        let d = clock.localDate()
        slogObj = SessionLog(store: store, clock: clock,
                             rel: "ECG/\(DF.s(d, "yyyyMMdd"))/session_\(DF.s(d, "yyyyMMdd-HHmmss")).log")
        slog("SCAN", String(format: "%@ event=%02X batt=%d rssi=%d", info.deviceID, info.event, info.battery, info.rssi))
        status.onEvent("connecting", ["name": info.label])
        if info.emulator { t.emit("app", "emulator", peer: info.deviceID) }
        t.emit("op", "connect", peer: info.deviceID)
        if auto && (info.event == EV.elec || info.event == EV.arr) {
            var a = AlertModel(device: info.label, t0: clock.now())
            a.title = eventTitle(info.event, info.sub)
            a.steps = [("active", "通知を受信（Event Log）"), ("todo", "記録待ち（検知後 15 秒）"), ("todo", "波形を受信中"),
                       ("todo", "保存（ECG＋メタ＋イベント）"), ("todo", "パッチへ確認（ACK）")]
            alert = a
            ui.showAlert = true
        }
        t.connect(info.peer, onConnected: { [weak self] in self?.onConnected() },
                  onFailed: { [weak self] e in self?.onFailed(e) },
                  onDisconnected: { [weak self] e in self?.finalize(e) },
                  onInvalidated: { [weak self] in self?.onInvalidated() })
    }

    private func onConnected() {
        guard let c = conn else { return }
        state = .discovering
        c.tConn = clock.now()
        slog("CONNECT", "\(c.info.deviceID) connected")
        t.discover { [weak self] chars in self?.onDiscovered(chars) }
    }

    private func onDiscovered(_ chars: Set<String>) {
        guard let c = conn else { return }
        c.chars = chars
        let lack = [UUIDs.chStream, UUIDs.chEventLog, UUIDs.chStatus, UUIDs.chControl].filter { !chars.contains($0) }
            .map { UUIDs.short[$0] ?? $0 }
        if !lack.isEmpty {                       // A04-04
            err(ProtocolError("missing characteristics: \(lack.joined(separator: ","))"), "discover")
            fail("パッチの特性が足りません（\(lack.joined(separator: ","))）", "protocol", sendStop: false)
            return
        }
        c.subPending = 3
        let st: (String, BleErr?) -> Void = { [weak self] u, e in self?.onSubState(u, e) }
        t.subscribe(UUIDs.chStatus, onValue: { [weak self] _, d in self?.onStatusValue(d) }, onState: st)
        t.subscribe(UUIDs.chEventLog, onValue: { [weak self] _, d in self?.onEventLog(d) }, onState: st)
        t.subscribe(UUIDs.chStream, onValue: { [weak self] _, d in self?.onStream(d) }, onState: st)
    }

    private func onSubState(_ uuid: String, _ e: BleErr?) {
        guard let c = conn else { return }
        if let e = e { trace.w("SUBSCRIBE", UUIDs.short[uuid] ?? uuid, e) }
        c.subPending -= 1
        if c.subPending == 0 { t.read(UUIDs.chStatus) { [weak self] _, d in self?.onFirstStatus(d) } }
    }

    private func onFirstStatus(_ d: [UInt8]) {
        guard let c = conn else { return }
        do { c.status = try DevStatus(d) } catch {
            err(error, "status")
            fail("Device Status を読めませんでした", "protocol", sendStop: false)
            return
        }
        if c.chars.contains(UUIDs.chSerial) {
            t.read(UUIDs.chSerial) { [weak self] _, d in
                let sn = String(decoding: d, as: UTF8.self).trimmingCharacters(in: CharacterSet(charactersIn: "\u{0} ")).uppercased()
                self?.verifyID(sn)
            }
        } else if c.chars.contains(UUIDs.chEmu) && settings.acceptEmulator {
            t.read(UUIDs.chEmu) { [weak self] _, d in
                guard let self = self else { return }
                if d.count < 4 {
                    self.fail("Emulator Info が不正です", "protocol", sendStop: false)
                    return
                }
                self.verifyID(String(format: "%08X", d.u32(0)))
            }
        } else {
            fail("デバイス ID を照合できませんでした", "id_unknown", sendStop: false)
        }
    }

    private func verifyID(_ sn: String) {
        guard let c = conn else { return }
        if sn != c.info.deviceID {               // A04-03：誤接続防止
            slog("CONNECT", "ID mismatch adv=\(c.info.deviceID) read=\(sn)")
            fail("デバイス ID が一致しません（\(sn)）", "id_mismatch", sendStop: false)
            return
        }
        onReady()
    }

    private func onReady() {
        guard let c = conn, let st = c.status else { return }
        c.ready = true
        c.mtu = t.maxWriteLen() + 3
        t.emit("app", "mtu", peer: c.info.deviceID, ["mtu": c.mtu!])
        let ms = Int((clock.now() - c.t0) * 1000)
        slog("CONNECT", "\(c.info.deviceID) mtu=\(c.mtu!) ready \(ms) ms\(c.info.emulator ? " (emulator)" : "")")
        if c.chars.contains(UUIDs.chDebug) {
            t.read(UUIDs.chDebug) { [weak self] _, d in self?.onDebug(d) }
            if settings.devDisplay { t.subscribe(UUIDs.chDebug, onValue: { [weak self] _, d in self?.onDebug(d) }, onState: nil) }
        }
        if c.chars.contains(UUIDs.chBatt) {
            t.read(UUIDs.chBatt) { [weak self] _, d in if let b = d.first { self?.conn?.battery = Int(b) } }
        }
        status.onEvent("connected", ["name": c.info.label + (c.info.emulator ? "（模擬器）" : ""), "mtu": c.mtu!,
                                     "detail": detailText()])
        let s: BaseSession
        if st.state == ST.s2 && !(c.auto && (c.info.event == EV.elec || c.info.event == EV.arr)) {
            s = SetupSession(self, patientID: c.patientID)
        } else if st.unnotified > 0 || st.state == ST.s5 {
            s = EventSession(self)
        } else {
            status.onEvent("message", ["text": "処理するデータがありません（状態 S\(st.state)）"])
            disconnect("nothing")
            return
        }
        session = s
        s.onStatus(st)
        for ev in c.earlyEvents { s.onEvent(ev) }      // 購読直後に届いた Event Log
        c.earlyEvents = []
        s.start()
    }

    func detailText() -> String {
        guard let c = conn else { return "Bluetooth: " + (btState == "poweredOn" ? "オン" : btState) }
        let st = c.status
        var parts = [c.info.label + (c.info.emulator ? "（模擬器）" : "")]
        if let m = c.mtu { parts.append("MTU \(m)") }
        if let b = c.battery ?? st?.batt, b != 0xFF { parts.append("電池 \(b)%") }
        if let st = st { parts.append("電極 " + (st.electrode == 0 ? "OK" : "NG")) }
        if settings.devDisplay {
            if let r = c.rssi.last { parts.append("RSSI \(r)") }
            if let d = c.dbg { parts.append(String(format: "%.0fms・%@", d.intervalMs, d.phyText)) }
        }
        return parts.joined(separator: "・")
    }

    // MARK: 受信の振り分け（A06）

    private func onStatusValue(_ d: [UInt8]) {
        let c = conn
        let st: DevStatus
        do { st = try DevStatus(d) } catch { err(error, "status"); return }
        t.emit("rx", "notify", peer: c?.info.deviceID ?? "", ch: "STS", data: d,
               ["result": st.result, "last_cmd": st.lastCmd, "state": st.state])
        guard let cc = c else { return }
        let prev = cc.status
        cc.status = st
        if st.state == ST.s6 { cc.powerOff = true }
        sender.onStatus(st)
        session?.onStatus(st)
        if prev == nil || prev!.electrode != st.electrode || prev!.batt != st.batt {
            status.onEvent("detail", ["text": detailText()])
        }
    }

    private func onEventLog(_ d: [UInt8]) {
        t.emit("rx", "indicate", peer: conn?.info.deviceID ?? "", ch: "EVT", data: d)
        let ev: EventRec
        do { ev = try EventRec(d) } catch { err(error, "event"); return }
        if let s = session { s.onEvent(ev) } else { conn?.earlyEvents.append(ev) }
    }

    private func onStream(_ d: [UInt8]) {
        if let c = conn {
            c.rxBytes += d.count
            let now = clock.now()
            if c.ecgFirst == nil { c.ecgFirst = now } else if let l = c.ecgLast, now - l < 0.5 { c.ecgActive += now - l }
            c.ecgLast = now
        }
        t.emit("rx", "notify", peer: conn?.info.deviceID ?? "", ch: "ECG", data: d)
        session?.onFragment(d)
    }

    private func onDebug(_ d: [UInt8]) {
        guard let c = conn, let info = DebugInfo(d) else { return }
        if info.ver != 1 {
            trace.w("DBG", "ver", info.ver)
            return
        }
        if let tc = c.tConn, clock.now() - tc < 1.0 {
            // 別紙 2 未決 8：接続直後の値（接続間隔 0・RSSI 127・MTU 23）は更新前のことがあるため、1 秒後に読み直す
            if c.dbgRereadAt == nil { c.dbgRereadAt = tc + 1.0 }
            if c.dbg == nil { c.dbg = info }
            return
        }
        c.dbg = info
        t.emit("dbg", "debug_info", peer: c.info.deviceID, ch: "DBG",
               ["int_ms": info.intervalMs, "lat": info.slaveLatency, "to_s": info.timeoutS, "phy": info.phyText,
                "mtu": info.attMtu, "tx_free": info.txFree, "rssi": info.rssi, "busy": info.notifyBusy,
                "last_disc": info.lastDisc])
    }

    // MARK: 異常通知画面（SC5）の元データ

    func alertUpdate(_ ev: EventRec, _ s: EventSession) {
        if alert == nil {
            alert = AlertModel(device: conn?.info.label ?? "", t0: clock.now())
            ui.showAlert = true
        }
        guard var a = alert else { return }
        a.title = ev.title
        a.time = ev.timeText
        a.counter = "未通知 \(max(s.total, s.no)) 件中 \(s.no) 件目"
        if ev.event == EV.arr {
            a.chips = [("\(ev.rrMs) ms", "R-R"), ("\(ev.medianMs) ms", "直前の中央値"), ("\(ev.hrBpm)", "心拍/分")]
        } else {
            let b = conn?.status?.batt ?? 0xFF
            a.chips = [(ELEC_SUB[ev.sub & 0x7F] ?? "—", "判定"),
                       (ev.timeText.count > 11 ? String(ev.timeText.dropFirst(11)) : "—", "検知時刻"),
                       (b != 0xFF ? "\(b)%" : "—", "電池")]
        }
        a.steps = Array(zip(s.steps, s.stepText))
        a.canView = s.steps[3] == "done"
        if let ls = s.lastSaved { a.saved = ls }
        alert = a
    }

    // MARK: 保存

    @discardableResult
    func saveRecord(_ r: Record, raw: [Int: [UInt8]], event: EventRec?, why: String, extra: [String: Any] = [:]) throws -> SavedItem {
        let dev = conn?.info.deviceID ?? "UNKNOWN"
        let item = try exporter.save(r, deviceID: dev, raw: raw, event: event, extra: extra)
        slog("SAVE", "\(item.name).csv (\(r.ecg.count)) \(why)")
        status.onEvent("saved", ["name": item.name, "samples": r.ecg.count])
        savedList.insert(item, at: 0)
        ui.lastSaved = item
        if alert != nil && event != nil { alert!.saved = item }
        return item
    }

    func onSetupAcked() {
        guard let c = conn else { return }
        settings.register(c.info.deviceID, result: "ok", now: clock.localDate())
        slog("SETUP", "装着確認が完了しました。計測を開始しました")
        status.onEvent("done", ["count": 1])
        status.onEvent("message", ["text": "装着確認が完了しました。計測を開始しました"])
        ui.setupDone = true
    }

    // MARK: 失敗・切断（A07）

    /// 9.4 節の結果コードの処理
    func resultError(_ r: Int?, _ what: String, retry: (() -> Void)? = nil) {
        let name = Res.name(r)
        slog("RESULT", "\(what) -> \(name)")
        if r == Res.badLen || r == Res.unknown {
            err(ProtocolError("\(what) \(name)"), "result")
            fail("本アプリの不具合（\(what) \(name)）", "bad_cmd", sendStop: false)
        } else if r == Res.badState {
            t.read(UUIDs.chStatus) { [weak self] _, d in
                guard let self = self else { return }
                let st = try? DevStatus(d)
                if let st = st { self.conn?.status = st }
                self.status.onEvent("message", ["text": "パッチの状態が合いません（S\(st.map { String($0.state) } ?? "?")）"])
                self.disconnect("bad_state")
            }
        } else if r == Res.nand, let rt = retry, !nandRetried {
            nandRetried = true
            status.onEvent("message", ["text": "パッチ側の NAND 異常（再試行します）"])
            rt()
        } else if r == Res.busy {
            fail("パッチが別の転送中です", "busy")
        } else {
            fail("パッチがエラーを返しました（\(what) \(name)）", "result")
        }
    }

    func fail(_ text: String, _ reason: String, sendStop: Bool = true) {
        status.onEvent("error", ["text": text])
        if alert != nil { alert!.error = text }
        if let s = session, s.saved == nil { s.onLinkLost() }
        if sendStop, let c = conn, c.ready {
            sender.cancelAll()
            sender.send(Command.stop()) { [weak self] _ in self?.disconnect(reason) }
        } else {
            disconnect(reason)
        }
    }

    /// SET_TIME は秒までしか送れないため、秒の変わり目を待って送る。cb(結果, 送った時刻)
    func sendSetTime(_ cb: @escaping (Int?, Date) -> Void) {
        let us = DF.microsecond(clock.localDate())
        if us < 30000 {
            sendSetTimeNow(cb)
            return
        }
        let wait = 1.0 - Double(us) / 1e6
        timePending = (clock.now() + wait, cb)
        slog("CMD", String(format: "SET_TIME は %.2f 秒後（秒の変わり目）に送ります", wait))
    }

    private func sendSetTimeNow(_ cb: @escaping (Int?, Date) -> Void) {
        var sentAt = clock.localDate()
        let make: () -> Command = { [unowned self] in    // 実際に書き込む時刻で作る（再送・待たされた場合も古い時刻を送らない）
            var d = self.clock.localDate()
            let us = DF.microsecond(d)
            if us >= 500000 { d = d.addingTimeInterval(Double(1000000 - us) / 1e6) }   // 早く起きた場合は次の秒に丸める
            d = Date(timeIntervalSince1970: floor(d.timeIntervalSince1970))
            sentAt = d
            return Command.setTime(d)
        }
        let first = make()
        sender.send(first, make: make) { r in cb(r, sentAt) }
    }

    /// SET_TIME を秒の変わり目まで待っている間も「送信中」とみなす
    var cmdBusy: Bool { sender.busy || timePending != nil }

    func disconnect(_ reason: String) {
        guard let c = conn, state != .disconnecting else { return }
        state = .disconnecting
        c.discReason = reason
        c.discDeadline = clock.now() + P.discTimeout
        slog("DISC", "request reason=\(reason)")
        t.emit("op", "disconnect", peer: c.info.deviceID, ["reason": reason])
        t.disconnect()
    }

    private func onFailed(_ e: BleErr?) {
        trace.w("CONNECT", "failed", e?.description ?? "")
        slog("CONNECT", "failed \(e?.description ?? "")")
        if conn != nil && alert != nil { alert!.error = "接続できませんでした（パッチ側に未通知として残ります）" }
        status.onEvent("error", ["text": "パッチに接続できませんでした"])
        cleanupConn("connect_failed")
    }

    /// didModifyServices：模擬器の電源 OFF・GATT 変更 → パッチ側の終了として切断
    private func onInvalidated() {
        guard let c = conn else { return }
        c.invalidated = true
        slog("DISC", "services invalidated")
        session?.onLinkLost()
        disconnect("services_invalidated")
    }

    private func finalize(_ e: BleErr?) {
        guard let c = conn else { return }
        let s = session
        var judge: String
        var text: String?
        if let s = s, s.ackedAll {
            judge = "normal"
        } else if c.powerOff || c.invalidated {
            judge = "power_off"
            text = "ECG パッチの電源が切れました"
            s?.onLinkLost()
        } else if let r = c.discReason, ["nothing", "user_stop", "bad_state", "state"].contains(r) {
            judge = r
            text = r == "nothing" ? nil : "接続を終了しました（\(r)）"
        } else if let s = s, !s.done {
            judge = "lost"
            text = "受信の途中で ECG パッチとの接続が切れました"
            s.onLinkLost()
        } else {
            judge = c.discReason ?? "remote"
            text = c.discReason == "ack_done" ? nil : "接続が切れました"
        }
        slog("DISC", "reason=\(c.discReason ?? "remote") judge=\(judge) err=\(e?.description ?? "None") (status S\(c.status.map { String($0.state) } ?? "?"))")
        t.emit("cb", "didDisconnect", peer: c.info.deviceID, err: e, ["judge": judge, "unexpected": judge == "lost"])
        if let tx = text {
            status.onEvent("disconnected", ["text": tx])
            if alert != nil && judge != "normal" && alert!.error.isEmpty { alert!.error = tx }
        }
        if judge == "normal" && s is EventSession {
            settings.register(c.info.deviceID, result: nil, now: clock.localDate())
        }
        summary(c, judge)
        cleanupConn(judge)
    }

    private func summary(_ c: ConnCtx, _ judge: String) {
        guard let rec = recorder else { return }
        let s = session
        let rs = c.rssi.isEmpty ? [0] : c.rssi
        // 受信速度＝ECG の受信バイト ÷ 受信していた時間（転送の合間の待ちは除く）
        let rate: String = c.ecgActive > 0.05 ? String(format: "%.1f", Double(c.rxBytes) / c.ecgActive / 1000) : ""
        rec.appendSummary([DF.s(c.tWall ?? clock.localDate()), c.info.deviceID, monitor?.connectMs.map { String($0) } ?? "",
                           c.mtu.map { String($0) } ?? "", String(format: "%.1f", Double(rs.reduce(0, +)) / Double(rs.count)),
                           c.rxBytes, rate, s?.asm.gapsAndDups ?? 0, s?.crcNG ?? 0, s?.reqPagesSent ?? 0, judge])
    }

    private func cleanupConn(_ judge: String) {
        if let c = conn {
            scanner.cooldown[c.info.peer] = clock.now() + 15
            scanner.table[c.info.peer] = nil
            scanner.seen.remove(c.info.peer)
        }
        conn = nil
        session = nil
        timePending = nil
        sender.cancelAll()
        slogObj = nil
        if btState == "poweredOn" { startScan() } else { state = .idle }
    }

    // MARK: 周期処理（タイムアウト：9.7 節）

    func tick() {
        let now = clock.now()
        if let c = conn {
            if state == .disconnecting {
                if let d = c.discDeadline, now > d {
                    trace.w("DISC", "force cleanup")              // A07-04
                    finalize(BleErr(domain: "app", code: 0, name: "disconnect_timeout"))
                }
                return
            }
            if !c.ready && now - c.t0 > P.connectTimeout {
                emitApp("warn", "timeout", ["what": "CONNECT"])
                if alert != nil { alert!.error = "接続できませんでした（パッチ側に未通知として残ります）" }
                fail("パッチに接続できませんでした（\(Int(P.connectTimeout)) 秒）", "connect_timeout", sendStop: false)
                return
            }
            if let at = c.dbgRereadAt, now >= at, c.ready {
                c.dbgRereadAt = nil
                t.read(UUIDs.chDebug) { [weak self] _, d in self?.onDebug(d) }
            }
            if let tp = timePending, now >= tp.0 {
                timePending = nil
                sendSetTimeNow(tp.1)
            }
            sender.tick()
            session?.tick()
            if c.ready && now - lastRSSIt >= 1.0 {
                lastRSSIt = now
                t.readRSSI { [weak self] r in
                    guard let self = self, let c = self.conn, r != 127 else { return }
                    c.rssi.append(r)
                    if c.rssi.count > 30 { c.rssi.removeFirst() }
                    self.t.emit("cb", "rssi", peer: c.info.deviceID, ["rssi": r])
                }
            }
        }
        status.tick(scanning: state == .scanning)
    }

    /// 16.1：終了時は STOP → 受信済みを保存 → 切断
    func shutdown() {
        if let c = conn, c.ready, let s = session, !s.done {
            sender.cancelAll()
            t.write(UUIDs.chControl, Command.stop().bytes) { _ in }
        }
        if let s = session, s.saved == nil {
            if let ss = s as? SetupSession, let w = ss.window {
                _ = try? saveRecord(w, raw: ss.raw, event: nil, why: "shutdown")
            } else {
                s.savePartial("shutdown")
            }
        }
        t.scanStop()
        if conn != nil { t.disconnect() }
    }

    // 画面用
    var setupSession: SetupSession? { session as? SetupSession }
    var inSession: Bool { session != nil || state == .connecting || state == .discovering }
    var previewSamples: [Int16] {
        guard let s = session, let hi = s.pages.keys.max() else { return [] }
        var buf: [Int16] = []
        for seq in [hi - 1, hi] {
            if let p = s.pages[seq] { buf.append(contentsOf: p.samples.prefix(p.nCycles * P.cycle)) }
        }
        return buf
    }
}
