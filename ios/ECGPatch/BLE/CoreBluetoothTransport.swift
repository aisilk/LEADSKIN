// CoreBluetoothTransport.swift — 実機の BLE（CoreBluetooth、セントラル）
//
// iOS からの通知はメインキューで受け、そのまま SessionManager に渡す（Pythonista 版の作業スレッド・
// 型固定 objc_msgSend などの回避策は不要）。

import CoreBluetooth
import Foundation

final class CoreBluetoothTransport: NSObject, BleTransport, CBCentralManagerDelegate, CBPeripheralDelegate {
    var observers: [BleObserver] = []
    var clock: Clock = SystemClock()

    private var cm: CBCentralManager?
    private var onState: ((String) -> Void)?
    private var onAdv: ((String, String?, [UInt8]?, Int) -> Void)?
    private var known: [UUID: CBPeripheral] = [:]          // 広告で見つけた相手（接続まで保持）
    private var periph: CBPeripheral?
    private var chars: [String: CBCharacteristic] = [:]
    private var pendingServices = 0
    private var onDiscovered: ((Set<String>) -> Void)?
    private var cbConnected: (() -> Void)?
    private var cbFailed: ((BleErr?) -> Void)?
    private var cbDisconnected: ((BleErr?) -> Void)?
    private var cbInvalidated: (() -> Void)?
    private var subValue: [String: (String, [UInt8]) -> Void] = [:]
    private var subState: [String: (String, BleErr?) -> Void] = [:]
    private var reads: [String: [(String, [UInt8]) -> Void]] = [:]
    private var writes: [(BleErr?) -> Void] = []
    private var onRSSI: ((Int) -> Void)?
    private(set) var scanning = false

    static let stateNames = ["unknown", "resetting", "unsupported", "unauthorized", "poweredOff", "poweredOn"]

    private var dev: String { periph?.identifier.uuidString.prefix(8).description ?? "" }

    private static func key(_ u: CBUUID) -> String { u.uuidString.uppercased() }

    private static func err(_ e: Error?) -> BleErr? {
        guard let e = e as NSError? else { return nil }
        let name = e.domain == CBErrorDomain ? (CB_ERRORS[e.code] ?? "") : e.localizedDescription
        return BleErr(domain: e.domain, code: e.code, name: name)
    }

    // MARK: BleTransport

    func start(onState: @escaping (String) -> Void) {
        self.onState = onState
        if cm == nil { cm = CBCentralManager(delegate: self, queue: .main) }
        emit("op", "create_central")
    }

    func scanStart(onAdv: @escaping (String, String?, [UInt8]?, Int) -> Void) {
        self.onAdv = onAdv
        guard let cm = cm, cm.state == .poweredOn else { return }
        cm.scanForPeripherals(withServices: [CBUUID(string: UUIDs.svcECG)],
                              options: [CBCentralManagerScanOptionAllowDuplicatesKey: true])
        scanning = true
        emit("op", "scan_start")
    }

    func scanStop() {
        if scanning { cm?.stopScan() }
        scanning = false
        emit("op", "scan_stop")
    }

    func connect(_ peer: String, onConnected: @escaping () -> Void, onFailed: @escaping (BleErr?) -> Void,
                 onDisconnected: @escaping (BleErr?) -> Void, onInvalidated: @escaping () -> Void) {
        cbConnected = onConnected; cbFailed = onFailed; cbDisconnected = onDisconnected; cbInvalidated = onInvalidated
        guard let id = UUID(uuidString: peer), let p = known[id] ?? cm?.retrievePeripherals(withIdentifiers: [id]).first else {
            onFailed(BleErr(domain: "app", code: 12, name: "unknownDevice"))
            return
        }
        periph = p
        chars = [:]; subValue = [:]; subState = [:]; reads = [:]; writes = []
        p.delegate = self
        cm?.connect(p, options: nil)
    }

    func discover(onDone: @escaping (Set<String>) -> Void) {
        onDiscovered = onDone
        periph?.discoverServices([CBUUID(string: UUIDs.svcECG), CBUUID(string: UUIDs.svcDIS), CBUUID(string: UUIDs.svcBAT)])
    }

    func subscribe(_ ch: String, onValue: @escaping (String, [UInt8]) -> Void, onState: ((String, BleErr?) -> Void)?) {
        subValue[ch] = onValue
        if let s = onState { subState[ch] = s }
        guard let p = periph, let c = chars[ch] else {
            onState?(ch, BleErr(domain: "app", code: 0, name: "no characteristic"))
            return
        }
        p.setNotifyValue(true, for: c)
        emit("op", "subscribe", peer: dev, ch: UUIDs.short[ch] ?? ch)
    }

    func read(_ ch: String, onValue: @escaping (String, [UInt8]) -> Void) {
        guard let p = periph, let c = chars[ch] else { return }
        reads[ch, default: []].append(onValue)
        p.readValue(for: c)
    }

    func write(_ ch: String, _ data: [UInt8], onDone: @escaping (BleErr?) -> Void) {
        guard let p = periph, let c = chars[ch] else {
            onDone(BleErr(domain: "app", code: 3, name: "notConnected"))
            return
        }
        writes.append(onDone)
        emit("tx", "write", peer: dev, ch: UUIDs.short[ch] ?? ch, data: data)
        p.writeValue(Data(data), for: c, type: .withResponse)
    }

    func disconnect() {
        if let p = periph { cm?.cancelPeripheralConnection(p) }
    }

    func maxWriteLen() -> Int { periph?.maximumWriteValueLength(for: .withoutResponse) ?? 20 }

    func readRSSI(onRSSI: @escaping (Int) -> Void) {
        self.onRSSI = onRSSI
        periph?.readRSSI()
    }

    // MARK: CBCentralManagerDelegate

    func centralManagerDidUpdateState(_ central: CBCentralManager) {
        let i = central.state.rawValue
        let name = i >= 0 && i < Self.stateNames.count ? Self.stateNames[i] : "unknown"
        emit("cb", "state", ["state": name])
        if central.state != .poweredOn { scanning = false }
        onState?(name)
    }

    func centralManager(_ central: CBCentralManager, didDiscover peripheral: CBPeripheral,
                        advertisementData: [String: Any], rssi RSSI: NSNumber) {
        known[peripheral.identifier] = peripheral
        let name = advertisementData[CBAdvertisementDataLocalNameKey] as? String ?? peripheral.name
        let mfr = (advertisementData[CBAdvertisementDataManufacturerDataKey] as? Data).map { [UInt8]($0) }
        onAdv?(peripheral.identifier.uuidString, name, mfr, RSSI.intValue)
    }

    func centralManager(_ central: CBCentralManager, didConnect peripheral: CBPeripheral) {
        guard peripheral == periph else { return }
        emit("cb", "didConnect", peer: dev)
        cbConnected?()
    }

    func centralManager(_ central: CBCentralManager, didFailToConnect peripheral: CBPeripheral, error: Error?) {
        guard peripheral == periph else { return }
        let e = Self.err(error)
        emit("cb", "didFailToConnect", peer: dev, err: e)
        periph = nil
        cbFailed?(e)
    }

    func centralManager(_ central: CBCentralManager, didDisconnectPeripheral peripheral: CBPeripheral, error: Error?) {
        guard peripheral == periph else { return }
        let e = Self.err(error)
        periph = nil
        chars = [:]
        writes = []
        cbDisconnected?(e)
    }

    // MARK: CBPeripheralDelegate

    func peripheral(_ peripheral: CBPeripheral, didDiscoverServices error: Error?) {
        let svcs = peripheral.services ?? []
        emit("cb", "didDiscoverServices", peer: dev, err: Self.err(error), ["n": svcs.count])
        pendingServices = svcs.count
        if svcs.isEmpty { finishDiscovery() }
        for s in svcs { peripheral.discoverCharacteristics(nil, for: s) }
    }

    func peripheral(_ peripheral: CBPeripheral, didDiscoverCharacteristicsFor service: CBService, error: Error?) {
        for c in service.characteristics ?? [] { chars[Self.key(c.uuid)] = c }
        pendingServices -= 1
        if pendingServices <= 0 { finishDiscovery() }
    }

    private func finishDiscovery() {
        let set = Set(chars.keys)
        emit("cb", "didDiscoverCharacteristics", peer: dev, ["chars": set.map { UUIDs.short[$0] ?? $0 }.sorted().joined(separator: ",")])
        let cb = onDiscovered
        onDiscovered = nil
        cb?(set)
    }

    func peripheral(_ peripheral: CBPeripheral, didUpdateNotificationStateFor characteristic: CBCharacteristic, error: Error?) {
        let k = Self.key(characteristic.uuid)
        let e = Self.err(error)
        emit("cb", "didUpdateNotificationState", peer: dev, ch: UUIDs.short[k] ?? k, err: e, ["on": characteristic.isNotifying])
        if let s = subState.removeValue(forKey: k) { s(k, e) }
    }

    func peripheral(_ peripheral: CBPeripheral, didUpdateValueFor characteristic: CBCharacteristic, error: Error?) {
        let k = Self.key(characteristic.uuid)
        let data = [UInt8](characteristic.value ?? Data())
        if error != nil {
            emit("cb", "didUpdateValue", peer: dev, ch: UUIDs.short[k] ?? k, err: Self.err(error))
        }
        if var q = reads[k], !q.isEmpty {           // 読出しの応答を先に渡す
            let cb = q.removeFirst()
            reads[k] = q
            if k != UUIDs.chStream && k != UUIDs.chStatus && k != UUIDs.chEventLog {
                emit("rx", "read", peer: dev, ch: UUIDs.short[k] ?? k, data: data)
            }
            cb(k, data)
            return
        }
        subValue[k]?(k, data)
    }

    func peripheral(_ peripheral: CBPeripheral, didWriteValueFor characteristic: CBCharacteristic, error: Error?) {
        let e = Self.err(error)
        if let e = e { emit("cb", "didWriteValue", peer: dev, ch: "CTL", err: e) }
        if !writes.isEmpty { writes.removeFirst()(e) }
    }

    func peripheral(_ peripheral: CBPeripheral, didReadRSSI RSSI: NSNumber, error: Error?) {
        onRSSI?(RSSI.intValue)
    }

    func peripheral(_ peripheral: CBPeripheral, didModifyServices invalidatedServices: [CBService]) {
        emit("cb", "didModifyServices", peer: dev, ["n": invalidatedServices.count])
        if invalidatedServices.contains(where: { $0.uuid == CBUUID(string: UUIDs.svcECG) }) {
            cbInvalidated?()
        }
    }
}
