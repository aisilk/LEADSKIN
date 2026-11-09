# -*- coding: utf-8 -*-
"""
偽 iOS 環境（fake_ios）の上で、iPhone アプリ（ecg_patch_app.py）と パッチ模擬器（patch_emu.py）を
「Pythonista の分岐」のまま動かし、偽 CoreBluetooth の電波でつないで通信を確認する。

  python tests/smoke_fake_ios.py

  確認すること：
   ・CoreBluetoothTransport／PeripheralTransport のセレクタ名・型記号・デリゲート名
   ・装着確認（SET_TIME → SET_PATIENT_ID → REQ_SETUP_WAVE → 保存 → ACK_SETUP → 切断）
   ・異常通知（自動接続 → SC5 → WAITING → 受信 → 保存 → ACK_EVENT → 切断）
   ・電源 OFF（didModifyServices）
   ・画面（SC2／SC3／SC4／SC5／SC7）の描画処理と模擬器の操作画面
"""
import os
import sys
import glob
import json
import shutil
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = tempfile.mkdtemp(prefix='fakeios_')
os.makedirs(os.path.join(HOME, 'Documents'))
os.environ['HOME'] = HOME

import fake_ios as F          # noqa: E402
F.install()
sys.path.insert(0, os.path.join(HERE, '..', 'iphone'))
sys.path.insert(0, os.path.join(HERE, '..', 'ipad'))
import ecg_patch_app as A     # noqa: E402
import patch_emu as E         # noqa: E402

assert A.ON_PYTHONISTA and E.ON_PYTHONISTA
for _m in (A, E):              # 型固定 objc_msgSend・objc_getClass を検査付きの偽物に差し替える
    _m._send = F.fake_send
    _m._cls = F.fake_cls

DOCS = os.path.join(HOME, 'Documents')
results = []


def check(name, cond, info=''):
    results.append((name, bool(cond), info))
    print('%s %s %s' % ('PASS' if cond else 'FAIL', name, info))


import time as _time_mod
_real_sleep = _time_mod.sleep


def step(app, dt=0.05):
    _real_sleep(0.0003)           # ダイアログ用スレッド（in_thread）に実行の機会を与える
    F.FT.advance(dt)
    F.run_delays()                # 両方の ui.delay（app.ui_loop・模擬器 loop）
    for _ in range(4):
        F.AIR.tick() if _ == 0 else None
        F.AIR.run()
        emu.worker_step()         # 模擬器の作業スレッドの代わり
        app.ctx.transport.main_tick()
        app.mgr.pump()            # 作業スレッドの代わり
        app.mgr.tick()


def run(app, secs, until=None):
    for _ in range(int(secs / 0.05)):
        step(app)
        if until and until():
            return True
    return until is None


# ---- 模擬器（iPad）
emu = E.EmuView(threaded=False)
emu.present('fullscreen')
emu.frame = (0, 0, 1024, 1366)
emu.layout()
core = emu.core

# ---- iPhone アプリ
app = A.EcgPatchApp()
app.worker = type('W', (), {'start': lambda self: None})()
app.ctx.settings['accept_emulator'] = False        # 既定のまま：検出時の確認で ON にする（r6）
app.start()
app.root.frame = (0, 0, 390, 844)
app.t_start -= 3.0

run(app, 30)                                          # 記録 7 ページ・広告・スキャン
check('emulator accepted via prompt (r6)', app.ctx.settings['accept_emulator'] is True)
check('ADV trace line', 'ADV name=ECGP-A1B2C3D4~0100005A' in open(os.path.join(DOCS, 'ecg_patch_trace.txt'),
                                                                encoding='utf-8').read())
check('central poweredOn', app.mgr.bt_state == 'poweredOn', app.mgr.bt_state)
check('emulator advertising', core.adv_name and core.adv_name.startswith('ECGP-A1B2C3D4~01'), core.adv_name)
rows = app.mgr.patch_rows()
check('patch listed', rows and rows[0][0] == 'ECGP-A1B2C3D4', rows)
app.root.portrait.draw()
app.root.portrait.status.draw()
check('SC2 draw', 'データ未検出' in F.DRAWN['texts'])

# ---- 装着確認：行の「接続」をタップ
st = app.root.portrait.status
st.show(app.snap['status'])
st.draw()
(rx, ry, rw, rh), peer, btn = st.row_rects[0]


class Touch:
    location = (rx + rw - 30, ry + 15)


st.t_down = None
st.touch_began(Touch())
st.touch_ended(Touch())
ok = run(app, 40, until=lambda: core.state == E.S3 and app.mgr.state == 'SCANNING')
check('setup completed over fake CoreBluetooth', ok, app.mgr.status_p.console[-5:])
check('setup commands', core.commands[:4] == [0x01, 0x02, 0x10, 0x11], core.commands)
for k in ('C01', 'C02', 'C03', 'C04', 'C05', 'C06', 'C07', 'C13', 'C15'):
    check('emu check ' + k, core.checks.items[k][0] == 'OK', core.checks.items[k])
files = glob.glob(os.path.join(DOCS, 'ECG', '*', '*_setup30s.csv'))
check('setup csv saved', len(files) == 1, files)
check('registered', app.ctx.settings.is_registered('A1B2C3D4'))
check('app disconnected itself after ACK (9.6)', F.CALLS['cancelPeripheralConnection:'] >= 1)

# ---- 画面：SC3 → SC4（横向き）
run(app, 0.5)
check('SC3 loaded from BLE', app.loaded is not None and app.loaded.source == 'ble', app.screen)
th = [t for t in __import__('threading').enumerate() if t.name != 'MainThread']
for t in th:
    t.join(timeout=0.01)
import time as _t                                     # 解析スレッドの終了を待つ
for _ in range(200):
    if app.loaded.analysis is not None:
        break
    _t.sleep(0.01) if hasattr(_t, 'sleep') else None
check('display summary done (no app judgement, r10)', app.loaded.analysis is not None and app.loaded.analysis.get('beats') == [], app.loaded.analysis)
F.DRAWN['texts'] = []
app.root.portrait.draw()
check('SC3 draw', 'データ検出' in F.DRAWN['texts'], [t for t in F.DRAWN['texts']][:4])
app.root.frame = (0, 0, 852, 393)
check('landscape → SC4', not app.root.wave.hidden)
app.root.wave.draw()
app.root.wave.mode = 'mixed'
app.root.wave.draw()
check('SC4 draw (both modes)', 'パッチの判定' in F.DRAWN['texts'])
app.root.frame = (0, 0, 390, 844)

# ---- SC7 BLE モニタ（全タブ）
app.open_monitor()
for i in range(5):
    app.root.monitor.seg.selected_index = i
    for f in range(4):
        app.root.monitor.filter.selected_index = f
        app.root.monitor.refresh()
app.root.monitor.filter.selected_index = 0
app.root.monitor.refresh()
check('SC7 log tab', len(app.root.monitor.tv.text.splitlines()) > 10, len(app.root.monitor.tv.text.splitlines()))
app.root.monitor.seg.selected_index = 0
app.root.monitor.refresh()
check('SC7 overview', 'ATT MTU     : 247' in app.root.monitor.tv.text, app.root.monitor.tv.text[:200])
app.close_monitor()

# ---- 異常通知：模擬器で不整脈（休止）
core.trigger_arrhythmia('pause')
ok = run(app, 90, until=lambda: core.events and core.events[0]['notified'] and app.mgr.state == 'SCANNING')
check('event completed over fake CoreBluetooth', ok, app.mgr.status_p.console[-5:])
check('SC5 shown', app.screen == 'SC5', app.screen)
check('alert sound + HUD', any(h.startswith('sound') for h in F.HUD) and '異常通知を受信しました' in F.HUD)
F.DRAWN['texts'] = []
app.root.portrait.draw()
check('SC5 draw', '● 異常通知' in F.DRAWN['texts'] and any('不整脈の疑い（休止）' == t for t in F.DRAWN['texts']))
for k in ('C08', 'C09', 'C10', 'C11'):
    check('emu check ' + k, core.checks.items[k][0] == 'OK', core.checks.items[k])
ej = glob.glob(os.path.join(DOCS, 'ECG', '*', '*_event.json'))
check('event json', len(ej) == 1 and json.load(open(ej[0]))['acked_at'], ej)
app.on_view_wave(None)
run(app, 0.3)
check('view wave → SC3 with detect line', app.loaded and app.loaded.detect_index > 0, app.loaded and app.loaded.detect_index)

# ---- 電源 OFF（再び S2 にして接続し、途中で電源 OFF）
core.power_on_reset()
core.set_electrode(True)
run(app, 10)
rows = app.mgr.patch_rows()
app.ask_connect(rows[0][4])
run(app, 6, until=lambda: app.mgr.state == 'SETUP')
run(app, 5)
core.power_off()
ok = run(app, 5, until=lambda: app.mgr.state == 'SCANNING')
check('power off detected (didModifyServices)', ok and 'ECG パッチの電源が切れました' in app.mgr.status_p.console,
      app.mgr.status_p.console[-3:])
check('partial saved', glob.glob(os.path.join(DOCS, 'ECG', '*', '*_partial.csv')))

# ---- 模擬器の画面・終了
emu.refresh()
check('emu checklist text', emu.checks.text.startswith('通信チェックリスト'), emu.checks.text.splitlines()[0])
check('no ui.in_background calls (r6)', not F.IN_BG_CALLS, F.IN_BG_CALLS)
app.on_close(None)                                    # 右上の ✖（受信中でないので確認なしで閉じる）
check('close button closes the app (r6)', not app.running and app.closed.is_set(), (app.in_session, app.running, app.mgr.state, F.IN_BG_CALLS))
emu.close()
check('trace written', os.path.exists(os.path.join(DOCS, 'ecg_patch_trace.txt')))
tr = glob.glob(os.path.join(DOCS, 'ECG_patch', 'trace', 'ble_trace_*.jsonl'))
check('ble trace jsonl', tr and all(json.loads(ln) for ln in open(tr[0]) if ln.strip()), tr)
check('ble_summary.csv', os.path.exists(os.path.join(DOCS, 'ECG_patch', 'trace', 'ble_summary.csv')))

check('no ui.delay in app / emulator (r5)', F.DELAY_CALLS[0] == 0, F.DELAY_CALLS[0])
check('periodic update() ran (app ui_loop)', app.probe_n > 100, app.probe_n)
check('no memory-corruption report', app.mgr.bad_count == 0 and app.mgr.probe_ok > 100, (app.mgr.probe_ok, app.mgr.bad_count))
check('no objc_util calls at runtime (r17 rule 2)', not F.RUNTIME_OBJC_UTIL, F.RUNTIME_OBJC_UTIL[:5])
check('no BLE operation inside delegates (r17 rule 5)', not F.VIOLATIONS, sorted(set(F.VIOLATIONS)))
check('app trace has BLE start steps', all(k in open(os.path.join(DOCS, 'ecg_patch_trace.txt'), encoding='utf-8').read()
                                           for k in ('BLE-1', 'BLE-2', 'BLE-3', 'CB> scan')))
check('emulator trace written', os.path.exists(os.path.join(DOCS, 'patch_emu_trace.txt')))
print()
print('selectors used:', ', '.join(sorted(k for k in F.CALLS if not k.startswith('delegate'))))
print('delegates called:', ', '.join(sorted(k[9:] for k in F.CALLS if k.startswith('delegate'))))
n_fail = sum(1 for r in results if not r[1])
print('\n%d checks, %d failed' % (len(results), n_fail))
shutil.rmtree(HOME, ignore_errors=True)
sys.exit(1 if n_fail else 0)
