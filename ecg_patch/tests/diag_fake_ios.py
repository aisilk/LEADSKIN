# -*- coding: utf-8 -*-
"""段階診断（ecg_diag_iphone.py・DIAG）の偽 iOS 試験：python tests/diag_fake_ios.py"""
import os
import sys
import time
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = tempfile.mkdtemp(prefix='fakediag_')
os.makedirs(os.path.join(HOME, 'Documents'))
os.environ['HOME'] = HOME

import fake_ios as F          # noqa: E402
F.install()
sys.path.insert(0, os.path.join(HERE, '..', 'iphone'))
import ecg_patch_app as A     # noqa: E402
import ecg_diag_iphone as D   # noqa: E402
A._send = F.fake_send
A._cls = F.fake_cls
DOCS = os.path.join(HOME, 'Documents')
fails = []


def check(name, cond, info=''):
    print('%s %s %s' % ('PASS' if cond else 'FAIL', name, info))
    if not cond:
        fails.append(name)


def drive(app, secs):
    for _ in range(int(secs / 0.05)):
        F.FT.advance(0.05)
        F.run_delays()
        F.AIR.tick()
        F.AIR.run()
        app.mgr.pump()
        app.mgr.tick()


# ---- S0（View.update）・S0D（ui.delay）：アプリのコードを使わない
for method in ('update', 'delay'):
    n0 = F.DELAY_CALLS[0]
    v, st = D.run_s0(block=False, method=method)
    v.frame = (0, 0, 390, 844)
    for _ in range(400):
        F.FT.advance(0.05)
        F.run_delays()
        time.sleep(0.0005)
    time.sleep(0.2)
    v.close()
    check('S0 %s probes ok' % method, st['ok'] >= 95 and st['bad'] == 0, st)
    check('S0 %s uses ui.delay only for delay' % method, (F.DELAY_CALLS[0] > n0) == (method == 'delay'))

for key, _, flags in [x for x in D.STAGES if isinstance(x[2], dict)]:
    A.DIAG.update(flags)
    A.DIAG['stage'] = key
    A.DIAG['probe'] = True
    F.CALLS.clear()
    A._DELEGATE_CLASS = None
    app = A.EcgPatchApp()
    app.worker = type('W', (), {'start': lambda self: None})()
    app.start()
    app.root.frame = (0, 0, 390, 844)
    drive(app, 65)
    app.root.presented = False
    m = app.mgr
    check(key + ' probes ok', m.probe_ok > 250 and m.bad_count == 0, (m.probe_ok, m.bad_count))
    check(key + ' BLE started only in S4', (F.CALLS['initWithDelegate:queue:options:'] > 0) == flags['ble'])
    check(key + ' ObjC class only S3/S4', (A._DELEGATE_CLASS is not None) == flags['objc_class'])
    check(key + ' verdict OK shown', 'OK' in app.root.boot.text.splitlines()[0], app.root.boot.text.splitlines()[0])
    app.shutdown()

# ---- 破損を注入：BLE を始めずに知らせる
A.DIAG.update(draw=True, objc_class=True, ble=True, probe=True, stage='S4')
F.CALLS.clear()
app = A.EcgPatchApp()
app.worker = type('W', (), {'start': lambda self: None})()
app.start()
app.root.frame = (0, 0, 390, 844)
app.mgr.q.put(((1, 2), 5))
drive(app, 5)
check('corruption detected', app.mgr.corrupted and app.mgr.bad_count == 1)
check('BLE not started after corruption', F.CALLS['initWithDelegate:queue:options:'] == 0)
check('NG shown', 'NG' in app.root.boot.text.splitlines()[0], app.root.boot.text.splitlines()[0])
tr = open(os.path.join(DOCS, 'ecg_patch_trace.txt'), encoding='utf-8').read()
check('trace CORRUPT line', 'CORRUPT BAD_ITEM tuple/int' in tr)
app.shutdown()
res = open(os.path.join(DOCS, 'ecg_diag_result.txt'), encoding='utf-8').read()
check('result file has all stages', all(' %s ' % k in res for k in ('S0', 'S0D', 'S1', 'S2', 'S3', 'S4')), res[-300:])
# ---- 版違い・ファイルなし：理由を残して止まる
import shutil                                             # noqa: E402
D.console.alert = lambda *a, **k: 1
orig_file = D.__file__
tmpd = tempfile.mkdtemp()
D.__file__ = os.path.join(tmpd, 'ecg_diag_iphone.py')
check('missing app file reported', D.load_app() is None and 'ecg_patch_app.py がありません' in
      open(os.path.join(DOCS, 'ecg_diag_result.txt'), encoding='utf-8').read())
shutil.copy(os.path.join(HERE, '..', 'iphone', 'ecg_patch_app.py'), os.path.join(tmpd, 'ecg_patch_app.py'))
src = open(os.path.join(tmpd, 'ecg_patch_app.py'), encoding='utf-8').read().replace(A.APP_VERSION, '2026-10-27 r1')
open(os.path.join(tmpd, 'ecg_patch_app.py'), 'w', encoding='utf-8').write(src)
check('old version reported', D.load_app() is None and '2026-10-27 r1' in
      open(os.path.join(DOCS, 'ecg_diag_result.txt'), encoding='utf-8').read())
D.__file__ = orig_file
sys.modules['ecg_patch_app'] = A
check('place check', A.check_place('/private/var/mobile/Library/Mobile Documents/com~apple~CloudDocs/Downloads/ecg_patch_app 3.py')
      and not A.check_place(os.path.join(DOCS, 'ecg_patch_app.py')))
print('\n%d failed' % len(fails))
sys.exit(1 if fails else 0)
