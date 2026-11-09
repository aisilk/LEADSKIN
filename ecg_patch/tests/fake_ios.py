# -*- coding: utf-8 -*-
"""
PC 用の偽 iOS 環境：Pythonista の ui・console・dialogs・sound・objc_util と、
CoreBluetooth（セントラル・ペリフェラル）を Python で模擬する。

install() を呼んでから ecg_patch_app／patch_emu を import すると、両方とも
「Pythonista 上」の分岐（CoreBluetoothTransport・PeripheralTransport・画面）で動く。
CoreBluetooth の呼出しは Apple の実際のセレクタ名・引数の数で検査し、
デリゲートは AIR（電波）を通して相手側に届ける。
"""
import sys
import types
import ctypes
import itertools
import collections

# =============================================================================
# 偽の時計（time.time／time.monotonic を差し替える）
# =============================================================================
class FakeTime:
    def __init__(self):
        self.t = 1_000_000.0

    def advance(self, dt):
        self.t += dt


FT = FakeTime()


# =============================================================================
# 偽 objc_util
# =============================================================================
REG = {}
_ids = itertools.count(0x1000, 0x10)
CALLS = collections.Counter()


class FObj:
    def __init__(self):
        self._ptr = next(_ids)
        REG[self._ptr] = self
        self.rc = 1
        self.freed = False

    @property
    def ptr(self):
        return self._ptr

    def retain(self):
        assert not self.freed, 'retain after free: %r' % self
        self.rc += 1
        return self

    def release(self):
        assert not self.freed and self.rc > 0, 'over-release: %r' % self
        self.rc -= 1
        if self.rc == 0:
            self.freed = True
            for v in getattr(self, '_owned', ()):      # 中身の解放（NSArray・NSDictionary）
                v.release()

    def init(self):
        return self


class OI:
    """ObjCInstance の代わり"""
    def __init__(self, o):
        if isinstance(o, OI):
            o = o._o
        elif isinstance(o, int):
            o = REG[o]
        self._o = o
        self.ptr = o._ptr

    def __str__(self):
        return str(self._o)

    def __getattr__(self, name):
        if name.startswith('__'):
            raise AttributeError(name)
        m = getattr(self._o, name)

        def call(*a):
            r = m(*[_obj(x) for x in a])
            return OI(r) if isinstance(r, FObj) else r
        return call


def _obj(x):
    if isinstance(x, OI):
        return x._o
    if isinstance(x, FObj):
        return x
    if isinstance(x, int) and x in REG:
        return REG[x]
    return x


class FString(FObj):
    def __init__(self, s=''):
        super().__init__()
        self.s = s

    def initWithUTF8String_(self, b):
        assert isinstance(b, bytes)
        self.s = b.decode('utf-8')
        return self

    def UTF8String(self):
        return self.s.encode('utf-8')

    def __str__(self):
        return self.s

    def UUIDString(self):                 # NSUUID 互換（識別子）
        return FString(self.s)


class FNumber(FObj):
    def __init__(self, v=0):
        super().__init__()
        self.v = v

    def initWithBool_(self, b):
        self.v = bool(b)
        return self

    def longLongValue(self):
        return int(self.v)

    def boolValue(self):
        return bool(self.v)


class FArray(FObj):
    def __init__(self, items=()):
        super().__init__()
        self.items = list(items)

    def initWithObjects_count_(self, addr, n):
        ptrs = (ctypes.c_void_p * max(1, n)).from_address(addr)[:n]
        self.items = [REG[p].retain() for p in ptrs]
        self._owned = list(self.items)
        return self

    def count(self):
        return len(self.items)

    def objectAtIndex_(self, i):
        return self.items[i]


class FDict(FObj):
    def __init__(self, d=None):
        super().__init__()
        self.d = d or {}

    def initWithObjects_forKeys_count_(self, vaddr, kaddr, n):
        vals = (ctypes.c_void_p * max(1, n)).from_address(vaddr)[:n]
        keys = (ctypes.c_void_p * max(1, n)).from_address(kaddr)[:n]
        self.d = {str(REG[k]): REG[v].retain() for k, v in zip(keys, vals)}
        self._owned = list(self.d.values())
        return self

    def objectForKey_(self, k):
        return self.d.get(str(k))


class FData(FObj):
    def __init__(self, b=b''):
        super().__init__()
        self.b = bytes(b)
        self.buf = ctypes.create_string_buffer(self.b, max(1, len(self.b)))

    def initWithBytes_length_(self, addr, n):
        self.b = ctypes.string_at(addr, n) if n else b''
        self.buf = ctypes.create_string_buffer(self.b, max(1, len(self.b)))
        return self

    def length(self):
        return len(self.b)

    def bytes(self):
        return ctypes.addressof(self.buf)


class FError(FObj):
    def __init__(self, domain, code, desc=''):
        super().__init__()
        self.dm, self.cd, self.desc = domain, code, desc

    def domain(self):
        return FString(self.dm)

    def code(self):
        return self.cd

    def localizedDescription(self):
        return FString(self.desc)

    def __str__(self):
        return '%s %d' % (self.dm, self.cd)


class FUUID(FObj):
    def __init__(self, s):
        super().__init__()
        self.s = s.upper()

    def UUIDString(self):
        return FString(self.s)


RUNTIME_OBJC_UTIL = []       # r17 No.10・11：起動後の objc_util 呼出しを検出する


def ns(x):
    RUNTIME_OBJC_UTIL.append('ns(%r)' % (x,))
    raise AssertionError('objc_util.ns() must not be used (r17 rule 2)')


def _ns_internal(x):
    if isinstance(x, (OI, FObj)):
        return OI(x)
    if isinstance(x, bool) or isinstance(x, (int, float)):
        return OI(FNumber(x))
    if isinstance(x, str):
        return OI(FString(x))
    if isinstance(x, (bytes, bytearray)):
        return OI(FData(x))
    if isinstance(x, (list, tuple)):
        return OI(FArray([_obj(_ns_internal(e)) for e in x]))
    if isinstance(x, dict):
        return OI(FDict({str(k): _obj(_ns_internal(v)) for k, v in x.items()}))
    raise TypeError('ns: %r' % type(x))


class FClass(FObj):
    def __init__(self, name, factory):
        super().__init__()
        self.name = name
        self.factory = factory

    def alloc(self):
        return self.factory()


    def UUIDWithString_(self, s):
        assert self.name == 'CBUUID' and isinstance(s, FString)
        return FUUID(s.s)


class FDelegate(FObj):
    def __init__(self, methods):
        super().__init__()
        self.methods = methods

    def init(self):
        return self


CLASSES = {}


def _class_obj(name):
    if name not in CLASSES:
        fac = {'CBCentralManager': lambda: FCentralManager(), 'CBPeripheralManager': lambda: FPeripheralManager(),
               'CBMutableCharacteristic': lambda: FMutableChar(), 'CBMutableService': lambda: FMutableService(),
               'NSObject': FObj, 'NSData': FData, 'NSString': FString, 'NSArray': FArray,
               'NSDictionary': FDict, 'NSNumber': FNumber, 'CBUUID': lambda: FUUID('0000')}[name]
        CLASSES[name] = FClass(name, fac)
    return CLASSES[name]


def fake_cls(name):
    """objc_getClass の代わり"""
    return _class_obj(name)._ptr


def ObjCClass(name):
    assert name == 'NSObject', 'objc_util.ObjCClass(%s) must not be used (r17 rule 2)' % name
    return OI(_class_obj(name))


def ObjCInstance(p):
    RUNTIME_OBJC_UTIL.append('ObjCInstance')
    raise AssertionError('objc_util.ObjCInstance must not be used (r17 rule 2)')


# Apple のデリゲートのセレクタ（引数の数を検査する）
KNOWN_DELEGATE = {
    'centralManagerDidUpdateState:', 'centralManager:didDiscoverPeripheral:advertisementData:RSSI:',
    'centralManager:didConnectPeripheral:', 'centralManager:didFailToConnectPeripheral:error:',
    'centralManager:didDisconnectPeripheral:error:', 'peripheral:didDiscoverServices:',
    'peripheral:didDiscoverCharacteristicsForService:error:',
    'peripheral:didUpdateNotificationStateForCharacteristic:error:',
    'peripheral:didUpdateValueForCharacteristic:error:', 'peripheral:didWriteValueForCharacteristic:error:',
    'peripheral:didModifyServices:', 'peripheral:didReadRSSI:error:',
    'peripheralManagerDidUpdateState:', 'peripheralManager:didAddService:error:',
    'peripheralManagerDidStartAdvertising:error:', 'peripheralManager:central:didSubscribeToCharacteristic:',
    'peripheralManager:central:didUnsubscribeFromCharacteristic:', 'peripheralManager:didReceiveReadRequest:',
    'peripheralManager:didReceiveWriteRequests:', 'peripheralManagerIsReadyToUpdateSubscribers:',
}


def _selector_of(fn):
    n = fn.__name__
    return n.replace('_', ':')


def create_objc_class(name, superclass=None, methods=(), protocols=(), **kw):
    table = {}
    for fn in methods:
        sel = _selector_of(fn)
        assert sel in KNOWN_DELEGATE, 'unknown delegate selector %s' % sel
        enc = getattr(fn, 'encoding', None)
        assert enc, 'no encoding for %s' % sel
        n_args = sel.count(':')
        assert enc == 'v@:' + '@' * n_args, 'bad encoding %s for %s' % (enc, sel)
        assert fn.__code__.co_argcount == n_args + 2, 'arg count %s' % sel
        table[sel] = fn
    for p in protocols:
        assert p in ('CBCentralManagerDelegate', 'CBPeripheralDelegate', 'CBPeripheralManagerDelegate'), p
    return OI(FClass(name, lambda: FDelegate(table)))


IN_DELEGATE = [0]
CB_OPS = {'initWithDelegate:queue:options:', 'scanForPeripheralsWithServices:options:', 'stopScan',
          'connectPeripheral:options:', 'cancelPeripheralConnection:', 'discoverServices:',
          'discoverCharacteristics:forService:', 'setNotifyValue:forCharacteristic:', 'readValueForCharacteristic:',
          'writeValue:forCharacteristic:type:', 'addService:', 'startAdvertising:', 'stopAdvertising',
          'removeAllServices', 'updateValue:forCharacteristic:onSubscribedCentrals:', 'respondToRequest:withResult:'}
VIOLATIONS = []


def _call_delegate(delegate, sel, *args):
    fn = delegate.methods.get(sel)
    if fn is None:
        return
    CALLS['delegate ' + sel] += 1
    IN_DELEGATE[0] += 1
    try:
        fn(delegate._ptr, 0, *[a._ptr if isinstance(a, FObj) else a for a in args])
    finally:
        IN_DELEGATE[0] -= 1


# ---- 型固定の _send（セレクタ・型記号を検査）
SIG = {
    # CBCentralManager
    'initWithDelegate:queue:options:': '@@@@', 'scanForPeripheralsWithServices:options:': 'v@@', 'stopScan': 'v',
    'connectPeripheral:options:': 'v@@', 'cancelPeripheralConnection:': 'v@', 'state': 'q',
    # CBPeripheral
    'setDelegate:': 'v@', 'discoverServices:': 'v@', 'discoverCharacteristics:forService:': 'v@@',
    'setNotifyValue:forCharacteristic:': 'vB@', 'readValueForCharacteristic:': 'v@',
    'writeValue:forCharacteristic:type:': 'v@@q', 'maximumWriteValueLengthForType:': 'Qq', 'readRSSI': 'v',
    'identifier': '@', 'services': '@', 'characteristics': '@', 'UUID': '@', 'value': '@', 'isNotifying': 'B',
    # 基本
    'alloc': '@', 'init': '@', 'retain': '@', 'release': 'v', 'UUIDWithString:': '@@', 'UUIDString': '@',
    'initWithUTF8String:': '@*', 'UTF8String': '*', 'initWithBytes:length:': '@PQ', 'length': 'Q',
    'initWithObjects:count:': '@PQ', 'initWithObjects:forKeys:count:': '@PPQ', 'initWithBool:': '@B',
    'bytes': 'P', 'objectForKey:': '@@', 'count': 'Q', 'objectAtIndex:': '@Q', 'longLongValue': 'q',
    'domain': '@', 'code': 'q', 'localizedDescription': '@',
    # CBPeripheralManager
    'addService:': 'v@', 'startAdvertising:': 'v@', 'stopAdvertising': 'v', 'removeAllServices': 'v',
    'updateValue:forCharacteristic:onSubscribedCentrals:': 'B@@@', 'respondToRequest:withResult:': 'v@q',
    'initWithType:properties:value:permissions:': '@@Q@Q', 'initWithType:primary:': '@@B',
    'setCharacteristics:': 'v@', 'characteristic': '@', 'offset': 'Q', 'setValue:': 'v@',
    'maximumUpdateValueLength': 'Q',
}


def fake_send(obj, selname, sig, *args):
    assert selname in SIG, 'unknown selector %s' % selname
    assert SIG[selname] == sig, 'signature of %s: %s (expected %s)' % (selname, sig, SIG[selname])
    assert len(sig) - 1 == len(args) == selname.count(':'), 'arg count of %s' % selname
    assert obj is None or isinstance(obj, int), 'receiver must be a raw pointer: %r' % (obj,)
    target = REG.get(obj)
    assert target is not None, 'nil/unknown receiver for %s' % selname
    assert not target.freed, 'use after release: %s on %r' % (selname, target)
    if selname in CB_OPS and IN_DELEGATE[0]:
        VIOLATIONS.append(selname)                 # r17 No.14：デリゲートの中で BLE を操作しない
    CALLS[selname] += 1
    m = getattr(target, selname.replace(':', '_'))
    conv = []
    for t, a in zip(sig[1:], args):
        if t == '@':
            assert a is None or isinstance(a, int), 'object argument must be a raw pointer: %r' % (a,)
            o = REG.get(a) if a is not None else None
            assert a is None or o is not None, 'unknown pointer for %s' % selname
            assert o is None or not o.freed, 'freed object passed to %s' % selname
            conv.append(o)
        else:
            conv.append(a)
    r = m(*conv)
    if sig[0] == '@':
        return r._ptr if isinstance(r, FObj) else r
    if sig[0] == 'v':
        return None
    return r


# =============================================================================
# 偽 CoreBluetooth と「電波」
# =============================================================================
class Air:
    def __init__(self):
        self.q = collections.deque()
        self.cm = None
        self.pm = None
        self.periph = None          # セントラルから見たペリフェラル
        self.last_adv = 0.0
        self.budget = 0
        self.need_ready = False
        self.mtu = 247

    def later(self, fn):
        self.q.append(fn)

    def run(self, max_n=10000):
        n = 0
        while self.q and n < max_n:
            self.q.popleft()()
            n += 1
        return n

    def tick(self):
        self.budget = 16
        if self.need_ready and self.pm is not None:
            self.need_ready = False
            _call_delegate(self.pm.delegate, 'peripheralManagerIsReadyToUpdateSubscribers:', self.pm)
        cm, pm = self.cm, self.pm
        if cm and pm and cm.scanning and pm.adv and pm.services and FT.t - self.last_adv >= 0.1:
            self.last_adv = FT.t
            if self.periph is None:
                self.periph = FPeripheral(self)
            adv = FDict({'kCBAdvDataLocalName': FString(pm.adv['name']),
                         'kCBAdvDataServiceUUIDs': FArray(pm.adv['uuids']), 'kCBAdvDataIsConnectable': FNumber(1)})
            _call_delegate(cm.delegate, 'centralManager:didDiscoverPeripheral:advertisementData:RSSI:',
                           cm, self.periph, adv, FNumber(-52))


AIR = Air()


class FCentralManager(FObj):
    def initWithDelegate_queue_options_(self, delegate, q, opts):
        assert q is None
        self.delegate = delegate
        self.scanning = False
        AIR.cm = self
        AIR.later(lambda: _call_delegate(self.delegate, 'centralManagerDidUpdateState:', self))
        return self

    def state(self):
        return 5

    def setDelegate_(self, d):
        self.delegate = d or FDelegate({})

    def scanForPeripheralsWithServices_options_(self, uuids, opts):
        assert isinstance(uuids, FArray) and all(isinstance(u, FUUID) for u in uuids.items)
        assert isinstance(opts, FDict) and 'kCBScanOptionAllowDuplicates' in opts.d
        self.scanning = True

    def stopScan(self):
        self.scanning = False

    def connectPeripheral_options_(self, p, opts):
        assert isinstance(p, FPeripheral)

        def go():
            if AIR.pm and AIR.pm.services:
                p.connected = True
                _call_delegate(self.delegate, 'centralManager:didConnectPeripheral:', self, p)
            else:
                _call_delegate(self.delegate, 'centralManager:didFailToConnectPeripheral:error:', self, p,
                               FError('CBErrorDomain', 10))
        AIR.later(go)

    def cancelPeripheralConnection_(self, p):
        def go():
            if not p.connected:
                _call_delegate(self.delegate, 'centralManager:didDisconnectPeripheral:error:', self, p, None)
                return
            p.connected = False
            if AIR.pm:
                for ch in list(AIR.pm.subscribed):
                    AIR.pm.subscribed.discard(ch)
                    _call_delegate(AIR.pm.delegate, 'peripheralManager:central:didUnsubscribeFromCharacteristic:',
                                   AIR.pm, FCentral(), ch)
            for c_ in p.char_list():
                c_.notifying = False
            _call_delegate(self.delegate, 'centralManager:didDisconnectPeripheral:error:', self, p, None)
        AIR.later(go)


class FService(FObj):
    def __init__(self, uuid, chars):
        super().__init__()
        self.uuid = FUUID(uuid)
        self.chars = chars

    def UUID(self):
        return self.uuid

    def characteristics(self):
        return FArray(self.chars)


class FChar(FObj):
    """セントラル側の CBCharacteristic"""
    def __init__(self, pm_char):
        super().__init__()
        self.pm_char = pm_char
        self.val = None
        self.notifying = False

    def UUID(self):
        return self.pm_char.uuid

    def value(self):
        return self.val

    def isNotifying(self):
        return self.notifying


class FCentral(FObj):
    def maximumUpdateValueLength(self):
        return AIR.mtu - 3


class FRequest(FObj):
    def __init__(self, ch, value=None, kind='read', central_char=None):
        super().__init__()
        self.ch = ch
        self.val = value
        self.kind = kind
        self.central_char = central_char

    def characteristic(self):
        return self.ch

    def offset(self):
        return 0

    def value(self):
        return self.val

    def setValue_(self, d):
        self.val = FData(d.b)                      # 実機と同じく値を写して持つ


class FPeripheral(FObj):
    def __init__(self, air):
        super().__init__()
        self.ident = FString('6F1C2A2B-0000-4000-8000-00000000E1E1')
        self.delegate = None
        self.connected = False
        self.svcs = []
        self.map = {}               # pm_char → FChar

    def identifier(self):
        return self.ident

    def setDelegate_(self, d):
        self.delegate = d

    def char_list(self):
        return list(self.map.values())

    def services(self):
        return FArray(self.svcs)

    def discoverServices_(self, uuids):
        want = {u.s for u in uuids.items} if uuids is not None else None

        def go():
            self.svcs = []
            for s in AIR.pm.services:
                if want is None or s.uuid.s in want:
                    chars = []
                    for pc in s.chars:
                        fc = self.map.get(pc) or FChar(pc)
                        self.map[pc] = fc
                        chars.append(fc)
                    self.svcs.append(FService(s.uuid.s, chars))
            _call_delegate(self.delegate, 'peripheral:didDiscoverServices:', self, None)
        AIR.later(go)

    def discoverCharacteristics_forService_(self, uuids, svc):
        assert isinstance(svc, FService)
        AIR.later(lambda: _call_delegate(self.delegate, 'peripheral:didDiscoverCharacteristicsForService:error:',
                                         self, svc, None))

    def maximumWriteValueLengthForType_(self, t):
        return 512 if t == 0 else AIR.mtu - 3

    def setNotifyValue_forCharacteristic_(self, flag, ch):
        assert isinstance(flag, bool) and isinstance(ch, FChar)
        assert ch.pm_char.props & 0x30, 'characteristic does not support notify/indicate'

        def go():
            ch.notifying = flag
            AIR.pm.subscribed.add(ch.pm_char)
            _call_delegate(AIR.pm.delegate, 'peripheralManager:central:didSubscribeToCharacteristic:',
                           AIR.pm, FCentral(), ch.pm_char)
            _call_delegate(self.delegate, 'peripheral:didUpdateNotificationStateForCharacteristic:error:',
                           self, ch, None)
        AIR.later(go)

    def readValueForCharacteristic_(self, ch):
        assert ch.pm_char.props & 0x02, 'characteristic is not readable'
        req = FRequest(ch.pm_char, kind='read', central_char=ch)
        AIR.later(lambda: _call_delegate(AIR.pm.delegate, 'peripheralManager:didReceiveReadRequest:', AIR.pm, req))

    def writeValue_forCharacteristic_type_(self, data, ch, typ):
        assert typ == 0 and isinstance(data, FData)
        assert ch.pm_char.props & 0x08, 'characteristic is not writable'
        req = FRequest(ch.pm_char, value=FData(data.b), kind='write', central_char=ch)
        AIR.later(lambda: _call_delegate(AIR.pm.delegate, 'peripheralManager:didReceiveWriteRequests:',
                                         AIR.pm, FArray([req])))

    def readRSSI(self):
        AIR.later(lambda: _call_delegate(self.delegate, 'peripheral:didReadRSSI:error:', self, FNumber(-58), None))


class FMutableChar(FObj):
    def initWithType_properties_value_permissions_(self, uuid, props, value, perms):
        assert isinstance(uuid, FUUID) and value is None
        self.uuid, self.props, self.perms = uuid.retain(), props, perms
        return self

    def UUID(self):
        return self.uuid


class FMutableService(FObj):
    def initWithType_primary_(self, uuid, primary):
        self.uuid, self.primary, self.chars = uuid.retain(), primary, []
        return self

    def setCharacteristics_(self, arr):
        self.chars = [c_.retain() for c_ in arr.items]


class FPeripheralManager(FObj):
    def initWithDelegate_queue_options_(self, delegate, q, opts):
        self.delegate = delegate
        self.services = []
        self.adv = None
        self.subscribed = set()
        AIR.pm = self
        AIR.later(lambda: _call_delegate(self.delegate, 'peripheralManagerDidUpdateState:', self))
        return self

    def state(self):
        return 5

    def setDelegate_(self, d):
        self.delegate = d or FDelegate({})

    def addService_(self, svc):
        assert isinstance(svc, FMutableService) and svc.chars
        self.services.append(svc.retain())
        AIR.later(lambda: _call_delegate(self.delegate, 'peripheralManager:didAddService:error:', self, svc, None))

    def startAdvertising_(self, d):
        name = d.d.get('kCBAdvDataLocalName')
        uuids = d.d.get('kCBAdvDataServiceUUIDs')
        assert isinstance(name, FString) and isinstance(uuids, FArray)
        self.adv = {'name': str(name), 'uuids': [u.retain() for u in uuids.items]}
        AIR.later(lambda: _call_delegate(self.delegate, 'peripheralManagerDidStartAdvertising:error:', self, None))

    def stopAdvertising(self):
        self.adv = None

    def removeAllServices(self):
        old = self.services
        self.services = []
        p = AIR.periph
        if p is not None and p.connected and old:
            inval = [s for s in p.svcs]
            p.svcs = []
            AIR.later(lambda: _call_delegate(p.delegate, 'peripheral:didModifyServices:', p, FArray(inval)))

    def updateValue_forCharacteristic_onSubscribedCentrals_(self, data, ch, centrals):
        assert isinstance(data, FData) and isinstance(ch, FMutableChar)
        assert ch.props & 0x30
        p = AIR.periph
        if p is None or not p.connected:
            return True
        if AIR.budget <= 0:
            AIR.need_ready = True
            return False
        AIR.budget -= 1
        fc = p.map.get(ch)
        assert len(data.b) <= AIR.mtu - 3, 'notification longer than MTU-3'

        def go():
            if fc is not None and fc.notifying:
                fc.val = FData(data.b)
                _call_delegate(p.delegate, 'peripheral:didUpdateValueForCharacteristic:error:', p, fc, None)
        AIR.later(go)
        return True

    def respondToRequest_withResult_(self, req, result):
        assert isinstance(req, FRequest) and not getattr(req, 'responded', False)
        req.responded = True
        p = AIR.periph
        fc = req.central_char

        def go():
            if req.kind == 'read':
                fc.val = FData(req.val.b if req.val is not None else b'')
                _call_delegate(p.delegate, 'peripheral:didUpdateValueForCharacteristic:error:', p, fc,
                               None if result == 0 else FError('CBATTErrorDomain', result))
            else:
                _call_delegate(p.delegate, 'peripheral:didWriteValueForCharacteristic:error:', p, fc,
                               None if result == 0 else FError('CBATTErrorDomain', result))
        AIR.later(go)


# =============================================================================
# 偽 ui・console・dialogs・sound
# =============================================================================
DELAYS = []
IN_BG_CALLS = []                # ui.in_background の呼出し（r6 以降 0 のはず）
DELAY_CALLS = [0]               # ui.delay を呼んだ回数（r5 以降、アプリ・模擬器は 0 のはず）
PRESENTED = []
DRAWN = collections.Counter()


def make_ui():
    m = types.ModuleType('ui')
    m.ALIGN_LEFT, m.ALIGN_CENTER, m.ALIGN_RIGHT = 0, 1, 2
    m.LB_TRUNCATE_TAIL = 4

    class View:
        def __new__(cls, *a, **kw):              # Pythonista と同じく、サブクラスが super().__init__ を呼ばなくてよい
            self = object.__new__(cls)
            self.subviews = []
            self.x = self.y = 0.0
            self.width, self.height = 100.0, 100.0
            self.hidden = False
            self.alpha = 1.0
            self.enabled = True
            self.update_interval = 0.0
            self._next_update = 0.0
            return self

        def __init__(self, *a, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

        @property
        def frame(self):
            return (self.x, self.y, self.width, self.height)

        @frame.setter
        def frame(self, f):
            assert len(f) == 4 and all(isinstance(v, (int, float)) for v in f), f
            self.x, self.y, self.width, self.height = f
            if hasattr(type(self), 'layout') and type(self).layout is not View.layout:
                self.layout()

        def layout(self):
            pass

        def add_subview(self, v):
            self.subviews.append(v)

        def set_needs_display(self):
            DRAWN['needs_display'] += 1

        def present(self, *a, **kw):
            self.presented = True
            PRESENTED.append(self)

        def close(self):
            self.presented = False
            if hasattr(self, 'will_close'):
                self.will_close()

    class Button(View):
        pass

    class Label(View):
        pass

    class TextView(View):
        text = ''

    class SegmentedControl(View):
        selected_index = 0

    class Path:
        def __init__(self):
            self.line_width = 1.0
            self.n = 0

        @classmethod
        def rounded_rect(cls, x, y, w, h, r):
            for v in (x, y, w, h, r):
                assert isinstance(v, (int, float))
            return cls()

        @classmethod
        def oval(cls, x, y, w, h):
            return cls()

        @classmethod
        def rect(cls, x, y, w, h):
            return cls()

        def move_to(self, x, y):
            self.n += 1

        def line_to(self, x, y):
            assert isinstance(x, (int, float)) and isinstance(y, (int, float))
            self.n += 1

        def fill(self):
            DRAWN['fill'] += 1

        def stroke(self):
            DRAWN['stroke'] += 1

        def set_line_dash(self, d):
            assert isinstance(d, list)

    def set_color(c):
        assert isinstance(c, (str, tuple)), c
        if isinstance(c, str):
            assert c.startswith('#') or c in ('white', 'black'), c

    def fill_rect(x, y, w, h):
        DRAWN['rect'] += 1

    def draw_string(s, rect=None, font=None, color=None, alignment=0, line_break_mode=0):
        assert isinstance(s, str) and len(rect) == 4 and isinstance(font, tuple) and len(font) == 2, (s, rect, font)
        set_color(color)
        DRAWN['text'] += 1
        DRAWN.setdefault('texts', [])
        DRAWN['texts'].append(s)

    def delay(fn, t):
        DELAY_CALLS[0] += 1
        DELAYS.append((FT.t + t, fn))

    def in_background(fn):
        # 実機と同じく、スクリプトのスレッド（closed.wait() で止まっている）待ちになり実行されない
        def wrapper(*a, **k):
            IN_BG_CALLS.append(fn.__name__)
        return wrapper

    for k, v in dict(View=View, Button=Button, Label=Label, TextView=TextView, SegmentedControl=SegmentedControl,
                     Path=Path, set_color=set_color, fill_rect=fill_rect, draw_string=draw_string, delay=delay,
                     in_background=in_background).items():
        setattr(m, k, v)
    return m


HUD = []


def make_console():
    m = types.ModuleType('console')
    m.hud_alert = lambda msg, icon='success', dur=1.0: HUD.append(msg)
    m.alert = lambda *a, **k: 1
    m.open_in = lambda p: HUD.append('open_in ' + p)
    m.input_alert = lambda *a, **k: ''
    return m


def make_dialogs():
    m = types.ModuleType('dialogs')
    m.input_alert = lambda *a, **k: 'P0001'
    m.pick_document = lambda types=None: None
    m.form_dialog = lambda title='', sections=None: {'accept_emulator': True}
    return m


def make_sound():
    m = types.ModuleType('sound')
    m.play_effect = lambda name: HUD.append('sound ' + name)
    return m


def make_objc_util():
    m = types.ModuleType('objc_util')
    m.ObjCClass = ObjCClass
    m.ObjCInstance = ObjCInstance
    m.create_objc_class = create_objc_class
    m.ns = ns

    class _Fn:
        restype = None
        argtypes = None

        def __call__(self, *a):
            raise AssertionError('real objc runtime must not be called')

    class _C:
        sel_registerName = _Fn()
    m.c = _C()
    return m


def install():
    import time as _time
    _time.time = lambda: FT.t
    _time.monotonic = lambda: FT.t
    sys.modules['ui'] = make_ui()
    sys.modules['console'] = make_console()
    sys.modules['dialogs'] = make_dialogs()
    sys.modules['sound'] = make_sound()
    sys.modules['objc_util'] = make_objc_util()


def _walk(v):
    yield v
    for c_ in getattr(v, 'subviews', []):
        yield from _walk(c_)


def run_delays():
    """ui.delay の予約と、表示中の View の update()（update_interval）を時刻どおりに呼ぶ"""
    due = [d for d in DELAYS if d[0] <= FT.t]
    for d in due:
        DELAYS.remove(d)
    for _, fn in due:
        fn()
    for root in list(PRESENTED):
        if not getattr(root, 'presented', False):
            continue
        for v in list(_walk(root)):
            iv = getattr(v, 'update_interval', 0.0) or 0.0
            if iv > 0 and hasattr(v, 'update') and FT.t >= v._next_update:
                v._next_update = FT.t + iv
                v.update()
