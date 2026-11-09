# -*- coding: utf-8 -*-
"""
ecg_patch_app.py  ―  ECG パッチ iPhone アプリケーション（Pythonista 3）

  仕様書 : AISILK-SPEC-ECG-001-C Rev0.7（ECG パッチ iPhone アプリケーション ソフトウェア仕様書）
  実行環境: Pythonista 3.4（Python 3.10）／ iPhone（iOS 26 以上）
  配置   : 「この iPhone 内 > Pythonista 3」に置き、Pythonista から実行する

配布形式（5.8 節）に合わせて 1 ファイルにまとめている。各節の見出しに機能ブロック番号
（A01〜A20）を付けた。CoreBluetooth は cb モジュールを使わず、objc_util＋ctypes の
型固定 objc_msgSend（_send）で直接呼ぶ（4.3 節・9.3 節）。

端末に依存しない部分（プロトコル・ページ再構成・時刻補完・切り出し・保存・解析・
セッション）は純粋な Python なので、PC（CPython 3.10 以上）で import して試験できる
（tests/ を参照）。
"""
import os
import re
import sys
import json
import time
import queue
import struct
import random
import threading
import datetime
import traceback
import collections
import math
from array import array
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

try:                                    # Pythonista だけにあるモジュール
    import ui
    import console
    import dialogs
    import sound
    import ctypes
    from objc_util import ObjCClass, create_objc_class, c     # クラス作成だけに使う（r17 No.10・11）
    ON_PYTHONISTA = True
except ImportError:                     # PC（試験）では端末依存の部分を使わない
    ON_PYTHONISTA = False

# =============================================================================
# 6.1 定数・付録 A データ定義
# =============================================================================
APP_VERSION = '2026-11-09 r10'
DEV_BUILD = True                        # 開発版（accept_emulator・画面試験を表示）
# 診断の切替え（ecg_diag_iphone.py が書き換える）。通常はすべて True。
#   draw：自作の描画（ui.View.draw）、objc_class：デリゲートのクラス作成、ble：BLE の開始、probe：スレッド間の検査
DIAG = {'stage': '', 'draw': True, 'objc_class': True, 'ble': True, 'probe': True}

FS, CYCLE, PAGE_CYCLES, PAGE_SAMPLES = 256, 128, 8, 1024
PAGE_BODY, PAGE_SPARE, PAGE_USER, FMT_V1 = 2048, 64, 2112, 0xA1
SETUP_NEED_CYCLES = 60                  # 電極正常の連続 30 秒
EVENT_HALF_SAMPLES = 3840               # 異常通知の前後 15 秒
CONNECT_TIMEOUT = 15.0
PAGE_IDLE_TIMEOUT = 8.0
SETUP_TOTAL_TIMEOUT = 120.0
EVENT_WAVE_TIMEOUT = 45.0
CMD_RESULT_TIMEOUT = 5.0
ACK_DISC_WAIT = 5.0                     # ACK 後にパッチからの切断を待つ時間（9.6 節）
DISC_TIMEOUT = 2.0                      # 本アプリからの切断の完了待ち（A07）
WAITING_LONG = 20.0                     # 記録待ちが長いときの表示（13.10.4）
ALERT_CONNECT_LIMIT = 60.0              # 異常通知で接続できないとき（13.10.4）
UI_TICK = 0.2
MAX_SESSION_PAGES = 40
REQ_PAGES_MAX = 3

UUID_TAIL = '-6E0C-4C1B-8A5D-3E2F7C9B0D01'               # 案
SVC_ECG = 'A1510001' + UUID_TAIL
CH_STREAM = 'A1510002' + UUID_TAIL
CH_EVENTLOG = 'A1510003' + UUID_TAIL
CH_STATUS = 'A1510004' + UUID_TAIL
CH_CONTROL = 'A1510005' + UUID_TAIL
CH_DEBUG = 'A1510006' + UUID_TAIL                       # Debug Info（任意）
CH_EMU = 'A15100EE' + UUID_TAIL                         # Emulator Info（模擬器のみ）
SVC_DIS, CH_SERIAL = '180A', '2A25'
SVC_BAT, CH_BATT = '180F', '2A19'
CH_SHORT = {CH_STREAM: 'ECG', CH_EVENTLOG: 'EVT', CH_STATUS: 'STS', CH_CONTROL: 'CTL',
            CH_DEBUG: 'DBG', CH_SERIAL: 'SN', CH_BATT: 'BAT', CH_EMU: 'EMU'}

PF_ARR, PF_ELEC, PF_NOTIFIED, PF_TIME_UNSET, PF_SHORT, PF_REBOOT, PF_TIME_EST = (1 << i for i in range(7))
CMD_SET_TIME, CMD_SET_PID, CMD_REQ_SETUP, CMD_ACK_SETUP = 0x01, 0x02, 0x10, 0x11
CMD_REQ_EVENT, CMD_ACK_EVENT, CMD_REQ_PAGES, CMD_STOP = 0x20, 0x21, 0x30, 0x40
CMD_NAMES = {0x01: 'SET_TIME', 0x02: 'SET_PATIENT_ID', 0x10: 'REQ_SETUP_WAVE', 0x11: 'ACK_SETUP',
             0x20: 'REQ_EVENT_WAVE', 0x21: 'ACK_EVENT', 0x30: 'REQ_PAGES', 0x40: 'STOP'}
RES_NAMES = ['OK', 'BAD_LEN', 'BAD_STATE', 'ALREADY', 'RANGE', 'NAND_ERR', 'WAITING', 'UNKNOWN', 'BUSY']
RES_OK, RES_BAD_LEN, RES_BAD_STATE, RES_ALREADY, RES_RANGE, RES_NAND, RES_WAITING, RES_UNKNOWN, RES_BUSY = range(9)

ST_S2, ST_S3, ST_S5, ST_S6 = 2, 3, 5, 6                 # Device Status の state（Sn の n）
EV_SETUP, EV_ELEC, EV_ARR, EV_BATT = 0x01, 0x02, 0x03, 0x04
# 広告の Status（付属 B 16.2 節。本体 No.26：r10 で付属 B に合わせた。r9 までは bit0＝時刻・bit2＝電極）
ADV_ST_ELEC, ADV_ST_UNNOTIFIED, ADV_ST_TIMESET, ADV_ST_REC = 0x01, 0x02, 0x04, 0x08
COMPANY_DEV = 0xFFFF                                     # 開発中の Company ID（案）

HDR = struct.Struct('<BI6BBBBH3hBHH')    # 27B ページヘッダ（spare 0..26。本体 7.8 節 Rev1.2・No.44：fs＝23〜24、CRC＝25〜26）
TILT_NONE = -32768                       # 傾きは記録しない（0x8000。本体 No.34）
EVREC = struct.Struct('<4BIIH6BHHBIBH')  # 32B イベント
STAT = struct.Struct('<10BIIH')          # 20B Device Status
FRAG = struct.Struct('<4B')              # 4B  断片ヘッダ
MFR = struct.Struct('<HIBBIBB')          # 14B Manufacturer データ（Company ID を含む）
DBGINFO = struct.Struct('<BBBBHHHHBbBBIHHHHI')  # 32B Debug Info（付属 B 16.11）
EMUINFO = struct.Struct('<IBBBB8s')      # 16B Emulator Info（付属 D 15.4、device_id が先頭）

ARR_SUB = {1: '頻脈', 2: '徐脈', 3: '長いポーズ', 4: '期外収縮', 5: '心房細動', 6: '早期拍', 7: '休止'}
ARR_SUB_EN = {1: 'tachy', 2: 'brady', 3: 'long_pause', 4: 'pvc', 5: 'af', 6: 'early', 7: 'pause'}
ELEC_SUB = {1: 'リードオフ', 2: '信号飽和', 3: 'フラット', 4: 'ノイズ'}
ELEC_SUB_EN = {1: 'lead_off', 2: 'saturation', 3: 'flat', 4: 'noise'}

COL = {'green': '#2E9E4F', 'red': '#D93025', 'blue': '#1F5FA8', 'orange': '#E07B00',
       'gray': '#9AA0A6', 'ink': '#1A2433', 'paper': '#FFF7F7', 'grid1': '#F6CACA',
       'grid2': '#E58E8E', 'leadoff': '#BDBDBD', 'motion': '#FFF3B0', 'shock': '#E9D5FF',
       'purple': '#7B3FB5', 'bg': '#FFFFFF', 'light': '#F1F3F4'}


def crc16_ccitt_false(data, crc=0xFFFF):
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def hexs(b, limit=None):
    b = bytes(b)
    if limit is not None and len(b) > limit:
        return ' '.join('%02X' % x for x in b[:limit]) + ' …'
    return ' '.join('%02X' % x for x in b)


def event_title(event, sub):
    """13.10.2 節：種別の表示"""
    motion = '（体動中）' if sub & 0x80 else ''
    s = sub & 0x7F
    if event == EV_ARR:
        t = '不整脈の疑い（%s）' % ARR_SUB.get(s, 'サブ %d' % s)
    elif event == EV_ELEC:
        t = '電極異常（%s）' % ELEC_SUB.get(s, 'サブ %d' % s)
    elif event == EV_BATT:
        t = '電池低下'
    elif event == EV_SETUP:
        t = '装着確認'
    else:
        t = 'イベント %02X' % event
    return t + motion


def kind_text(event):
    return {EV_SETUP: '装着確認', EV_ELEC: '電極異常', EV_ARR: '不整脈疑い', EV_BATT: '電池低下'}.get(event, '—')


# =============================================================================
# A16 エラー処理（5.6 節）
# =============================================================================
class EcgAppError(Exception):
    user_message = '予期しないエラーが発生しました'
    action = 'log'                      # log / retry / abort_session / restart_scan / stop_app


class BleError(EcgAppError):
    action = 'restart_scan'


class ScanError(BleError):
    user_message = 'Bluetooth が使えません'


class ConnectError(BleError):
    user_message = 'パッチに接続できませんでした'


class BleTimeout(BleError):
    user_message = 'パッチから応答がありません'


class WriteError(BleError):
    action = 'retry'


class ProtocolError(EcgAppError):
    action = 'log'


class CrcError(ProtocolError):
    action = 'retry'
    user_message = '一部を受信できませんでした'


class ResultCodeError(ProtocolError):
    def __init__(self, code, msg=''):
        super().__init__(msg or 'result=%s' % code)
        self.code = code


class StorageError(EcgAppError):
    user_message = 'ファイルを保存できませんでした'
    action = 'abort_session'


class AnalysisError(EcgAppError):
    action = 'log'


class ErrorHandler:
    """例外の対処表（5.6 節）。trace と画面通知（sink）へ記録する。"""
    _inst = None

    def __init__(self, trace=None, sink=None):
        self.trace = trace
        self.sink = sink                # sink(kind, text)
        self.count = 0
        ErrorHandler._inst = self

    @classmethod
    def instance(cls):
        if cls._inst is None:
            cls._inst = ErrorHandler()
        return cls._inst

    def handle(self, e, where=''):
        self.count += 1
        if isinstance(e, EcgAppError):
            act, msg = e.action, e.user_message
        else:
            act, msg = 'log', 'BLE エラー: %s: %s' % (type(e).__name__, e)
        if self.trace:
            self.trace.w('ERR', where, type(e).__name__, str(e), 'action=' + act)
            if not isinstance(e, EcgAppError):
                self.trace.w('TB', safe_tb(e))
        if self.sink:
            try:
                self.sink('error', msg)
            except Exception:
                pass
        return act


def safe_tb(e, limit=12):
    """ファイルを読まずに「ファイル名:行」だけを並べる。r1 の実機ではメモリが壊れた後に
    traceback.format_exc()（linecache でソースを開く）の中で落ちたため、例外処理では使わない。"""
    out = []
    tb = getattr(e, '__traceback__', None)
    while tb is not None:
        co = tb.tb_frame.f_code
        out.append('%s:%d %s' % (os.path.basename(co.co_filename), tb.tb_lineno, co.co_name))
        tb = tb.tb_next
    return ' > '.join(out[-limit:])


def in_thread(fn):
    """ui.in_background の代わり。専用のスレッドで実行する（r17 No.3）。
    ui.in_background はスクリプトのスレッドで実行されるが、本アプリはそのスレッドを
    closed.wait() で止めている（r17 規則 7）ため、ui.in_background の処理は永久に実行されない（r5 実機で ✖・接続・設定が無反応）。"""
    def wrapper(*a, **k):
        threading.Thread(target=guard(fn), args=a, kwargs=k, name='ui_' + fn.__name__, daemon=True).start()
    wrapper.__name__ = fn.__name__
    return wrapper


def guard(fn):
    """ObjC デリゲート・スレッドの入口に付ける。例外を外に出さない。"""
    def wrapper(*a, **k):
        try:
            return fn(*a, **k)
        except Exception as e:
            ErrorHandler.instance().handle(e, where=getattr(fn, '__name__', '?'))
    wrapper.__name__ = getattr(fn, '__name__', 'guarded')
    return wrapper


# =============================================================================
# 共通：時刻（Clock）・保存先（FileStore）・動作記録（A17 Trace）
# =============================================================================
class Clock(ABC):
    @abstractmethod
    def now(self) -> float: ...

    @abstractmethod
    def local_datetime(self): ...


class SystemClock(Clock):
    def now(self):
        return time.monotonic()

    def local_datetime(self):
        return datetime.datetime.now()


class FakeClock(Clock):
    """試験用：advance(秒) で進める"""
    def __init__(self, start=None):
        self.t = 0.0
        self.base = start or datetime.datetime(2026, 11, 21, 10, 15, 0)

    def advance(self, s):
        self.t += s

    def now(self):
        return self.t

    def local_datetime(self):
        return self.base + datetime.timedelta(seconds=self.t)


class FileStore(ABC):
    @abstractmethod
    def write_atomic(self, relpath: str, data: bytes) -> str: ...


class DocumentsStore(FileStore):
    """実機＝~/Documents（Pythonista 3 フォルダ）、試験＝一時フォルダ。"""
    def __init__(self, root):
        self.root = root
        self.fail_next = False          # 試験用：書込み途中の異常（A11-04）

    def path(self, relpath):
        return os.path.join(self.root, relpath)

    def exists(self, relpath):
        return os.path.exists(self.path(relpath))

    def makedirs(self, reldir):
        os.makedirs(self.path(reldir), exist_ok=True)

    def write_atomic(self, relpath, data):
        p = self.path(relpath)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + '.tmp'
        try:
            with open(tmp, 'wb') as f:
                f.write(data)
                if self.fail_next:
                    self.fail_next = False
                    raise IOError('injected write failure')
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, p)
        except Exception as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise StorageError(str(e))
        return p

    def append_line(self, relpath, line):
        p = self.path(relpath)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
        return p


class Trace:
    """A17：r17 と同じ 1 行ずつの動作記録。前回分は _prev に移す。"""
    def __init__(self, path=None, verbose=True, echo=False):
        self.path = path
        self.verbose = verbose
        self.echo = echo
        self.lines = collections.deque(maxlen=400)
        self.lock = threading.Lock()
        self.f = None
        if path:
            try:
                if os.path.exists(path):
                    os.replace(path, path.replace('.txt', '_prev.txt'))
                self.f = open(path, 'a', encoding='utf-8')
            except Exception:
                self.f = None

    def w(self, *parts):
        line = '%s %s' % (datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3],
                          ' '.join(str(p) for p in parts))
        with self.lock:
            self.lines.append(line)
            if self.f:
                try:
                    self.f.write(line + '\n')
                    self.f.flush()
                    os.fsync(self.f.fileno())        # r17 No.9：落ちても直前の行が残る
                except Exception:
                    pass
        if self.echo:
            print(line)

    def v(self, *parts):                # 詳細記録（verbose_trace が OFF なら書かない）
        if self.verbose:
            self.w(*parts)

    def close(self):
        with self.lock:
            if self.f:
                self.f.close()
                self.f = None

    @staticmethod
    def tail(path, n=25):
        try:
            with open(path, encoding='utf-8', errors='replace') as f:
                return f.read().splitlines()[-n:]
        except Exception:
            return []


# =============================================================================
# 6.4 設定ファイル（ecg_patch_settings.json）
# =============================================================================
DEFAULT_SETTINGS = {
    'version': 1,
    'registered': [],
    'auto_connect_alert': True,
    'auto_fetch_unnotified': True,
    'save_pages_bin': True,
    'verbose_trace': True,
    'list_expire_s': 10,
    'dev_display': False,
    'ble_trace': True,
    'accept_emulator': False,
}


class Settings:
    def __init__(self, path=None, trace=None):
        self.path = path
        self.trace = trace
        self.d = json.loads(json.dumps(DEFAULT_SETTINGS))
        self.load_error = None

    def load(self):
        if not self.path or not os.path.exists(self.path):
            self.save()
            return self
        try:
            with open(self.path, encoding='utf-8') as f:
                d = json.load(f)
            if not isinstance(d, dict):
                raise ValueError('not an object')
            for k, v in DEFAULT_SETTINGS.items():
                self.d[k] = d.get(k, v)
        except Exception as e:          # A01-02：壊れていたら既定値で起動し、記録に残す
            self.load_error = str(e)
            if self.trace:
                self.trace.w('SETTINGS', 'broken, use defaults:', e)
        return self

    def save(self):
        if not self.path:
            return
        tmp = self.path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self.d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def __getitem__(self, k):
        return self.d.get(k, DEFAULT_SETTINGS.get(k))

    def __setitem__(self, k, v):
        self.d[k] = v

    def is_registered(self, device_id):
        return any(r.get('device_id') == device_id for r in self.d['registered'])

    def register(self, device_id, result, now_dt):
        ts = now_dt.strftime('%Y-%m-%d %H:%M:%S')
        for r in self.d['registered']:
            if r.get('device_id') == device_id:
                if result:
                    r['setup_at'], r['setup_result'] = ts, result
                r['last_seen'] = ts
                break
        else:
            self.d['registered'].append({'device_id': device_id, 'setup_at': ts,
                                         'setup_result': result or 'ok', 'last_seen': ts})
        self.save()


# =============================================================================
# proto：アドバタイズ・Device Status・イベント・コマンド（9.1・9.4・10.1.3 節）
# =============================================================================
@dataclass
class AdvInfo:
    peer: str
    device_id: str
    name: str
    event: int
    sub: int
    ts: int
    battery: int
    status: int
    rssi: int = 0
    last_seen: float = 0.0
    emulator: bool = False
    company: int = COMPANY_DEV

    @property
    def unnotified(self):
        return bool(self.status & ADV_ST_UNNOTIFIED)

    @property
    def time_set(self):
        return bool(self.status & ADV_ST_TIMESET) or (not self.emulator and self.ts != 0)

    @property
    def electrode_ng(self):
        return bool(self.status & ADV_ST_ELEC)

    @property
    def label(self):
        return 'ECGP-' + self.device_id


EMU_NAME_RE = re.compile(r'^ECGP-([0-9A-Fa-f]{8})~([0-9A-Fa-f]{8})$')


def parse_mfr(b):
    company, dev_id, event, sub, ts, batt, status = MFR.unpack(bytes(b[:14]))
    return dict(company=company, device_id='%08X' % dev_id, event=event, sub=sub,
                ts=ts, battery=batt, status=status)


def parse_adv(peer, mfr, name, rssi, now, accept_emulator):
    """Manufacturer データ（本物）または名前（模擬器）から AdvInfo を作る。対象外は None。"""
    if mfr is not None and len(mfr) >= 14:
        d = parse_mfr(mfr)
        if d['company'] != COMPANY_DEV:
            return None
        return AdvInfo(peer, d['device_id'], name or 'ECGP-' + d['device_id'], d['event'], d['sub'],
                       d['ts'], d['battery'], d['status'], rssi, now, False, d['company'])
    if mfr is not None and len(mfr) > 0:
        raise ProtocolError('short manufacturer data %d' % len(mfr))   # A03-04
    if accept_emulator and name:
        m = EMU_NAME_RE.match(name)
        if m:
            x = bytes.fromhex(m.group(2))
            return AdvInfo(peer, m.group(1).upper(), name, x[0], x[1], 0, x[3], x[2], rssi, now, True)
    return None


def is_emulator_name(name):
    return bool(name and EMU_NAME_RE.match(name))


STAT_FIELDS = ('ver', 'state', 'electrode', 'alarm', 'batt', 'unnotified', 'time_set', 'last_cmd',
               'result', 'rsv', 'latest_page_seq', 'base_page_seq', 'fw_ver')


@dataclass
class DevStatus:
    ver: int
    state: int
    electrode: int
    alarm: int
    batt: int
    unnotified: int
    time_set: int
    last_cmd: int
    result: int
    rsv: int
    latest_page_seq: int
    base_page_seq: int
    fw_ver: int

    @classmethod
    def from_bytes(cls, b):
        if len(b) != STAT.size:
            raise ProtocolError('status length %d' % len(b))
        return cls(*STAT.unpack(bytes(b)))


@dataclass
class EventRec:
    rec_ver: int
    event: int
    sub: int
    status: int
    event_seq: int
    page_seq: int
    sample_ofs: int
    ts: tuple
    rr_ms: int
    median_ms: int
    hr_bpm: int
    wave_start: int
    wave_pages: int
    crc: int
    crc_ok: bool = True

    @classmethod
    def from_bytes(cls, b):
        b = bytes(b)
        if len(b) != EVREC.size:
            raise ProtocolError('event length %d' % len(b))
        v = EVREC.unpack(b)
        ok = crc16_ccitt_false(b[:30]) == v[18]
        return cls(v[0], v[1], v[2], v[3], v[4], v[5], v[6], tuple(v[7:13]), v[13], v[14], v[15],
                   v[16], v[17], v[18], ok)

    @property
    def title(self):
        return event_title(self.event, self.sub)

    @property
    def time_text(self):
        y, mo, d, h, mi, s = self.ts
        if mo == 0:
            return '時刻未設定'
        return '%04d-%02d-%02d %02d:%02d:%02d' % (2000 + y, mo, d, h, mi, s)

    @property
    def detect_index(self):
        return self.page_seq * PAGE_SAMPLES + self.sample_ofs


@dataclass
class Command:
    opcode: int
    params: bytes = b''

    def to_bytes(self):
        return bytes([self.opcode]) + bytes(self.params)

    @property
    def name(self):
        return CMD_NAMES.get(self.opcode, 'CMD_%02X' % self.opcode)

    @staticmethod
    def set_time(now):
        return Command(CMD_SET_TIME, bytes([now.year - 2000, now.month, now.day, now.hour, now.minute, now.second]))

    @staticmethod
    def set_patient_id(pid):
        a = pid.encode('ascii', 'replace')[:16]
        return Command(CMD_SET_PID, bytes([len(a)]) + a)

    @staticmethod
    def req_setup():
        return Command(CMD_REQ_SETUP)

    @staticmethod
    def ack_setup(ok=True):
        return Command(CMD_ACK_SETUP, bytes([0 if ok else 1]))

    @staticmethod
    def req_event(seq):
        return Command(CMD_REQ_EVENT, struct.pack('<I', seq))

    @staticmethod
    def ack_event(seq):
        return Command(CMD_ACK_EVENT, struct.pack('<I', seq))

    @staticmethod
    def req_pages(seq, n):
        return Command(CMD_REQ_PAGES, struct.pack('<IH', seq, n))

    @staticmethod
    def stop():
        return Command(CMD_STOP)


DBG_FIELDS = ('ver', 'state', 'ble_sub', 'phy', 'conn_interval', 'slave_latency', 'sup_timeout', 'att_mtu',
              'tx_free', 'rssi', 'last_disc', 'sec', 'notify_ok', 'notify_busy', 'cmd_rx', 'cmd_err',
              'conn_count', 'uptime_s')
PHY_TXT = {1: '1M', 2: '2M', 3: 'Coded'}


def parse_dbg_info(b):
    d = dict(zip(DBG_FIELDS, DBGINFO.unpack(bytes(b[:32]))))
    d['int_ms'] = d['conn_interval'] * 1.25
    d['to_s'] = d['sup_timeout'] / 100
    d['phy_txt'] = '%s/%s' % (PHY_TXT.get(d['phy'] & 15, '?'), PHY_TXT.get(d['phy'] >> 4, '?'))
    return d


# =============================================================================
# A08 ページ再構成・A09 ページ解析（10.1 節）
# =============================================================================
@dataclass
class Page:
    seq: int
    ts: tuple
    n_cycles: int
    flags: int
    elec: int
    accel: int
    tilt: tuple
    batt: int
    crc_ok: bool
    samples: array = field(repr=False)
    fs: int = FS                    # サンプリング周波数（Hz。offset 23〜24）

    @classmethod
    def from_bytes(cls, b):
        if len(b) != PAGE_USER:
            raise ProtocolError('page length %d' % len(b))
        (fmt, seq, yy, mo, dd, hh, mi, ss, ncyc, flags, elec, accel,
         tx, ty, tz, batt, fs, crc) = HDR.unpack_from(b, PAGE_BODY)
        if fmt != FMT_V1:
            raise ProtocolError('page format %02X' % fmt)
        ok = crc16_ccitt_false(b[:PAGE_BODY + 25]) == crc   # CRC の対象は本体＋スペア 0〜24（本体 No.44）
        s = array('h')
        s.frombytes(bytes(b[:PAGE_BODY]))
        if sys.byteorder != 'little':
            s.byteswap()
        ncyc = max(1, min(PAGE_CYCLES, ncyc))
        return cls(seq, (2000 + yy, mo, dd, hh, mi, ss), ncyc, flags, elec, accel, (tx, ty, tz), batt, ok, s, fs)

    def cycle_electrode(self, k):
        return (self.elec >> k) & 1

    def cycle_accel(self, k):
        return (self.accel >> (2 * k)) & 3

    @property
    def has_time(self):
        return not (self.flags & PF_TIME_UNSET) and 1 <= self.ts[1] <= 12

    @property
    def batt_text(self):
        """電池状態（offset 22。コンパレータ方式：bit0＝90% 以上・bit1＝30% 未満。0xFF＝未測定）"""
        if self.batt == 0xFF:
            return '未測定'
        return '90%以上' if self.batt & 1 else ('30%未満' if self.batt & 2 else '30〜90%')


class PageAssembler:
    """断片（xfer_id, page_idx, frag_idx, flags）を連結して 2,112B のページを作る。"""
    def __init__(self):
        self.key = None
        self.buf = bytearray()
        self.next_frag = 0
        self.xfer = None
        self.stats = collections.Counter()   # frags, pages, gaps, dups, xfer_change, bad_len

    def reset(self):
        self.key, self.buf, self.next_frag = None, bytearray(), 0

    @property
    def partial_bytes(self):
        return len(self.buf) if self.key else 0

    def feed(self, frag):
        if len(frag) < FRAG.size:
            raise ProtocolError('fragment length %d' % len(frag))
        xfer, pidx, fidx, flags = FRAG.unpack_from(frag, 0)
        self.stats['frags'] += 1
        if self.xfer is not None and xfer != self.xfer:
            self.stats['xfer_change'] += 1
            self.reset()                       # A08-03：前の再構成を破棄
        self.xfer = xfer
        if (xfer, pidx) != self.key:
            if self.key is not None:
                self.stats['gaps'] += 1        # 前のページの最終断片が来なかった
            self.key, self.buf, self.next_frag = (xfer, pidx), bytearray(), 0
        if fidx != self.next_frag:             # 飛び・重複 → このページは破棄（後で REQ_PAGES）
            self.stats['dups' if fidx < self.next_frag else 'gaps'] += 1
            self.key = None
            self.buf = bytearray()
            return None
        self.buf += frag[4:]
        self.next_frag += 1
        if flags & 0x01:                       # ページ最終断片
            page, self.key = bytes(self.buf), None
            self.buf = bytearray()
            if len(page) == PAGE_USER:
                self.stats['pages'] += 1
                return page
            self.stats['bad_len'] += 1
        return None


# =============================================================================
# A10 時刻補完・切り出し（10.3・10.4 節）
# =============================================================================
@dataclass
class Record:
    kind: str                       # setup30s / event_elec / event_arr / partial
    ecg: array
    meta: list                      # [(page, time_str, cycle, elec, accel, tx, ty, tz, arr)]
    first_time: datetime.datetime
    estimated: bool
    pages: list                     # 使ったページ通番
    detect_index: int = -1          # 異常通知：ecg 内の検知位置（サンプル）


def fix_times(pages, base_seq, now_dt, latest_seq=None, anchor=None):
    """page_seq → (ページ先頭時刻, 推定か)。10.3 節。
    anchor＝(base_seq, SET_TIME で送った時刻)：時刻のあるページを 1 枚も受信していないときの推定に使う。"""
    ref = {}
    for seq, p in pages.items():
        if p.has_time:
            try:
                ref[seq] = datetime.datetime(*p.ts)
            except ValueError:
                pass
    out = {}
    if latest_seq is None:
        latest_seq = max(pages) if pages else 0
    for seq, p in pages.items():
        est = bool(p.flags & PF_TIME_EST)
        if seq in ref:
            dt = ref[seq]
        elif base_seq in ref:
            dt = ref[base_seq] - datetime.timedelta(seconds=(base_seq - seq) * 4)
        elif ref:
            near = min(ref, key=lambda s: abs(s - seq))
            dt = ref[near] - datetime.timedelta(seconds=(near - seq) * 4)
        elif anchor is not None:        # 時刻のあるページがない：SET_TIME の時刻から推定（±4 秒）
            dt = anchor[1] - datetime.timedelta(seconds=(anchor[0] - seq) * 4)
            est = True
        else:                           # iPhone の時計から推定
            dt = now_dt - datetime.timedelta(seconds=(latest_seq - seq + 1) * 4)
            est = True
        out[seq] = (dt, est)
    return out


def _segments(seqs):
    seg = []
    for s in sorted(seqs):
        if seg and s != seg[-1] + 1:
            yield seg
            seg = []
        seg.append(s)
    if seg:
        yield seg


def _time_str(dt, est):
    return ('~' if est else '') + dt.strftime('%Y-%m-%d %H:%M:%S') + '.%d' % (dt.microsecond // 100000)


def build_record(kind, pages, times, cycles, detect_index=-1):
    """cycles: [(seq, k)] → Record"""
    ecg = array('h')
    meta = []
    est_any = False
    for seq, k in cycles:
        p = pages[seq]
        ecg.extend(p.samples[k * CYCLE:(k + 1) * CYCLE])
        dt, est = times[seq]
        est_any |= est
        t = dt + datetime.timedelta(seconds=0.5 * k)
        meta.append((seq, _time_str(t, est), k, p.cycle_electrode(k), p.cycle_accel(k),
                     p.tilt[0], p.tilt[1], p.tilt[2], 1 if p.flags & PF_ARR else 0))
    s0, k0 = cycles[0]
    first = times[s0][0] + datetime.timedelta(seconds=0.5 * k0)
    return Record(kind, ecg, meta, first, est_any, sorted(set(s for s, _ in cycles)), detect_index)


class Cutter:
    @staticmethod
    def setup_window(pages, times, need=SETUP_NEED_CYCLES):
        """電極正常の周期が need 個以上続く区間のうち最新の区間の末尾 need 周期。"""
        best = None
        for seg in _segments(pages):
            run = []
            for seq in seg:
                p = pages[seq]
                for k in range(p.n_cycles):
                    if p.cycle_electrode(k) == 0:
                        run.append((seq, k))
                        if len(run) >= need:
                            best = run[-need:]
                    else:
                        run = []
                if p.n_cycles < PAGE_CYCLES:    # 途中の短いページで区間を切る
                    run = []
        if best is None:
            return None
        return build_record('setup30s', pages, times, best)

    @staticmethod
    def normal_run_cycles(pages):
        """最新ページから遡った電極正常の連続周期数（表示用）"""
        n = 0
        segs = list(_segments(pages))
        if not segs:
            return 0
        for seq in reversed(segs[-1]):
            p = pages[seq]
            for k in reversed(range(p.n_cycles)):
                if p.cycle_electrode(k):
                    return n
                n += 1
        return n

    @staticmethod
    def event_window(pages, times, page_seq, sample_ofs, kind='event_arr', half=EVENT_HALF_SAMPLES):
        """検知位置 t を中心に [t−3840, t+3840) を切り出す。周期境界にそろえる。"""
        t = page_seq * PAGE_SAMPLES + sample_ofs
        lo = t - half
        lo -= lo % CYCLE
        hi = lo + 2 * half
        seg = None
        for s in _segments(pages):
            if page_seq in s:
                seg = s
                break
        if seg is None:                 # 検知ページを受信していない：最も近い区間
            segs = list(_segments(pages))
            if not segs:
                return None
            seg = min(segs, key=lambda s: min(abs(x - page_seq) for x in s))
        cycles = []
        for seq in seg:
            p = pages[seq]
            for k in range(p.n_cycles):
                g = seq * PAGE_SAMPLES + k * CYCLE
                if lo <= g and g + CYCLE <= hi:
                    cycles.append((seq, k))
        if not cycles:
            return None
        g0 = cycles[0][0] * PAGE_SAMPLES + cycles[0][1] * CYCLE
        return build_record(kind, pages, times, cycles, detect_index=t - g0)

    @staticmethod
    def partial(pages, times):
        """受信済みの最新の連続区間（_partial 保存用）"""
        segs = list(_segments(pages))
        if not segs:
            return None
        seg = segs[-1]
        cycles = [(s, k) for s in seg for k in range(pages[s].n_cycles)]
        return build_record('partial', pages, times, cycles)


# =============================================================================
# A11 ファイル保存（Exporter・SessionLog、10.5 節）
# =============================================================================
class SessionLog:
    def __init__(self, store, clock, relpath):
        self.store, self.clock, self.relpath = store, clock, relpath
        self.lines = []

    def write(self, kind, text):
        dt = self.clock.local_datetime()
        line = '%s.%03d\t%s\t%s' % (dt.strftime('%Y-%m-%d %H:%M:%S'), dt.microsecond // 1000, kind, text)
        self.lines.append(line)
        try:
            self.store.append_line(self.relpath, line)
        except Exception:
            pass


class Exporter:
    def __init__(self, store, clock, settings):
        self.store, self.clock, self.settings = store, clock, settings

    def day_dir(self):
        return os.path.join('ECG', self.clock.local_datetime().strftime('%Y%m%d'))

    def file_name(self, device_id, dt, kind):
        base = 'ECG_%s_%s_%s' % (device_id, dt.strftime('%Y%m%d-%H%M%S'), kind)
        d = self.day_dir()
        name, n = base, 1
        while self.store.exists(os.path.join(d, name + '.csv')):
            n += 1
            name = '%s_%d' % (base, n)
        return name

    @staticmethod
    def ecg_csv(rec):
        return ('ECG\n%d\n' % FS + '\n'.join(str(v) for v in rec.ecg) + '\n').encode('utf-8')

    @staticmethod
    def meta_csv(rec):
        rows = ['page,time,cycle,electrode,accel,tilt_x,tilt_y,tilt_z,arr']
        rows += [','.join(str(x) for x in r) for r in rec.meta]
        return ('\n'.join(rows) + '\n').encode('utf-8')

    def save(self, rec, device_id, raw_pages=None, event=None, extra=None):
        """一時名で書いてから改名（16.3 節）。戻り値：保存したパスの dict"""
        d = self.day_dir()
        name = self.file_name(device_id, rec.first_time, rec.kind)
        out = {'name': name, 'dir': self.store.path(d)}
        out['csv'] = self.store.write_atomic(os.path.join(d, name + '.csv'), self.ecg_csv(rec))
        out['meta'] = self.store.write_atomic(os.path.join(d, name + '_meta.csv'), self.meta_csv(rec))
        if event is not None:
            ej = self.event_json(rec, device_id, event, name, extra or {})
            out['event'] = self.store.write_atomic(os.path.join(d, name + '_event.json'),
                                                   json.dumps(ej, ensure_ascii=False, indent=1).encode('utf-8'))
        if raw_pages and self.settings['save_pages_bin']:
            blob = b''.join(raw_pages[s] for s in sorted(raw_pages))
            out['pages'] = self.store.write_atomic(os.path.join(d, name + '_pages.bin'), blob)
        out['samples'] = len(rec.ecg)
        return out

    def event_json(self, rec, device_id, ev, name, extra):
        is_arr = ev.event == EV_ARR
        sub = ev.sub & 0x7F
        return {
            'format': 'ecgpatch-event/1',
            'device_id': device_id, 'event_seq': ev.event_seq,
            'event': 'arrhythmia' if is_arr else ('electrode' if ev.event == EV_ELEC else 'other'),
            'event_code': ev.event,
            'sub': (ARR_SUB_EN if is_arr else ELEC_SUB_EN).get(sub, str(sub)), 'sub_code': ev.sub & 0x7F,
            'during_motion': bool(ev.sub & 0x80),
            'time': ev.time_text, 'page_seq': ev.page_seq, 'sample_ofs': ev.sample_ofs,
            'rr_ms': ev.rr_ms, 'median_ms': ev.median_ms, 'hr_bpm': ev.hr_bpm,
            'wave_start': ev.wave_start, 'wave_pages': ev.wave_pages,
            'pages_received': extra.get('pages_received', []),
            'pages_missing': extra.get('pages_missing', []),
            'ecg_csv': name + '.csv', 'samples': len(rec.ecg),
            'detect_index': rec.detect_index,
            'received_at': extra.get('received_at', ''), 'acked_at': extra.get('acked_at', ''),
        }


# =============================================================================
# A12 表示用のまとめ（r10：受信アプリは判定しない。本体 No.29）
# =============================================================================
def _median(v):
    s = sorted(v)
    n = len(s)
    return 0 if n == 0 else (s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) // 2)


class Analyzer:
    """本体仕様書 Rev1.2 9.2 節・No.29（11/09 決定）：受信アプリは R 波検出・判定を行わず、
    パッチの判定結果（Event Log）と波形を表示するだけとする。r10 で r9 までの R 波検出・判定を削除した。"""

    @staticmethod
    def leadoff_ranges(x, meta=None):
        """電極はがれ区間 [(start, end)]。パッチの記録（_meta.csv の electrode）だけを使う。meta がなければ空"""
        out = []
        for k, row in enumerate(meta or []):
            if int(row[3]):
                s, e = k * CYCLE, (k + 1) * CYCLE
                if out and out[-1][1] == s:
                    out[-1] = (out[-1][0], e)
                else:
                    out.append((s, e))
        return out

    @staticmethod
    def analyze(x, meta=None, fs=FS, event=None):
        """表示用のまとめ（解析はしない）。event＝_event.json（パッチの判定）"""
        lo = Analyzer.leadoff_ranges(x, meta)
        out = {'beats': [], 'leadoff': lo, 'abnormal': [], 'hr': None, 'n_arr': 0, 'verdict': '—'}
        if event:
            code = event.get('event_code')
            sub = (event.get('sub_code') or 0) | (0x80 if event.get('during_motion') else 0)
            out['verdict'] = event_title(code, sub)
            out['n_arr'] = 1 if code == EV_ARR else 0
            out['hr'] = event.get('hr_bpm') or None
            if event.get('rr_ms'):
                out['abnormal'].append('R-R %s ms・中央値 %s ms（パッチ）' % (event.get('rr_ms'), event.get('median_ms')))
            if event.get('time'):
                out['abnormal'].append('検知 %s' % event['time'])
        return out


def synth_ecg(seconds=30, hr=72, leadoff=False, seed=1):
    """r17 と同じ考え方の合成波形（ダミー表示用）"""
    rnd = random.Random(seed)
    n = int(seconds * FS)
    x = array('h', [0] * n)
    rr = int(60 * FS / hr)
    pos = 40
    while pos < n:
        for j in range(-40, 70):
            i = pos + j
            if 0 <= i < n:
                v = 0.0
                v += 120 * pow(2.718, -((j + 30) / 6.0) ** 2)      # P
                v -= 150 * pow(2.718, -((j + 3) / 1.6) ** 2)       # Q
                v += 1300 * pow(2.718, -(j / 2.2) ** 2)            # R
                v -= 260 * pow(2.718, -((j - 4) / 2.0) ** 2)       # S
                v += 260 * pow(2.718, -((j - 45) / 10.0) ** 2)     # T
                x[i] = int(v)
        pos += rr + rnd.randint(-6, 6)
    for i in range(n):
        x[i] = int(x[i] + 40 * math.sin(i / FS * 2 * math.pi * 0.25) + rnd.randint(-8, 8))
    if leadoff:
        a, b = int(n * 0.45), int(n * 0.62)
        for i in range(a, b):
            x[i] = rnd.randint(-3, 3)
    return x


# =============================================================================
# A18 BLE モニタ・A19 BLE トレース記録（17 章）
# =============================================================================
@dataclass(frozen=True)
class BleEvent:
    t: float
    kind: str                   # cb / op / rx / tx / app / dbg / warn
    ev: str
    peer: str = ''
    ch: str = ''
    data: bytes = b''
    err: tuple = None           # (domain, code, name)
    v: dict = field(default_factory=dict)


class BleObserver(ABC):
    @abstractmethod
    def on_ble_event(self, e: BleEvent) -> None: ...


CB_ERRORS = {0: 'unknown', 1: 'invalidParameters', 2: 'invalidHandle', 3: 'notConnected', 4: 'outOfSpace',
             5: 'operationCancelled', 6: 'connectionTimeout', 7: 'peripheralDisconnected',
             8: 'uuidNotAllowed', 9: 'alreadyAdvertising', 10: 'connectionFailed',
             11: 'connectionLimitReached', 12: 'unknownDevice', 13: 'operationNotSupported',
             14: 'peerRemovedPairingInformation', 15: 'encryptionTimedOut', 16: 'tooManyLEPairedDevices'}


class BleMonitor(BleObserver):
    """計数・統計・警告（17.2・17.6 節）。純粋な Python。"""
    def __init__(self, clock):
        self.clock = clock
        self.lock = threading.Lock()
        self.reset_counters()

    def reset_counters(self):
        self.counts = collections.Counter()
        self.bytes = collections.Counter()
        self.scan = {}                  # peer → dict(name, rssi, times deque, last, event, status)
        self.connect_ms = None
        self.t_connect = None
        self.rssi = collections.deque(maxlen=120)       # (t, rssi)
        self.rate = collections.deque(maxlen=120)       # (t, bytes/s)
        self._rx_sec = None
        self._rx_acc = 0
        self.results = collections.Counter()
        self.cmd_ms = []
        self._cmd_t = None
        self.disconnects = []
        self.warnings = collections.deque(maxlen=50)
        self.dbg = None
        self.mtu = None
        self.central_state = 'unknown'
        self.emulator = False
        self.frag_gaps = 0
        self._weak_since = None
        self._slow_since = None

    def warn(self, ev, text, **v):
        self.warnings.append((self.clock.now(), ev, text))
        self.counts['warn'] += 1

    def on_ble_event(self, e):
        with self.lock:
            self._on(e)

    def _on(self, e):
        self.counts[(e.kind, e.ch or e.ev)] += 1
        if e.data:
            self.bytes[(e.kind, e.ch)] += len(e.data)
        if e.ev == 'state':
            self.central_state = e.v.get('state', '?')
        elif e.ev == 'adv':
            s = self.scan.setdefault(e.peer, {'times': collections.deque(maxlen=21)})
            s.update(name=e.v.get('name', ''), rssi=e.v.get('rssi'), last=e.t, event=e.v.get('event'),
                     status=e.v.get('status'))
            if not s['times'] or e.t - s['times'][-1] >= 0.02:   # K5：広告とスキャン応答の組（約 2ms）は 1 回と数える
                s['times'].append(e.t)
        elif e.ev == 'connect' and e.kind == 'op':
            self.t_connect = e.t
            self.frag_gaps = 0
        elif e.ev == 'didConnect':
            if self.t_connect is not None:
                self.connect_ms = int(round((e.t - self.t_connect) * 1000))
                if self.connect_ms > 3000:
                    self.warn('slow_connect', '接続が遅い（%d ms）' % self.connect_ms)
        elif e.ev == 'rssi':
            r = e.v.get('rssi')
            if r is not None and r != 127:
                self.rssi.append((e.t, r))
                recent = [v for t, v in self.rssi if t >= e.t - 5]
                if recent and sum(recent) / len(recent) < -85:
                    if self._weak_since is None:
                        self._weak_since = e.t
                    elif e.t - self._weak_since >= 5 and self.counts['warn_weak'] == 0:
                        self.counts['warn_weak'] += 1
                        self.warn('weak_rssi', '電波が弱い：パッチに近づけてください')
                else:
                    self._weak_since = None
        elif e.kind == 'rx' and e.ch == 'ECG':
            sec = int(e.t)
            if self._rx_sec is None:
                self._rx_sec = sec
            if sec != self._rx_sec:
                self.rate.append((self._rx_sec, self._rx_acc))
                self._check_slow()
                for gap_sec in range(self._rx_sec + 1, sec):
                    self.rate.append((gap_sec, 0))
                self._rx_sec, self._rx_acc = sec, 0
            self._rx_acc += len(e.data)
        elif e.kind == 'tx' and e.ch == 'CTL':
            self._cmd_t = e.t
        elif e.kind == 'rx' and e.ch == 'STS' and 'result' in e.v:
            self.results[e.v['result']] += 1
            if self._cmd_t is not None:
                self.cmd_ms.append(int((e.t - self._cmd_t) * 1000))
                self._cmd_t = None
        elif e.kind == 'dbg':
            self.dbg = dict(e.v)
        elif e.ev == 'mtu':
            self.mtu = e.v.get('mtu')
        elif e.ev == 'frag_gap':
            self.frag_gaps += 1
            if self.frag_gaps == 3:
                self.warn('frag_gap', '断片の欠落が 3 回以上')
        elif e.ev == 'didDisconnect':
            code = e.err[1] if e.err else None
            name = e.err[2] if e.err else 'normal'
            self.disconnects.append((e.t, name, e.v.get('judge', '')))
            if code in (6, 7) or e.v.get('unexpected'):
                self.warn('unexpected_disc', '想定外の切断（%s）' % name)
        elif e.ev == 'emulator':
            self.emulator = True

    def _check_slow(self):
        last3 = list(self.rate)[-3:]
        if len(last3) == 3 and all(b > 0 for _, b in last3) and sum(b for _, b in last3) / 3 < 5000:
            self.counts['warn_slow'] += 1
            if self.counts['warn_slow'] == 1:
                self.warn('slow_rate', '速度が出ない（%.1f KB/s）' % (sum(b for _, b in last3) / 3000))

    def rx_rate_kbs(self):
        with self.lock:
            if not self.rate:
                return 0.0
            return self.rate[-1][1] / 1000.0

    def adv_interval_ms(self, peer):
        s = self.scan.get(peer)
        if not s or len(s['times']) < 3:
            return None
        t = list(s['times'])
        return int(_median([int((b - a) * 1000) for a, b in zip(t, t[1:])]))

    def series(self, name, sec=60):
        with self.lock:
            src = self.rssi if name == 'rssi' else self.rate
            now = self.clock.now()
            return [(t, v) for t, v in src if t >= now - sec]

    def snapshot(self):
        with self.lock:
            cm = self.cmd_ms
            return {
                'central_state': self.central_state, 'connect_ms': self.connect_ms, 'mtu': self.mtu,
                'rssi': self.rssi[-1][1] if self.rssi else None,
                'rate_kbs': (self.rate[-1][1] / 1000.0) if self.rate else 0.0,
                'counts': dict((('%s/%s' % k) if isinstance(k, tuple) else k, v) for k, v in self.counts.items()),
                'bytes_rx': sum(v for k, v in self.bytes.items() if k[0] == 'rx'),
                'bytes_tx': sum(v for k, v in self.bytes.items() if k[0] == 'tx'),
                'results': dict(self.results),
                'cmd_ms': (min(cm), sum(cm) // len(cm), max(cm)) if cm else None,
                'disconnects': list(self.disconnects)[-5:], 'warnings': list(self.warnings)[-8:],
                'dbg': dict(self.dbg) if self.dbg else None, 'emulator': self.emulator,
                'frag_gaps': self.frag_gaps,
            }

    def scan_table(self):
        with self.lock:
            rows = []
            for peer, s in self.scan.items():
                rows.append((s.get('name', ''), s.get('rssi'), self.adv_interval_ms(peer), s.get('last'),
                             s.get('event'), s.get('status')))
            return sorted(rows, key=lambda r: -(r[3] or 0))


class BleTraceRecorder(BleObserver):
    """ble_trace_*.jsonl（17.4 節）。書込みは専用スレッドで 0.5 秒ごとにまとめて行う。"""
    FILE_MAX = 5 * 1024 * 1024
    TOTAL_MAX = 50 * 1024 * 1024

    def __init__(self, store, clock, settings, reldir='ECG_patch/trace', threaded=True):
        self.store, self.clock, self.settings = store, clock, settings
        self.reldir = reldir
        self.ring = collections.deque(maxlen=20000)
        self.q = queue.Queue()
        self.enabled = bool(settings['ble_trace']) if settings else True
        self.t0 = clock.now()
        self.wall0 = clock.local_datetime()
        self.relpath = None
        self.size = 0
        self.threaded = threaded
        self._stop = threading.Event()
        self.th = None
        self.summary_rel = os.path.join(reldir, 'ble_summary.csv')
        self._adv_last = {}

    def start(self):
        self.enabled = True
        self._new_file()
        if self.threaded and self.th is None:
            self.th = threading.Thread(target=self._loop, name='ble_trace', daemon=True)
            self.th.start()

    def stop(self):
        self.flush()
        self.enabled = False

    def _new_file(self):
        stamp = self.clock.local_datetime().strftime('%Y%m%d_%H%M%S')
        name = 'ble_trace_%s.jsonl' % stamp
        n = 1
        while self.store.exists(os.path.join(self.reldir, name)):
            n += 1
            name = 'ble_trace_%s_%d.jsonl' % (stamp, n)
        self.relpath = os.path.join(self.reldir, name)
        self.size = 0
        self._cleanup()

    def _cleanup(self):
        try:
            d = self.store.path(self.reldir)
            if not os.path.isdir(d):
                return
            files = sorted((os.path.getmtime(os.path.join(d, f)), f) for f in os.listdir(d)
                           if f.startswith('ble_trace_'))
            total = sum(os.path.getsize(os.path.join(d, f)) for _, f in files)
            while files and total > self.TOTAL_MAX:
                _, f = files.pop(0)
                total -= os.path.getsize(os.path.join(d, f))
                os.remove(os.path.join(d, f))
        except Exception:
            pass

    def to_dict(self, e):
        wall = self.wall0 + datetime.timedelta(seconds=e.t - self.t0)
        d = {'t': round(e.t - self.t0, 3), 'wall': wall.strftime('%Y-%m-%d %H:%M:%S.') + '%03d' % (wall.microsecond // 1000),
             'k': e.kind, 'ev': e.ev}
        if e.peer:
            d['peer'] = e.peer
        if e.ch:
            d['ch'] = e.ch
        if e.data:
            d['len'] = len(e.data)
            if e.ch == 'CTL' and e.data[:1] == bytes([CMD_SET_PID]):
                d['hex'] = '%02X **' % CMD_SET_PID              # 患者 ID を伏せる
            elif e.ch == 'ECG' and self.settings is not None and not self.settings['verbose_trace']:
                d['hex'] = hexs(e.data[:8])
            else:
                d['hex'] = hexs(e.data)
        if e.err:
            d['err'] = {'domain': e.err[0], 'code': e.err[1], 'name': e.err[2]}
        if e.v:
            d['v'] = e.v
        return d

    def on_ble_event(self, e):
        if e.ev == 'adv':                       # K5：広告は内容が変わったときと 1 秒に 1 回だけ記録
            sig = (e.v.get('name'), e.v.get('event'), e.v.get('status'))
            last = self._adv_last.get(e.peer)
            if last is not None and last[0] == sig and e.t - last[1] < 1.0:
                return
            self._adv_last[e.peer] = (sig, e.t)
        d = self.to_dict(e)
        self.ring.append(d)
        if self.enabled:
            self.q.put(d)

    def _loop(self):
        while not self._stop.is_set():
            time.sleep(0.5)
            self.flush()

    def flush(self):
        lines = []
        while True:
            try:
                lines.append(json.dumps(self.q.get_nowait(), ensure_ascii=False))
            except queue.Empty:
                break
        if not lines or self.relpath is None:
            return
        try:
            p = self.store.path(self.relpath)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            data = '\n'.join(lines) + '\n'
            with open(p, 'a', encoding='utf-8') as f:
                f.write(data)
            self.size += len(data.encode('utf-8'))
            if self.size > self.FILE_MAX:
                self._new_file()
        except Exception:
            pass

    def append_summary(self, row):
        hdr = 'connected_at,device_id,connect_ms,mtu,rssi_avg,rx_bytes,rate_kbs,frag_gaps,crc_ng,req_pages,disc_reason'
        try:
            if not self.store.exists(self.summary_rel):
                self.store.append_line(self.summary_rel, hdr)
            self.store.append_line(self.summary_rel, ','.join(str(x) for x in row))
        except Exception:
            pass

    def export(self):
        self.flush()
        out = []
        if self.relpath and self.store.exists(self.relpath):
            out.append(self.store.path(self.relpath))
        if self.store.exists(self.summary_rel):
            out.append(self.store.path(self.summary_rel))
        return out

    def close(self):
        self._stop.set()
        self.flush()


# =============================================================================
# A02 BLE トランスポート（抽象）
# =============================================================================
class BleTransport(ABC):
    """コールバックは post(fn, *args) を通して作業スレッドで呼ぶ。"""
    def __init__(self):
        self._observers = []
        self.post = lambda fn, *a: fn(*a)
        self.clock = SystemClock()

    def add_observer(self, obs):
        self._observers.append(obs)

    def _emit(self, kind, ev, peer='', ch='', data=b'', err=None, **v):
        e = BleEvent(self.clock.now(), kind, ev, peer, ch, bytes(data), err, v)
        for o in self._observers:
            try:
                o.on_ble_event(e)
            except Exception as ex:
                ErrorHandler.instance().handle(ex, where='observer')

    @abstractmethod
    def start(self, on_state) -> None: ...          # CBCentralManager の生成

    @abstractmethod
    def scan_start(self, service_uuid, on_adv) -> None: ...   # on_adv(peer, adv: dict, rssi)

    @abstractmethod
    def scan_stop(self) -> None: ...

    @abstractmethod
    def connect(self, peer_id, on_connected, on_failed, on_disconnected, on_invalidated=None) -> None: ...

    @abstractmethod
    def discover(self, services, on_done) -> None: ...         # on_done(set of char uuid)

    @abstractmethod
    def subscribe(self, char_uuid, on_value, on_state=None) -> None: ...

    @abstractmethod
    def read(self, char_uuid, on_value) -> None: ...

    @abstractmethod
    def write(self, char_uuid, data, on_done) -> None: ...      # on_done(err | None)

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def max_write_len(self) -> int: ...

    @abstractmethod
    def read_rssi(self) -> None: ...

    @abstractmethod
    def central_state(self) -> str: ...

    def main_tick(self):                # 実機：メインスレッドで溜めた操作を実行
        pass

    def shutdown(self):
        pass


# =============================================================================
# 13.9.5 StatusPanelPresenter（下部の接続状態表示）
# =============================================================================
@dataclass
class StatusVM:
    dot: str
    head: str
    percent: int
    bar_color: str
    detail: str
    progress: str
    patches: list
    electrode_cells: list
    log: list
    head_color: str = 'ink'
    warn: str = ''


class StatusPanelPresenter:
    def __init__(self, clock):
        self.clock = clock
        self.log = collections.deque(maxlen=4)
        self.console = []
        self.last_pct_logged = 0
        self.dot, self.head, self.percent, self.bar_color = 'gray', '起動しました', None, 'gray'
        self.head_color = 'ink'
        self.detail, self.progress = 'BLE を準備しています…', ''
        self.patches = []
        self.cells = None
        self.done_at = None
        self.mode = None
        self.warn = ''
        self.bt = 'unknown'
        self.add('起動しました ／ BLE を準備しています…')

    def add(self, msg):
        self.log.append(msg)
        self.console.append(msg)

    def on_event(self, ev, **d):
        if ev == 'bt_state':
            self.bt = d['state']
            if d['state'] == 'poweredOn':
                self.dot, self.head, self.percent = 'gray', '待機中（パッチを探しています）', 0
                self.bar_color, self.detail = 'gray', 'Bluetooth: オン'
                self.add('BLE 受信待機中（ECG パッチを待っています）')
            elif d['state'] == 'unauthorized':
                self.dot, self.head, self.percent = 'gray', 'Bluetooth が許可されていません', None
                self.detail = '設定 > Pythonista > Bluetooth を許可してください'
                self.add('Bluetooth が許可されていません')
            else:
                self.dot, self.percent = 'gray', None
                self.head = 'Bluetooth: オフ   設定アプリで Bluetooth をオンにしてください'
                self.detail = 'Bluetooth: %s' % d['state']
                self.add(self.head)
        elif ev == 'scan':
            self.dot, self.head, self.percent, self.bar_color = 'gray', '待機中（パッチを探しています）', 0, 'gray'
            self.head_color, self.mode, self.cells, self.progress, self.warn = 'ink', None, None, '', ''
            self.detail = 'Bluetooth: オン'
        elif ev == 'found':
            self.add('%s を見つけました（%s）' % (d['name'], d['kind']))
        elif ev == 'alert':
            self.add('%s の異常通知を受信しました（自動接続）' % d['name'])
        elif ev == 'connecting':
            self.dot, self.head, self.percent, self.bar_color = 'blue', '接続中（%s）' % d['name'], 0, 'blue'
            self.head_color = 'ink'
            self.detail = d['name']
        elif ev == 'connected':
            self.head = '接続しました（MTU %s）' % d['mtu']
            self.add('%s に接続しました（MTU %s）' % (d['name'], d['mtu']))
            self.detail = d.get('detail', self.detail)
        elif ev == 'detail':
            self.detail = d['text']
        elif ev == 'time_set':
            self.add('時刻は設定済みです' if d.get('already') else '時刻を設定しました（%s）' % d['when'])
        elif ev == 'rx_start':
            self.mode = d['mode']
            self.last_pct_logged = 0
            if d['mode'] == 'setup':
                self.dot, self.bar_color = 'blue', 'blue'
                self.head = '受信中 0%（装着確認）'
                self.add('受信開始: 装着確認（%d ページ / %s バイト）' % (d['pages'], format(d['pages'] * PAGE_USER, ',')))
            else:
                self.dot, self.bar_color = 'red', 'red'
                self.head = '受信中 0%%（異常通知 #%d）' % d['no']
                self.add('受信開始: 異常通知 #%d（%d ページ / %s バイト）' % (d['no'], d['pages'], format(d['pages'] * PAGE_USER, ',')))
            self.percent = 0
        elif ev == 'progress':
            pct = max(0, min(100, int(d['pct'])))
            self.percent = pct
            self.progress = d.get('text', '')
            if self.mode == 'setup':
                self.head = '受信中 %d%%（装着確認）' % pct
            elif self.mode == 'event':
                self.head = '受信中 %d%%（異常通知 #%d）' % (pct, d.get('no', 0))
            if 'cells' in d:
                self.cells = d['cells']
            step = (pct // 20) * 20
            if 20 <= step < 100 and step > self.last_pct_logged:
                self.last_pct_logged = step
                self.add(' 受信中… %d%%' % step)
        elif ev == 'electrode':
            if d['ok']:
                self.warn = ''
                self.add('電極 OK')
            else:
                self.warn = '電極 NG：貼付を確認してください'
                self.add('電極 NG：貼付を確認してください')
        elif ev == 'waiting':
            self.dot, self.bar_color, self.head_color = 'red', 'red', 'ink'
            self.head = d.get('text') or '記録待ち（検知後 15 秒）'     # r9：下部の表示も残り秒数を更新（試験 No.7）
            self.percent = d.get('pct', 0)
            if d.get('first'):
                self.add('記録待ち（検知後 15 秒）…')
        elif ev == 'resend':
            self.add('再送を要求しました（ページ %d）' % d['seq'])
        elif ev == 'saving':
            self.head, self.percent = '保存中…', 100
        elif ev == 'saved':
            self.add('受信 %s（%d 点、BLE）＋ _meta.csv' % (d['name'] + '.csv', d['samples']))
        elif ev == 'done':
            self.dot, self.bar_color, self.percent, self.head_color = 'green', 'green', 100, 'ink'
            self.head = '受信完了（%d 件目）' % d['count']
            self.add('受信完了（%d 件目）。次の通知を待っています' % d['count'])
            self.done_at = self.clock.now()
        elif ev == 'message':
            self.add(d['text'])
            if d.get('head'):
                self.head = d['head']
        elif ev == 'disconnected':
            self.dot, self.bar_color, self.head_color = 'orange', 'gray', 'orange'
            self.head = d.get('text') or '受信の途中で ECG パッチとの接続が切れました'
            self.add(self.head)
            self.done_at = self.clock.now()
        elif ev == 'error':
            self.dot, self.head_color = 'orange', 'orange'
            self.head = d['text']
            self.add(d['text'])

    def tick(self, scanning):
        if self.done_at is not None and self.clock.now() - self.done_at >= 3 and scanning:
            self.done_at = None
            self.on_event('scan')

    def view_model(self, patches=None):
        return StatusVM(self.dot, self.head, self.percent, self.bar_color, self.detail, self.progress,
                        list(patches if patches is not None else self.patches),
                        list(self.cells) if self.cells else None, list(self.log), self.head_color, self.warn)


# =============================================================================
# A03 スキャン（Scanner）
# =============================================================================
class Scanner:
    def __init__(self, settings, clock):
        self.settings, self.clock = settings, clock
        self.table = {}                 # peer → AdvInfo
        self.cooldown = {}              # peer → この時刻まで自動接続しない
        self.seen = set()
        self.adv_logged = {}
        self.alert_logged = set()
        self.emulator_hint = False

    def on_adv(self, info):
        new = info.peer not in self.seen
        self.seen.add(info.peer)
        self.table[info.peer] = info
        return new

    def devices(self):
        now = self.clock.now()
        exp = self.settings['list_expire_s']
        for p in [p for p, a in self.table.items() if now - a.last_seen > exp]:
            del self.table[p]
            self.seen.discard(p)
        return sorted(self.table.values(), key=lambda a: -a.last_seen)

    def auto_target(self):
        now = self.clock.now()
        for a in self.devices():
            if self.cooldown.get(a.peer, 0) > now:
                continue
            if not self.settings.is_registered(a.device_id):
                continue
            if a.event in (EV_ELEC, EV_ARR) and self.settings['auto_connect_alert']:
                return a
            if a.unnotified and self.settings['auto_fetch_unnotified']:
                return a
        return None


# =============================================================================
# A05 送信（CommandSender）
# =============================================================================
class CommandSender:
    """Write の結果（ATT）と Device Status の結果コードの両方を待つ（9.4 節）。"""
    def __init__(self, mgr):
        self.m = mgr
        self.cur = None
        self.pending = collections.deque()
        self.sent = []                  # 送った opcode（試験・記録用）

    def send(self, cmd, on_result, make=None):
        """make：送る直前にコマンドを作り直す関数（SET_TIME は送る時刻で作る：r9）"""
        if self.cur is not None:
            self.pending.append((cmd, on_result, make))
            return
        self.cur = {'cmd': cmd, 'cb': on_result, 'tries': 0, 'busy': 0, 'result': None,
                    'write_ok': None, 't': 0.0, 'waiting': False, 'retry_at': None, 'make': make}
        self._write()

    def busy(self):
        return self.cur is not None

    def _write(self):
        c_ = self.cur
        c_['t'] = self.m.clock.now()
        c_['write_ok'] = None
        c_['result'] = None
        c_['tries'] += 1
        if c_['make'] is not None:
            c_['cmd'] = c_['make']()
        data = c_['cmd'].to_bytes()
        self.sent.append(c_['cmd'].opcode)
        shown = '%02X **' % CMD_SET_PID if c_['cmd'].opcode == CMD_SET_PID else hexs(data)
        self.m.slog('CMD', '%s %s (try %d)' % (c_['cmd'].name, shown, c_['tries']))
        self.m.t.write(CH_CONTROL, data, self._on_write_done)

    def _on_write_done(self, err):
        c_ = self.cur
        if c_ is None:
            return
        if err:
            self.m.trace.w('WRITE_ERR', c_['cmd'].name, err)
            if c_['tries'] < 2:
                self._write()
            else:
                self._finish(None)
            return
        c_['write_ok'] = True
        self._maybe_complete()

    def on_status(self, st):
        c_ = self.cur
        if c_ is None or st.last_cmd != c_['cmd'].opcode:
            return
        c_['result'] = st.result
        self._maybe_complete()

    def _maybe_complete(self):
        c_ = self.cur
        r = c_['result']
        if r is None or not c_['write_ok']:
            return
        self.m.slog('RES', '%s -> result=%d %s' % (c_['cmd'].name, r, RES_NAMES[r] if r < len(RES_NAMES) else '?'))
        if r == RES_BUSY:
            if c_['busy'] < 3:
                c_['busy'] += 1
                c_['tries'] = 0
                c_['retry_at'] = self.m.clock.now() + 1.0
                if c_['make'] is not None:            # r9：SET_TIME の再送も秒の変わり目に合わせる
                    us = self.m.clock.local_datetime().microsecond
                    c_['retry_at'] += (1.0 - us / 1e6) % 1.0
                c_['result'] = None
                return
            self._finish(r)
            return
        if r == RES_WAITING:
            c_['result'] = None
            first = not c_['waiting']
            c_['waiting'] = True
            if first:
                c_['cb'](RES_WAITING)
            return
        self._finish(r)

    def tick(self):
        c_ = self.cur
        if c_ is None:
            return
        now = self.m.clock.now()
        if c_['retry_at'] is not None:
            if now >= c_['retry_at']:
                c_['retry_at'] = None
                self._write()
            return
        if c_['waiting']:
            return
        if now - c_['t'] > CMD_RESULT_TIMEOUT:
            self.m.emit_app('warn', 'timeout', what='CMD_RESULT', cmd=c_['cmd'].name)
            if c_['tries'] < 2:
                self._write()
            else:
                self._finish(None)

    def _finish(self, r):
        c_ = self.cur
        self.cur = None
        try:
            c_['cb'](r)
        finally:
            if self.cur is None and self.pending:
                self.send(*self.pending.popleft())

    def release_waiting(self):
        """WAITING 中のコマンドを完了扱いにする（波形が届き始めたとき）"""
        if self.cur is not None and self.cur['waiting']:
            self.cur = None
            if self.pending:
                self.send(*self.pending.popleft())

    def cancel_all(self):
        self.cur = None
        self.pending.clear()


# =============================================================================
# A13 セッション（装着確認・異常通知）
# =============================================================================
class BaseSession:
    mode = ''

    def __init__(self, mgr):
        self.m = mgr
        self.asm = PageAssembler()
        self.pages = {}
        self.raw = {}
        self.bad_raw = {}
        self.first_frag_t = None
        self.last_frag_t = None
        self.req_count = collections.Counter()
        self.unavailable = set()        # REQ_PAGES が RANGE だったページ（飛ばす）
        self.crc_ng = 0
        self.req_pages_sent = 0
        self.done = False
        self.acked_all = False
        self.saved = None
        self.receiving = False
        self.waiting = False

    # ---- 受信
    def on_fragment(self, data):
        now = self.m.clock.now()
        if self.first_frag_t is None:
            self.first_frag_t = now
        self.last_frag_t = now
        gaps_before = self.asm.stats['gaps'] + self.asm.stats['dups']
        b = self.asm.feed(data)
        if self.asm.stats['gaps'] + self.asm.stats['dups'] != gaps_before:
            self.m.emit_app('warn', 'frag_gap', frag=data[2], page=data[1])
        if b is not None:
            self._on_page_bytes(b)
        if data[3] & 0x02:              # 転送最終
            self.check_missing()
        self.on_progress()

    def _on_page_bytes(self, b):
        try:
            p = Page.from_bytes(b)
        except ProtocolError as e:
            self.m.err(e, 'page')
            return
        if not p.crc_ok:
            self.crc_ng += 1
            self.bad_raw[p.seq] = b
            self.m.slog('RX', 'page %d crc=NG' % p.seq)
            self.m.emit_app('warn', 'crc_ng', page=p.seq)
            self.request_pages([p.seq])
            return
        self.bad_raw.pop(p.seq, None)
        if p.fs != getattr(self, 'fs_seen', None):   # r10：ページヘッダのサンプリング周波数を記録（変わったときだけ）
            self.fs_seen = p.fs
            self.m.slog('RX', 'page fs=%d' % p.fs)
        self.pages[p.seq] = p
        self.raw[p.seq] = b
        while len(self.pages) > MAX_SESSION_PAGES:
            s = min(self.pages)
            del self.pages[s]
            self.raw.pop(s, None)
        self.m.slog('RX', 'page %d crc=ok elec=%s accel=%04X' % (p.seq, format(p.elec, '08b')[::-1], p.accel))
        self.on_page(p)

    def expected(self):
        return []

    def missing(self):
        exp = self.expected()
        return [s for s in exp if s not in self.pages and s not in self.unavailable]

    def check_missing(self, force=False):
        miss = self.missing()
        if miss:
            self.request_pages(miss)

    def request_pages(self, seqs):
        seqs = [s for s in sorted(set(seqs)) if self.req_count[s] < REQ_PAGES_MAX]
        if not seqs:
            return
        for seg in _segments(seqs):
            for s in seg:
                self.req_count[s] += 1
            self.req_pages_sent += 1
            self.m.status_p.on_event('resend', seq=seg[0])
            self.m.sender.send(Command.req_pages(seg[0], len(seg)), lambda r, seg=seg: self._after_req_pages(r, seg))

    def _after_req_pages(self, r, seg):
        if r == RES_RANGE:                           # 9.4 節：記録して当該ページを飛ばす
            self.m.slog('RESULT', 'REQ_PAGES %d+%d -> RANGE（飛ばす）' % (seg[0], len(seg)))
            self.unavailable.update(seg)
            self.on_range_skipped()

    def on_range_skipped(self):
        pass

    def tick(self):
        now = self.m.clock.now()
        if (self.receiving and not self.waiting and self.last_frag_t is not None
                and now - self.last_frag_t > PAGE_IDLE_TIMEOUT):
            self.m.emit_app('warn', 'timeout', what='PAGE_IDLE')
            self.last_frag_t = now
            self.check_missing(force=True)

    def times(self):
        c_ = self.m.conn
        st = c_.status if c_ else None
        base = st.base_page_seq if st else 0
        latest = st.latest_page_seq if st else None
        anchor = (base, c_.time_anchor) if c_ is not None and c_.time_anchor is not None and st and st.time_set else None
        return fix_times(self.pages, base, self.m.clock.local_datetime(), latest, anchor)

    def on_progress(self):
        pass

    def on_status(self, st):
        pass

    def on_event(self, ev):
        pass

    def save_partial(self, reason):
        if not self.pages or self.saved:
            return None
        rec = Cutter.partial(self.pages, self.times())
        self.saved = self.m.save_record(rec, self.raw, None, reason)
        return self.saved


class SetupSession(BaseSession):
    mode = 'setup'

    def __init__(self, mgr, patient_id=None):
        super().__init__(mgr)
        self.patient_id = patient_id
        self.t_req = None
        self.ng_prompted = False
        self.saving = False
        self.ack_deadline = None
        self.window = None
        self.electrode_ok = None
        self.ref_wait_until = None

    def start(self):
        st = self.m.conn.status
        if st.state != ST_S2:
            self.m.slog('SETUP', 'state S%d is not S2' % st.state)
            self.m.finish_session('state')
            return
        self.m.state = 'SETUP'
        self.m.emit_app('app', 'session', mode='setup')
        if not st.time_set:
            self.m.send_set_time(self._after_time)         # K2：秒の変わり目に送る
        else:
            self.m.status_p.on_event('time_set', already=True)
            self._patient()

    def _after_time(self, r, now):
        if r in (RES_OK, RES_ALREADY):
            if r == RES_OK and self.m.conn:
                self.m.conn.time_anchor = now
            self.m.status_p.on_event('time_set', already=(r == RES_ALREADY), when=now.strftime('%Y-%m-%d %H:%M:%S'))
            self._patient()
        elif r is None:
            self.m.fail('時刻を設定できませんでした（応答なし）', 'cmd_timeout')
        else:
            self.m.result_error(r, 'SET_TIME', retry=lambda: self.start())

    def _patient(self):
        if self.patient_id:
            self.m.sender.send(Command.set_patient_id(self.patient_id), lambda r: self._req())
        else:
            self._req()

    def _req(self):
        self.m.sender.send(Command.req_setup(), self._after_req)

    def _after_req(self, r):
        if r == RES_OK:
            self.t_req = self.m.clock.now()
            self.receiving = True
            self.m.status_p.on_event('rx_start', mode='setup', pages=8)
        elif r is None:
            self.m.fail('パッチから応答がありません', 'cmd_timeout')
        else:
            self.m.result_error(r, 'REQ_SETUP_WAVE', retry=self._req)

    def expected(self):
        if not self.pages:
            return []
        return list(range(min(self.pages), max(self.pages) + 1))

    def on_status(self, st):
        ok = st.electrode == 0
        if ok != self.electrode_ok:
            if self.electrode_ok is not None or not ok:
                self.m.status_p.on_event('electrode', ok=ok)
            self.electrode_ok = ok

    def cells(self):
        """直近 60 周期（30 秒）の電極状態。None は未受信。"""
        if not self.pages:
            return [None] * 60
        hi = max(self.pages)
        out = []
        for seq in range(hi - 7, hi + 1):
            p = self.pages.get(seq)
            for k in range(PAGE_CYCLES):
                out.append(None if p is None or k >= p.n_cycles else p.cycle_electrode(k))
        return out[-60:]

    def normal_secs(self):
        return min(SETUP_NEED_CYCLES, Cutter.normal_run_cycles(self.pages)) / 2.0

    def on_progress(self):
        secs = self.normal_secs()
        pct = 100 if self.window else int(secs / 30.0 * 100)
        self.m.status_p.on_event('progress', pct=pct, cells=self.cells(),
                                 text='電極正常 %.1f／30 秒・受信 %d ページ' % (secs, len(self.pages)))

    def on_page(self, p):
        if self.saving or self.done:
            return
        self.window = Cutter.setup_window(self.pages, self.times())
        if self.window:
            if not any(q.has_time for q in self.pages.values()) and self.m.conn.time_anchor is not None:
                # 10.3 節：基準ページ（SET_TIME 後の最初のページ）の時刻を使うため、届くまで待つ
                if self.ref_wait_until is None:
                    self.ref_wait_until = self.m.clock.now() + 9.0
                    self.m.slog('SETUP', '30 秒そろいました。時刻の基準ページを待ちます')
                if self.m.clock.now() < self.ref_wait_until:
                    return
            self._complete()

    def _complete(self):
        self.saving = True
        self.m.status_p.on_event('progress', pct=100, cells=self.cells(),
                                 text='電極正常 30.0／30 秒・受信 %d ページ' % len(self.pages))
        self.m.status_p.on_event('saving')
        try:
            self.saved = self.m.save_record(self.window, self.raw, None, 'setup')
        except StorageError as e:
            self.m.err(e, 'save_setup')
            self.m.fail('ファイルを保存できませんでした', 'storage', send_stop=False)
            return
        self.m.sender.send(Command.ack_setup(True), self._after_ack)

    def _after_ack(self, r):
        if r == RES_OK:
            self.acked_all = True
            self.done = True
            self.receiving = False
            self.ack_deadline = self.m.clock.now() + ACK_DISC_WAIT    # 最大 5 秒（本体 No.9・No.32。r10 で模擬器も 5 秒に戻した）
            self.m.on_setup_acked()
        elif r is None:
            self.m.fail('ACK の応答がありません（保存済み）', 'cmd_timeout')
        else:
            self.m.result_error(r, 'ACK_SETUP', retry=lambda: self.m.sender.send(Command.ack_setup(True), self._after_ack))

    def tick(self):
        super().tick()
        now = self.m.clock.now()
        if self.ack_deadline is not None and now > self.ack_deadline:
            self.ack_deadline = None
            emu = self.m.conn is not None and self.m.conn.info.emulator
            self.m.emit_app('app' if emu else 'warn', 'no_disc_after_ack')    # K6：模擬器は自分から切断できない
            self.m.disconnect('ack_done')
            return
        if self.window and not self.saving and self.ref_wait_until is not None and now >= self.ref_wait_until:
            self.m.slog('SETUP', '基準ページが届かないため推定時刻（~）で保存します')
            self._complete()
        if self.receiving and not self.done and self.t_req is not None and not self.ng_prompted \
                and now - self.t_req > SETUP_TOTAL_TIMEOUT:
            self.ng_prompted = True
            self.m.emit_app('warn', 'timeout', what='SETUP_TOTAL')
            self.m.status_p.on_event('error', text='電極 NG：30 秒そろいません（続ける／中止）')
            self.m.ui_event('setup_timeout')

    def user_continue(self):
        self.t_req = self.m.clock.now()
        self.ng_prompted = False

    def on_link_lost(self):
        if self.saved:
            return
        if self.window:
            self.saved = self.m.save_record(self.window, self.raw, None, 'setup_noack')
        else:
            self.save_partial('setup_lost')


class EventSession(BaseSession):
    mode = 'event'

    def __init__(self, mgr):
        super().__init__(mgr)
        self.queue = collections.deque()
        self.seen = set()
        self.cur = None
        self.no = 0
        self.total = 1
        self.acked = 0
        self.t_req = None
        self.wait_t0 = None
        self.long_wait_shown = False
        self.idle_deadline = None
        self.ack_deadline = None
        self.steps = ['todo'] * 5
        self.step_text = ['通知を受信（Event Log）', '記録待ち（検知後 15 秒）', '波形を受信中',
                          '保存（ECG＋メタ＋イベント）', 'パッチへ確認（ACK）']
        self.last_saved = None
        self.started = False
        self.time_wait = False

    def start(self):
        st = self.m.conn.status
        self.m.state = 'EVENT'
        self.started = True
        self.total = max(1, st.unnotified)
        self.m.emit_app('app', 'session', mode='event')
        if not st.time_set:                          # N-02：時刻未設定なら SET_TIME を 1 回
            self.time_wait = True                    # r9：SET_TIME が終わるまで REQ_EVENT_WAVE を送らない
            self.m.send_set_time(self._after_time)   # K2：秒の変わり目に送る
        else:
            self._next()

    def _after_time(self, r, now):
        self.time_wait = False
        if r in (RES_OK, RES_ALREADY):
            if r == RES_OK and self.m.conn:
                self.m.conn.time_anchor = now
            self.m.status_p.on_event('time_set', already=(r == RES_ALREADY), when=now.strftime('%Y-%m-%d %H:%M:%S'))
        else:
            self.m.slog('EVENT', 'SET_TIME result=%s（続行）' % r)
        self._next()

    def on_event(self, ev):
        if ev.event_seq in self.seen:                # APP-Q13：event_seq で重複を除く
            return
        self.seen.add(ev.event_seq)
        if not ev.crc_ok:
            self.m.slog('EVENT', 'event %d crc NG（続行）' % ev.event_seq)
        self.queue.append(ev)
        self.total = max(self.total, self.no + len(self.queue))
        self.m.slog('EVENT', 'seq=%d %s page=%d ofs=%d wave=%d+%d' % (
            ev.event_seq, ev.title, ev.page_seq, ev.sample_ofs, ev.wave_start, ev.wave_pages))
        if self.started and self.cur is None and not self.time_wait and not self.m.cmd_busy():
            self._next()

    def _next(self):
        if self.cur is not None or self.time_wait:
            return
        if self.queue:
            self.idle_deadline = None
            self.ack_deadline = None
            self._start_event(self.queue.popleft())
            return
        st = self.m.conn.status if self.m.conn else None
        # 未通知が 0 ならパッチからの切断を最大 5 秒待つ（本体 No.9・No.32。r10 で模擬器も 5 秒に戻した）
        wait = ACK_DISC_WAIT if (st is None or st.unnotified == 0) else 10.0
        self.idle_deadline = self.m.clock.now() + wait

    def _start_event(self, ev):
        self.cur = ev
        self.no += 1
        self.pages, self.raw, self.bad_raw = {}, {}, {}
        self.asm = PageAssembler()
        self.req_count = collections.Counter()
        self.unavailable = set()
        self.first_frag_t = self.last_frag_t = None
        self.wait_t0 = None
        self.long_wait_shown = False
        self.steps = ['done', 'active', 'todo', 'todo', 'todo']
        self.step_text[1] = '記録待ち（検知後 15 秒）'
        self.step_text[2] = '波形を受信中'
        self.last_saved = None
        self.m.alert_update(ev, self)
        self.t_req = self.m.clock.now()
        self.m.sender.send(Command.req_event(ev.event_seq), self._after_req)

    def _after_req(self, r):
        now = self.m.clock.now()
        if r == RES_WAITING:
            self.waiting = True
            self.wait_t0 = now
            self.receiving = True
            self.m.status_p.on_event('waiting', first=True, pct=0)
            self.m.alert_update(self.cur, self)
            return
        self.waiting = False
        if r == RES_OK:
            self.receiving = True
            self.t_req = now                         # 受信開始から EVENT_WAVE_TIMEOUT
            self.steps[1], self.steps[2] = 'done', 'active'
            if self.first_frag_t is None:
                self.m.status_p.on_event('rx_start', mode='event', pages=self.cur.wave_pages, no=self.cur.event_seq)
            self.m.alert_update(self.cur, self)
        elif r == RES_RANGE:
            self.m.slog('EVENT', 'RANGE: event %d を飛ばす' % self.cur.event_seq)
            self.cur = None
            self._next()
        elif r is None:
            self.m.fail('パッチから応答がありません', 'cmd_timeout')
        else:
            self.m.result_error(r, 'REQ_EVENT_WAVE', retry=lambda: self.m.sender.send(
                Command.req_event(self.cur.event_seq), self._after_req))

    def expected(self):
        if self.cur is None:
            return []
        return list(range(self.cur.wave_start, self.cur.wave_start + self.cur.wave_pages))

    def recv_bytes(self):
        return sum(1 for s in self.expected() if s in self.pages) * PAGE_USER + self.asm.partial_bytes

    def on_fragment(self, data):
        if self.waiting:                             # 波形が届き始めた＝記録待ち終了
            self.waiting = False
            self.m.sender.release_waiting()
            self.steps[1], self.steps[2] = 'done', 'active'
            self.t_req = self.m.clock.now()
            if self.cur:
                self.m.status_p.on_event('rx_start', mode='event', pages=self.cur.wave_pages, no=self.cur.event_seq)
        super().on_fragment(data)

    def on_progress(self):
        if self.cur is None:
            return
        tot = self.cur.wave_pages * PAGE_USER
        got = min(tot, self.recv_bytes())
        pct = int(got * 100 / tot) if tot else 0
        n = sum(1 for s in self.expected() if s in self.pages)
        self.step_text[2] = '波形を受信中 %d%%（%d／%d ページ）' % (pct, n, self.cur.wave_pages)
        self.m.status_p.on_event('progress', pct=pct, no=self.cur.event_seq,
                                 text='%d／%d ページ・%s／%s バイト' % (n, self.cur.wave_pages, format(got, ','), format(tot, ',')))
        self.m.alert_update(self.cur, self)

    def on_page(self, p):
        if self.cur and not self.missing():
            self._complete(full=True)

    def on_range_skipped(self):
        if self.cur and self.receiving and not self.waiting and self.pages and not self.missing():
            self._complete(full=True)

    def _complete(self, full):
        ev = self.cur
        self.receiving = False
        self.steps[2] = 'done' if full else 'warn'
        self.m.status_p.on_event('saving')
        kind = 'event_arr' if ev.event == EV_ARR else 'event_elec'
        rec = Cutter.event_window(self.pages, self.times(), ev.page_seq, ev.sample_ofs, kind=kind)
        if rec is None:
            self.m.slog('EVENT', 'no data to save for %d' % ev.event_seq)
            self.steps[3] = 'error'
            self.cur = None
            self.m.alert_update(ev, self)
            self._next()
            return
        extra = {'pages_received': sorted(s for s in self.pages if s in self.expected()),
                 'pages_missing': sorted(set(self.missing()) | (self.unavailable & set(self.expected()))),
                 'received_at': self.m.clock.local_datetime().strftime('%Y-%m-%d %H:%M:%S')}
        try:
            self.last_saved = self.m.save_record(rec, self.raw, ev, 'event', extra)
        except StorageError as e:
            self.m.err(e, 'save_event')
            self.steps[3] = 'error'
            self.step_text[3] = '保存できませんでした'
            self.m.alert_update(ev, self)
            self.m.fail('ファイルを保存できませんでした', 'storage', send_stop=False)
            return
        self.steps[3] = 'done'
        self.m.alert_update(ev, self)
        if not full:                                  # 一部だけ：ACK しない（次回再送される）
            self.m.fail('一部を受信できませんでした（ACK しません）', 'event_incomplete', send_stop=False)
            return
        self.steps[4] = 'active'
        extra['acked_at'] = ''
        self.m.sender.send(Command.ack_event(ev.event_seq), lambda r: self._after_ack(r, ev, rec, extra))

    def _after_ack(self, r, ev, rec, extra):
        if r == RES_OK:
            self.steps[4] = 'done'
            self.acked += 1
            # acked_at を event.json に書き足す
            try:
                if self.last_saved and self.last_saved.get('event'):
                    with open(self.last_saved['event'], encoding='utf-8') as f:
                        d = json.load(f)
                    d['acked_at'] = self.m.clock.local_datetime().strftime('%Y-%m-%d %H:%M:%S')
                    rel = os.path.relpath(self.last_saved['event'], self.m.store.root)
                    self.m.store.write_atomic(rel, json.dumps(d, ensure_ascii=False, indent=1).encode('utf-8'))
            except Exception as e:
                self.m.trace.w('EVENT', 'acked_at update failed', e)
            self.m.alert_update(ev, self)
            self.m.status_p.on_event('done', count=self.acked)
            self.cur = None
            self._next()
        elif r is None:
            self.m.fail('ACK の応答がありません（保存済み）', 'cmd_timeout')
        else:
            self.m.result_error(r, 'ACK_EVENT', retry=lambda: self.m.sender.send(
                Command.ack_event(ev.event_seq), lambda rr: self._after_ack(rr, ev, rec, extra)))

    def tick(self):
        super().tick()
        now = self.m.clock.now()
        if self.cur is not None:
            if self.waiting and self.wait_t0 is not None:
                w = now - self.wait_t0
                left = max(0, int(15 - w + 0.999))
                if w > WAITING_LONG:
                    # r7：20 秒を超えたら表示を保つ（r6 は次の周期で「記録待ち（まもなく）」に戻っていた：試験 No.9）
                    self.step_text[1] = '記録済みの範囲だけ受信します（待ち %d 秒）' % int(w)
                    if not self.long_wait_shown:
                        self.long_wait_shown = True
                        self.steps[1] = 'warn'
                        self.m.status_p.on_event('message', text='記録待ちが 20 秒を超えました')
                else:
                    self.step_text[1] = '記録待ち（残り %d 秒）' % left if left > 0 else '記録待ち（まもなく）'
                self.m.status_p.on_event('waiting', pct=min(100, int(w / 15 * 100)), text=self.step_text[1])
                self.m.alert_update(self.cur, self)
            elif self.receiving and self.t_req is not None and now - self.t_req > EVENT_WAVE_TIMEOUT:
                self.m.emit_app('warn', 'timeout', what='EVENT_WAVE')
                if self.pages:
                    self._complete(full=False)
                else:
                    self.m.fail('波形を受信できませんでした', 'event_timeout')
        elif self.queue and not self.m.cmd_busy():
            self._next()
        elif self.idle_deadline is not None and now > self.idle_deadline and not self.m.cmd_busy():
            self.idle_deadline = None
            self.acked_all = self.acked > 0 and not self.queue
            self.done = True
            if self.acked_all:
                self.m.slog('EVENT', '全 %d 件の ACK が完了' % self.acked)
            self.m.emit_app('app', 'no_disc_after_ack' if self.acked_all else 'nothing_to_do')
            self.m.disconnect('ack_done' if self.acked_all else 'nothing')

    def on_link_lost(self):
        if self.cur is not None and self.pages and self.last_saved is None:
            ev = self.cur
            kind = 'event_arr' if ev.event == EV_ARR else 'event_elec'
            rec = Cutter.event_window(self.pages, self.times(), ev.page_seq, ev.sample_ofs, kind=kind)
            if rec is not None:
                extra = {'pages_received': sorted(self.pages), 'pages_missing': self.missing(),
                         'received_at': self.m.clock.local_datetime().strftime('%Y-%m-%d %H:%M:%S')}
                try:
                    self.last_saved = self.m.save_record(rec, self.raw, ev, 'event_lost', extra)
                except StorageError as e:
                    self.m.err(e, 'save_event_lost')
            n = sum(1 for s in self.expected() if s in self.pages)
            self.steps[2] = 'warn'
            self.step_text[2] = '中断（%d／%d ページ）' % (n, ev.wave_pages)
            self.m.alert_update(ev, self)


# =============================================================================
# A04 接続・A06 受信・A07 切断・A13 SessionManager（7 章・9 章・16 章）
# =============================================================================
class ConnCtx:
    def __init__(self, info, t0, patient_id, auto):
        self.info = info
        self.t0 = t0
        self.patient_id = patient_id
        self.auto = auto
        self.chars = set()
        self.sub_pending = 0
        self.status = None
        self.ready = False
        self.power_off = False
        self.invalidated = False
        self.t_conn = None              # r10：didConnect の時刻（Debug Info は 1 秒以降の値を使う）
        self.dbg_reread_at = None
        self.disc_reason = None
        self.disc_deadline = None
        self.mtu = None
        self.time_anchor = None         # SET_TIME で送った時刻
        self.dbg = None
        self.battery = None
        self.rssi = []
        self.rx_bytes = 0
        self.t_wall = None              # K1：接続を始めた時刻
        self.ecg_first = None           # K1：ECG Stream の最初と最後の受信（monotonic）
        self.ecg_last = None
        self.ecg_active = 0.0           # K1：受信していた時間（0.5 秒以上の空きは含めない）


class SessionManager:
    """本アプリの状態機械（IDLE / SCANNING / CONNECTING / DISCOVERING / SETUP / EVENT / DISCONNECTING）"""

    def __init__(self, transport, clock, store, settings, trace, monitor=None, recorder=None):
        self.t = transport
        self.clock = clock
        self.store = store
        self.settings = settings
        self.trace = trace
        self.monitor = monitor
        self.recorder = recorder
        self.q = queue.Queue()
        self.lock = threading.RLock()
        self.state = 'IDLE'
        self.bt_state = 'unknown'
        self.scanner = Scanner(settings, clock)
        self.sender = CommandSender(self)
        self.status_p = StatusPanelPresenter(clock)
        self.exporter = Exporter(store, clock, settings)
        self.conn = None
        self.session = None
        self.saved_list = []            # 保存したファイル（新しい順）
        self.ui_events = collections.deque()
        self.alert = None               # SC5 の表示元
        self.slog_obj = None
        self.errors = ErrorHandler(trace, lambda k, m: self.status_p.on_event('error', text=m))
        self.last_rssi_t = 0
        self.time_pending = None
        self.corrupted = False
        self.bad_count = 0
        self.probe_ok = 0
        transport.post = self.post
        transport.clock = clock

    # ---- 作業スレッドのキュー
    def post(self, fn, *a):
        self.q.put((fn, a))

    def pump(self, timeout=0.0, max_items=2000):
        n = 0
        while n < max_items:
            try:
                item = self.q.get(timeout=timeout) if (timeout and n == 0) else self.q.get_nowait()
            except queue.Empty:
                break
            with self.lock:
                fn, a = item if (type(item) is tuple and len(item) == 2) else (item, None)
                if not callable(fn) or type(a) is not tuple:
                    # 積んだはずの (関数, 引数) が別の物に化けている＝Python のメモリが壊れている（r1・r3 の実機で発生）
                    self.on_corruption('BAD_ITEM %s/%s' % (type(fn).__name__, type(a).__name__))
                    n += 1
                    continue
                try:
                    fn(*a)
                except Exception as e:
                    self.err(e, getattr(fn, '__qualname__', type(fn).__name__))
            n += 1
        return n

    # ---- メモリ破損の検出（診断）
    def on_corruption(self, what):
        self.bad_count += 1
        self.trace.w('CORRUPT', what, 'count=%d' % self.bad_count, 'stage=%s' % DIAG.get('stage', ''))
        if not self.corrupted:
            self.corrupted = True
            self.status_p.on_event('error', text='内部エラー（メモリ破損）を検出しました。BLE を止めています')

    def probe(self, n, tag):
        """画面のスレッドから積んだ検査用の値が、そのまま届いたかを確かめる"""
        if tag == ('probe', n, n * 7):
            self.probe_ok += 1
            if self.probe_ok % 50 == 0:
                self.trace.w('PROBE', 'ok=%d bad=%d' % (self.probe_ok, self.bad_count))
        else:
            self.on_corruption('PROBE n=%r tag=%r' % (type(n).__name__, type(tag).__name__))

    # ---- 記録
    def err(self, e, where):
        self.errors.handle(e, where)

    def slog(self, kind, text):
        self.trace.v(kind, text)
        if self.slog_obj:
            self.slog_obj.write(kind, text)

    def emit_app(self, kind, ev, **v):
        self.t._emit(kind, ev, peer=self.conn.info.device_id if self.conn else '', **v)

    def ui_event(self, *ev):
        self.ui_events.append(ev)

    # ---- 起動・BLE 状態
    def start(self):
        if self.corrupted:
            self.trace.w('BLE', 'not started: memory corruption detected before BLE')
            return
        self.t.start(self.on_bt_state)

    def on_bt_state(self, name):
        self.bt_state = name
        self.trace.w('BT', name)
        self.status_p.on_event('bt_state', state=name)
        if name == 'poweredOn':
            if self.state in ('IDLE', 'SCANNING'):
                self.start_scan()
        else:
            if self.conn is not None:                 # 16.2：Bluetooth OFF → 中止して保存
                if self.session:
                    self.session.on_link_lost()
                self._cleanup_conn('bt_off')
            self.state = 'IDLE'

    def start_scan(self):
        self.state = 'SCANNING'
        self.t.scan_start(SVC_ECG, self.on_adv)
        self.status_p.on_event('scan')
        self.trace.w('SCAN', 'start')

    def on_adv(self, peer, adv, rssi):
        now = self.clock.now()
        name = adv.get('name')
        try:
            info = parse_adv(peer, adv.get('mfr'), name, rssi, now, self.settings['accept_emulator'])
        except ProtocolError as e:
            self.trace.v('ADV', 'invalid', peer, e)
            return
        # K4：広告の内容（名前・Event・状態・受け入れ）が変わったときに 1 行記録（r7 までは最初の 1 回だけ）
        sig = (name, info is not None, info.event if info else None, info.sub if info else None,
               info.status if info else None)
        if self.scanner.adv_logged.get(peer) != sig:
            self.scanner.adv_logged[peer] = sig
            self.trace.w('ADV', 'name=%s mfr=%s rssi=%s accepted=%s%s' % (
                name, 'none' if adv.get('mfr') is None else len(adv['mfr']), rssi, info is not None,
                (' event=%02X sub=%02X status=%02X reg=%s' % (info.event, info.sub, info.status,
                                                               self.settings.is_registered(info.device_id))) if info else ''))
        if info is None:
            if is_emulator_name(name) and not self.scanner.emulator_hint:
                self.scanner.emulator_hint = True
                self.status_p.on_event('message', text='模擬器を検出しました（設定「模擬器を受け入れる」を ON に）')
                self.ui_event('ask_emulator', name)
            return
        self.t._emit('cb', 'adv', peer=info.device_id, name=info.label, rssi=rssi, event=info.event, status=info.status)
        new = self.scanner.on_adv(info)
        if new:
            self.status_p.on_event('found', name=info.label, kind=kind_text(info.event) + ('・模擬器' if info.emulator else ''))
            self.slog_global('SCAN', '%s event=%02X batt=%d rssi=%d%s' % (info.device_id, info.event, info.battery, rssi,
                                                                          ' emulator' if info.emulator else ''))
        if self.state == 'SCANNING':
            tgt = self.scanner.auto_target()
            if tgt is not None:
                if tgt.event in (EV_ELEC, EV_ARR):
                    self.status_p.on_event('alert', name=tgt.label)
                    self.ui_event('alert', tgt.label, event_title(tgt.event, tgt.sub))
                self._begin_connect(tgt, None, auto=True)

    def slog_global(self, kind, text):
        self.trace.v(kind, text)

    def patch_rows(self):
        rows = []
        for a in self.scanner.devices()[:3]:
            reg = self.settings.is_registered(a.device_id)
            if a.event in (EV_ELEC, EV_ARR):
                kind = event_title(a.event, a.sub) if reg else '未登録パッチの異常通知'
                key = (a.peer, a.event, a.sub, reg)
                if key not in self.scanner.alert_logged:           # K4：異常通知の表示を記録
                    self.scanner.alert_logged.add(key)
                    self.trace.w('LIST', '%s %s（%s）' % (a.label, kind, '登録済み' if reg else '未登録'))
                btn = '自動接続' if reg and self.settings['auto_connect_alert'] else ('接続' if reg else '')
                color = 'red'
            else:
                kind = kind_text(a.event)
                btn = '接続'
                color = 'blue' if a.event == EV_SETUP else 'ink'
            sub = '電池 %s%%・RSSI %d・%s%s%s' % ('—' if a.battery == 0xFF else a.battery, a.rssi,
                                                '時刻設定済み' if a.time_set else '時刻未設定',
                                                '・電極 NG' if a.electrode_ng else '',
                                                '・模擬器' if a.emulator else '')
            rows.append((a.label, kind, sub, btn, a.peer, color))
        return rows

    # ---- 利用者の操作（UI から post される）
    def connect_to(self, peer, patient_id=None):
        if self.state != 'SCANNING':
            self.trace.w('CONNECT', 'ignored in', self.state)
            if self.state == 'IDLE' and self.bt_state == 'poweredOn':   # r10：探索が止まっていたら再開する
                self.start_scan()
            return
        info = self.scanner.table.get(peer)
        if info is None:
            self.trace.w('CONNECT', 'peer not in list', peer)
            return
        self.scanner.cooldown.pop(peer, None)        # r10：「接続」は待ち時間（直前の切断から 15 秒）より優先
        if info.event in (EV_ELEC, EV_ARR) and not self.settings.is_registered(info.device_id):
            self.status_p.on_event('message', text='未登録パッチの異常通知は表示のみです')
            return
        self._begin_connect(info, patient_id, auto=False)

    def user_stop(self):
        """「中止」：STOP を送り、受信分を _partial で保存して切断"""
        if self.session is None or self.conn is None:
            return
        s = self.session

        def after(_r):
            if not s.saved:
                if isinstance(s, SetupSession) and s.window:
                    s.saved = self.save_record(s.window, s.raw, None, 'setup_stop')
                else:
                    s.save_partial('stop')
            self.disconnect('user_stop')
        self.sender.cancel_all()
        self.sender.send(Command.stop(), after)

    def setup_continue(self):
        if isinstance(self.session, SetupSession):
            self.session.user_continue()

    # ---- 接続（A04：9.2 節の手順 1〜10）
    def _begin_connect(self, info, patient_id, auto):
        self.t.scan_stop()
        self.state = 'CONNECTING'
        self.conn = ConnCtx(info, self.clock.now(), patient_id, auto)
        self.conn.t_wall = self.clock.local_datetime()
        self.sender = CommandSender(self)
        rel = os.path.join('ECG', self.clock.local_datetime().strftime('%Y%m%d'),
                           'session_%s.log' % self.clock.local_datetime().strftime('%Y%m%d-%H%M%S'))
        self.slog_obj = SessionLog(self.store, self.clock, rel)
        self.slog('SCAN', '%s event=%02X batt=%d rssi=%d' % (info.device_id, info.event, info.battery, info.rssi))
        self.status_p.on_event('connecting', name=info.label)
        if info.emulator:
            self.t._emit('app', 'emulator', peer=info.device_id)
        self.t._emit('op', 'connect', peer=info.device_id)
        if auto and info.event in (EV_ELEC, EV_ARR):
            self.alert = {'device': info.label, 'title': event_title(info.event, info.sub), 'time': '',
                          'counter': '', 'chips': [], 'steps': [('active', '通知を受信（Event Log）')] +
                          [('todo', t) for t in ('記録待ち（検知後 15 秒）', '波形を受信中',
                                                 '保存（ECG＋メタ＋イベント）', 'パッチへ確認（ACK）')],
                          'can_view': False, 'saved': None, 'error': '', 't0': self.clock.now()}
            self.ui_event('show_sc5')
        self.t.connect(info.peer, self._on_connected, self._on_failed, self._on_disconnected, self._on_invalidated)

    def _on_connected(self):
        if self.conn is None:
            return
        self.state = 'DISCOVERING'
        self.conn.t_conn = self.clock.now()
        self.slog('CONNECT', '%s connected' % self.conn.info.device_id)
        self.t.discover([SVC_ECG, SVC_DIS, SVC_BAT], self._on_discovered)

    def _on_discovered(self, chars):
        c_ = self.conn
        if c_ is None:
            return
        c_.chars = set(chars)
        need = [CH_STREAM, CH_EVENTLOG, CH_STATUS, CH_CONTROL]
        lack = [CH_SHORT[u] for u in need if u not in c_.chars]
        if lack:                                     # A04-04
            self.err(ProtocolError('missing characteristics: %s' % ','.join(lack)), 'discover')
            self.fail('パッチの特性が足りません（%s）' % ','.join(lack), 'protocol', send_stop=False)
            return
        c_.sub_pending = 3
        self.t.subscribe(CH_STATUS, self._on_status_value, self._on_sub_state)
        self.t.subscribe(CH_EVENTLOG, self._on_eventlog, self._on_sub_state)
        self.t.subscribe(CH_STREAM, self._on_stream, self._on_sub_state)

    def _on_sub_state(self, uuid, err):
        c_ = self.conn
        if c_ is None:
            return
        if err:
            self.trace.w('SUBSCRIBE', CH_SHORT.get(uuid, uuid), err)
        c_.sub_pending -= 1
        if c_.sub_pending == 0:
            self.t.read(CH_STATUS, self._on_first_status)

    def _on_first_status(self, uuid, data):
        c_ = self.conn
        if c_ is None:
            return
        try:
            c_.status = DevStatus.from_bytes(data)
        except ProtocolError as e:
            self.err(e, 'status')
            self.fail('Device Status を読めませんでした', 'protocol', send_stop=False)
            return
        if CH_SERIAL in c_.chars:
            self.t.read(CH_SERIAL, self._on_serial)
        elif CH_EMU in c_.chars and self.settings['accept_emulator']:
            self.t.read(CH_EMU, self._on_emu_info)
        else:
            self.fail('デバイス ID を照合できませんでした', 'id_unknown', send_stop=False)

    def _on_serial(self, uuid, data):
        sn = bytes(data).decode('ascii', 'replace').strip('\x00 ').upper()
        self._verify_id(sn)

    def _on_emu_info(self, uuid, data):
        if len(data) < 4:
            self.fail('Emulator Info が不正です', 'protocol', send_stop=False)
            return
        dev = struct.unpack_from('<I', bytes(data))[0]
        self._verify_id('%08X' % dev)

    def _verify_id(self, sn):
        c_ = self.conn
        if c_ is None:
            return
        if sn != c_.info.device_id:                  # A04-03：誤接続防止
            self.slog('CONNECT', 'ID mismatch adv=%s read=%s' % (c_.info.device_id, sn))
            self.fail('デバイス ID が一致しません（%s）' % sn, 'id_mismatch', send_stop=False)
            return
        self._on_ready()

    def _on_ready(self):
        c_ = self.conn
        c_.ready = True
        c_.mtu = self.t.max_write_len() + 3
        self.t._emit('app', 'mtu', peer=c_.info.device_id, mtu=c_.mtu)
        ms = int((self.clock.now() - c_.t0) * 1000)
        self.slog('CONNECT', '%s mtu=%d ready %d ms%s' % (c_.info.device_id, c_.mtu, ms, ' (emulator)' if c_.info.emulator else ''))
        if CH_DEBUG in c_.chars:
            self.t.read(CH_DEBUG, self._on_debug)
            if self.settings['dev_display']:
                self.t.subscribe(CH_DEBUG, self._on_debug)
        if CH_BATT in c_.chars:
            self.t.read(CH_BATT, self._on_batt)
        st = c_.status
        self.status_p.on_event('connected', name=c_.info.label + ('（模擬器）' if c_.info.emulator else ''),
                               mtu=c_.mtu, detail=self.detail_text())
        if st.state == ST_S2 and not (c_.auto and c_.info.event in (EV_ELEC, EV_ARR)):
            self.session = SetupSession(self, c_.patient_id)
        elif st.unnotified > 0 or st.state == ST_S5:
            self.session = EventSession(self)
        else:
            self.status_p.on_event('message', text='処理するデータがありません（状態 S%d）' % st.state)
            self.disconnect('nothing')
            return
        self.session.on_status(st)
        for ev in getattr(c_, 'early_events', []):   # 購読直後に届いた Event Log
            self.session.on_event(ev)
        self.session.start()

    def detail_text(self):
        c_ = self.conn
        if c_ is None:
            return 'Bluetooth: %s' % ('オン' if self.bt_state == 'poweredOn' else self.bt_state)
        st = c_.status
        parts = [c_.info.label + ('（模擬器）' if c_.info.emulator else '')]
        if c_.mtu:
            parts.append('MTU %d' % c_.mtu)
        batt = c_.battery if c_.battery is not None else (st.batt if st else None)
        if batt is not None and batt != 0xFF:
            parts.append('電池 %d%%' % batt)
        if st:
            parts.append('電極 %s' % ('OK' if st.electrode == 0 else 'NG'))
        if self.settings['dev_display']:
            if c_.rssi:
                parts.append('RSSI %d' % c_.rssi[-1])
            if c_.dbg:
                parts.append('%.0fms・%s' % (c_.dbg['int_ms'], c_.dbg['phy_txt']))
        return '・'.join(parts)

    # ---- 受信の振り分け（A06）
    def _on_status_value(self, uuid, data):
        c_ = self.conn
        try:
            st = DevStatus.from_bytes(data)
        except ProtocolError as e:                   # A06-03
            self.err(e, 'status')
            return
        self.t._emit('rx', 'notify', peer=c_.info.device_id if c_ else '', ch='STS', data=data,
                     result=st.result, last_cmd=st.last_cmd, state=st.state)
        if c_ is None:
            return
        prev = c_.status
        c_.status = st
        if st.state == ST_S6:
            c_.power_off = True
        self.sender.on_status(st)
        if self.session:
            self.session.on_status(st)
        if prev is None or prev.electrode != st.electrode or prev.batt != st.batt:
            self.status_p.on_event('detail', text=self.detail_text())

    def _on_eventlog(self, uuid, data):
        c_ = self.conn
        self.t._emit('rx', 'indicate', peer=c_.info.device_id if c_ else '', ch='EVT', data=data)
        try:
            ev = EventRec.from_bytes(data)
        except ProtocolError as e:
            self.err(e, 'event')
            return
        if self.session is None:
            if c_ is not None:
                c_.__dict__.setdefault('early_events', []).append(ev)
            return
        self.session.on_event(ev)

    def _on_stream(self, uuid, data):
        c_ = self.conn
        if c_ is not None:
            c_.rx_bytes += len(data)
            t_ = self.clock.now()
            if c_.ecg_first is None:
                c_.ecg_first = t_
            elif t_ - c_.ecg_last < 0.5:
                c_.ecg_active += t_ - c_.ecg_last
            c_.ecg_last = t_
        self.t._emit('rx', 'notify', peer=c_.info.device_id if c_ else '', ch='ECG', data=data)
        if self.session is not None:
            try:
                self.session.on_fragment(data)
            except ProtocolError as e:
                self.err(e, 'fragment')

    def _on_debug(self, uuid, data):
        if len(data) < 32 or self.conn is None:
            return
        d = parse_dbg_info(data)
        if d['ver'] != 1:
            self.trace.w('DBG', 'ver', d['ver'])
            return
        c_ = self.conn
        if c_.t_conn is not None and self.clock.now() - c_.t_conn < 1.0:
            # r10（別紙 2 未決 8）：接続直後の値（接続間隔 0・RSSI 127・MTU 23）は更新前のことがあるため、1 秒後に読み直す
            if c_.dbg_reread_at is None:
                c_.dbg_reread_at = c_.t_conn + 1.0
            if c_.dbg is None:
                c_.dbg = d
            return
        self.conn.dbg = d
        self.t._emit('dbg', 'debug_info', peer=self.conn.info.device_id, ch='DBG',
                     int_ms=d['int_ms'], lat=d['slave_latency'], to_s=d['to_s'], phy=d['phy_txt'],
                     mtu=d['att_mtu'], tx_free=d['tx_free'], rssi=d['rssi'], busy=d['notify_busy'],
                     last_disc=d['last_disc'])

    def _on_batt(self, uuid, data):
        if data and self.conn:
            self.conn.battery = data[0]

    # ---- 異常通知画面（SC5）の元データ
    def alert_update(self, ev, s):
        a = self.alert
        if a is None:
            a = self.alert = {'device': self.conn.info.label if self.conn else '', 't0': self.clock.now(),
                              'saved': None, 'error': ''}
            self.ui_event('show_sc5')
        a['title'] = ev.title
        a['time'] = ev.time_text
        a['counter'] = '未通知 %d 件中 %d 件目' % (max(s.total, s.no), s.no)
        if ev.event == EV_ARR:
            a['chips'] = [('%d ms' % ev.rr_ms, 'R-R'), ('%d ms' % ev.median_ms, '直前の中央値'),
                          ('%d' % ev.hr_bpm, '心拍/分')]
        else:
            batt = self.conn.status.batt if self.conn and self.conn.status else 0xFF
            a['chips'] = [(ELEC_SUB.get(ev.sub & 0x7F, '—'), '判定'), (ev.time_text[11:] or '—', '検知時刻'),
                          ('%s%%' % batt if batt != 0xFF else '—', '電池')]
        a['steps'] = list(zip(s.steps, s.step_text))
        a['can_view'] = s.steps[3] == 'done'
        if s.last_saved:
            a['saved'] = dict(s.last_saved, detect_index=-1)
            a['saved']['kind'] = 'event'

    # ---- 保存
    def save_record(self, rec, raw, event, why, extra=None):
        dev = self.conn.info.device_id if self.conn else 'UNKNOWN'
        paths = self.exporter.save(rec, dev, raw, event, extra)
        self.slog('SAVE', '%s (%d) %s' % (paths['name'] + '.csv', len(rec.ecg), why))
        self.status_p.on_event('saved', name=paths['name'], samples=len(rec.ecg))
        item = dict(paths, kind=rec.kind, device=dev, time=rec.first_time.strftime('%Y-%m-%d %H:%M:%S'),
                    detect_index=rec.detect_index, leadoff=any(r[3] for r in rec.meta))
        self.saved_list.insert(0, item)
        self.ui_event('saved', item)
        if self.alert is not None and event is not None:
            self.alert['saved'] = item
        return paths

    def on_setup_acked(self):
        c_ = self.conn
        self.settings.register(c_.info.device_id, 'ok', self.clock.local_datetime())
        self.slog('SETUP', '装着確認が完了しました。計測を開始しました')
        self.status_p.on_event('done', count=1)
        self.status_p.on_event('message', text='装着確認が完了しました。計測を開始しました')
        self.ui_event('setup_done')

    # ---- 失敗・切断（A07）
    def result_error(self, r, what, retry=None):
        """9.4 節の結果コードの処理"""
        name = RES_NAMES[r] if r is not None and r < len(RES_NAMES) else str(r)
        self.slog('RESULT', '%s -> %s' % (what, name))
        if r in (RES_BAD_LEN, RES_UNKNOWN):
            self.err(ResultCodeError(r, '%s %s' % (what, name)), 'result')
            self.fail('本アプリの不具合（%s %s）' % (what, name), 'bad_cmd', send_stop=False)
        elif r == RES_BAD_STATE:
            self.t.read(CH_STATUS, self._after_bad_state)
        elif r == RES_NAND and retry and not getattr(self, '_nand_retried', False):
            self._nand_retried = True
            self.status_p.on_event('message', text='パッチ側の NAND 異常（再試行します）')
            retry()
        elif r == RES_BUSY:
            self.fail('パッチが別の転送中です', 'busy')
        else:
            self.fail('パッチがエラーを返しました（%s %s）' % (what, name), 'result')

    def _after_bad_state(self, uuid, data):
        try:
            st = DevStatus.from_bytes(data)
        except ProtocolError:
            st = None
        if self.conn:
            self.conn.status = st or self.conn.status
        self.status_p.on_event('message', text='パッチの状態が合いません（S%s）' % (st.state if st else '?'))
        self.disconnect('bad_state')

    def fail(self, text, reason, send_stop=True):
        self.status_p.on_event('error', text=text)
        if self.alert is not None:
            self.alert['error'] = text
        if self.session and not self.session.saved:
            try:
                self.session.on_link_lost()
            except Exception as e:
                self.err(e, 'save_on_fail')
        if send_stop and self.conn and self.conn.ready:
            self.sender.cancel_all()
            self.sender.send(Command.stop(), lambda r: self.disconnect(reason))
        else:
            self.disconnect(reason)

    def send_set_time(self, cb):
        """K2：SET_TIME は秒までしか送れないため、秒の変わり目を待って送る（誤差 1 秒未満 → 数十 ms）。
        cb(result, 送った時刻)"""
        dt = self.clock.local_datetime()
        wait = 1.0 - dt.microsecond / 1e6
        if dt.microsecond < 30000:
            self._send_set_time_now(cb)
            return
        self.time_pending = (self.clock.now() + wait, cb)
        self.slog('CMD', 'SET_TIME は %.2f 秒後（秒の変わり目）に送ります' % wait)

    def _send_set_time_now(self, cb):
        sent = [None]

        def make():                                   # r9：実際に書き込む時刻で作る（再送・待たされた場合も古い時刻を送らない）
            dt = self.clock.local_datetime()
            if dt.microsecond >= 500000:              # 早く起きた場合は次の秒に丸める
                dt += datetime.timedelta(microseconds=1000000 - dt.microsecond)
            sent[0] = dt.replace(microsecond=0)
            return Command.set_time(sent[0])
        self.sender.send(make(), lambda r: cb(r, sent[0]), make=make)

    def cmd_busy(self):
        """r9：SET_TIME を秒の変わり目まで待っている間も「送信中」とみなす（試験 No.11）"""
        return self.sender.busy() or self.time_pending is not None

    def finish_session(self, reason):
        self.disconnect(reason)

    def disconnect(self, reason):
        c_ = self.conn
        if c_ is None or self.state == 'DISCONNECTING':
            return
        self.state = 'DISCONNECTING'
        c_.disc_reason = reason
        c_.disc_deadline = self.clock.now() + DISC_TIMEOUT
        self.slog('DISC', 'request reason=%s' % reason)
        self.t._emit('op', 'disconnect', peer=c_.info.device_id, reason=reason)
        self.t.disconnect()

    def _on_failed(self, err):
        self.trace.w('CONNECT', 'failed', err)
        self.slog('CONNECT', 'failed %s' % (err,))
        if self.conn and self.alert is not None:
            self.alert['error'] = '接続できませんでした（パッチ側に未通知として残ります）'
        self.status_p.on_event('error', text='パッチに接続できませんでした')
        self._cleanup_conn('connect_failed')

    def _on_invalidated(self):
        """didModifyServices：模擬器の電源 OFF・GATT 変更 → パッチ側の終了として切断"""
        if self.conn is None:
            return
        self.conn.invalidated = True
        self.slog('DISC', 'services invalidated')
        if self.session:
            self.session.on_link_lost()
        self.disconnect('services_invalidated')

    def _on_disconnected(self, err):
        self._finalize(err)

    def _finalize(self, err):
        c_ = self.conn
        if c_ is None:
            return
        s = self.session
        if s is not None and s.acked_all:
            judge = 'normal'
            text = None
        elif c_.power_off or c_.invalidated:
            judge = 'power_off'
            text = 'ECG パッチの電源が切れました'
            if s:
                s.on_link_lost()
        elif c_.disc_reason in ('nothing', 'user_stop', 'bad_state', 'state'):
            judge = c_.disc_reason
            text = None if c_.disc_reason == 'nothing' else '接続を終了しました（%s）' % c_.disc_reason
        elif s is not None and not s.done:
            judge = 'lost'
            text = '受信の途中で ECG パッチとの接続が切れました'
            s.on_link_lost()
        else:
            judge = c_.disc_reason or 'remote'
            text = None if c_.disc_reason in ('ack_done',) else '接続が切れました'
        self.slog('DISC', 'reason=%s judge=%s err=%s (status S%s)' % (
            c_.disc_reason or 'remote', judge, err, c_.status.state if c_.status else '?'))
        self.t._emit('cb', 'didDisconnect', peer=c_.info.device_id, err=err if isinstance(err, tuple) else None,
                     judge=judge, unexpected=judge in ('lost',))
        if text:
            self.status_p.on_event('disconnected', text=text)
            if self.alert is not None and judge != 'normal':
                self.alert['error'] = self.alert.get('error') or text
        if judge == 'normal' and isinstance(s, EventSession):
            self.settings.register(c_.info.device_id, None, self.clock.local_datetime())
        self._summary(c_, judge)
        self._cleanup_conn(judge)

    def _summary(self, c_, judge):
        if self.recorder is None:
            return
        s = self.session
        snap = self.monitor.snapshot() if self.monitor else {}
        rs = [r for r in c_.rssi] or [0]
        # K1：受信速度＝ECG の受信バイト ÷ 受信していた時間（転送の合間の待ちは除く。r7 までは 1 秒単位で数えて誤っていた）
        dur = c_.ecg_active
        rate = round(c_.rx_bytes / dur / 1000.0, 1) if dur > 0.05 else ''
        t_wall = c_.t_wall or self.clock.local_datetime()
        self.recorder.append_summary([
            t_wall.strftime('%Y-%m-%d %H:%M:%S'), c_.info.device_id, snap.get('connect_ms'),
            c_.mtu, round(sum(rs) / len(rs), 1), c_.rx_bytes, rate,
            (s.asm.stats['gaps'] + s.asm.stats['dups']) if s else 0, s.crc_ng if s else 0,
            s.req_pages_sent if s else 0, judge])

    def _cleanup_conn(self, judge):
        c_ = self.conn
        if c_ is not None:
            self.scanner.cooldown[c_.info.peer] = self.clock.now() + 15
            self.scanner.table.pop(c_.info.peer, None)
            self.scanner.seen.discard(c_.info.peer)
        self.conn = None
        self.session = None
        self.time_pending = None
        self.sender.cancel_all()
        self.slog_obj = None
        self.ui_event('session_end', judge)
        if self.bt_state == 'poweredOn':
            self.start_scan()
        else:
            self.state = 'IDLE'

    # ---- 周期処理（タイムアウト：9.7 節）
    def tick(self):
        with self.lock:
            now = self.clock.now()
            c_ = self.conn
            if c_ is not None:
                if self.state == 'DISCONNECTING':
                    if c_.disc_deadline is not None and now > c_.disc_deadline:
                        self.trace.w('DISC', 'force cleanup')     # A07-04
                        self._finalize(('app', 0, 'disconnect_timeout'))
                    return
                if not c_.ready and now - c_.t0 > CONNECT_TIMEOUT:
                    self.emit_app('warn', 'timeout', what='CONNECT')
                    if self.alert is not None:
                        self.alert['error'] = '接続できませんでした（パッチ側に未通知として残ります）'
                    self.fail('パッチに接続できませんでした（%d 秒）' % CONNECT_TIMEOUT, 'connect_timeout', send_stop=False)
                    return
                if c_.dbg_reread_at is not None and now >= c_.dbg_reread_at and c_.ready:
                    c_.dbg_reread_at = None
                    self.t.read(CH_DEBUG, self._on_debug)
                if self.time_pending is not None and now >= self.time_pending[0]:
                    cb = self.time_pending[1]
                    self.time_pending = None
                    self._send_set_time_now(cb)
                self.sender.tick()
                if self.session is not None:
                    self.session.tick()
                if c_.ready and now - self.last_rssi_t >= 1.0:
                    self.last_rssi_t = now
                    self.t.read_rssi()
            self.status_p.tick(self.state == 'SCANNING')

    def on_rssi(self, rssi):
        if self.conn is not None and rssi is not None and rssi != 127:
            self.conn.rssi.append(rssi)
            self.conn.rssi = self.conn.rssi[-30:]

    # ---- 画面用のスナップショット（UI スレッドから呼ぶ）
    def snapshot(self):
        acquired = self.lock.acquire(timeout=0.05)
        try:
            s = self.session
            prev = None
            cells = None
            electrode_ok = None
            normal = None
            mode = s.mode if s else None
            if s is not None and s.pages:
                hi = max(s.pages)
                buf = array('h')
                for seq in (hi - 1, hi):
                    p = s.pages.get(seq)
                    if p is not None:
                        buf.extend(p.samples[:p.n_cycles * CYCLE])
                prev = buf
            if isinstance(s, SetupSession):
                cells = s.cells()
                normal = s.normal_secs()
                electrode_ok = s.electrode_ok
            st = self.conn.status if self.conn else None
            return {
                'state': self.state, 'bt': self.bt_state, 'mode': mode,
                'status': self.status_p.view_model(self.patch_rows() if self.state == 'SCANNING' else []),
                'preview': prev, 'cells': cells, 'normal_secs': normal,
                'electrode_ok': electrode_ok if electrode_ok is not None else (st.electrode == 0 if st else None),
                'alert': json.loads(json.dumps(self.alert, default=str)) if self.alert else None,
                'emulator': bool(self.conn and self.conn.info.emulator),
                'saved': list(self.saved_list[:5]),
            }
        finally:
            if acquired:
                self.lock.release()

    def shutdown(self, timeout=2.0):
        """16.1：STOP → 切断 → 受信済みを _partial で保存"""
        with self.lock:
            if self.conn is not None and self.conn.ready and self.session is not None and not self.session.done:
                try:
                    self.sender.cancel_all()
                    self.t.write(CH_CONTROL, Command.stop().to_bytes(), lambda e: None)
                except Exception:
                    pass
            if self.session is not None and not self.session.saved:
                try:
                    if isinstance(self.session, SetupSession) and self.session.window:
                        self.save_record(self.session.window, self.session.raw, None, 'shutdown')
                    else:
                        self.session.save_partial('shutdown')
                except Exception as e:
                    self.err(e, 'shutdown_save')
            try:
                self.t.scan_stop()
                if self.conn is not None:
                    self.t.disconnect()
            except Exception:
                pass


# =============================================================================
# A02 実機：CoreBluetoothTransport（型固定 objc_msgSend、9.3 節）
#   r17 の再発防止策（ECG CSV BLE 転送 不具合と対応策レポート 5 章）に合わせる：
#    ・objc_util のメソッド呼出し・ns()・ObjCInstance は使わない（No.10・11）。objc_util は
#      デリゲートのクラス作成だけ（起動時にスクリプトのスレッドで 1 回）。
#    ・Objective-C はすべて生のポインタ（int）と型固定の _send で呼ぶ。自分で作ったものは
#      release、iOS から受け取って持つものは retain して、閉じるまで解放しない（No.12）。
#    ・BLE の操作は作業スレッド（SessionManager と同じスレッド）で行い、iOS からの通知
#      （デリゲート）は値を取り出して post で渡すだけ（No.14）。
#    ・CoreBluetooth を呼ぶ直前に動作記録へ 1 行書く（fsync）。落ちたら最後の行が落ちた場所（No.9）。
# =============================================================================
CB_STATES = ['unknown', 'resetting', 'unsupported', 'unauthorized', 'poweredOff', 'poweredOn']
_CB_TRACE = None                       # CoreBluetooth 呼出しの記録先（Trace）

if ON_PYTHONISTA:
    from ctypes import c_void_p, c_bool, c_longlong, c_ulonglong, c_double, c_int, c_char_p

    # 型記号：@ オブジェクト、P 生のアドレス、* C 文字列、B BOOL、q/Q 整数、d 実数、v なし
    _CT = {'@': c_void_p, 'P': c_void_p, '*': c_char_p, 'B': c_bool, 'q': c_longlong, 'Q': c_ulonglong,
           'd': c_double, 'i': c_int, 'v': None}
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
                raise BleError('Objective-C class not found: %s' % name)
            _CLASSES[name] = k
        return k

    def _ptr(o):
        """create_objc_class の戻り値（ObjCClass）からポインタを取り出す（起動時だけ）"""
        if o is None or isinstance(o, int):
            return o
        p = getattr(o, 'ptr', o)
        return p.value if isinstance(p, c_void_p) else p

    def _send(obj, selname, sig, *args):
        """型を固定した objc_msgSend（sig：戻り値＋引数の型記号）。obj・引数は生のポインタ（int）"""
        f = _MSG.get(sig)
        if f is None:
            f = ctypes.CFUNCTYPE(_CT[sig[0]], c_void_p, c_void_p, *[_CT[x] for x in sig[1:]])(('objc_msgSend', c))
            _MSG[sig] = f
        return f(obj, _sel(selname), *args)

    def _cbcall(what, obj, selname, sig, *args):
        """CoreBluetooth の操作：呼ぶ前に記録（fsync）してから呼ぶ"""
        if _CB_TRACE is not None:
            _CB_TRACE.w('CB>', what or selname)
        return _send(obj, selname, sig, *args)

    def _retain(p):
        if p:
            _send(p, 'retain', '@')
        return p

    def _release(p):
        if p:
            _send(p, 'release', 'v')

    def _new(cls_name, init, sig, *args):
        """alloc＋init（+1 で返る。使い終わったら _release）"""
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
        if not n:
            return b''
        return ctypes.string_at(_send(p, 'bytes', 'P'), n)

    def _new_array(ptrs):
        arr = (c_void_p * max(1, len(ptrs)))(*ptrs)
        return _new('NSArray', 'initWithObjects:count:', '@PQ', ctypes.addressof(arr), len(ptrs))

    def _new_dict(pairs):
        keys = (c_void_p * max(1, len(pairs)))(*[k for k, _ in pairs])
        vals = (c_void_p * max(1, len(pairs)))(*[v for _, v in pairs])
        return _new('NSDictionary', 'initWithObjects:forKeys:count:', '@PPQ',
                    ctypes.addressof(vals), ctypes.addressof(keys), len(pairs))

    def _new_bool(v):
        return _new('NSNumber', 'initWithBool:', '@B', bool(v))

    _CONST = {}

    def _const_str(s):
        """辞書のキーなどに使う NSString（作って保持し続ける）"""
        p = _CONST.get(s)
        if p is None:
            p = _CONST[s] = _new_str(s)
        return p

    _UUIDS = {}

    def _cbuuid(s):
        """CBUUID（作って保持し続ける）"""
        p = _UUIDS.get(s)
        if p is None:
            p = _retain(_send(_cls('CBUUID'), 'UUIDWithString:', '@@', _const_str(s)))
            _UUIDS[s] = p
        return p

    def _uuid_str(cbuuid_p):
        return _str(_send(cbuuid_p, 'UUIDString', '@')).upper()

    def _err_tuple(p):
        if not p:
            return None
        dom = _str(_send(p, 'domain', '@'))
        code = int(_send(p, 'code', 'q'))
        name = CB_ERRORS.get(code, '') if dom == 'CBErrorDomain' else _str(_send(p, 'localizedDescription', '@'))
        return (dom, code, name)

    _ACTIVE = None                       # 現在のトランスポート（デリゲートから参照）

    def _act():
        return _ACTIVE

    # ---- デリゲート（モジュール直下の関数。名前＝セレクタ。引数は生のポインタ）
    def centralManagerDidUpdateState_(_self, _cmd, central):
        t = _act()
        if t:
            t._d_state(central)

    def centralManager_didDiscoverPeripheral_advertisementData_RSSI_(_self, _cmd, central, periph, adv, rssi):
        t = _act()
        if t:
            t._d_discover(periph, adv, rssi)

    def centralManager_didConnectPeripheral_(_self, _cmd, central, periph):
        t = _act()
        if t:
            t._d_connect(periph)

    def centralManager_didFailToConnectPeripheral_error_(_self, _cmd, central, periph, err):
        t = _act()
        if t:
            t._d_fail(periph, err)

    def centralManager_didDisconnectPeripheral_error_(_self, _cmd, central, periph, err):
        t = _act()
        if t:
            t._d_disconnect(periph, err)

    def peripheral_didDiscoverServices_(_self, _cmd, periph, err):
        t = _act()
        if t:
            t._d_services(periph, err)

    def peripheral_didDiscoverCharacteristicsForService_error_(_self, _cmd, periph, svc, err):
        t = _act()
        if t:
            t._d_chars(periph, svc, err)

    def peripheral_didUpdateNotificationStateForCharacteristic_error_(_self, _cmd, periph, ch, err):
        t = _act()
        if t:
            t._d_notify_state(ch, err)

    def peripheral_didUpdateValueForCharacteristic_error_(_self, _cmd, periph, ch, err):
        t = _act()
        if t:
            t._d_value(ch, err)

    def peripheral_didWriteValueForCharacteristic_error_(_self, _cmd, periph, ch, err):
        t = _act()
        if t:
            t._d_write(ch, err)

    def peripheral_didModifyServices_(_self, _cmd, periph, invalidated):
        t = _act()
        if t:
            t._d_modify(invalidated)

    def peripheral_didReadRSSI_error_(_self, _cmd, periph, rssi, err):
        t = _act()
        if t:
            t._d_rssi(rssi, err)

    _ENC = {
        centralManagerDidUpdateState_: 'v@:@',
        centralManager_didDiscoverPeripheral_advertisementData_RSSI_: 'v@:@@@@',
        centralManager_didConnectPeripheral_: 'v@:@@',
        centralManager_didFailToConnectPeripheral_error_: 'v@:@@@',
        centralManager_didDisconnectPeripheral_error_: 'v@:@@@',
        peripheral_didDiscoverServices_: 'v@:@@',
        peripheral_didDiscoverCharacteristicsForService_error_: 'v@:@@@',
        peripheral_didUpdateNotificationStateForCharacteristic_error_: 'v@:@@@',
        peripheral_didUpdateValueForCharacteristic_error_: 'v@:@@@',
        peripheral_didWriteValueForCharacteristic_error_: 'v@:@@@',
        peripheral_didModifyServices_: 'v@:@@',
        peripheral_didReadRSSI_error_: 'v@:@@@',
    }
    for _f, _e in _ENC.items():
        _f.encoding = _e

    _DELEGATE_CLASS = None

    def _delegate_class():
        """8 章 5：クラス名に版と時刻の接尾辞を付け、再実行時の二重登録を避ける（A01-03）。
        objc_util を使うのはここだけ。起動時にスクリプトのスレッドで 1 回だけ呼ぶ。"""
        global _DELEGATE_CLASS
        if _DELEGATE_CLASS is None:
            suffix = re.sub(r'\W', '', APP_VERSION) + '_%x' % int(time.time() * 1000)
            cls = create_objc_class('EcgPatchCentral_' + suffix, ObjCClass('NSObject'), methods=list(_ENC.keys()),
                                    protocols=['CBCentralManagerDelegate', 'CBPeripheralDelegate'])
            _DELEGATE_CLASS = (cls, _ptr(cls))      # Python 側の参照も持ち続ける
        return _DELEGATE_CLASS[1]

    class CoreBluetoothTransport(BleTransport):
        """BLE の操作は呼び出したスレッド（作業スレッド）でそのまま実行する。
        デリゲート（メインスレッド）は値を取り出して post で作業スレッドへ渡すだけ。"""

        def __init__(self, trace):
            super().__init__()
            global _CB_TRACE
            self.trace = trace
            _CB_TRACE = trace
            self.cm = None
            self.delegate = None
            self.periphs = {}               # peer → CBPeripheral のポインタ（retain、閉じるまで保持）
            self.adv_peer_id = {}
            self.p = None                   # 接続中のペリフェラル
            self.peer = None
            self.chars = {}                 # UUID → CBCharacteristic のポインタ（retain）
            self.kept = []                  # retain したもの（閉じるまで解放しない）
            self.subs = {}
            self.sub_state = {}
            self.reads = collections.defaultdict(list)
            self.writes = collections.defaultdict(list)
            self.on_state = None
            self.on_adv = None
            self.cbs = {}
            self.svc_pending = 0
            self.on_done = None
            self._state = 'unknown'
            self._wlen = 20
            self.scanning = False

        # ---- 操作（作業スレッド）
        def start(self, on_state):
            global _ACTIVE
            self.on_state = on_state
            cls = _delegate_class()
            self.trace.w('BLE-1', 'delegate alloc')
            self.delegate = _send(_send(cls, 'alloc', '@'), 'init', '@')
            _ACTIVE = self
            self.trace.w('BLE-2', 'CBCentralManager alloc/init (queue=main)')
            self.cm = _cbcall('CBCentralManager init', _send(_cls('CBCentralManager'), 'alloc', '@'),
                              'initWithDelegate:queue:options:', '@@@@', self.delegate, None, None)
            self.trace.w('BLE-3', 'CBCentralManager created, waiting state')
            self._emit('op', 'create_central')

        def scan_start(self, service_uuid, on_adv):
            self.on_adv = on_adv
            if self.cm is None:
                return
            key = _const_str('kCBScanOptionAllowDuplicates')
            yes = _new_bool(True)
            opts = _new_dict([(key, yes)])
            uuids = _new_array([_cbuuid(service_uuid)])
            _cbcall('scan', self.cm, 'scanForPeripheralsWithServices:options:', 'v@@', uuids, opts)
            for p in (uuids, opts, yes):
                _release(p)
            self.scanning = True
            self._emit('op', 'scan_start')

        def scan_stop(self):
            if self.cm is not None and self.scanning:
                _cbcall('stopScan', self.cm, 'stopScan', 'v')
                self.scanning = False
                self._emit('op', 'scan_stop')

        def connect(self, peer_id, on_connected, on_failed, on_disconnected, on_invalidated=None):
            self.cbs = {'connected': on_connected, 'failed': on_failed, 'disconnected': on_disconnected,
                        'invalidated': on_invalidated}
            p = self.periphs.get(peer_id)
            if p is None:
                self.post(on_failed, ('app', 0, 'unknown peripheral'))
                return
            self.p, self.peer = p, peer_id
            self.chars, self.subs, self.sub_state = {}, {}, {}
            self.reads.clear()
            self.writes.clear()
            _cbcall('setDelegate', p, 'setDelegate:', 'v@', self.delegate)
            _cbcall('connect', self.cm, 'connectPeripheral:options:', 'v@@', p, None)

        def discover(self, services, on_done):
            self.on_done = on_done
            if self.p is None:
                return
            arr = _new_array([_cbuuid(s) for s in services])
            self._emit('op', 'discoverServices')
            _cbcall('discoverServices', self.p, 'discoverServices:', 'v@', arr)
            _release(arr)

        def subscribe(self, char_uuid, on_value, on_state=None):
            self.subs[char_uuid] = on_value
            if on_state:
                self.sub_state[char_uuid] = on_state
            ch = self.chars.get(char_uuid)
            if self.p is None or ch is None:
                return
            _cbcall('setNotify ' + CH_SHORT.get(char_uuid, char_uuid), self.p,
                    'setNotifyValue:forCharacteristic:', 'vB@', True, ch)
            self._emit('op', 'setNotify', ch=CH_SHORT.get(char_uuid, char_uuid))

        def read(self, char_uuid, on_value):
            ch = self.chars.get(char_uuid)
            if self.p is None or ch is None:
                return
            self.reads[char_uuid].append(on_value)
            _cbcall('read ' + CH_SHORT.get(char_uuid, char_uuid), self.p, 'readValueForCharacteristic:', 'v@', ch)
            self._emit('op', 'read', ch=CH_SHORT.get(char_uuid, char_uuid))

        def write(self, char_uuid, data, on_done):
            ch = self.chars.get(char_uuid)
            if self.p is None or ch is None:
                self.post(on_done, ('app', 0, 'not connected'))
                return
            self.writes[char_uuid].append(on_done)
            d = _new_data(data)
            _cbcall('write ' + CH_SHORT.get(char_uuid, char_uuid), self.p,
                    'writeValue:forCharacteristic:type:', 'v@@q', d, ch, 0)
            _release(d)
            self._emit('tx', 'write', peer=self._dev(), ch=CH_SHORT.get(char_uuid, char_uuid), data=data)

        def disconnect(self):
            if self.cm is not None and self.p is not None:
                _cbcall('cancelPeripheralConnection', self.cm, 'cancelPeripheralConnection:', 'v@', self.p)
                self._emit('op', 'cancelPeripheralConnection')
            elif self.cbs.get('disconnected'):
                self.post(self.cbs['disconnected'], None)

        def max_write_len(self):
            return self._wlen

        def read_rssi(self):
            if self.p is not None:
                _send(self.p, 'readRSSI', 'v')

        def central_state(self):
            return self._state

        def shutdown(self):
            """作業スレッドから呼ぶ。デリゲートを外すだけで、保持したものは解放しない（No.12）"""
            global _ACTIVE
            try:
                if self.cm is not None:
                    if self.scanning:
                        _cbcall('stopScan', self.cm, 'stopScan', 'v')
                    if self.p is not None:
                        _cbcall('cancelPeripheralConnection', self.cm, 'cancelPeripheralConnection:', 'v@', self.p)
                    _send(self.cm, 'setDelegate:', 'v@', None)
                if self.p is not None:
                    _send(self.p, 'setDelegate:', 'v@', None)
            except Exception as e:
                self.trace.w('CB', 'shutdown error', e)
            _ACTIVE = None

        def _dev(self):
            return self.adv_peer_id.get(self.peer, '')

        def _keep(self, p):
            self.kept.append(_retain(p))
            return p

        # ---- デリゲート（メインスレッド）：値を取り出して渡すだけ
        @guard
        def _d_state(self, central):
            st = int(_send(central, 'state', 'q'))
            self._state = CB_STATES[st] if 0 <= st < len(CB_STATES) else str(st)
            self.trace.w('CB<', 'state', self._state)
            self._emit('cb', 'state', state=self._state)
            if self._state != 'poweredOn':
                self.scanning = False
            self.post(self.on_state, self._state)

        @guard
        def _d_discover(self, periph, adv, rssi):
            pid = _str(_send(_send(periph, 'identifier', '@'), 'UUIDString', '@'))
            if pid not in self.periphs:
                self.periphs[pid] = self._keep(periph)
            mfr_p = _send(adv, 'objectForKey:', '@@', _const_str('kCBAdvDataManufacturerData'))
            name_p = _send(adv, 'objectForKey:', '@@', _const_str('kCBAdvDataLocalName'))
            d = {'mfr': _data_bytes(mfr_p) if mfr_p else None, 'name': _str(name_p) if name_p else None}
            r = int(_send(rssi, 'longLongValue', 'q')) if rssi else 127
            if d['mfr'] and len(d['mfr']) >= 6:
                self.adv_peer_id[pid] = '%08X' % struct.unpack_from('<I', d['mfr'], 2)[0]
            elif d['name'] and d['name'].startswith('ECGP-'):
                self.adv_peer_id[pid] = d['name'][5:13].upper()
            if self.on_adv:
                self.post(self.on_adv, pid, d, r)

        @guard
        def _d_connect(self, periph):
            self.trace.w('CB<', 'didConnect')
            self._emit('cb', 'didConnect', peer=self._dev())
            self.post(self.cbs['connected'])

        @guard
        def _d_fail(self, periph, err):
            e = _err_tuple(err)
            self.trace.w('CB<', 'didFailToConnect', e)
            self._emit('cb', 'didFailToConnect', peer=self._dev(), err=e)
            self.p = None
            self.post(self.cbs['failed'], e)

        @guard
        def _d_disconnect(self, periph, err):
            e = _err_tuple(err)
            self.trace.w('CB<', 'didDisconnect', e)
            self.p = None
            self.chars = {}
            self.post(self.cbs['disconnected'], e)

        @guard
        def _d_services(self, periph, err):
            self.trace.w('CB<', 'didDiscoverServices')
            self._emit('cb', 'didDiscoverServices', peer=self._dev(), err=_err_tuple(err))
            svcs = _send(periph, 'services', '@')
            n = int(_send(svcs, 'count', 'Q')) if svcs else 0
            self.svc_pending = n
            if n == 0:
                self.post(self.on_done, set())
                return
            svc_list = [self._keep(_send(svcs, 'objectAtIndex:', '@Q', i)) for i in range(n)]
            self.post(self._discover_chars, periph, svc_list)     # 操作は作業スレッドで

        def _discover_chars(self, periph, svc_list):
            for s in svc_list:
                _cbcall('discoverCharacteristics', periph, 'discoverCharacteristics:forService:', 'v@@', None, s)

        @guard
        def _d_chars(self, periph, svc, err):
            chs = _send(svc, 'characteristics', '@')
            n = int(_send(chs, 'count', 'Q')) if chs else 0
            for i in range(n):
                ch = _send(chs, 'objectAtIndex:', '@Q', i)
                u = _uuid_str(_send(ch, 'UUID', '@'))
                if u not in self.chars:
                    self.chars[u] = self._keep(ch)
            self.svc_pending -= 1
            if self.svc_pending <= 0:
                self._wlen = int(_send(periph, 'maximumWriteValueLengthForType:', 'Qq', 1))
                self.trace.w('CB<', 'didDiscoverCharacteristics', len(self.chars))
                self._emit('cb', 'didDiscoverCharacteristics', peer=self._dev(), n=len(self.chars))
                self.post(self.on_done, set(self.chars))

        @guard
        def _d_notify_state(self, ch, err):
            u = _uuid_str(_send(ch, 'UUID', '@'))
            e = _err_tuple(err)
            self._emit('cb', 'didUpdateNotificationState', peer=self._dev(), ch=CH_SHORT.get(u, u), err=e,
                       notifying=bool(_send(ch, 'isNotifying', 'B')))
            cb = self.sub_state.pop(u, None)
            if cb:
                self.post(cb, u, e)

        @guard
        def _d_value(self, ch, err):
            u = _uuid_str(_send(ch, 'UUID', '@'))
            if err:
                self._emit('cb', 'didUpdateValue', peer=self._dev(), ch=CH_SHORT.get(u, u), err=_err_tuple(err))
                if self.reads.get(u):
                    self.reads[u].pop(0)
                return
            data = _data_bytes(_send(ch, 'value', '@'))
            if self.reads.get(u):
                cb = self.reads[u].pop(0)
                if u not in (CH_STATUS, CH_STREAM, CH_EVENTLOG):
                    self._emit('rx', 'read', peer=self._dev(), ch=CH_SHORT.get(u, u), data=data)
                self.post(cb, u, data)
            elif u in self.subs:
                self.post(self.subs[u], u, data)

        @guard
        def _d_write(self, ch, err):
            u = _uuid_str(_send(ch, 'UUID', '@'))
            e = _err_tuple(err)
            self._emit('cb', 'didWriteValue', peer=self._dev(), ch=CH_SHORT.get(u, u), err=e)
            if self.writes.get(u):
                self.post(self.writes[u].pop(0), e)

        @guard
        def _d_modify(self, invalidated):
            n = int(_send(invalidated, 'count', 'Q')) if invalidated else 0
            self.trace.w('CB<', 'didModifyServices', n)
            self._emit('cb', 'didModifyServices', peer=self._dev(), n=n)
            if n and self.cbs.get('invalidated'):
                self.post(self.cbs['invalidated'])

        @guard
        def _d_rssi(self, rssi, err):
            r = int(_send(rssi, 'longLongValue', 'q')) if rssi else 127
            self._emit('cb', 'rssi', peer=self._dev(), rssi=r)
            if _APP_MGR is not None:
                self.post(_APP_MGR.on_rssi, r)

_APP_MGR = None


# =============================================================================
# A14 描画計算（WaveGeometry、13.2〜13.4 節）
# =============================================================================
@dataclass
class Layout:
    width: float
    height: float
    header: tuple
    arr_band: tuple
    plot: tuple
    axis: tuple
    tilt: tuple
    panel: tuple


class WaveGeometry:
    PANEL_W = 220
    HEADER_H = 44
    ARR_H = 16
    AXIS_H = 20
    TILT_H = 40
    MARGIN = 8

    def layout(self, width, height, with_tilt=True, top=0, left=0):
        pw = self.PANEL_W
        x0 = left + self.MARGIN
        plot_w = width - pw - self.MARGIN * 2 - left
        y = top
        header = (left, y, width - left, self.HEADER_H)
        y += self.HEADER_H
        arr = (x0, y, plot_w, self.ARR_H)
        y += self.ARR_H
        tilt_h = self.TILT_H if with_tilt else 0
        plot_h = height - y - self.AXIS_H - tilt_h - 4
        plot = (x0, y, plot_w, plot_h)
        axis = (x0, y + plot_h, plot_w, self.AXIS_H)
        tilt = (x0, y + plot_h + self.AXIS_H, plot_w, tilt_h)
        panel = (width - pw, self.HEADER_H + top, pw, height - self.HEADER_H - top)
        return Layout(width, height, header, arr, plot, axis, tilt, panel)

    @staticmethod
    def scale(samples, exclude_ranges, plot_h):
        vals = [v for i, v in enumerate(samples) if not any(a <= i < b for a, b in exclude_ranges)] if exclude_ranges else list(samples)
        if not vals:
            vals = list(samples) or [0]
        s = sorted(vals)
        n = len(s)
        base = s[n // 2]
        p_lo = s[int(n * 0.005)]
        p_hi = s[min(n - 1, int(n * 0.995))]
        amp = max(p_hi - p_lo, 1)
        return base, (plot_h * 0.8) / amp

    @staticmethod
    def envelope(samples, plot, start=0, count=None, base=0, scale=1.0):
        """1pt ごとの最小・最大の縦線（間引き）。[(x, y_top, y_bottom)]"""
        x0, y0, w, h = plot
        n = len(samples) - start if count is None else count
        if n <= 0 or w <= 0:
            return []
        cy = y0 + h / 2
        cols = int(w)

        def yv(v):
            y = cy - (v - base) * scale
            return min(y0 + h, max(y0, y))
        out = []
        if n / max(cols, 1) <= 2:
            step = w / max(n - 1, 1)
            for i in range(n):
                y = yv(samples[start + i])
                out.append((x0 + i * step, y, y))
            return out
        for c_ in range(cols):
            a = start + int(c_ * n / cols)
            b = max(start + int((c_ + 1) * n / cols), a + 1)
            seg = samples[a:b]
            out.append((x0 + c_, yv(max(seg)), yv(min(seg))))
        return out

    @staticmethod
    def grid(plot, px_per_s):
        """細線 0.2 秒・太線 1 秒（縦横同じ間隔で正方形の方眼）"""
        x0, y0, w, h = plot
        out = []
        step = px_per_s * 0.2
        if step < 2:
            step = px_per_s
        i = 0
        x = x0
        while x <= x0 + w + 0.1:
            out.append(('v', x, i % 5 == 0))
            i += 1
            x = x0 + i * step
        i = 0
        cy = y0 + h / 2
        while i * step <= h / 2 + 0.1:
            for y in ((cy - i * step), (cy + i * step)) if i else (cy,):
                out.append(('h', y, i % 5 == 0))
            i += 1
        return out

    @staticmethod
    def bands(meta_rows):
        """背景帯：はがれ＞衝撃＞動き始め＞安静。連続はまとめる。[(k0, k1, kind)]"""
        out = []
        for k, r in enumerate(meta_rows or []):
            elec, acc = int(r[3]), int(r[4])
            kind = 'leadoff' if elec else ('shock' if acc == 2 else ('motion' if acc == 1 else None))
            if kind is None:
                continue
            if out and out[-1][2] == kind and out[-1][1] == k:
                out[-1] = (out[-1][0], k + 1, kind)
            else:
                out.append((k, k + 1, kind))
        return out

    @staticmethod
    def arr_bands(meta_rows):
        out = []
        for k, r in enumerate(meta_rows or []):
            if int(r[8]):
                if out and out[-1][1] == k:
                    out[-1] = (out[-1][0], k + 1)
                else:
                    out.append((k, k + 1))
        return out


# =============================================================================
# 画面遷移（ScreenRouter、5.10 節）・ファイル読込み（13.7 節）
# =============================================================================
class ScreenRouter:
    """画面遷移の状態機械（純粋）。sink(screen, data) に通知する。"""
    def __init__(self, sink=None):
        self.current = 'SC1'
        self.prev = 'SC2'
        self.sink = sink or (lambda s, d: None)
        self.ignored = []

    def go(self, sc, **d):
        if sc != self.current:
            if sc == 'SC7':
                self.prev = self.current
            self.current = sc
        self.sink(sc, d)

    def on_event(self, ev, **d):
        cur = self.current
        if ev == 'init_done' and cur == 'SC1':
            self.go('SC2')
        elif ev in ('patch_selected', 'file_selected', 'saved') and cur in ('SC2', 'SC3'):
            self.go('SC3', **d)
        elif ev == 'alert' and cur in ('SC2', 'SC3', 'SC4', 'SC6'):
            self.go('SC5', **d)
        elif ev == 'view_wave' and cur == 'SC5':
            self.go('SC3', **d)
        elif ev == 'back' and cur in ('SC3', 'SC5', 'SC6'):
            self.go('SC2')
        elif ev == 'settings' and cur in ('SC2', 'SC4'):
            self.go('SC6')
        elif ev == 'monitor' and cur in ('SC2', 'SC3', 'SC5', 'SC6'):
            self.go('SC7')
        elif ev == 'close' and cur == 'SC7':
            self.go(self.prev)
        elif ev == 'close' and cur == 'SC6':
            self.go('SC2')
        else:
            self.ignored.append((cur, ev))

    def on_orientation(self, o, has_data=True):
        if o == 'landscape' and self.current == 'SC3' and has_data:
            self.go('SC4')
        elif o == 'portrait' and self.current == 'SC4':
            self.go('SC3')


def load_ecg_file(path):
    """CSV（ヘッダ 2 行／4 行／なし）・JSON を読む。(samples, fs, meta_rows or None, info)"""
    info = {'name': os.path.basename(path), 'header': [], 'path': path}
    if path.lower().endswith('.json'):
        with open(path, encoding='utf-8') as f:
            d = json.load(f)
        if 'ecg_csv' in d:                         # _event.json → 同じフォルダの CSV
            x, fs, meta, info2 = load_ecg_file(os.path.join(os.path.dirname(path), d['ecg_csv']))
            info2['event'] = d
            return x, fs, meta, info2
        x = array('h', [int(v) for v in d.get('samples', [])])
        return x, int(d.get('fs', FS)), None, info
    with open(path, encoding='utf-8', errors='replace') as f:
        lines = [ln.strip() for ln in f.read().splitlines() if ln.strip()]
    hdr = []
    i = 0
    while i < len(lines) and i < 4:
        try:
            int(float(lines[i].split(',')[0]))
            if i == 1 and hdr and hdr[0].upper() == 'ECG':
                hdr.append(lines[i])
                i += 1
                continue
            break
        except ValueError:
            hdr.append(lines[i])
            i += 1
    fs = FS
    if hdr and hdr[0].upper() == 'ECG' and len(hdr) >= 2:
        try:
            fs = int(float(hdr[1]))
        except ValueError:
            pass
    x = array('h')
    for ln in lines[i:]:
        try:
            v = int(float(ln.split(',')[0]))
            x.append(max(-32768, min(32767, v)))
        except ValueError:
            pass
    info['header'] = hdr[:3]
    meta = None
    mp = path[:-4] + '_meta.csv' if path.lower().endswith('.csv') else None
    if mp and os.path.exists(mp):
        meta = load_meta(mp)
        info['meta'] = os.path.basename(mp)
    ej = path[:-4] + '_event.json' if path.lower().endswith('.csv') else None
    if ej and os.path.exists(ej):
        try:
            with open(ej, encoding='utf-8') as f:
                info['event'] = json.load(f)
        except Exception:
            pass
    return x, fs, meta, info


def load_meta(path):
    rows = []
    with open(path, encoding='utf-8') as f:
        for ln in f.read().splitlines()[1:]:
            p = ln.split(',')
            if len(p) >= 9:
                rows.append((int(p[0]), p[1], int(p[2]), int(p[3]), int(p[4]), int(p[5]), int(p[6]), int(p[7]), int(p[8])))
    return rows


@dataclass
class Loaded:
    name: str
    ecg: array
    fs: int
    meta: list
    info: dict
    analysis: dict = None
    detect_index: int = -1
    source: str = 'file'
    meta_name: str = ''


# =============================================================================
# A01 初期設定（AppInit）
# =============================================================================
class AppContext:
    pass


class AppInit:
    def __init__(self, env='device', transport=None, clock=None, root=None, settings_path=None):
        self.env = env
        self.transport = transport
        self.clock = clock
        self.root = root
        self.settings_path = settings_path

    def build(self):
        ctx = AppContext()
        if self.env == 'device':
            root = self.root or os.path.expanduser('~/Documents')
            ctx.clock = self.clock or SystemClock()
            ctx.trace = Trace(os.path.join(root, 'ecg_patch_trace.txt'), echo=False)
        else:
            import tempfile
            root = self.root or tempfile.mkdtemp(prefix='ecgapp_')
            ctx.clock = self.clock or FakeClock()
            ctx.trace = Trace(None)
        ctx.root = root
        ctx.store = DocumentsStore(root)
        ctx.settings = Settings(self.settings_path or os.path.join(root, 'ecg_patch_settings.json'), ctx.trace).load()
        ctx.trace.verbose = bool(ctx.settings['verbose_trace'])
        if self.transport is None:
            if not ON_PYTHONISTA:
                raise RuntimeError('CoreBluetoothTransport は Pythonista でのみ使えます')
            self.transport = CoreBluetoothTransport(ctx.trace)
        ctx.transport = self.transport
        ctx.monitor = BleMonitor(ctx.clock)
        ctx.recorder = BleTraceRecorder(ctx.store, ctx.clock, ctx.settings, threaded=(self.env == 'device'))
        ctx.transport.add_observer(ctx.monitor)
        ctx.transport.add_observer(ctx.recorder)
        ctx.mgr = SessionManager(ctx.transport, ctx.clock, ctx.store, ctx.settings, ctx.trace, ctx.monitor, ctx.recorder)
        ctx.store.makedirs(os.path.join('ECG', ctx.clock.local_datetime().strftime('%Y%m%d')))
        if ctx.settings['ble_trace']:
            ctx.recorder.start()
        return ctx


# =============================================================================
# A15 画面（Pythonista ui）・A20 SC7 BLE モニタ
# =============================================================================
if ON_PYTHONISTA:

    def _rgb(c_):
        return COL.get(c_, c_)

    def _text(s, x, y, w, h, size=13, color='ink', bold=False, align=None, font=None):
        ui.draw_string(str(s), rect=(x, y, w, h), font=(font or ('<system-bold>' if bold else '<system>'), size),
                       color=_rgb(color), alignment=align if align is not None else ui.ALIGN_LEFT,
                       line_break_mode=ui.LB_TRUNCATE_TAIL)

    def _rrect(x, y, w, h, r, fill=None, stroke=None, lw=1.0, dash=None):
        p = ui.Path.rounded_rect(x, y, w, h, r)
        if fill:
            ui.set_color(_rgb(fill))
            p.fill()
        if stroke:
            ui.set_color(_rgb(stroke))
            p.line_width = lw
            if dash:
                p.set_line_dash(dash)
            p.stroke()

    def _btn(title, action, bg='#E8EEF7', fg='#1F5FA8', font=14):
        b = ui.Button(title=title)
        b.action = action
        b.background_color = bg
        b.tint_color = fg
        b.corner_radius = 8
        b.font = ('<system-bold>', font)
        return b

    class StatusPanelView(ui.View):
        """下部の接続状態（13.9 節）。1 秒長押しで SC7。パッチ行の「接続」をタップで接続。"""
        def __init__(self, app):
            self.app = app
            self.vm = None
            self.row_rects = []
            self.t_down = None
            self.background_color = '#F8F9FA'
            self.border_color = '#DADCE0'
            self.border_width = 1
            self.corner_radius = 10
            self.long_fired = False

        def show(self, vm):
            self.vm = vm
            self.set_needs_display()

        def touch_began(self, touch):
            self.t_down = time.time()
            self.long_fired = False                 # 長押しの判定は画面の定期処理（update）で行う

        def long_check(self):
            if self.t_down is not None and time.time() - self.t_down >= 0.95 and not self.long_fired:
                self.long_fired = True
                self.app.open_monitor()

        def touch_ended(self, touch):
            held = time.time() - (self.t_down or time.time())
            self.t_down = None
            if self.long_fired or held >= 0.95:
                return
            x, y = touch.location
            for (rx, ry, rw, rh), peer, btn in self.row_rects:
                if rx <= x <= rx + rw and ry <= y <= ry + rh and btn == '接続':
                    self.app.on_connect_tap(peer)
                    return

        def draw(self):
            vm = self.vm
            if vm is None or not DIAG['draw']:
                return
            w = self.width
            x, y = 12, 8
            ui.set_color(_rgb({'gray': 'gray', 'blue': 'blue', 'red': 'red', 'green': 'green', 'orange': 'orange'}[vm.dot]))
            ui.Path.oval(x, y + 4, 10, 10).fill()
            _text(vm.head, x + 16, y, w - x - 28, 18, 14, vm.head_color if vm.head_color != 'ink' else 'ink', bold=True)
            y += 22
            if vm.percent is not None:
                _rrect(x, y, w - 24, 8, 4, fill='#E0E0E0')
                if vm.percent > 0:
                    _rrect(x, y, max(8, (w - 24) * vm.percent / 100.0), 8, 4, fill=vm.bar_color)
                y += 14
            _text(vm.detail, x, y, w - 24, 16, 12, 'gray')
            y += 18
            if self.app.emulator:
                _rrect(w - 70, 6, 58, 18, 5, fill='#7B3FB5')
                _text('模擬器', w - 70, 7, 58, 16, 11, 'white', bold=True, align=ui.ALIGN_CENTER)
            if vm.warn:
                _text(vm.warn, x, y, w - 24, 16, 12, 'red', bold=True)
                y += 18
            if vm.progress:
                _text(vm.progress, x, y, w - 24, 16, 12, 'ink')
                y += 18
            if vm.electrode_cells:
                cw = (w - 24) / 60.0
                for i, cc in enumerate(vm.electrode_cells):
                    ui.set_color('#BDBDBD' if cc is None else (COL['red'] if cc else COL['green']))
                    ui.fill_rect(x + i * cw, y, max(1, cw - 0.6), 10)
                y += 16
            self.row_rects = []
            for (name, kind, sub, btn, peer, color) in vm.patches[:3]:
                _rrect(x, y, w - 24, 38, 6, fill='white', stroke='#DADCE0')
                bw = 72 if btn else 0
                # r7：種別の幅をボタンの幅から決め、長い種別は字を小さくする（r6 は 92pt で「未登録パッ…」と切れた：試験 No.14）
                kw = (w - 24) - 124 - (bw + 12 if btn else 8)
                kfs = max(9, min(11, int(kw / max(1, len(kind)))))
                _text(name, x + 8, y + 3, 116, 16, 13, 'ink', bold=True)
                _text(kind, x + 124, y + 3, kw, 16, kfs, color, bold=True)
                _text(sub, x + 8, y + 20, w - 24 - 100, 14, 10, 'gray')
                if btn:
                    _rrect(x + w - 24 - bw - 6, y + 6, bw, 26, 6, fill=COL['blue'] if btn == '接続' else '#FCE8E6')
                    _text(btn, x + w - 24 - bw - 6, y + 11, bw, 16, 12, 'white' if btn == '接続' else 'red',
                          bold=True, align=ui.ALIGN_CENTER)
                self.row_rects.append(((x, y, w - 24, 38), peer, btn))
                y += 42
            y = max(y, self.height - 4 * 16 - 8)
            _text('受信の記録', x, y - 16, 120, 14, 10, 'gray')
            for ln in vm.log[-4:]:
                _text(ln, x, y, w - 24, 15, 11, 'ink')
                y += 16

    class PortraitView(ui.View):
        """SC2・SC3（r17 と同じ縦画面）と SC5（異常通知）"""
        def __init__(self, app):
            self.app = app
            self.background_color = 'white'
            self.title = ui.Label(text='ECG R-R ビューア', font=('<system-bold>', 18))
            self.add_subview(self.title)
            self.close_btn = _btn('×', app.on_close, bg='#F1F3F4', fg='#5F6368', font=20)
            self.add_subview(self.close_btn)
            self.buttons = [_btn('ファイル', app.on_file), _btn('ダミー', app.on_dummy),
                            _btn('ダミー（電極はがれ）', app.on_dummy_leadoff, font=12)]
            self.buttons2 = [_btn('共有', app.on_share), _btn('クリア', app.on_clear),
                             _btn('設定', app.on_settings), _btn('中止', app.on_stop, bg='#FCE8E6', fg='#D93025')]
            for b in self.buttons + self.buttons2:
                self.add_subview(b)
            self.sc5_buttons = [_btn('波形を見る', app.on_view_wave, bg='#D93025', fg='white'),
                                _btn('閉じる', app.on_sc5_close)]
            for b in self.sc5_buttons:
                self.add_subview(b)
            self.status = StatusPanelView(app)
            self.add_subview(self.status)
            self.areas = {}

        def layout(self):
            w, h = self.width, self.height
            top = 44
            m = 12
            self.title.frame = (m, top, w - 80, 34)
            self.close_btn.frame = (w - 50, top + 2, 38, 30)
            y = top + 34
            sc5 = self.app.screen == 'SC5'
            if sc5:
                self.areas['alert'] = (m, y + 4, w - 2 * m, 330)
                y += 4 + 330
                bw = (w - 3 * m) / 2
                for i, b in enumerate(self.sc5_buttons):
                    b.frame = (m + i * (bw + m), y - 46, bw, 36)
                    b.hidden = False
                for b in self.buttons + self.buttons2:
                    b.hidden = True
            else:
                for b in self.sc5_buttons:
                    b.hidden = True
                self.areas['data'] = (m, y + 4, w - 2 * m, 124)
                y += 4 + 124
                self.areas['chips'] = (m, y + 6, w - 2 * m, 52)
                y += 58
                bw = (w - 4 * m) / 3
                for i, b in enumerate(self.buttons):
                    b.frame = (m + i * (bw + m), y + 4, bw, 34)
                    b.hidden = False
                y += 40
                bw2 = (w - 5 * m) / 4
                for i, b in enumerate(self.buttons2):
                    b.frame = (m + i * (bw2 + m), y + 2, bw2, 30)
                    b.hidden = False
                self.buttons2[3].hidden = not self.app.in_session
                y += 34
                self.areas['hint'] = (m, y + 4, w - 2 * m, 104)
                y += 108
            self.status.frame = (m, y + 4, w - 2 * m, max(220, h - y - 24))

        def draw(self):
            app = self.app
            if not DIAG['draw']:
                return
            if app.screen == 'SC5':
                self.draw_alert(*self.areas.get('alert', (0, 0, 0, 0)))
                return
            x, y, w, h = self.areas.get('data', (0, 0, 0, 0))
            rec = app.loaded
            if rec is None:
                _rrect(x, y, w, h, 10, fill='#F1F3F4', stroke='#DADCE0')
                _text('データ未検出', x + 14, y + 12, w - 28, 22, 18, 'gray', bold=True)
                _text('パッチから受信するか、「ファイル」「ダミー」で読み込んでください', x + 14, y + 42, w - 28, 18, 12, 'gray')
                if app.snap and app.snap.get('saved'):
                    s0 = app.snap['saved'][0]
                    _text('最新の保存: %s' % s0['name'], x + 14, y + 66, w - 28, 16, 11, 'ink')
            else:
                fill = '#E6F4EA' if not rec.info.get('event') else '#FCE8E6'
                _rrect(x, y, w, h, 10, fill=fill, stroke=COL['green'] if not rec.info.get('event') else COL['red'])
                _text('データ検出' if not rec.info.get('event') else '● データ検出（異常通知）', x + 14, y + 8, w - 28, 22, 17,
                      'green' if not rec.info.get('event') else 'red', bold=True)
                _text('ECG データファイル: ' + rec.name, x + 14, y + 34, w - 28, 16, 12, 'ink')
                _text('メタファイル: ' + (rec.meta_name or 'なし'), x + 14, y + 52, w - 28, 16, 12, 'ink')
                n = len(rec.ecg)
                _text('%.1f 秒・%d 点・%s Hz' % (n / float(rec.fs), n, rec.fs), x + 14, y + 70, w - 28, 16, 12, 'gray')
                if rec.info.get('dir'):
                    _text('保存場所: ' + rec.info['dir'].replace(os.path.expanduser('~/Documents'), 'Pythonista 3'),
                          x + 14, y + 88, w - 28, 16, 11, 'gray')
                elif rec.info.get('header'):
                    _text('ヘッダ: ' + ' / '.join(rec.info['header'][:3]), x + 14, y + 88, w - 28, 16, 11, 'gray')
            # チップ 3 個
            x, y, w, h = self.areas.get('chips', (0, 0, 0, 0))
            cw = (w - 16) / 3
            chips = app.chips()
            for i, (val, label, col) in enumerate(chips):
                cx = x + i * (cw + 8)
                _rrect(cx, y, cw, h, 8, fill='white', stroke='#DADCE0')
                _text(label, cx + 8, y + 4, cw - 16, 14, 10, 'gray')
                _text(val, cx + 8, y + 20, cw - 16, 24, 16, col, bold=True)
            # 横向きの案内／受信中のプレビュー
            x, y, w, h = self.areas.get('hint', (0, 0, 0, 0))
            snap = app.snap or {}
            if snap.get('preview') is not None and app.in_session:
                self.draw_preview(snap['preview'], x, y, w, h)
            elif rec is not None:
                _rrect(x, y, w, h, 10, fill='#E8F0FE', stroke=COL['blue'], lw=1.5)
                _text('📱  iPhone を横向きにしてください', x, y + h / 2 - 12, w, 24, 17, 'blue', bold=True, align=ui.ALIGN_CENTER)
            else:
                _rrect(x, y, w, h, 10, stroke='#BDBDBD', dash=[4, 3])
                _text('データを読み込むと表示できます', x, y + h / 2 - 10, w, 20, 13, 'gray', align=ui.ALIGN_CENTER)

        def draw_preview(self, samples, x, y, w, h):
            ui.set_color(COL['paper'])
            ui.fill_rect(x, y, w, h)
            geo = WaveGeometry()
            plot = (x + 2, y + 2, w - 4, h - 4)
            pps = (w - 4) / 8.0
            for kind, pos, thick in geo.grid(plot, pps):
                if thick:
                    continue
                ui.set_color(COL['grid1'])
                if kind == 'v':
                    ui.fill_rect(pos, y + 2, 0.5, h - 4)
                else:
                    ui.fill_rect(x + 2, pos, w - 4, 0.5)
            if len(samples) < 10:
                return
            base, sc = geo.scale(samples, [], h - 4)
            pts = geo.envelope(samples, plot, base=base, scale=sc)
            path = ui.Path()
            path.line_width = 1.0
            for i, (px, y1, y2) in enumerate(pts):
                if i == 0:
                    path.move_to(px, y1)
                else:
                    path.line_to(px, y1)
                if y2 != y1:
                    path.line_to(px, y2)
            ui.set_color(COL['ink'])
            path.stroke()
            _text('受信中のプレビュー（直近 8 秒）', x + 6, y + 4, w, 14, 10, 'gray')

        def draw_alert(self, x, y, w, h):
            a = (self.app.snap or {}).get('alert') or {}
            _rrect(x, y, w, h - 52, 12, fill='#FCE8E6', stroke=COL['red'], lw=2)
            _text('● 異常通知', x + 14, y + 10, 160, 22, 16, 'red', bold=True)
            _text(a.get('counter', ''), x + w - 180, y + 12, 166, 18, 12, 'red', align=ui.ALIGN_RIGHT)
            _text(a.get('title', ''), x + 14, y + 36, w - 28, 26, 19, 'ink', bold=True)
            _text('%s   検知 %s' % (a.get('device', ''), a.get('time', '')), x + 14, y + 64, w - 28, 16, 12, 'gray')
            cy = y + 86
            chips = a.get('chips') or []
            cw = (w - 28 - 16) / 3
            for i, (val, label) in enumerate(chips[:3]):
                cx = x + 14 + i * (cw + 8)
                _rrect(cx, cy, cw, 46, 8, fill='white', stroke='#F4B4AE')
                _text(label, cx + 6, cy + 3, cw - 12, 13, 10, 'gray')
                _text(val, cx + 6, cy + 18, cw - 12, 22, 15, 'red', bold=True)
            sy = cy + 58
            marks = {'done': ('✓', 'green'), 'active': ('●', 'blue'), 'todo': ('○', 'gray'),
                     'warn': ('!', 'orange'), 'error': ('×', 'red')}
            for i, (st, txt) in enumerate(a.get('steps') or []):
                mk, col = marks.get(st, ('○', 'gray'))
                _text('%s' % mk, x + 16, sy, 20, 20, 15, col, bold=True)
                _text('%s %s' % ('①②③④⑤'[i], txt), x + 38, sy + 1, w - 56, 18, 13,
                      col if st in ('warn', 'error') else 'ink', bold=(st == 'active'))
                sy += 24
            if a.get('error'):
                _text(a['error'], x + 14, sy + 2, w - 28, 18, 12, 'orange', bold=True)
            self.sc5_buttons[0].enabled = bool(a.get('can_view'))
            self.sc5_buttons[0].alpha = 1.0 if a.get('can_view') else 0.4

    class WaveView(ui.View):
        """SC4 横向きビューア（モード A：ECG のみ／B：状態ミックス）"""
        def __init__(self, app):
            self.app = app
            self.background_color = 'white'
            self.mode = 'ecg_only'
            self.geo = WaveGeometry()
            self.btn_mode = _btn('状態表示', self.toggle_mode, font=12)
            self.btn_share = _btn('共有', app.on_share, font=12)
            for b in (self.btn_mode, self.btn_share):
                self.add_subview(b)

        def toggle_mode(self, sender):
            rec = self.app.loaded
            if rec is None or not rec.meta:
                console.hud_alert('メタファイルがないため状態表示はできません', 'error', 1.2)
                return
            self.mode = 'mixed' if self.mode == 'ecg_only' else 'ecg_only'
            self.set_needs_display()

        def layout(self):
            w = self.width
            self.btn_share.frame = (w - 74, 6, 64, 30)
            self.btn_mode.frame = (w - 160, 6, 80, 30)
            self.btn_mode.enabled = bool(self.app.loaded and self.app.loaded.meta)
            self.btn_mode.alpha = 1.0 if self.btn_mode.enabled else 0.4

        def draw(self):
            rec = self.app.loaded
            if not DIAG['draw']:
                return
            if rec is None:
                _text('データがありません', 0, self.height / 2 - 10, self.width, 20, 15, 'gray', align=ui.ALIGN_CENTER)
                return
            left = 44 if self.width > 800 else 8
            has_tilt = any(r[5] != TILT_NONE for r in (rec.meta or []))       # r10：傾きは記録しない（0x8000）ので帯を出さない
            lay = self.geo.layout(self.width, self.height - 8, with_tilt=has_tilt, left=left)
            an = rec.analysis or {}
            # ヘッダ
            hx, hy, hw, hh = lay.header
            badges = [('データ検出', 'green')]
            badges.append(('電極 はがれ有', 'red') if an.get('leadoff') else ('電極 正常', 'green'))
            bx = hx + 4
            for t, col in badges:
                _rrect(bx, hy + 10, 92, 24, 6, fill=col)
                _text(t, bx, hy + 14, 92, 16, 12, 'white', bold=True, align=ui.ALIGN_CENTER)
                bx += 98
            n = len(rec.ecg)
            _text('%s  %.1f 秒' % (rec.name, n / float(rec.fs)), bx + 4, hy + 12, self.width - bx - 180, 20, 13, 'ink', bold=True)
            # 波形エリア
            px, py, pw, ph = lay.plot
            secs = n / float(rec.fs)
            pps = pw / secs if secs else 1
            ui.set_color(COL['paper'])
            ui.fill_rect(px, py, pw, ph)
            if self.mode == 'mixed' and rec.meta:
                for k0, k1, kind in self.geo.bands(rec.meta):
                    x0 = px + k0 * 0.5 * pps
                    x1 = px + k1 * 0.5 * pps
                    if kind == 'leadoff':
                        ui.set_color((0.74, 0.74, 0.74, 0.5))
                    else:
                        ui.set_color(COL[kind])
                    ui.fill_rect(x0, py, x1 - x0, ph)
                    _text({'leadoff': '電極はがれ', 'shock': '急激な衝撃', 'motion': '動き始め'}[kind],
                          x0 + 2, py + 2, max(10, x1 - x0), 14, 10, 'ink')
            for kind, pos, thick in self.geo.grid(lay.plot, pps):
                ui.set_color(COL['grid2'] if thick else COL['grid1'])
                lw = 1.0 if thick else 0.5
                if kind == 'v':
                    ui.fill_rect(pos, py, lw, ph)
                else:
                    ui.fill_rect(px, pos, pw, lw)
            if self.mode == 'ecg_only' or not rec.meta:
                for a, b in an.get('leadoff', []):
                    x0 = px + a / float(rec.fs) * pps
                    x1 = px + b / float(rec.fs) * pps
                    ui.set_color((0.74, 0.74, 0.74, 0.5))
                    ui.fill_rect(x0, py, x1 - x0, ph)
                    _text('電極はがれ', x0, py + ph / 2 - 8, x1 - x0, 16, 11, 'ink', align=ui.ALIGN_CENTER)
            base, sc = self.geo.scale(rec.ecg, an.get('leadoff', []), ph)
            pts = self.geo.envelope(rec.ecg, lay.plot, base=base, scale=sc)
            path = ui.Path()
            path.line_width = 1.0
            for i, (x_, y1, y2) in enumerate(pts):
                if i == 0:
                    path.move_to(x_, y1)
                else:
                    path.line_to(x_, y1)
                if y2 != y1:
                    path.line_to(x_, y2)
            ui.set_color(COL['ink'])
            path.stroke()
            # 不整脈帯（上端 16pt）
            ax, ay, aw, ah = lay.arr_band
            if rec.meta:
                for k0, k1 in self.geo.arr_bands(rec.meta):
                    ui.set_color(COL['red'])
                    ui.fill_rect(ax + k0 * 0.5 * pps, ay + 2, (k1 - k0) * 0.5 * pps, ah - 4)
            # 検知位置（異常通知）
            if rec.detect_index >= 0:
                dx = px + rec.detect_index / float(rec.fs) * pps
                p = ui.Path()
                p.move_to(dx, py)
                p.line_to(dx, py + ph)
                p.set_line_dash([4, 3])
                p.line_width = 1.5
                ui.set_color(COL['blue'])
                p.stroke()
            # 時間軸
            tx, ty, tw, th = lay.axis
            step = 1 if secs <= 10 else (2 if secs <= 30 else 5)
            s = 0
            while s <= secs + 0.01:
                _text('%ds' % s, tx + s * pps - 10, ty + 2, 30, 14, 10, 'gray')
                s += step
            # 傾き
            if has_tilt and lay.tilt[3] > 0:
                gx, gy, gw, gh = lay.tilt
                for j, col in ((5, COL['red']), (6, COL['green']), (7, COL['blue'])):
                    vals = [r[j] for r in rec.meta]
                    lo_, hi_ = min(vals), max(vals)
                    rng = max(hi_ - lo_, 1)
                    p = ui.Path()
                    for k, v in enumerate(vals):
                        xx = gx + (k + 0.5) * 0.5 * pps
                        yy = gy + gh - 4 - (v - lo_) / rng * (gh - 8)
                        if k == 0:
                            p.move_to(xx, yy)
                        else:
                            p.line_to(xx, yy)
                    ui.set_color(col)
                    p.line_width = 1
                    p.stroke()
            # 右パネル
            qx, qy, qw, qh = lay.panel
            ui.set_color('#F8F9FA')
            ui.fill_rect(qx, qy, qw, qh)
            yy = qy + 6
            # r10（本体 No.29）：アプリは判定しない。異常通知はパッチの判定（Event Log）を表示する
            _text('パッチの判定', qx + 8, yy, qw - 16, 14, 10, 'gray')
            yy += 16
            col = 'red' if rec.info.get('event') else 'gray'
            _text(an.get('verdict', '—'), qx + 8, yy, qw - 16, 20, 14, col, bold=True)
            yy += 24
            _text('心拍 %s/分（パッチ）' % (an.get('hr') or '—'), qx + 8, yy, qw - 16, 16, 12)
            yy += 20
            for t in (an.get('abnormal') or [])[:4]:
                _text('• ' + t, qx + 8, yy, qw - 16, 15, 11, 'red')
                yy += 16

    class MonitorView(ui.View):
        """SC7 BLE モニタ（概要・スキャン・接続・通信量・ログ）"""
        TABS = ['概要', 'スキャン', '接続', '通信量', 'ログ']

        def __init__(self, app):
            self.app = app
            self.background_color = 'white'
            self.seg = ui.SegmentedControl(segments=self.TABS)
            self.seg.selected_index = 0
            self.seg.action = lambda s: self.refresh()
            self.add_subview(self.seg)
            self.filter = ui.SegmentedControl(segments=['すべて', 'CB', '送受信', '警告'])
            self.filter.selected_index = 0
            self.filter.action = lambda s: self.refresh()
            self.add_subview(self.filter)
            self.tv = ui.TextView(editable=False, font=('Menlo', 10))
            self.add_subview(self.tv)
            self.btns = [_btn('記録 停止', self.toggle_rec, font=12), _btn('書出し', self.export, font=12),
                         _btn('クリア', self.clear, font=12), _btn('閉じる', lambda s: app.close_monitor(), font=12)]
            for b in self.btns:
                self.add_subview(b)
            self.paused = False

        def layout(self):
            w, h = self.width, self.height
            top = 44
            self.seg.frame = (8, top, w - 16, 30)
            self.filter.frame = (8, top + 36, w - 16, 28)
            self.tv.frame = (8, top + 70, w - 16, h - top - 70 - 52)
            bw = (w - 40) / 4
            for i, b in enumerate(self.btns):
                b.frame = (8 + i * (bw + 8), h - 46, bw, 34)

        def toggle_rec(self, sender):
            r = self.app.ctx.recorder
            if r.enabled:
                r.stop()
                sender.title = '記録 開始'
            else:
                r.start()
                sender.title = '記録 停止'

        @in_thread
        def export(self, sender):
            files = self.app.ctx.recorder.export()
            if not files:
                console.hud_alert('記録がありません', 'error', 1.0)
                return
            console.open_in(files[0])

        def clear(self, sender):
            self.app.ctx.monitor.reset_counters()
            self.app.ctx.recorder.ring.clear()
            self.refresh()

        @staticmethod
        def spark(vals, lo=None, hi=None):
            if not vals:
                return '（データなし）'
            bars = '▁▂▃▄▅▆▇█'
            lo = min(vals) if lo is None else lo
            hi = max(vals) if hi is None else hi
            rng = max(hi - lo, 1e-9)
            return ''.join(bars[min(7, max(0, int((v - lo) / rng * 7.99)))] for v in vals[-60:])

        def refresh(self):
            ctx = self.app.ctx
            m = ctx.monitor
            snap = m.snapshot()
            tab = self.seg.selected_index
            self.filter.hidden = tab != 4
            L = []
            mgr = ctx.mgr
            if tab == 0:
                L.append('— セントラル —')
                L.append('Bluetooth   : %s' % snap['central_state'])
                L.append('アプリ状態  : %s' % mgr.state)
                L.append('相手        : %s%s' % (mgr.conn.info.label if mgr.conn else '—',
                                               '（模擬器）' if mgr.conn and mgr.conn.info.emulator else ''))
                L.append('— リンク —')
                L.append('接続所要    : %s ms' % (snap['connect_ms'] if snap['connect_ms'] is not None else '—'))
                L.append('ATT MTU     : %s' % (snap['mtu'] or '—'))
                L.append('RSSI(iPhone): %s dBm' % (snap['rssi'] if snap['rssi'] is not None else '—'))
                d = snap['dbg']
                if d:
                    L.append('接続間隔    : %.1f ms  遅延 %s  監視 %.1f s  PHY %s' % (d['int_ms'], d['lat'], d['to_s'], d['phy']))
                    L.append('パッチ側    : RSSI %s  送信枠 %s  BUSY %s' % (d['rssi'], d['tx_free'], d['busy']))
                else:
                    L.append('パッチ側    : —（Debug Info なし）')
                L.append('— 通信 —')
                L.append('受信速度    : %.1f KB/s' % snap['rate_kbs'])
                L.append('受信/送信   : %s / %s バイト' % (format(snap['bytes_rx'], ','), format(snap['bytes_tx'], ',')))
                L.append('断片の欠落  : %d' % snap['frag_gaps'])
                L.append('結果コード  : %s' % ', '.join('%s=%d' % (RES_NAMES[k] if k < len(RES_NAMES) else k, v)
                                                       for k, v in sorted(snap['results'].items())) or '—')
                if snap['cmd_ms']:
                    L.append('応答時間    : 最小 %d / 平均 %d / 最大 %d ms' % snap['cmd_ms'])
                L.append('— 警告 —')
                for t, ev, txt in snap['warnings'] or []:
                    L.append('%7.1f  %s' % (t, txt))
                if not snap['warnings']:
                    L.append('なし')
            elif tab == 1:
                L.append('%-22s %5s %6s %7s %3s %3s' % ('名前', 'RSSI', '間隔ms', '最終', 'Ev', 'St'))
                now = ctx.clock.now()
                for name, rssi, iv, last, ev, st in m.scan_table():
                    L.append('%-22s %5s %6s %6.1fs %3s %3s' % (name[:22], rssi, iv or '—', now - (last or now),
                                                              '%02X' % ev if ev is not None else '—',
                                                              '%02X' % st if st is not None else '—'))
            elif tab == 2:
                c_ = mgr.conn
                if c_ is None:
                    L.append('接続していません')
                else:
                    L.append('パッチ      : %s%s' % (c_.info.label, '（模擬器）' if c_.info.emulator else ''))
                    L.append('特性        : %s' % ', '.join(sorted(CH_SHORT.get(u, u) for u in c_.chars)))
                    st = c_.status
                    if st:
                        L.append('状態        : S%d  電極 %s  電池 %s%%  未通知 %d  時刻 %s' % (
                            st.state, 'NG' if st.electrode else 'OK', st.batt, st.unnotified,
                            '設定済み' if st.time_set else '未設定'))
                        L.append('ページ      : latest %d  base %d  fw %04X' % (st.latest_page_seq, st.base_page_seq, st.fw_ver))
                        L.append('直前コマンド: %s → %s' % (CMD_NAMES.get(st.last_cmd, '%02X' % st.last_cmd),
                                                         RES_NAMES[st.result] if st.result < len(RES_NAMES) else st.result))
                    if c_.dbg:
                        L.append('— Debug Info —')
                        for k in DBG_FIELDS:
                            L.append('  %-14s %s' % (k, c_.dbg.get(k)))
                    s = mgr.session
                    if s:
                        L.append('— 再構成 —')
                        L.append('  %s' % dict(s.asm.stats))
                        L.append('  CRC NG %d  REQ_PAGES %d  受信ページ %d' % (s.crc_ng, s.req_pages_sent, len(s.pages)))
            elif tab == 3:
                rs = [v for _, v in m.series('rssi')]
                rt = [v / 1000.0 for _, v in m.series('rate')]
                L.append('RSSI（60 秒） %s' % (('%d dBm' % rs[-1]) if rs else ''))
                L.append(self.spark(rs, -100, -30))
                L.append('受信速度（60 秒） %s' % (('%.1f KB/s' % rt[-1]) if rt else ''))
                L.append(self.spark(rt, 0))
                L.append('')
                L.append('特性ごとの件数')
                for k, v in sorted(snap['counts'].items()):
                    L.append('  %-28s %d' % (k, v))
                L.append('')
                L.append('切断')
                for t, name, judge in snap['disconnects']:
                    L.append('  %7.1f  %s  %s' % (t, name, judge))
            else:
                f = self.filter.selected_index
                rows = list(ctx.recorder.ring)[-300:]
                for d in reversed(rows):
                    k = d['k']
                    if f == 1 and k != 'cb':
                        continue
                    if f == 2 and k not in ('tx', 'rx'):
                        continue
                    if f == 3 and k != 'warn':
                        continue
                    s = '%8.3f %-4s %-22s %-4s' % (d['t'], k, d['ev'][:22], d.get('ch', ''))
                    if 'hex' in d:
                        s += ' %3d %s' % (d['len'], d['hex'][:48])
                    if 'v' in d:
                        s += ' ' + json.dumps(d['v'], ensure_ascii=False)[:60]
                    if 'err' in d:
                        s += ' ERR %s' % d['err'].get('name')
                    L.append(s)
                    if len(L) > 200:
                        break
            self.tv.text = '\n'.join(L)

    class RootView(ui.View):
        def __init__(self, app):
            self.app = app
            self.background_color = 'white'
            self.portrait = PortraitView(app)
            self.wave = WaveView(app)
            self.monitor = MonitorView(app)
            self.boot = ui.TextView(editable=False, font=('Menlo', 10))
            self.boot.background_color = '#202124'
            self.boot.text_color = '#E8EAED'
            for v in (self.portrait, self.wave, self.monitor, self.boot):
                self.add_subview(v)
            self.monitor.hidden = True
            self.started = False

        def layout(self):
            w, h = self.width, self.height
            land = w > h
            for v in (self.portrait, self.wave, self.monitor):
                v.frame = (0, 0, w, h)
            self.boot.frame = (12, 120, w - 24, h - 240)
            self.app.router.on_orientation('landscape' if land else 'portrait', has_data=self.app.loaded is not None)
            self.app.apply_screen()
            if not self.started:
                self.started = True
                # r5：ui.delay を使わず、View の update()（r17 と同じ 0.2 秒周期）で定期処理を行う。
                # iPhone XS 実機で「ui.delay の予約＋作業スレッド」だけでメモリ破損が出たため（診断 S0）
                self.update_interval = UI_TICK

        def update(self):
            if self.started:
                self.app.ui_loop()

        def will_close(self):
            self.app.shutdown()

    class EcgPatchApp:
        """A01 の組立て結果と画面をつなぐ（Presenter 相当の処理を含む）"""
        def __init__(self):
            self.ctx = AppInit('device').build()
            global _APP_MGR
            _APP_MGR = self.ctx.mgr
            self.mgr = self.ctx.mgr
            self.trace = self.ctx.trace
            self.closed = threading.Event()
            self.loaded = None
            self.snap = None
            self.screen = 'SC1'
            self.emulator = False
            self.in_session = False
            self.t_start = time.time()
            self.ble_started = False
            self.running = True
            self.need_redraw = False
            self._t_ui = 0.0
            self.ui_actions = collections.deque()   # 別スレッド（ダイアログ）から画面の操作を頼む口
            self.router = ScreenRouter(sink=lambda sc, d: None)
            self.trace_prev = Trace.tail(os.path.join(self.ctx.root, 'ecg_patch_trace_prev.txt'), 25)
            self.fault_prev = Trace.tail(os.path.join(self.ctx.root, 'ecg_patch_fault.txt'), 12)
            try:
                import faulthandler
                self.fault_f = open(os.path.join(self.ctx.root, 'ecg_patch_fault.txt'), 'w')
                faulthandler.enable(self.fault_f)
            except Exception:
                self.fault_f = None
            self.trace.w('START', APP_VERSION, 'python', sys.version.split()[0])
            self.place_warning = check_place(__file__)
            if self.place_warning:
                self.trace.w('PLACE', self.place_warning, os.path.abspath(__file__))
            ErrorHandler._inst = self.mgr.errors
            if DIAG['stage']:
                self.trace.w('DIAG', 'stage=%s' % DIAG['stage'], json.dumps(DIAG))
            if DIAG['objc_class']:
                self.trace.w('INIT', 'delegate class')  # 8 章 5：ObjC クラスはスクリプトのスレッドで先に作る
                _delegate_class()
            self.probe_n = 0
            self.worker = threading.Thread(target=self.work_loop, name='ecg_worker', daemon=True)
            self.root = RootView(self)
            lines = ['ECG パッチ iPhone アプリ  %s' % APP_VERSION, '']
            if self.place_warning:
                lines += ['!! ' + self.place_warning, '   ' + os.path.abspath(__file__), '']
            lines += [
                     '① 設定読込み ……… %s' % ('既定値（%s）' % self.ctx.settings.load_error if self.ctx.settings.load_error else '完了'),
                     '② フォルダ作成 ……… 完了（ECG/）',
                     '③ BLE 準備 ……… 3 秒後に開始', '']
            if self.trace_prev:
                lines += ['— 前回の動作記録（最後 25 行）—'] + self.trace_prev
            if self.fault_prev and any(ln.strip() for ln in self.fault_prev):
                lines += ['', '— 前回落ちた位置 —'] + self.fault_prev
            self.root.boot.text = '\n'.join(lines)

        def start(self):
            self.worker.start()
            self.root.present('fullscreen', hide_title_bar=True, orientations=['portrait', 'landscape'])

        # ---- 作業スレッド（BLE の操作はすべてここ：r17 No.14）
        def work_loop(self):
            self.trace.w('WORKER', 'start')
            while self.running:
                try:
                    self.mgr.pump(timeout=0.05)
                    self.mgr.tick()
                except Exception as e:
                    ErrorHandler.instance().handle(e, where='work_loop')   # A16-03：止めない

        def on_ui(self, fn, *a):
            """画面の操作はメインスレッドの定期処理（update）で行う"""
            self.ui_actions.append((fn, a))

        # ---- メインスレッドの周期処理（RootView.update から 0.2 秒ごと）
        def ui_loop(self):
            if not self.running:
                return
            try:
                while self.ui_actions:
                    fn, a = self.ui_actions.popleft()
                    try:
                        fn(*a)
                    except Exception as e:
                        ErrorHandler.instance().handle(e, where='ui_action')
                now = time.time()
                if DIAG['probe']:                       # スレッド間の受渡しの検査（0.2 秒ごと）
                    self.probe_n += 1
                    n = self.probe_n
                    self.mgr.post(self.mgr.probe, n, ('probe', n, n * 7))
                if not self.ble_started and now - self.t_start >= 3.0:
                    self.ble_started = True
                    if DIAG['ble']:
                        self.mgr.post(self.mgr.start)
                    else:
                        self.trace.w('DIAG', 'BLE skipped')
                    if not DIAG['stage']:
                        self.root.boot.hidden = True
                    self.router.on_event('init_done')
                    self.screen = 'SC2'
                    self.apply_screen()
                if DIAG['stage']:
                    self.show_diag(now)
                self.refresh()
                self.root.portrait.status.long_check()
            except Exception as e:
                ErrorHandler.instance().handle(e, where='ui_loop')

        def show_diag(self, now):
            """診断中は標準の TextView（自作の描画を使わない）に結果を出し、結果ファイルに残す"""
            el = now - self.t_start
            m = self.mgr
            verdict = 'NG（メモリ破損を検出）' if m.corrupted else ('OK' if el >= 60 else '実行中')
            text = ('診断 %s   %s\n経過 %d 秒（60 秒で判定）\n検査 OK %d 回／破損 %d 回\n'
                    '描画 %s・ObjC クラス %s・BLE %s\n\n%s' % (
                        DIAG['stage'], verdict, el, m.probe_ok, m.bad_count,
                        'あり' if DIAG['draw'] else 'なし', 'あり' if DIAG['objc_class'] else 'なし',
                        'あり' if DIAG['ble'] else 'なし', '\n'.join(m.status_p.log)))
            self.root.boot.text = text
            if int(el) // 10 != getattr(self, '_diag_last', -1):
                self._diag_last = int(el) // 10
                try:
                    with open(os.path.join(self.ctx.root, 'ecg_diag_result.txt'), 'a', encoding='utf-8') as f:
                        f.write('%s %s %s elapsed=%d probe_ok=%d bad=%d bt=%s\n' % (
                            datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), APP_VERSION, DIAG['stage'],
                            el, m.probe_ok, m.bad_count, m.bt_state))
                        f.flush()
                        os.fsync(f.fileno())
                except Exception:
                    pass

        def refresh(self):
            self.snap = self.mgr.snapshot()
            self.emulator = self.snap['emulator']
            was = self.in_session
            self.in_session = self.snap['state'] in ('CONNECTING', 'DISCOVERING', 'SETUP', 'EVENT', 'DISCONNECTING')
            while self.mgr.ui_events:
                self.on_ui_event(self.mgr.ui_events.popleft())
            if self.need_redraw:
                self.need_redraw = False
                self.apply_screen()
            if self.screen == 'SC7':
                if not self.root.monitor.paused:
                    self.root.monitor.refresh()
                return
            self.root.portrait.status.show(self.snap['status'])
            if was != self.in_session:
                self.root.portrait.layout()
            self.root.portrait.set_needs_display()

        def on_ui_event(self, ev):
            kind = ev[0]
            if kind == 'alert':
                try:
                    sound.play_effect('game:Ding_3')
                except Exception:
                    pass
                console.hud_alert('異常通知を受信しました', 'error', 1.5)
            elif kind == 'show_sc5':
                self.router.current = self.screen if self.screen != 'SC1' else 'SC2'
                self.router.on_event('alert')
                self.screen = 'SC5'
                self.apply_screen()
            elif kind == 'saved':
                item = ev[1]
                if item['kind'] in ('setup30s',):
                    self.load_path(item['csv'], source='ble')
                    if self.screen == 'SC2':
                        self.screen = 'SC3'
            elif kind == 'setup_done':
                console.hud_alert('装着確認が完了しました。計測を開始しました', 'success', 2.0)
            elif kind == 'setup_timeout':
                self.ask_setup_timeout()
            elif kind == 'ask_emulator':
                self.ask_accept_emulator(ev[1])

        @in_thread
        def ask_accept_emulator(self, name):
            try:
                console.alert('パッチ模擬器を検出しました', '%s は模擬器です。試験のために受け入れますか？\n（設定「模擬器を受け入れる」を ON にします）' % name,
                              '受け入れる')
            except KeyboardInterrupt:
                return
            st = self.ctx.settings
            st['accept_emulator'] = True
            st.save()
            self.trace.w('SETTINGS', 'accept_emulator=True')
            self.mgr.post(self.mgr.status_p.add, '模擬器を受け入れます（設定を ON にしました）')   # r9：post は位置引数だけ

        @in_thread
        def ask_setup_timeout(self):
            try:
                r = console.alert('電極 NG', '電極正常の 30 秒がそろいません。貼付を確認してください。', '続ける', '中止',
                                  hide_cancel_button=True)
            except KeyboardInterrupt:
                r = 2
            if r == 1:
                self.mgr.post(self.mgr.setup_continue)
            else:
                self.mgr.post(self.mgr.user_stop)

        def apply_screen(self):
            r = self.root
            land = r.width > r.height
            if self.screen == 'SC7':
                r.monitor.hidden = False
                r.portrait.hidden = r.wave.hidden = True
                return
            r.monitor.hidden = True
            if land and self.loaded is not None and self.screen in ('SC2', 'SC3', 'SC4', 'SC5'):
                r.wave.hidden = False
                r.portrait.hidden = True
                r.wave.layout()
                r.wave.set_needs_display()
            else:
                r.wave.hidden = True
                r.portrait.hidden = False
                r.portrait.layout()
                r.portrait.set_needs_display()

        def chips(self):
            rec = self.loaded
            snap = self.snap or {}
            if self.in_session and snap.get('mode') == 'setup':
                ok = snap.get('electrode_ok')
                secs = snap.get('normal_secs') or 0
                return [('OK' if ok else ('NG' if ok is not None else '—'), '電極状態', 'green' if ok else 'red'),
                        ('%.1f／30 秒' % secs, '電極正常', 'blue'), ('—', '心拍/分', 'gray')]
            if rec is None or rec.analysis is None:
                return [('—', '電極状態', 'gray'), ('—', '不整脈（パッチ）', 'gray'), ('—', '心拍/分', 'gray')]
            an = rec.analysis
            lo = bool(an.get('leadoff'))
            arr = an.get('n_arr', 0)                  # r10：パッチの判定（異常通知）だけ。アプリは判定しない
            return [('はがれ有' if lo else '正常', '電極状態', 'red' if lo else 'green'),
                    ('あり' if arr else '—', '不整脈（パッチ）', 'orange' if arr else 'gray'),
                    (str(an.get('hr') or '—'), '心拍/分', 'ink' if an.get('hr') else 'gray')]

        # ---- 読込み
        def set_loaded(self, ld):
            self.loaded = ld
            self.analyze_async(ld)
            self.apply_screen()

        def analyze_async(self, ld):
            def run():
                try:
                    ld.analysis = Analyzer.analyze(ld.ecg, ld.meta, ld.fs, ld.info.get('event'))
                except Exception as e:
                    ErrorHandler.instance().handle(AnalysisError(str(e)), where='analyze')
                    ld.analysis = {'beats': [], 'verdict': '—', 'leadoff': []}
                self.need_redraw = True              # 作業スレッドから画面を触らない（5.9 節）
            th = threading.Thread(target=run, daemon=True)
            th.start()

        def load_path(self, path, source='file'):
            try:
                x, fs, meta, info = load_ecg_file(path)
            except Exception as e:
                console.hud_alert('読み込めません: %s' % e, 'error', 1.5)
                return
            info['dir'] = os.path.dirname(path) if source == 'ble' else ''
            ld = Loaded(info['name'], x, fs, meta, info, source=source, meta_name=info.get('meta', ''))
            if info.get('event') and 'detect_index' in info['event']:
                ld.detect_index = int(info['event']['detect_index'])
            self.set_loaded(ld)

        # ---- ボタン
        def on_close(self, sender):
            busy = self.mgr.state in ('CONNECTING', 'DISCOVERING', 'SETUP', 'EVENT', 'DISCONNECTING')
            self.trace.w('UI', 'close', self.mgr.state)
            if busy:
                self.confirm_close()
            else:
                self.root.close()

        @in_thread
        def confirm_close(self):
            try:
                console.alert('終了しますか', '受信中です。STOP を送り、受信分を保存して終了します（パッチは計測を続けます）',
                              '終了する')
            except KeyboardInterrupt:
                return
            self.on_ui(self.root.close)

        @in_thread
        def on_file(self, sender):
            p = dialogs.pick_document(types=['public.comma-separated-values-text', 'public.json',
                                             'public.plain-text', 'public.data'])
            if p:
                self.on_ui(self._file_picked, p)

        def _file_picked(self, p):
            self.load_path(p)
            self.screen = 'SC3'
            self.apply_screen()

        def on_dummy(self, sender):
            x = synth_ecg(30, 72, False, seed=int(time.time()) & 0xFFFF)
            self.set_loaded(Loaded('ダミー（30 秒）', x, FS, None, {'name': 'dummy'}, source='dummy'))
            self.screen = 'SC3'

        def on_dummy_leadoff(self, sender):
            x = synth_ecg(30, 72, True, seed=5)
            self.set_loaded(Loaded('ダミー（電極はがれ・30 秒）', x, FS, None, {'name': 'dummy'}, source='dummy'))
            self.screen = 'SC3'

        def on_clear(self, sender):
            self.loaded = None
            self.screen = 'SC2'
            self.apply_screen()

        @in_thread
        def on_share(self, sender):
            ld = self.loaded
            if ld is None or ld.source == 'dummy':
                console.hud_alert('共有するファイルがありません', 'error', 1.0)
                return
            path = ld.info.get('path')
            if path is None or not os.path.exists(path):
                saved = (self.snap or {}).get('saved') or []
                path = saved[0]['csv'] if saved else None
            if path:
                console.open_in(path)

        def on_stop(self, sender):
            self.mgr.post(self.mgr.user_stop)

        @in_thread
        def on_settings(self, sender):
            st = self.ctx.settings
            reg = ', '.join(r['device_id'] for r in st['registered']) or 'なし'
            fields = [
                {'type': 'switch', 'key': 'auto_connect_alert', 'title': '異常通知に自動接続', 'value': bool(st['auto_connect_alert'])},
                {'type': 'switch', 'key': 'auto_fetch_unnotified', 'title': '未通知ありに自動接続', 'value': bool(st['auto_fetch_unnotified'])},
                {'type': 'switch', 'key': 'save_pages_bin', 'title': '受信ページの生データを保存', 'value': bool(st['save_pages_bin'])},
                {'type': 'switch', 'key': 'verbose_trace', 'title': '詳細記録', 'value': bool(st['verbose_trace'])},
                {'type': 'switch', 'key': 'dev_display', 'title': '開発者表示', 'value': bool(st['dev_display'])},
                {'type': 'switch', 'key': 'ble_trace', 'title': 'BLE トレース記録', 'value': bool(st['ble_trace'])},
            ]
            if DEV_BUILD:
                fields.append({'type': 'switch', 'key': 'accept_emulator', 'title': '模擬器を受け入れる（開発版）',
                               'value': bool(st['accept_emulator'])})
            sections = [('設定', fields),
                         ('登録済みパッチ: ' + reg, [{'type': 'switch', 'key': '_clear_reg', 'title': '登録を消去する', 'value': False}]),
                         ('その他', [{'type': 'switch', 'key': '_monitor', 'title': 'BLE モニタを開く', 'value': False}])]
            r = dialogs.form_dialog('設定（SC6）', sections=sections)
            if r is None:
                return
            for k, v in r.items():
                if k.startswith('_'):
                    continue
                st[k] = bool(v)
            if r.get('_clear_reg'):
                st['registered'] = []
            st.save()
            self.trace.verbose = bool(st['verbose_trace'])
            self.trace.w('SETTINGS', json.dumps({k: st[k] for k in DEFAULT_SETTINGS if k != 'registered'}))
            if r.get('_monitor'):
                self.on_ui(self.open_monitor)

        def on_connect_tap(self, peer):
            self.ask_connect(peer)

        @in_thread
        def ask_connect(self, peer):
            info = self.mgr.scanner.table.get(peer)
            pid = None
            if info is not None and info.event == EV_SETUP:
                try:
                    pid = dialogs.input_alert('%s に接続' % info.label, 'パッチのラベルと照合してください。\n患者 ID（任意、16 文字まで）',
                                              '', '接続')
                except KeyboardInterrupt:
                    return
                pid = (pid or '').strip()[:16] or None
            self.trace.w('UI', 'connect', info.label if info else peer)
            self.on_ui(self._connect_chosen, peer, pid)

        def _connect_chosen(self, peer, pid):
            self.loaded = None
            self.screen = 'SC3'
            self.apply_screen()
            self.mgr.post(self.mgr.connect_to, peer, pid)

        def on_view_wave(self, sender):
            a = (self.snap or {}).get('alert') or {}
            sv = a.get('saved')
            if sv and sv.get('csv'):
                self.load_path(sv['csv'], source='ble')
            self.screen = 'SC3'
            self.mgr.post(setattr, self.mgr, 'alert', None)
            self.apply_screen()
            console.hud_alert('iPhone を横向きにしてください', 'success', 1.2)

        def on_sc5_close(self, sender):
            self.screen = 'SC2'
            if not self.in_session:
                self.mgr.post(setattr, self.mgr, 'alert', None)
            self.apply_screen()

        def open_monitor(self):
            self.prev_screen = self.screen
            self.screen = 'SC7'
            self.root.monitor.refresh()
            self.apply_screen()

        def close_monitor(self):
            self.screen = getattr(self, 'prev_screen', 'SC2')
            self.apply_screen()

        # ---- 終了（16.1 節）
        def shutdown(self):
            if not self.running:
                return
            self.trace.w('STOP', 'shutdown')
            done = threading.Event()

            def in_worker():                      # BLE の後始末も作業スレッドで行う
                try:
                    self.mgr.shutdown()
                    t0 = time.time()
                    while time.time() - t0 < 1.0 and not self.mgr.q.empty():
                        self.mgr.pump()
                    self.ctx.transport.shutdown()
                except Exception as e:
                    self.trace.w('STOP', 'error', e)
                finally:
                    done.set()
            if getattr(self.worker, 'is_alive', lambda: False)():
                self.mgr.post(in_worker)
                done.wait(3.0)
            else:
                in_worker()
            self.running = False
            try:
                self.ctx.recorder.close()
                self.ctx.settings.save()
            except Exception:
                pass
            self.trace.w('STOP', 'done')
            self.trace.close()
            self.closed.set()


def check_place(path, expect='ecg_patch_app.py'):
    """置き場所の確認（r17 規則 11）。問題があれば説明文、なければ ''"""
    p = os.path.abspath(path)
    msgs = []
    if 'Mobile Documents' in p or 'CloudDocs' in p:
        msgs.append('iCloud Drive から実行しています。「この iPhone 内 > Pythonista 3」に置いて Pythonista から開いてください')
    if os.path.basename(p) != expect:
        msgs.append('ファイル名が %s ではありません（%s）。古い版と取り違えないよう名前をそろえてください' % (expect, os.path.basename(p)))
    return ' ／ '.join(msgs)


def main():
    if not ON_PYTHONISTA:
        print('このスクリプトは iPhone の Pythonista 3 で実行してください（PC では tests/ を実行）。')
        return
    app = EcgPatchApp()
    app.start()
    app.closed.wait()                    # 5.9 節：画面を閉じるまで待つ


if __name__ == '__main__':
    main()
