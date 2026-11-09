# -*- coding: utf-8 -*-
"""
MockTransport：iPhone アプリ（ecg_patch_app.SessionManager）と パッチ模擬器のコア
（patch_emu.PatchCore）を、CoreBluetooth を使わずに PC 上でつなぐ（5.7 節 ①）。

  アプリ側からは BleTransport、模擬器側からは Hooks として見える。
  FakeClock で時間を進め、Env.run() で両方の周期処理を回す。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'iphone'))
sys.path.insert(0, os.path.join(HERE, '..', 'ipad'))

import ecg_patch_app as A      # noqa: E402
import patch_emu as E          # noqa: E402

ALL_CHARS = {A.CH_STREAM, A.CH_EVENTLOG, A.CH_STATUS, A.CH_CONTROL, A.CH_DEBUG, A.CH_EMU}


class MockTransport(A.BleTransport, E.Hooks):
    def __init__(self, core, clock, mtu=247, peer='PEER-1'):
        A.BleTransport.__init__(self)
        self.core = core
        core.hooks = self
        self.clock = clock
        self.mtu = mtu
        self.peer = peer
        self.scanning = False
        self.on_adv = None
        self.adv_name = None
        self.services = True
        self.connected = False
        self.cbs = {}
        self.subs = {}
        self.last_adv = -1.0
        self.chars = set(ALL_CHARS)
        self.no_discover_reply = False   # A04-02
        self.emu_id_override = None      # A04-03
        self.ops = []
        self.budget_per_tick = 12        # 1 tick（0.05 秒）に送れる Notify 数（約 58 KB/s）
        self.budget = self.budget_per_tick

    # ---- アプリ側（BleTransport）
    def start(self, on_state):
        self.post(on_state, 'poweredOn')

    def scan_start(self, service_uuid, on_adv):
        self.scanning = True
        self.on_adv = on_adv
        self.ops.append('scan_start')

    def scan_stop(self):
        self.scanning = False
        self.ops.append('scan_stop')

    def connect(self, peer_id, on_connected, on_failed, on_disconnected, on_invalidated=None):
        self.cbs = {'connected': on_connected, 'failed': on_failed, 'disconnected': on_disconnected,
                    'invalidated': on_invalidated}
        self.ops.append('connect')
        if not self.services or self.adv_name is None:
            self.post(on_failed, ('CBErrorDomain', 10, 'connectionFailed'))
            return
        self.connected = True
        self._emit('cb', 'didConnect')
        self.post(on_connected)

    def discover(self, services, on_done):
        self.ops.append('discover')
        if self.no_discover_reply:
            return
        self.post(on_done, set(self.chars))

    def subscribe(self, char_uuid, on_value, on_state=None):
        self.ops.append('subscribe:' + A.CH_SHORT.get(char_uuid, char_uuid))
        self.subs[char_uuid] = on_value
        self.core.on_subscribe(char_uuid, self.mtu - 3)
        if on_state:
            self.post(on_state, char_uuid, None)
        self.core.pump()

    def read(self, char_uuid, on_value):
        self.ops.append('read:' + A.CH_SHORT.get(char_uuid, char_uuid))
        data = self.core.on_read(char_uuid)
        if char_uuid == A.CH_EMU and self.emu_id_override is not None:
            data = E.EMUINFO.pack(self.emu_id_override, 1, 2, 0, 0, b'PATCHEMU')
        self.post(on_value, char_uuid, data)
        self.core.pump()

    def write(self, char_uuid, data, on_done):
        self._emit('tx', 'write', ch=A.CH_SHORT.get(char_uuid, ''), data=data)
        r = self.core.on_write(char_uuid, data)
        self.post(on_done, None if r == 0 else ('CBATTErrorDomain', r, 'att'))
        self.core.pump()

    def disconnect(self):
        self.ops.append('disconnect')
        if not self.connected:
            if self.cbs.get('disconnected'):
                self.post(self.cbs['disconnected'], None)
            return
        self.connected = False
        for u in list(self.subs):
            self.core.on_unsubscribe(u)
        self.subs.clear()
        self.post(self.cbs['disconnected'], None)

    def max_write_len(self):
        return self.mtu - 3

    def read_rssi(self):
        self._emit('cb', 'rssi', rssi=-60)

    def central_state(self):
        return 'poweredOn'

    # ---- 模擬器側（Hooks）
    def notify(self, uuid, data):
        if not self.connected or uuid not in self.subs:
            return True
        if self.budget <= 0:
            return False                 # 送信枠なし（iOS の updateValue が NO を返すのと同じ）
        self.budget -= 1
        self.post(self.subs[uuid], uuid, bytes(data))
        return True

    def set_adv(self, name):
        self.adv_name = name

    def remove_services(self):
        self.services = False
        self.adv_name = None
        if self.connected and self.cbs.get('invalidated'):
            self.post(self.cbs['invalidated'])

    def restore_services(self):
        self.services = True

    def tick(self):
        self.budget = self.budget_per_tick
        now = self.clock.now()
        if self.scanning and self.adv_name and self.services and now - self.last_adv >= 0.1:
            self.last_adv = now
            self.post(self.on_adv, self.peer, {'mfr': None, 'name': self.adv_name}, -55)


class Env:
    """アプリ（試験用の組立て）＋模擬器コア＋MockTransport"""
    def __init__(self, mtu=247, accept_emulator=True, start_seq=5000, device_id=0xA1B2C3D4):
        self.clock = A.FakeClock()
        self.core = E.PatchCore(device_id=device_id, now=0.0, wall=self.clock.local_datetime, start_seq=start_seq)
        self.tr = MockTransport(self.core, self.clock, mtu)
        self.ctx = A.AppInit('test', transport=self.tr, clock=self.clock).build()
        self.ctx.settings['accept_emulator'] = accept_emulator
        self.mgr = self.ctx.mgr
        self.mgr.start()
        self.mgr.pump()

    def step(self, dt=0.05):
        self.clock.advance(dt)
        self.core.tick(self.clock.now())
        self.tr.tick()
        self.mgr.pump()
        self.mgr.tick()
        self.mgr.pump()

    def run(self, secs, until=None, dt=0.05):
        for _ in range(int(round(secs / dt))):
            self.step(dt)
            if until is not None and until():
                return True
        return until is None

    def connect(self, patient_id=None):
        self.run(1.0, until=lambda: self.tr.peer in self.mgr.scanner.table)
        self.mgr.post(self.mgr.connect_to, self.tr.peer, patient_id)
        self.mgr.pump()

    def register(self, dev='A1B2C3D4'):
        self.ctx.settings.register(dev, 'ok', self.clock.local_datetime())

    def files(self, suffix=''):
        out = []
        for d, _, fs in os.walk(os.path.join(self.ctx.root, 'ECG')):
            out += [os.path.join(d, f) for f in fs if f.endswith(suffix)]
        return sorted(out)
