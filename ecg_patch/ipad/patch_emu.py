# -*- coding: utf-8 -*-
"""
patch_emu.py  ―  ECG パッチ模擬器（iPad／別の iPhone、Pythonista 3）

  目的 : ecg_patch_app.py（iPhone）の BLE 通信を、実際の BLE でつないで確認する
         （AISILK-SPEC-ECG-001-C Rev0.7 20.2 節・5.10.5 節）。
  役割 : CoreBluetooth のペリフェラル（CBPeripheralManager）としてパッチの GATT を出す。
         断片・コマンド・結果コード・Event Log・Device Status・Debug Info・Emulator Info を
         付属 B 第 16 章の形式で送受信し、操作画面から電極はがれ・不整脈・電源 OFF・
         故障（断片の欠落・CRC 異常・BUSY・NAND 異常・記録待ちの延長）を起こせる。
  確認 : 受信したコマンドと送った内容を「通信チェックリスト」（C01〜C15）で自動判定する。

iOS のペリフェラルは Manufacturer データを広告できないため、名前を
「ECGP-<ID 8 桁>~<event><sub><status><battery>（各 2 桁 16 進）」にして広告する
（付属 D 15.4 節）。iPhone アプリ側は設定「模擬器を受け入れる」を ON にする。
iOS のペリフェラルからは切断できないため、ACK 後の切断は iPhone アプリが行う（9.6 節）。
「電源 OFF」はサービスを外して表す（iPhone 側は didModifyServices で検出）。

端末に依存しない PatchCore は PC でも import でき、tests/ の結合試験でアプリと直接つなぐ。
"""
import os
import re
import sys
import json
import math
import time
import queue
import struct
import threading
import random
import datetime
import collections
from array import array

try:
    import ui
    import console
    import ctypes
    from objc_util import ObjCClass, create_objc_class, c     # クラス作成だけに使う（r17 No.10・11）
    ON_PYTHONISTA = True
except ImportError:
    ON_PYTHONISTA = False

EMU_VERSION = '2026-11-09 r10'

# ---- 付録 A と同じ定義（iPhone アプリと一致させる）
FS, CYCLE, PAGE_CYCLES, PAGE_SAMPLES = 256, 128, 8, 1024
PAGE_BODY, PAGE_SPARE, PAGE_USER, FMT_V1 = 2048, 64, 2112, 0xA1
UUID_TAIL = '-6E0C-4C1B-8A5D-3E2F7C9B0D01'
SVC_ECG = 'A1510001' + UUID_TAIL
CH_STREAM = 'A1510002' + UUID_TAIL
CH_EVENTLOG = 'A1510003' + UUID_TAIL
CH_STATUS = 'A1510004' + UUID_TAIL
CH_CONTROL = 'A1510005' + UUID_TAIL
CH_DEBUG = 'A1510006' + UUID_TAIL
CH_EMU = 'A15100EE' + UUID_TAIL
CH_SHORT = {CH_STREAM: 'ECG', CH_EVENTLOG: 'EVT', CH_STATUS: 'STS', CH_CONTROL: 'CTL', CH_DEBUG: 'DBG', CH_EMU: 'EMU'}

PF_ARR, PF_ELEC, PF_NOTIFIED, PF_TIME_UNSET, PF_SHORT, PF_REBOOT, PF_TIME_EST = (1 << i for i in range(7))
CMD_SET_TIME, CMD_SET_PID, CMD_REQ_SETUP, CMD_ACK_SETUP = 0x01, 0x02, 0x10, 0x11
CMD_REQ_EVENT, CMD_ACK_EVENT, CMD_REQ_PAGES, CMD_STOP = 0x20, 0x21, 0x30, 0x40
CMD_NAMES = {0x01: 'SET_TIME', 0x02: 'SET_PATIENT_ID', 0x10: 'REQ_SETUP_WAVE', 0x11: 'ACK_SETUP',
             0x20: 'REQ_EVENT_WAVE', 0x21: 'ACK_EVENT', 0x30: 'REQ_PAGES', 0x40: 'STOP'}
RES_NAMES = ['OK', 'BAD_LEN', 'BAD_STATE', 'ALREADY', 'RANGE', 'NAND_ERR', 'WAITING', 'UNKNOWN', 'BUSY']
RES_OK, RES_BAD_LEN, RES_BAD_STATE, RES_ALREADY, RES_RANGE, RES_NAND, RES_WAITING, RES_UNKNOWN, RES_BUSY = range(9)
S2, S3, S5, S6 = 2, 3, 5, 6
EV_SETUP, EV_ELEC, EV_ARR = 0x01, 0x02, 0x03
# 広告の Status（付属 B 16.2 節。r10 で合わせた。r9 までは bit0＝時刻・bit2＝電極）
ADV_ST_ELEC, ADV_ST_UNNOTIFIED, ADV_ST_TIMESET, ADV_ST_REC = 0x01, 0x02, 0x04, 0x08
ATT_OK, ATT_INVALID_OFFSET, ATT_WRITE_NOT_PERMITTED, ATT_INVALID_LEN = 0x00, 0x07, 0x03, 0x0D

HDR = struct.Struct('<BI6BBBBH3hBHH')    # 27B（本体 7.8 節 Rev1.2：fs＝offset 23〜24、CRC＝25〜26、CRC の対象はスペア 0〜24 まで）
FS_HZ, TILT_NONE = 256, -32768
EVREC = struct.Struct('<4BIIH6BHHBIBH')
STAT = struct.Struct('<10BIIH')
FRAG = struct.Struct('<4B')
DBGINFO = struct.Struct('<BBBBHHHHBbBBIHHHHI')
EMUINFO = struct.Struct('<IBBBB8s')

NAND_KEEP = 450                       # 保持するページ数（30 分）
FW_VER = 0x0100


def crc16_ccitt_false(data, crc=0xFFFF):
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def hexs(b, limit=24):
    b = bytes(b)
    s = ' '.join('%02X' % x for x in b[:limit])
    return s + (' …' if len(b) > limit else '')


# =============================================================================
# 合成心電（NAND に記録する波形）
# =============================================================================
class EcgSynth:
    def __init__(self, hr=72, seed=7):
        self.rnd = random.Random(seed)
        self.hr = hr
        self.beats = [60]
        self.rr_hist = collections.deque([int(60 * FS / hr)] * 8, maxlen=8)
        self.pending = None             # 'pause' / 'early' / ('mark', sub)
        self.special = []               # [(idx, kind, rr, median)]
        self.tmpl = self._template()

    @staticmethod
    def _template():
        t = {}
        for j in range(-40, 70):
            v = 120 * math.exp(-((j + 30) / 6.0) ** 2) - 150 * math.exp(-((j + 3) / 1.6) ** 2)
            v += 1300 * math.exp(-(j / 2.2) ** 2) - 260 * math.exp(-((j - 4) / 2.0) ** 2)
            v += 260 * math.exp(-((j - 45) / 10.0) ** 2)
            t[j] = v
        return t

    def _schedule(self, upto):
        while self.beats[-1] < upto:
            base = int(60 * FS / self.hr) + self.rnd.randint(-5, 5)
            med = sorted(self.rr_hist)[4]
            kind = None
            if self.pending == 'pause':
                rr, kind = int(base * 1.7), 'pause'
            elif self.pending == 'early':
                rr, kind = int(base * 0.6), 'early'
            else:
                rr = base
            nxt = self.beats[-1] + rr
            if kind:
                self.special.append((nxt, kind, rr, med))
                self.pending = None
            elif isinstance(self.pending, tuple):          # 波形を変えずに検知位置だけ付ける
                self.special.append((nxt, self.pending[1], rr, med))
                self.pending = None
            self.rr_hist.append(rr)
            self.beats.append(nxt)
        if len(self.beats) > 200:
            self.beats = self.beats[-100:]

    def page(self, start, leadoff_cycles, motion):
        """start＝グローバルなサンプル番号。戻り値：(samples 1024, この範囲の特別な拍)"""
        end = start + PAGE_SAMPLES
        self._schedule(end + 80)
        x = [0.0] * PAGE_SAMPLES
        for b in self.beats:
            if b < start - 70 or b > end + 40:
                continue
            for j, v in self.tmpl.items():
                i = b + j - start
                if 0 <= i < PAGE_SAMPLES:
                    x[i] += v
        out = array('h', [0] * PAGE_SAMPLES)
        for i in range(PAGE_SAMPLES):
            g = start + i
            v = x[i] + 40 * math.sin(g / FS * 2 * math.pi * 0.25) + self.rnd.randint(-8, 8)
            if motion:
                v += self.rnd.randint(-60, 60) * motion
            k = i // CYCLE
            if leadoff_cycles[k]:
                v = self.rnd.randint(-3, 3)
            out[i] = int(max(-32768, min(32767, v)))
        sp = [s for s in self.special if start <= s[0] < end]
        self.special = [s for s in self.special if s[0] >= end]
        return out, sp


# =============================================================================
# 通信チェックリスト（模擬器側で自動判定）
# =============================================================================
CHECKS = [
    ('C01', 'アドバタイズ開始（名前 ECGP-…~…）'),
    ('C02', '接続・購読（STS／EVT／ECG）'),
    ('C03', 'Device Status の読出し'),
    ('C04', 'Emulator Info の読出し（ID 照合）'),
    ('C05', 'SET_TIME（iPad の時計との差 ±5 秒）'),
    ('C06', 'REQ_SETUP_WAVE → 直近 8 ページ送信'),
    ('C07', 'ACK_SETUP(OK) → S3（計測開始）'),
    ('C08', 'Event Log の送信（Indicate）'),
    ('C09', 'REQ_EVENT_WAVE（WAITING → OK）'),
    ('C10', 'イベント波形の全ページ送信'),
    ('C11', 'ACK_EVENT（全件）'),
    ('C12', 'REQ_PAGES（欠落・CRC 異常の再送）'),
    ('C13', '切断（購読解除）'),
    ('C14', 'STOP'),
    ('C15', 'MTU（maximumUpdateValueLength）'),
]


class Checklist:
    def __init__(self, clock):
        self.clock = clock
        self.reset()

    def reset(self):
        self.items = collections.OrderedDict((k, ['-', '', None]) for k, _ in CHECKS)

    def set(self, key, ok=True, note=''):
        it = self.items[key]
        if it[0] == 'OK' and ok:
            it[1] = note or it[1]
            return
        it[0] = 'OK' if ok else 'NG'
        it[1] = note
        it[2] = datetime.datetime.now().strftime('%H:%M:%S')

    def text(self):
        out = []
        names = dict(CHECKS)
        for k, (st, note, t) in self.items.items():
            mark = {'OK': '✓', 'NG': '✗', '-': '・'}[st]
            out.append('%s %s %s%s%s' % (mark, k, names[k], ('  ' + note) if note else '', ('  [%s]' % t) if t else ''))
        n_ok = sum(1 for v in self.items.values() if v[0] == 'OK')
        n_ng = sum(1 for v in self.items.values() if v[0] == 'NG')
        out.insert(0, '通信チェックリスト  OK %d／NG %d／未 %d' % (n_ok, n_ng, len(self.items) - n_ok - n_ng))
        return '\n'.join(out)


# =============================================================================
# PatchCore：パッチの振る舞い（純粋な Python）
# =============================================================================
class Hooks:
    """トランスポートが実装する口（試験では MockLink）"""
    def notify(self, uuid, data):
        return True

    def set_adv(self, name):
        pass

    def remove_services(self):
        pass

    def restore_services(self):
        pass

    def log(self, line):
        pass


class PatchCore:
    def __init__(self, device_id=0xA1B2C3D4, now=0.0, wall=None, start_seq=5000, seed=7):
        self.device_id = device_id
        self.wall = wall or datetime.datetime.now
        self.now = now
        self.t_boot = now
        self.hooks = Hooks()
        self.synth = EcgSynth(seed=seed)
        self.log_lines = collections.deque(maxlen=400)
        self.checks = Checklist(self)
        self.stats = collections.Counter()
        self.reset(start_seq, now)

    # ---- 状態
    def reset(self, start_seq=None, now=None):
        if now is not None:
            self.now = now
        self.state = S2
        self.powered = True
        self.time_set = False
        self.base_seq = 0
        self.base_wall = None
        self.seq = start_seq if start_seq is not None else getattr(self, 'seq', 5000)
        self.page_t0 = self.now
        self.nand = collections.OrderedDict()
        self.electrode_off = False
        self.leadoff_until = None
        self.elec_spans = []                    # K7：電極はがれの時間帯 [開始, 終了 or None]（monotonic）
        self.motion = 0
        self.events = []
        self.event_seq = 0
        self.batt = 90
        self.subs = set()
        self.connected = False
        self.max_update = 20
        self.txq = collections.deque()
        self.xfer = 0
        self.setup_stream = False
        self.waiting_ev = None
        self.wait_extra = 0.0
        self.indicated = set()
        self.last_cmd = 0
        self.result = 0
        self.patient_id = None
        self.adv_name = None
        self.faults = collections.Counter()
        self.power_off_at = None
        self.last_dbg = 0.0
        self.commands = []                      # 受信したコマンド（opcode）
        self.setup_result = None
        self.last_elec_status = None

    def log(self, text):
        line = '%s %s' % (self.wall().strftime('%H:%M:%S'), text)
        self.log_lines.append(line)
        try:
            self.hooks.log(line)
        except Exception:
            pass

    @property
    def state_name(self):
        return {S2: 'S2 装着確認待ち', S3: 'S3 計測中', S5: 'S5 異常通知', S6: 'S6 電源 OFF'}.get(self.state, 'S%d' % self.state)

    def unnotified(self):
        return [e for e in self.events if not e['notified']]

    # ---- 周期処理
    def tick(self, now):
        self.now = now
        if not self.powered:
            return
        while now - self.page_t0 >= 4.0:
            self.page_t0 += 4.0
            self._record_page()
        if self.leadoff_until is not None and now >= self.leadoff_until:
            self.leadoff_until = None
            self.electrode_off = False
            self._close_span(now)
            self.log('電極 OK に戻りました')
        if self.waiting_ev is not None and self._ready(self.waiting_ev) and now >= self.waiting_ev.get('wait_until', 0):
            ev = self.waiting_ev
            self.waiting_ev = None
            self.checks.set('C09', True, 'WAITING %.1f 秒 → OK' % (now - ev['wait_t0']))
            self._result(CMD_REQ_EVENT, RES_OK)
            self._send_event_wave(ev)
        if self.connected and CH_DEBUG in self.subs and now - self.last_dbg >= 1.0:
            self.last_dbg = now
            self._queue(CH_DEBUG, self.debug_info())
        if self.power_off_at is not None and now >= self.power_off_at:
            self.power_off_at = None
            self.powered = False
            self.connected = False
            self.subs.clear()
            self.txq.clear()
            self._set_adv(None)
            self.hooks.remove_services()
            self.log('電源 OFF（サービスを外しました）')
            return
        el = 1 if self.electrode_off else 0
        if el != self.last_elec_status:
            self.last_elec_status = el
            self._status_notify()
        self._update_adv()
        self.pump()

    def _close_span(self, t):
        if self.elec_spans and self.elec_spans[-1][1] is None:
            self.elec_spans[-1][1] = t

    def _page_leadoff(self, ps):
        """K7：ページ（開始 ps、4 秒）の 0.5 秒の周期ごとに、はがれていた時間があれば 1（本物のパッチと同じ粒度）"""
        self.elec_spans = [sp for sp in self.elec_spans if sp[1] is None or sp[1] > ps - 10]
        if not self.elec_spans:
            return [1 if self.electrode_off else 0] * PAGE_CYCLES
        out = []
        for k in range(PAGE_CYCLES):
            cs, ce = ps + 0.5 * k, ps + 0.5 * (k + 1)
            out.append(1 if any(a < ce and (b is None or b > cs) for a, b in self.elec_spans) else 0)
        return out

    def _record_page(self):
        seq = self.seq
        leadoff = self._page_leadoff(self.page_t0 - 4.0)
        samples, specials = self.synth.page(seq * PAGE_SAMPLES, leadoff, self.motion)
        flags = 0
        if not self.time_set:
            flags |= PF_TIME_UNSET
            ts = (0, 0, 0, 0, 0, 0)
        else:
            w = self.base_wall + datetime.timedelta(seconds=(seq - self.base_seq) * 4)
            ts = (w.year - 2000, w.month, w.day, w.hour, w.minute, w.second)
        elec = 0
        for k, v in enumerate(leadoff):
            elec |= v << k
        if elec:
            flags |= PF_ELEC
        accel = 0
        for k in range(PAGE_CYCLES):
            accel |= (self.motion & 3) << (2 * k)
        new_events = []
        for idx, kind, rr, med in specials:
            if self.state in (S3, S5):
                sub = {'pause': 7, 'early': 6}.get(kind, kind if isinstance(kind, int) else 7)
                new_events.append((idx, sub, rr, med))
                flags |= PF_ARR
        tilt = (TILT_NONE,) * 3                         # 傾きは記録しない（0x8000。本体 No.34）
        batt_raw = 0x01                                 # 電池状態：bit0＝90% 以上（本体 7.8 節）
        body = samples.tobytes() if sys.byteorder == 'little' else self._swap(samples)
        hdr = HDR.pack(FMT_V1, seq, *ts, PAGE_CYCLES, flags, elec, accel, tilt[0], tilt[1], tilt[2], batt_raw,
                       FS_HZ, 0)
        crc = crc16_ccitt_false(body + hdr[:25])
        hdr = hdr[:25] + struct.pack('<H', crc)
        page = body + hdr + b'\xFF' * (PAGE_SPARE - HDR.size)
        assert len(page) == PAGE_USER
        self.nand[seq] = page
        if elec:                                        # 試験 No.10 の確認用：電極異常を記録したページ
            self.log('page %d に電極異常を記録（周期 %s）' % (seq, format(elec, '08b')[::-1]))
        while len(self.nand) > NAND_KEEP:
            self.nand.popitem(last=False)
        self.seq += 1
        for idx, sub, rr, med in new_events:
            self._create_event(EV_ARR, sub, idx, rr, med)
        if self.setup_stream and self.connected:
            self._start_transfer([seq])

    @staticmethod
    def _swap(samples):
        a = array('h', samples)
        a.byteswap()
        return a.tobytes()

    def page_bytes(self, seq):
        return self.nand.get(seq)

    # ---- イベント
    def _create_event(self, ev, sub, idx, rr=0, med=0):
        self.event_seq += 1
        ws = (idx - 3840) // PAGE_SAMPLES
        we = (idx + 3839) // PAGE_SAMPLES
        if self.nand:
            ws = max(ws, next(iter(self.nand)))         # 記録より前のページは送れない
        if self.time_set:
            w = self.base_wall + datetime.timedelta(seconds=(idx - self.base_seq * PAGE_SAMPLES) / FS)
            ts = (w.year - 2000, w.month, w.day, w.hour, w.minute, w.second)
        else:
            ts = (0, 0, 0, 0, 0, 0)
        rr_ms = rr * 1000 // FS
        med_ms = med * 1000 // FS
        e = {'seq': self.event_seq, 'event': ev, 'sub': sub | (0x80 if self.motion else 0), 'idx': idx,
             'page_seq': idx // PAGE_SAMPLES, 'ofs': idx % PAGE_SAMPLES, 'ts': ts, 'rr_ms': rr_ms,
             'median_ms': med_ms, 'hr': (60000 // med_ms) if med_ms else 0, 'wave_start': ws,
             'wave_pages': we - ws + 1, 'notified': False}
        self.events.append(e)
        if self.state == S3:
            self.state = S5
        self.log('イベント #%d 発生：%s sub=%d page=%d ofs=%d（記録完了まで約 %d 秒）' % (
            e['seq'], {EV_ARR: '不整脈疑い', EV_ELEC: '電極異常'}[ev], sub, e['page_seq'], e['ofs'],
            max(0, (ws + e['wave_pages'] - self.seq) * 4)))
        self._status_notify()
        if self.connected and CH_EVENTLOG in self.subs and len(self.unnotified()) == 1:
            self._indicate_next()
        return e

    def event_bytes(self, e):
        b = EVREC.pack(1, e['event'], e['sub'], 0, e['seq'], e['page_seq'], e['ofs'], *e['ts'],
                       e['rr_ms'], e['median_ms'], e['hr'] & 0xFF, e['wave_start'], e['wave_pages'], 0)
        return b[:30] + struct.pack('<H', crc16_ccitt_false(b[:30]))

    def _ready(self, e):
        return self.seq - 1 >= e['wave_start'] + e['wave_pages'] - 1

    def _indicate_next(self):
        un = self.unnotified()
        if not un:
            return
        e = un[0]
        if e['seq'] in self.indicated:
            return
        self.indicated.add(e['seq'])
        self._queue(CH_EVENTLOG, self.event_bytes(e))
        self.checks.set('C08', True, 'event #%d' % e['seq'])
        self.log('Event Log を送信（#%d）' % e['seq'])

    # ---- 操作画面から
    def trigger_arrhythmia(self, kind='pause'):
        if kind in ('pause', 'early'):
            self.synth.pending = kind
        else:
            self.synth.pending = ('mark', {'tachy': 1, 'brady': 2}.get(kind, 7))
        if self.state not in (S3, S5):
            self.log('不整脈の波形を入れました（イベントは S3／S5 のときだけ記録）')
        else:
            self.log('不整脈（%s）を次の拍に入れます' % kind)

    def trigger_electrode_event(self, seconds=6.0):
        self.electrode_off = True
        self.leadoff_until = self.now + seconds
        self._close_span(self.now)
        self.elec_spans.append([self.now, self.now + seconds])
        if self.state in (S3, S5):
            idx = self.seq * PAGE_SAMPLES + min(PAGE_SAMPLES - 1, int((self.now - self.page_t0) * FS))
            self._create_event(EV_ELEC, 1, idx)
        else:
            self.log('電極はがれ %d 秒（S2 なのでイベントなし）' % seconds)

    def set_electrode(self, off):
        self.electrode_off = off
        self.leadoff_until = None
        if off:
            self._close_span(self.now)
            self.elec_spans.append([self.now, None])
        else:
            self._close_span(self.now)
        self.log('電極 %s' % ('はがれ（NG）' if off else '正常（OK）'))

    def start_measuring(self, set_time=True):
        """装着確認をとばして S3 にする（set_time=False で時刻未設定のまま：A13-06 の確認用）"""
        if set_time and not self.time_set:
            self._set_time(self.wall())
        self.state = S3
        self.setup_stream = False
        self.log('計測開始（S3%s）' % ('' if self.time_set else '・時刻未設定'))

    def power_off(self):
        self.state = S6
        self.setup_stream = False
        self.waiting_ev = None
        self.txq.clear()                                # 送信中の転送は止まる
        self._status_notify()
        self.pump()
        self.power_off_at = self.now + 0.5

    def power_on_reset(self):
        self.reset(now=self.now)
        self.hooks.restore_services()
        self.log('電源 ON・S2 に戻しました（時刻未設定）')

    # ---- 広告
    def adv_tuple(self):
        if not self.powered:
            return None
        st = (ADV_ST_TIMESET if self.time_set else 0) | (ADV_ST_ELEC if self.electrode_off else 0)
        if self.state in (S3, S5):
            st |= ADV_ST_REC
        un = self.unnotified()
        if un:
            st |= ADV_ST_UNNOTIFIED
        if self.state == S2:
            return (EV_SETUP, 0, st, self.batt)
        if self.state == S5 and un:
            return (un[0]['event'], un[0]['sub'], st, self.batt)
        return None

    def adv_name_now(self):
        t = self.adv_tuple()
        if t is None:
            return None
        return 'ECGP-%08X~%02X%02X%02X%02X' % ((self.device_id,) + t)

    def _update_adv(self):
        name = self.adv_name_now()
        if name != self.adv_name:
            self._set_adv(name)

    def _set_adv(self, name):
        self.adv_name = name
        self.hooks.set_adv(name)
        if name:
            self.checks.set('C01', True, name)
            self.log('広告: %s' % name)
        else:
            self.log('広告を停止')

    # ---- GATT（トランスポートから）
    def on_subscribe(self, uuid, max_update=20):
        if not self.connected:
            self.connected = True
            self.stats['conn_count'] += 1
            self.indicated = set()
            self.log('セントラルが接続しました')
        self.subs.add(uuid)
        self.max_update = max(20, int(max_update))
        self.checks.set('C15', True, '%d B（MTU %d）' % (self.max_update, self.max_update + 3))
        self.log('購読: %s（max %d B）' % (CH_SHORT.get(uuid, uuid), self.max_update))
        if {CH_STATUS, CH_EVENTLOG, CH_STREAM} <= self.subs:
            self.checks.set('C02', True)
        if uuid == CH_EVENTLOG and self.unnotified():
            self._indicate_next()

    def on_unsubscribe(self, uuid):
        self.subs.discard(uuid)
        self.log('購読解除: %s' % CH_SHORT.get(uuid, uuid))
        if not self.subs and self.connected:
            self.connected = False
            self.setup_stream = False
            self.waiting_ev = None
            self.txq.clear()
            self.checks.set('C13', True, '状態 %s' % self.state_name)
            self.log('切断されました（状態 %s）' % self.state_name)

    def on_read(self, uuid):
        if uuid == CH_STATUS:
            self.checks.set('C03', True)
            return self.status_bytes()
        if uuid == CH_EMU:
            self.checks.set('C04', True, '%08X' % self.device_id)
            return self.emu_info()
        if uuid == CH_DEBUG:
            return self.debug_info()
        return b''

    def status_bytes(self):
        latest = self.seq - 1
        return STAT.pack(1, self.state, 1 if self.electrode_off else 0, 1 if self.unnotified() else 0, self.batt,
                         len(self.unnotified()), 1 if self.time_set else 0, self.last_cmd, self.result, 0,
                         latest, self.base_seq if self.time_set else 0, FW_VER)

    def emu_info(self):
        return EMUINFO.pack(self.device_id, 1, self.state, 0, 0, b'PATCHEMU')

    def debug_info(self):
        return DBGINFO.pack(1, self.state, 0, 0, 24, 0, 400, self.max_update + 3, max(0, 10 - min(10, len(self.txq))),
                            0, 0, 0, self.stats['notify_ok'] & 0xFFFFFFFF, self.stats['notify_busy'] & 0xFFFF,
                            self.stats['cmd_rx'] & 0xFFFF, self.stats['cmd_err'] & 0xFFFF,
                            self.stats['conn_count'] & 0xFFFF, int(self.now - self.t_boot) & 0xFFFFFFFF)

    def on_write(self, uuid, data):
        """戻り値：ATT の結果（0＝成功）。結果コードは Device Status の Notify で返す。"""
        if uuid != CH_CONTROL:
            return ATT_WRITE_NOT_PERMITTED
        data = bytes(data)
        if not data:
            return ATT_INVALID_LEN
        self.stats['cmd_rx'] += 1
        op = data[0]
        self.commands.append(op)
        name = CMD_NAMES.get(op, 'CMD_%02X' % op)
        shown = '02 **' if op == CMD_SET_PID else hexs(data)
        self.log('受信 %s [%s]' % (name, shown))
        if op != CMD_STOP and self.faults['busy'] > 0:
            self.faults['busy'] -= 1
            self._result(op, RES_BUSY)
            return ATT_OK
        if op != CMD_STOP and self.faults['nand'] > 0:
            self.faults['nand'] -= 1
            self._result(op, RES_NAND)
            return ATT_OK
        handler = {CMD_SET_TIME: self._cmd_set_time, CMD_SET_PID: self._cmd_pid, CMD_REQ_SETUP: self._cmd_req_setup,
                   CMD_ACK_SETUP: self._cmd_ack_setup, CMD_REQ_EVENT: self._cmd_req_event,
                   CMD_ACK_EVENT: self._cmd_ack_event, CMD_REQ_PAGES: self._cmd_req_pages,
                   CMD_STOP: self._cmd_stop}.get(op)
        if handler is None:
            self._result(op, RES_UNKNOWN)
            return ATT_OK
        r = handler(data[1:])
        if r is not None:
            self._result(op, r)
        return ATT_OK

    def _result(self, op, code):
        self.last_cmd, self.result = op, code
        if code not in (RES_OK, RES_ALREADY, RES_WAITING):
            self.stats['cmd_err'] += 1
        self.log('結果 %s → %d %s' % (CMD_NAMES.get(op, '%02X' % op), code, RES_NAMES[code] if code < len(RES_NAMES) else '?'))
        self._status_notify()

    def _status_notify(self):
        if self.connected and CH_STATUS in self.subs:
            self._queue(CH_STATUS, self.status_bytes())

    # ---- コマンド
    def _set_time(self, w):
        self.time_set = True
        self.base_seq = self.seq                                  # 記録中のページ
        # r8：秒未満を切り捨てない（パッチの時計を正しく表す。ページの時刻は書くときに秒に切り捨てる）
        self.base_wall = w - datetime.timedelta(seconds=self.now - self.page_t0)
        self.base_t = self.page_t0

    def patch_clock(self):
        """パッチの時計（時刻設定後）。試験用"""
        if not self.time_set:
            return None
        return self.base_wall + datetime.timedelta(seconds=self.now - self.base_t)

    def _cmd_set_time(self, p):
        if len(p) != 6:
            return RES_BAD_LEN
        if self.time_set:
            self.checks.set('C05', True, '設定済み（ALREADY）')
            return RES_ALREADY
        try:
            w = datetime.datetime(2000 + p[0], p[1], p[2], p[3], p[4], p[5])
        except ValueError:
            return RES_RANGE
        diff = (w - self.wall()).total_seconds()
        self._set_time(w)
        self.checks.set('C05', abs(diff) <= 5, '%s（差 %+.1f 秒）' % (w.strftime('%Y-%m-%d %H:%M:%S'), diff))
        return RES_OK

    def _cmd_pid(self, p):
        if not p or p[0] != len(p) - 1 or p[0] > 16:
            return RES_BAD_LEN
        self.patient_id = bytes(p[1:]).decode('ascii', 'replace')
        self.log('患者 ID を受信（%d 文字）' % len(self.patient_id))
        return RES_OK

    def _cmd_req_setup(self, p):
        if p:
            return RES_BAD_LEN
        if self.state != S2:
            return RES_BAD_STATE
        seqs = [s for s in range(self.seq - 8, self.seq) if s in self.nand]
        self._result(CMD_REQ_SETUP, RES_OK)
        self.setup_stream = True
        self._start_transfer(seqs, mark=lambda n=len(seqs): self.checks.set('C06', True, '%d ページ' % n))
        return None

    def _cmd_ack_setup(self, p):
        if len(p) != 1:
            return RES_BAD_LEN
        if self.state != S2:
            return RES_BAD_STATE
        if p[0] == 0:
            self.state = S3
            self.setup_stream = False
            self.setup_result = 'ok'
            self.checks.set('C07', True)
            self.log('装着確認 OK → S3（計測開始・BLE 停止）。iPhone からの切断を待ちます')
        else:
            self.setup_result = 'ng'
            self.checks.set('C07', False, 'NG を受信')
        return RES_OK

    def _find_event(self, seq):
        for e in self.events:
            if e['seq'] == seq:
                return e
        return None

    def _cmd_req_event(self, p):
        if len(p) != 4:
            return RES_BAD_LEN
        seq = struct.unpack('<I', p)[0]
        e = self._find_event(seq)
        if e is None:
            return RES_RANGE
        if not self._ready(e) or self.wait_extra > 0:
            e['wait_t0'] = self.now
            e['wait_until'] = self.now + self.wait_extra
            self.wait_extra = 0.0
            self.waiting_ev = e
            self.log('記録待ち（WAITING）event #%d' % seq)
            return RES_WAITING
        self.checks.set('C09', True, 'すぐに OK')
        self._result(CMD_REQ_EVENT, RES_OK)
        self._send_event_wave(e)
        return None

    def _send_event_wave(self, e):
        seqs = [s for s in range(e['wave_start'], e['wave_start'] + e['wave_pages']) if s in self.nand]
        self._start_transfer(seqs, event=True,
                             mark=lambda: self.checks.set('C10', True, '#%d %d ページ' % (e['seq'], len(seqs))))

    def _cmd_ack_event(self, p):
        if len(p) != 4:
            return RES_BAD_LEN
        e = self._find_event(struct.unpack('<I', p)[0])
        if e is None:
            return RES_RANGE
        e['notified'] = True
        left = self.unnotified()
        self.log('ACK_EVENT #%d（残り %d 件）' % (e['seq'], len(left)))
        if left:
            self._result(CMD_ACK_EVENT, RES_OK)
            self._indicate_next()
            return None
        self.state = S3
        self.checks.set('C11', True, '全 %d 件' % len(self.events))
        self.log('全件 ACK → S3（BLE 停止）。iPhone からの切断を待ちます')
        return RES_OK

    def _cmd_req_pages(self, p):
        if len(p) != 6:
            return RES_BAD_LEN
        seq, n = struct.unpack('<IH', p)
        seqs = [s for s in range(seq, seq + n) if s in self.nand]
        if not seqs:
            return RES_RANGE
        self.checks.set('C12', True, 'page %d +%d' % (seq, n))
        self._result(CMD_REQ_PAGES, RES_OK)
        self._start_transfer(seqs, resend=True)
        return None

    def _cmd_stop(self, p):
        self.setup_stream = False
        self.txq = collections.deque(x for x in self.txq if x[0] != CH_STREAM)
        if self.state == S2:
            self.state = S3
            self.setup_result = 'stopped'
            self.log('STOP：装着確認を中断して計測開始（S3）')
        self.checks.set('C14', True)
        return RES_OK

    # ---- 送信
    def _queue(self, uuid, data):
        self.txq.append((uuid, bytes(data)))

    def _start_transfer(self, seqs, event=False, resend=False, mark=None):
        self.xfer = (self.xfer + 1) & 0xFF
        payload = max(1, self.max_update - 4)
        crc_page = len(seqs) // 2                       # 故障は転送の中ほどのページに入れる
        drop_page = max(0, len(seqs) // 2 - 1)
        for pi, seq in enumerate(seqs):
            page = self.nand[seq]
            if pi == crc_page and self.faults['bad_crc'] > 0 and not resend:
                self.faults['bad_crc'] -= 1
                b = bytearray(page)
                b[100] ^= 0x5A
                page = bytes(b)
                self.log('故障：page %d の CRC を壊して送信' % seq)
            n = (PAGE_USER + payload - 1) // payload
            drop_at = n // 2 if (pi == drop_page and self.faults['drop_frag'] > 0 and not resend) else -1
            if drop_at >= 0:
                self.faults['drop_frag'] -= 1
                self.log('故障：page %d の断片 %d を欠落' % (seq, drop_at))
            for fi in range(n):
                flags = 0
                if fi == n - 1:
                    flags |= 0x01
                    if pi == len(seqs) - 1:
                        flags |= 0x02
                if event:
                    flags |= 0x04
                if resend:
                    flags |= 0x08
                if fi == drop_at:
                    continue
                chunk = page[fi * payload:(fi + 1) * payload]
                self._queue(CH_STREAM, FRAG.pack(self.xfer, pi & 0xFF, fi & 0xFF, flags) + chunk)
        self.log('送信開始 xfer=%d %d ページ（%d〜%d）%s%s' % (self.xfer, len(seqs), seqs[0] if seqs else 0,
                                                       seqs[-1] if seqs else 0, ' event' if event else '',
                                                       ' resend' if resend else ''))
        if mark:
            self.txq.append(('__mark__', mark))

    def pump(self):
        while self.txq and self.connected:
            uuid, data = self.txq[0]
            if uuid == '__mark__':
                self.txq.popleft()
                data()
                continue
            if uuid not in self.subs:
                self.txq.popleft()
                continue
            if self.hooks.notify(uuid, data):
                self.stats['notify_ok'] += 1
                self.stats['bytes_' + CH_SHORT.get(uuid, '?')] += len(data)
                self.txq.popleft()
            else:
                self.stats['notify_busy'] += 1
                break

    def summary(self):
        return '%s  ID %08X  %s  時刻 %s  最新 page %d  未通知 %d  送信待ち %d' % (
            '接続中' if self.connected else '未接続', self.device_id, self.state_name,
            '設定済み' if self.time_set else '未設定', self.seq - 1, len(self.unnotified()), len(self.txq))


# =============================================================================
# CoreBluetooth ペリフェラル（Pythonista）
#   r17 の再発防止策（不具合と対応策レポート 5 章）に合わせる：
#    ・objc_util はデリゲートのクラス作成だけ（起動時に 1 回）。ns()・ObjCInstance は使わない。
#    ・Objective-C は生のポインタと型固定の _send で呼ぶ。サービス・特性は閉じるまで保持する（No.12）。
#    ・BLE の操作と模擬器の処理は作業スレッドで行い、デリゲートは値を取り出して渡すだけ（No.14）。
#    ・CoreBluetooth を呼ぶ直前に動作記録へ 1 行書く（fsync）。落ちた位置は faulthandler（No.9）。
# =============================================================================
TRACE_PATH = os.path.expanduser('~/Documents/patch_emu_trace.txt')
FAULT_PATH = os.path.expanduser('~/Documents/patch_emu_fault.txt')


class EmuTrace:
    """1 行ずつ fsync する動作記録（前回分は _prev に移す）"""
    def __init__(self, path):
        self.path = path
        self.prev = []
        self.f = None
        try:
            if os.path.exists(path):
                with open(path, encoding='utf-8', errors='replace') as f:
                    self.prev = f.read().splitlines()[-25:]
                os.replace(path, path.replace('.txt', '_prev.txt'))
            self.f = open(path, 'a', encoding='utf-8')
        except Exception:
            self.f = None
        self.lock = threading.Lock()

    def w(self, *parts):
        line = '%s %s' % (datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3], ' '.join(str(p) for p in parts))
        with self.lock:
            if self.f:
                try:
                    self.f.write(line + '\n')
                    self.f.flush()
                    os.fsync(self.f.fileno())
                except Exception:
                    pass

    def close(self):
        with self.lock:
            if self.f:
                self.f.close()
                self.f = None


_TRACE = None


def in_thread(fn):
    """ui.in_background の代わり（r17 No.3）。スクリプトのスレッドは closed.wait() で止めているため専用スレッドで実行"""
    def wrapper(*a, **k):
        def run():
            try:
                fn(*a, **k)
            except Exception as e:
                if _TRACE is not None:
                    _TRACE.w('ERR', fn.__name__, type(e).__name__, e)
        threading.Thread(target=run, name='ui_' + fn.__name__, daemon=True).start()
    wrapper.__name__ = fn.__name__
    return wrapper

if ON_PYTHONISTA:
    from ctypes import c_void_p, c_bool, c_longlong, c_ulonglong, c_char_p

    _CT = {'@': c_void_p, 'P': c_void_p, '*': c_char_p, 'B': c_bool, 'q': c_longlong, 'Q': c_ulonglong, 'v': None}
    _MSG = {}
    _SELS = {}
    _CLASSES = {}
    _FN = {}

    def _cfn(name, restype, *argtypes):
        """objc ランタイムの関数を専用の型で作る（objc_util の c.xxx の型設定には触れない）"""
        f = _FN.get(name)
        if f is None:
            f = _FN[name] = ctypes.CFUNCTYPE(restype, *argtypes)((name, c))
        return f

    def _sel(name):
        s = _SELS.get(name)
        if s is None:
            s = _SELS[name] = _cfn('sel_registerName', c_void_p, c_char_p)(name.encode('ascii'))
        return s

    def _cls(name):
        k = _CLASSES.get(name)
        if k is None:
            k = _cfn('objc_getClass', c_void_p, c_char_p)(name.encode('ascii'))
            if not k:
                raise RuntimeError('Objective-C class not found: %s' % name)
            _CLASSES[name] = k
        return k

    def _ptr(o):
        if o is None or isinstance(o, int):
            return o
        p = getattr(o, 'ptr', o)
        return p.value if isinstance(p, c_void_p) else p

    def _send(obj, selname, sig, *args):
        f = _MSG.get(sig)
        if f is None:
            f = ctypes.CFUNCTYPE(_CT[sig[0]], c_void_p, c_void_p, *[_CT[x] for x in sig[1:]])(('objc_msgSend', c))
            _MSG[sig] = f
        return f(obj, _sel(selname), *args)

    def _cbcall(what, obj, selname, sig, *args):
        if _TRACE is not None:
            _TRACE.w('CB>', what)
        return _send(obj, selname, sig, *args)

    def _retain(p):
        if p:
            _send(p, 'retain', '@')
        return p

    def _release(p):
        if p:
            _send(p, 'release', 'v')

    def _new(cls_name, init, sig, *args):
        return _send(_send(_cls(cls_name), 'alloc', '@'), init, sig, *args)

    def _new_str(s):
        return _new('NSString', 'initWithUTF8String:', '@*', s.encode('utf-8'))

    def _str(p):
        if not p:
            return ''
        b = _send(p, 'UTF8String', '*')
        return b.decode('utf-8', 'replace') if b else ''

    def _new_data(b):
        b = bytes(b)
        buf = ctypes.create_string_buffer(b, max(1, len(b)))
        return _new('NSData', 'initWithBytes:length:', '@PQ', ctypes.addressof(buf), len(b))

    def _data_bytes(p):
        if not p:
            return b''
        n = _send(p, 'length', 'Q')
        return ctypes.string_at(_send(p, 'bytes', 'P'), n) if n else b''

    def _new_array(ptrs):
        arr = (c_void_p * max(1, len(ptrs)))(*ptrs)
        return _new('NSArray', 'initWithObjects:count:', '@PQ', ctypes.addressof(arr), len(ptrs))

    def _new_dict(pairs):
        keys = (c_void_p * max(1, len(pairs)))(*[k for k, _ in pairs])
        vals = (c_void_p * max(1, len(pairs)))(*[v for _, v in pairs])
        return _new('NSDictionary', 'initWithObjects:forKeys:count:', '@PPQ',
                    ctypes.addressof(vals), ctypes.addressof(keys), len(pairs))

    _CONST = {}

    def _const_str(s):
        p = _CONST.get(s)
        if p is None:
            p = _CONST[s] = _new_str(s)
        return p

    _UUIDS = {}

    def _cbuuid(s):
        p = _UUIDS.get(s)
        if p is None:
            p = _UUIDS[s] = _retain(_send(_cls('CBUUID'), 'UUIDWithString:', '@@', _const_str(s)))
        return p

    def _uuid_of(ch_p):
        return _str(_send(_send(ch_p, 'UUID', '@'), 'UUIDString', '@')).upper()

    def _err_text(p):
        if not p:
            return ''
        return '%s %d %s' % (_str(_send(p, 'domain', '@')), int(_send(p, 'code', 'q')),
                             _str(_send(p, 'localizedDescription', '@')))

    _EMU = None

    def peripheralManagerDidUpdateState_(_self, _cmd, pm):
        if _EMU:
            _EMU.d_state(pm)

    def peripheralManager_didAddService_error_(_self, _cmd, pm, svc, err):
        if _EMU:
            _EMU.d_added(svc, err)

    def peripheralManagerDidStartAdvertising_error_(_self, _cmd, pm, err):
        if _EMU:
            _EMU.d_adv(err)

    def peripheralManager_central_didSubscribeToCharacteristic_(_self, _cmd, pm, central, ch):
        if _EMU:
            _EMU.d_sub(central, ch)

    def peripheralManager_central_didUnsubscribeFromCharacteristic_(_self, _cmd, pm, central, ch):
        if _EMU:
            _EMU.d_unsub(central, ch)

    def peripheralManager_didReceiveReadRequest_(_self, _cmd, pm, req):
        if _EMU:
            _EMU.d_read(req)

    def peripheralManager_didReceiveWriteRequests_(_self, _cmd, pm, reqs):
        if _EMU:
            _EMU.d_write(reqs)

    def peripheralManagerIsReadyToUpdateSubscribers_(_self, _cmd, pm):
        if _EMU:
            _EMU.d_ready()

    _PENC = {
        peripheralManagerDidUpdateState_: 'v@:@',
        peripheralManager_didAddService_error_: 'v@:@@@',
        peripheralManagerDidStartAdvertising_error_: 'v@:@@',
        peripheralManager_central_didSubscribeToCharacteristic_: 'v@:@@@',
        peripheralManager_central_didUnsubscribeFromCharacteristic_: 'v@:@@@',
        peripheralManager_didReceiveReadRequest_: 'v@:@@',
        peripheralManager_didReceiveWriteRequests_: 'v@:@@',
        peripheralManagerIsReadyToUpdateSubscribers_: 'v@:@',
    }
    for _f, _e in _PENC.items():
        _f.encoding = _e

    _DELEGATE_CLASS = None

    def _delegate_class():
        """objc_util を使うのはここだけ（起動時にスクリプトのスレッドで 1 回）"""
        global _DELEGATE_CLASS
        if _DELEGATE_CLASS is None:
            suffix = re.sub(r'\W', '', EMU_VERSION) + '_%x' % int(time.time() * 1000)
            cls = create_objc_class('EcgPatchEmuDelegate_' + suffix, ObjCClass('NSObject'),
                                    methods=list(_PENC.keys()), protocols=['CBPeripheralManagerDelegate'])
            _DELEGATE_CLASS = (cls, _ptr(cls))
        return _DELEGATE_CLASS[1]

    # 特性：(UUID, properties, permissions)。properties: Read 0x02, Write 0x08, Notify 0x10, Indicate 0x20
    GATT = [(CH_STREAM, 0x10, 0x01), (CH_EVENTLOG, 0x20, 0x01), (CH_STATUS, 0x12, 0x01),
            (CH_CONTROL, 0x08, 0x02), (CH_DEBUG, 0x12, 0x01), (CH_EMU, 0x02, 0x01)]
    CB_STATES = ['unknown', 'resetting', 'unsupported', 'unauthorized', 'poweredOff', 'poweredOn']

    class PeripheralTransport(Hooks):
        """操作はすべて作業スレッドで呼ぶ。デリゲート（メインスレッド）は post で作業スレッドへ渡すだけ。"""

        def __init__(self, core, post, trace=None):
            self.core = core
            core.hooks = self
            self.post = post
            self.trace = trace
            self.chars = {}                     # UUID → CBMutableCharacteristic（閉じるまで保持）
            self.svc = None
            self.services_ok = False
            self.adv_name = None
            self.state = 'unknown'
            self.kept = []
            self.delegate = None
            self.pm = None

        def start(self):
            """作業スレッドで呼ぶ"""
            global _EMU
            self._t('BLE-1', 'delegate alloc')
            self.delegate = _send(_send(_delegate_class(), 'alloc', '@'), 'init', '@')
            _EMU = self
            self._t('BLE-2', 'CBPeripheralManager alloc/init (queue=main)')
            self.pm = _cbcall('CBPeripheralManager init', _send(_cls('CBPeripheralManager'), 'alloc', '@'),
                              'initWithDelegate:queue:options:', '@@@@', self.delegate, None, None)
            self._t('BLE-3', 'created, waiting state')

        def _t(self, *a):
            if self.trace:
                self.trace.w(*a)

        def log(self, line):
            self._t('LOG', line)

        # ---- デリゲート（メインスレッド）：値を取り出して渡すだけ
        def d_state(self, pm):
            st = int(_send(pm, 'state', 'q'))
            self.post(self._on_state, CB_STATES[st] if 0 <= st < len(CB_STATES) else str(st))

        def d_added(self, svc, err):
            self.post(self._on_added, _err_text(err))

        def d_adv(self, err):
            if err:
                self.post(self.core.log, '広告エラー: %s' % _err_text(err))

        def d_sub(self, central, ch):
            u = _uuid_of(ch)
            mx = int(_send(central, 'maximumUpdateValueLength', 'Q'))
            self.post(self._on_sub, u, mx)

        def d_unsub(self, central, ch):
            self.post(self.core.on_unsubscribe, _uuid_of(ch))

        def d_read(self, req):
            u = _uuid_of(_send(req, 'characteristic', '@'))
            off = int(_send(req, 'offset', 'Q'))
            self.post(self._on_read, _retain(req), u, off)

        def d_write(self, reqs):
            n = int(_send(reqs, 'count', 'Q'))
            items = []
            for i in range(n):
                r = _send(reqs, 'objectAtIndex:', '@Q', i)
                u = _uuid_of(_send(r, 'characteristic', '@'))
                items.append((_retain(r), u, _data_bytes(_send(r, 'value', '@'))))
            self.post(self._on_write, items)

        def d_ready(self):
            self.post(self.core.pump)

        # ---- 作業スレッド
        def _on_state(self, name):
            self.state = name
            self._t('CB<', 'state', name)
            self.core.log('Bluetooth: %s' % name)
            if name == 'poweredOn' and self.core.powered:
                self.build_services()

        def build_services(self):
            if self.services_ok or self.svc is not None:
                return
            chs = []
            self.chars = {}
            for u, props, perms in GATT:
                ch = _new('CBMutableCharacteristic', 'initWithType:properties:value:permissions:', '@@Q@Q',
                          _cbuuid(u), props, None, perms)
                self.chars[u] = ch                   # +1 のまま保持（解放しない）
                chs.append(ch)
            self.svc = _new('CBMutableService', 'initWithType:primary:', '@@B', _cbuuid(SVC_ECG), True)
            arr = _new_array(chs)
            _send(self.svc, 'setCharacteristics:', 'v@', arr)
            _release(arr)
            _cbcall('addService', self.pm, 'addService:', 'v@', self.svc)

        def _on_added(self, err):
            if err:
                self.core.log('サービス追加エラー: %s' % err)
                self.svc = None
                return
            self.services_ok = True
            self._t('CB<', 'didAddService')
            self.core.log('サービスを追加しました（%d 特性）' % len(self.chars))
            self._apply_adv()

        def _on_sub(self, u, mx):
            self._t('CB<', 'subscribe', CH_SHORT.get(u, u), mx)
            self.core.on_subscribe(u, mx)
            self.core.pump()

        def _on_read(self, req, u, off):
            try:
                data = self.core.on_read(u)
                if off > len(data):
                    _send(self.pm, 'respondToRequest:withResult:', 'v@q', req, ATT_INVALID_OFFSET)
                    return
                d = _new_data(data[off:])
                _send(req, 'setValue:', 'v@', d)
                _release(d)
                _send(self.pm, 'respondToRequest:withResult:', 'v@q', req, ATT_OK)
            finally:
                _release(req)
            self.core.pump()

        def _on_write(self, items):
            res = ATT_OK
            try:
                for _, u, val in items:
                    rr = self.core.on_write(u, val)
                    if rr != ATT_OK:
                        res = rr
                if items:
                    _send(self.pm, 'respondToRequest:withResult:', 'v@q', items[0][0], res)
            finally:
                for r, _, _ in items:
                    _release(r)
            self.core.pump()

        # ---- Hooks（PatchCore から作業スレッドで呼ばれる）
        def notify(self, uuid, data):
            ch = self.chars.get(uuid)
            if ch is None or not self.services_ok:
                return True                              # 捨てる
            d = _new_data(data)
            ok = bool(_send(self.pm, 'updateValue:forCharacteristic:onSubscribedCentrals:', 'B@@@', d, ch, None))
            _release(d)
            return ok

        def set_adv(self, name):
            self.adv_name = name
            self._apply_adv()

        def _apply_adv(self):
            if not self.services_ok:
                return
            _cbcall('stopAdvertising', self.pm, 'stopAdvertising', 'v')
            if self.adv_name:
                uuids = _new_array([_cbuuid(SVC_ECG)])
                name = _new_str(self.adv_name)
                d = _new_dict([(_const_str('kCBAdvDataLocalName'), name),
                               (_const_str('kCBAdvDataServiceUUIDs'), uuids)])
                _cbcall('startAdvertising ' + self.adv_name, self.pm, 'startAdvertising:', 'v@', d)
                for p in (d, name, uuids):
                    _release(p)

        def remove_services(self):
            _cbcall('stopAdvertising', self.pm, 'stopAdvertising', 'v')
            _cbcall('removeAllServices', self.pm, 'removeAllServices', 'v')
            self.services_ok = False
            self.kept.append(self.svc)                   # 外したサービスも閉じるまで解放しない
            self.svc = None

        def restore_services(self):
            if self.state == 'poweredOn':
                self.build_services()

        def shutdown(self):
            global _EMU
            try:
                if self.pm:
                    _cbcall('stopAdvertising', self.pm, 'stopAdvertising', 'v')
                    _cbcall('removeAllServices', self.pm, 'removeAllServices', 'v')
                    _send(self.pm, 'setDelegate:', 'v@', None)
            except Exception:
                pass
            _EMU = None

    # =========================================================================
    # 操作画面（画面は View.update の定期処理で表示するだけ。操作は作業スレッドへ渡す）
    #   r5：ui.delay は使わない（iPhone XS 実機で ui.delay＋作業スレッドだけでメモリ破損が出たため）
    # =========================================================================
    SETTINGS_PATH = os.path.expanduser('~/Documents/patch_emu_settings.json')

    def load_emu_settings():
        try:
            with open(SETTINGS_PATH, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {'device_id': 'A1B2C3D4'}

    def save_emu_settings(d):
        try:
            with open(SETTINGS_PATH, 'w', encoding='utf-8') as f:
                json.dump(d, f)
        except Exception:
            pass

    class EmuView(ui.View):
        def __init__(self, threaded=True):
            global _TRACE
            self.background_color = 'white'
            self.trace = EmuTrace(TRACE_PATH)
            _TRACE = self.trace
            self.trace.w('START', EMU_VERSION, 'python', sys.version.split()[0])
            here = os.path.abspath(__file__)
            if 'Mobile Documents' in here or 'CloudDocs' in here or os.path.basename(here) != 'patch_emu.py':
                self.trace.w('PLACE', here)
            try:
                import faulthandler
                self.fault_prev = []
                if os.path.exists(FAULT_PATH):
                    with open(FAULT_PATH, encoding='utf-8', errors='replace') as f:
                        self.fault_prev = [ln for ln in f.read().splitlines() if ln.strip()][-12:]
                self.fault_f = open(FAULT_PATH, 'w')
                faulthandler.enable(self.fault_f)
            except Exception:
                self.fault_f = None
            _delegate_class()                                  # ObjC クラスはここで 1 回だけ作る
            self.settings = load_emu_settings()
            dev = int(self.settings.get('device_id', 'A1B2C3D4'), 16)
            self.core = PatchCore(device_id=dev, now=time.monotonic(), wall=datetime.datetime.now)
            for ln in self.trace.prev:
                self.core.log_lines.append('[前回] ' + ln)
            for ln in self.fault_prev:
                self.core.log_lines.append('[前回落ちた位置] ' + ln)
            here = os.path.abspath(__file__)
            if 'Mobile Documents' in here or 'CloudDocs' in here or os.path.basename(here) != 'patch_emu.py':
                self.core.log_lines.append('!! 置き場所を確認：「この iPad 内 > Pythonista 3」に patch_emu.py の名前で置き、'
                                           'Pythonista から開いてください（今：%s）' % here)
            self.running = True
            self.closed = threading.Event()
            self.q = queue.Queue()
            self.threaded = threaded
            self.transport = PeripheralTransport(self.core, self.post, self.trace)
            self.last_tick = 0.0
            self.beats = 0
            self.t_start = time.monotonic()
            self.head = ui.Label(font=('<system-bold>', 17))
            self.sub = ui.Label(font=('<system>', 12), text_color='#5F6368', number_of_lines=2)
            self.add_subview(self.head)
            self.add_subview(self.sub)
            c_ = self.core
            self.btn_defs = [
                ('電極はがれ ON/OFF', lambda: c_.set_electrode(not c_.electrode_off), '#FCE8E6'),
                ('体動 0→1→2', self.cycle_motion, '#FEF7E0'),
                ('不整脈（休止）', lambda: c_.trigger_arrhythmia('pause'), '#FCE8E6'),
                ('不整脈（早期拍）', lambda: c_.trigger_arrhythmia('early'), '#FCE8E6'),
                ('不整脈（頻脈）', lambda: c_.trigger_arrhythmia('tachy'), '#FCE8E6'),
                ('電極異常イベント', lambda: c_.trigger_electrode_event(6.0), '#FCE8E6'),
                ('計測開始（S3）', lambda: c_.start_measuring(), '#E6F4EA'),
                ('計測開始（時刻なし）', lambda: c_.start_measuring(set_time=False), '#E6F4EA'),
                ('S2 に戻す（電源 ON）', lambda: c_.power_on_reset(), '#E6F4EA'),
                ('電源 OFF（S6）', lambda: c_.power_off(), '#F1F3F4'),
                ('故障：断片欠落 ×1', lambda: self.fault('drop_frag'), '#F3E8FD'),
                ('故障：CRC 異常 ×1', lambda: self.fault('bad_crc'), '#F3E8FD'),
                ('故障：BUSY ×2', lambda: self.fault('busy', 2), '#F3E8FD'),
                ('故障：NAND 異常 ×1', lambda: self.fault('nand'), '#F3E8FD'),
                ('故障：記録待ち +10 秒', self.extra_wait, '#F3E8FD'),
                ('チェックリスト消去', lambda: c_.checks.reset(), '#E8EEF7'),
                ('ID 変更', None, '#E8EEF7'),
                ('ログ書出し', None, '#E8EEF7'),
            ]
            self.buttons = []
            for title, act, bg in self.btn_defs:
                b = ui.Button(title=title)
                if title == 'ID 変更':
                    b.action = self.change_id
                elif title == 'ログ書出し':
                    b.action = self.export_log
                else:
                    b.action = self._button(title, act)
                b.background_color = bg
                b.tint_color = '#202124'
                b.corner_radius = 8
                b.font = ('<system-bold>', 13)
                self.add_subview(b)
                self.buttons.append(b)
            self.checks = ui.TextView(editable=False, font=('Menlo', 11))
            self.logv = ui.TextView(editable=False, font=('Menlo', 10))
            self.logv.background_color = '#202124'
            self.logv.text_color = '#E8EAED'
            self.add_subview(self.checks)
            self.add_subview(self.logv)
            self.started = False
            self._last_log_len = -1
            self.worker = threading.Thread(target=self.work_loop, name='emu_worker', daemon=True)

        def _button(self, title, fn):
            def action(sender):
                self.trace.w('UI', title)
                self.post(fn)                              # 操作は作業スレッドで行う
            return action

        def post(self, fn, *a):
            self.q.put((fn, a))

        # ---- 作業スレッド（BLE の操作・模擬器の処理）
        def work_loop(self):
            self.trace.w('WORKER', 'start')
            self.transport.start()
            while self.running:
                self.worker_step(block=True)

        def worker_step(self, block=False):
            self.beats += 1                                # 作業スレッドの生存確認（画面に表示）
            try:
                fn, a = self.q.get(timeout=0.02) if block else self.q.get_nowait()
            except queue.Empty:
                fn = None
            n = 0
            while fn is not None and n < 500:
                try:
                    fn(*a)
                except Exception as e:
                    self.core.log('例外: %s: %s' % (type(e).__name__, e))
                    self.trace.w('ERR', type(e).__name__, e)
                n += 1
                try:
                    fn, a = self.q.get_nowait()
                except queue.Empty:
                    fn = None
            now = time.monotonic()
            if now - self.last_tick >= 0.1:
                self.last_tick = now
                try:
                    self.core.tick(now)
                except Exception as e:
                    self.core.log('例外: %s: %s' % (type(e).__name__, e))
                    self.trace.w('ERR', type(e).__name__, e)

        def fault(self, name, n=1):
            self.core.faults[name] += n
            self.core.log('故障を予約: %s ×%d' % (name, n))

        def extra_wait(self):
            self.core.wait_extra += 10.0
            self.core.log('次の REQ_EVENT_WAVE で WAITING を %.0f 秒延長' % self.core.wait_extra)

        def cycle_motion(self):
            self.core.motion = (self.core.motion + 1) % 3
            self.core.log('体動 %d（%s）' % (self.core.motion, ['安静', '動き始め', '急激な衝撃'][self.core.motion]))

        @in_thread
        def change_id(self, sender):
            try:
                s = console.input_alert('デバイス ID（16 進 8 桁）', '空欄で乱数', '%08X' % self.core.device_id, 'OK')
            except KeyboardInterrupt:
                return
            s = (s or '').strip().upper()
            if not re.match(r'^[0-9A-F]{8}$', s):
                s = '%08X' % random.getrandbits(32)
            self.settings['device_id'] = s
            save_emu_settings(self.settings)

            def apply():
                self.core.device_id = int(s, 16)
                self.core.adv_name = None                   # 次の tick で広告し直す
                self.core.log('ID を %s に変更しました' % s)
            self.post(apply)

        @in_thread
        def export_log(self, sender):
            p = os.path.expanduser('~/Documents/patch_emu_log_%s.txt' % datetime.datetime.now().strftime('%Y%m%d_%H%M%S'))
            with open(p, 'w', encoding='utf-8') as f:
                f.write(self.core.checks.text() + '\n\n' + '\n'.join(list(self.core.log_lines)) + '\n')
            console.open_in(p)

        def layout(self):
            w, h = self.width, self.height
            top = 44 if h > w else 24
            m = 10
            self.head.frame = (m, top, w - 2 * m, 24)
            self.sub.frame = (m, top + 24, w - 2 * m, 36)
            y = top + 64
            cols = 4 if w >= 700 else 3
            bw = (w - (cols + 1) * m) / cols
            for i, b in enumerate(self.buttons):
                r, cidx = divmod(i, cols)
                b.frame = (m + cidx * (bw + m), y + r * 44, bw, 38)
            rows = (len(self.buttons) + cols - 1) // cols
            y += rows * 44 + 4
            rest = h - y - m
            if w >= 700:
                self.checks.frame = (m, y, (w - 3 * m) * 0.5, rest)
                self.logv.frame = (2 * m + (w - 3 * m) * 0.5, y, (w - 3 * m) * 0.5, rest)
            else:
                self.checks.frame = (m, y, w - 2 * m, rest * 0.45)
                self.logv.frame = (m, y + rest * 0.45 + 6, w - 2 * m, rest * 0.55 - 6)
            if not self.started:
                self.started = True
                if self.threaded:
                    self.worker.start()
                else:                                         # 試験用：作業スレッドなしで動かす
                    self.transport.start()
                self.update_interval = 0.25                  # View の update() で画面を更新

        def update(self):
            if not self.running:
                return
            try:
                self.refresh()
            except Exception as e:
                self.trace.w('ERR', 'refresh', type(e).__name__, e)

        def refresh(self):
            c_ = self.core
            self.head.text = 'ECG パッチ模擬器 %s  ECGP-%08X  [%s]' % (EMU_VERSION.split()[-1], c_.device_id, c_.state_name)
            bt = self.transport.state
            self.sub.text = 'Bluetooth: %s   %s\n広告: %s   電極 %s   体動 %d   送信 %s B   起動 %d 秒・作業 %d' % (
                bt, c_.summary(), c_.adv_name or '停止', 'NG' if c_.electrode_off else 'OK', c_.motion,
                format(c_.stats['bytes_ECG'], ','), time.monotonic() - self.t_start, self.beats)
            self.checks.text = c_.checks.text()
            lines = list(c_.log_lines)
            if len(lines) != self._last_log_len or (lines and lines[-1] != getattr(self, '_last_line', None)):
                self._last_log_len = len(lines)
                self._last_line = lines[-1] if lines else None
                self.logv.text = '\n'.join(reversed(lines[-150:]))

        def will_close(self):
            done = threading.Event()

            def stop():
                self.transport.shutdown()
                done.set()
            if self.threaded and self.worker.is_alive():
                self.post(stop)
                done.wait(2.0)
            else:
                stop()
            self.running = False
            self.trace.w('STOP', 'closed')
            self.trace.close()
            self.closed.set()


def main():
    if not ON_PYTHONISTA:
        print('このスクリプトは iPad／iPhone の Pythonista 3 で実行してください（PC では tests/ から PatchCore を使います）。')
        return
    v = EmuView()
    v.name = 'ECG パッチ模擬器 ' + EMU_VERSION
    v.present('fullscreen', hide_title_bar=False)
    v.closed.wait()                       # r17 No.2：画面を閉じるまでスクリプトを終わらせない


if __name__ == '__main__':
    main()
