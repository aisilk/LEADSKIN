// Transport.swift — BLE トランスポートの抽象（Python 版 A02）。実機は CoreBluetoothTransport、試験は MockTransport。

import Foundation

protocol BleTransport: AnyObject {
    var observers: [BleObserver] { get set }
    var clock: Clock { get set }

    func start(onState: @escaping (String) -> Void)                                   // CBCentralManager の生成
    func scanStart(onAdv: @escaping (_ peer: String, _ name: String?, _ mfr: [UInt8]?, _ rssi: Int) -> Void)
    func scanStop()
    func connect(_ peer: String, onConnected: @escaping () -> Void, onFailed: @escaping (BleErr?) -> Void,
                 onDisconnected: @escaping (BleErr?) -> Void, onInvalidated: @escaping () -> Void)
    func discover(onDone: @escaping (Set<String>) -> Void)                             // 見つかった特性の UUID
    func subscribe(_ ch: String, onValue: @escaping (String, [UInt8]) -> Void, onState: ((String, BleErr?) -> Void)?)
    func read(_ ch: String, onValue: @escaping (String, [UInt8]) -> Void)
    func write(_ ch: String, _ data: [UInt8], onDone: @escaping (BleErr?) -> Void)    // 応答ありの Write
    func disconnect()
    func maxWriteLen() -> Int
    func readRSSI(onRSSI: @escaping (Int) -> Void)
}

extension BleTransport {
    func emit(_ kind: String, _ ev: String, peer: String = "", ch: String = "", data: [UInt8] = [],
              err: BleErr? = nil, _ v: [String: Any] = [:]) {
        let e = BleEvent(t: clock.now(), kind: kind, ev: ev, peer: peer, ch: ch, data: data, err: err, v: v)
        for o in observers { o.onBleEvent(e) }
    }
}
