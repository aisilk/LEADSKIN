# -*- coding: utf-8 -*-
"""
PC での自動テスト（仕様書 5.5〜5.7 節・20.1 節）。

  python -m unittest discover -s tests -v

  単体試験（A03〜A19）と、iPhone アプリ（SessionManager）と パッチ模擬器（PatchCore）を
  MockTransport でつないだ結合試験（装着確認・異常通知・再送・結果コード・タイムアウト・
  中止・電源 OFF）を行う。iPad の模擬器と iPhone のアプリは同じコードを実 BLE で動かす。
"""
import os
import datetime
import json
import struct
import unittest
from array import array

from mock_link import A, E, Env


# =============================================================================
# 付録 A・A08・A09：データ定義・CRC・ページ・断片
# =============================================================================
class TestDataDefs(unittest.TestCase):
    def test_crc_and_sizes(self):                                   # A09-02
        self.assertEqual(A.crc16_ccitt_false(b'123456789'), 0x29B1)
        self.assertEqual(E.crc16_ccitt_false(b'123456789'), 0x29B1)
        self.assertEqual((A.HDR.size, A.EVREC.size, A.STAT.size, A.MFR.size, A.DBGINFO.size, A.EMUINFO.size),
                         (27, 32, 20, 14, 32, 16))

    def test_page_roundtrip(self):                                  # A09-01
        core = E.PatchCore(start_seq=5123)
        core.tick(4.01)
        b = core.page_bytes(5123)
        self.assertEqual(len(b), A.PAGE_USER)
        p = A.Page.from_bytes(b)
        self.assertEqual(p.seq, 5123)
        self.assertTrue(p.crc_ok)
        self.assertEqual(len(p.samples), 1024)
        self.assertTrue(p.flags & A.PF_TIME_UNSET)
        self.assertFalse(p.has_time)
        bad = bytearray(b)
        bad[10] ^= 1
        self.assertFalse(A.Page.from_bytes(bytes(bad)).crc_ok)

    def test_page_format_error(self):                               # A09-03
        core = E.PatchCore()
        core.tick(4.01)
        b = bytearray(core.page_bytes(5000))
        b[A.PAGE_BODY] = 0xA2
        with self.assertRaises(A.ProtocolError):
            A.Page.from_bytes(bytes(b))
        with self.assertRaises(A.ProtocolError):
            A.Page.from_bytes(bytes(b[:2000]))

    def _frags(self, core, seq, mtu):
        core.max_update = mtu - 3
        core.connected = True
        core.subs = {E.CH_STREAM}
        sent = []
        core.hooks = type('H', (E.Hooks,), {'notify': lambda s, u, d: sent.append(d) or True})()
        core._start_transfer([seq])
        core.pump()
        return sent

    def test_assembler_mtus(self):                                  # A08-01
        for mtu in (23, 185, 247):
            core = E.PatchCore()
            core.tick(4.01)
            frags = self._frags(core, 5000, mtu)
            asm = A.PageAssembler()
            out = [asm.feed(f) for f in frags]
            self.assertEqual(out[-1], core.page_bytes(5000), 'mtu %d' % mtu)
            self.assertTrue(all(o is None for o in out[:-1]))

    def test_assembler_gap_dup(self):                               # A08-02
        core = E.PatchCore()
        core.tick(4.01)
        frags = self._frags(core, 5000, 185)
        asm = A.PageAssembler()
        res = [asm.feed(f) for i, f in enumerate(frags) if i != 3]
        self.assertTrue(all(r is None for r in res))
        self.assertGreaterEqual(asm.stats['gaps'], 1)
        asm = A.PageAssembler()
        res = [asm.feed(f) for f in frags[:4] + [frags[3]] + frags[4:]]
        self.assertTrue(all(r is None for r in res))
        self.assertGreaterEqual(asm.stats['dups'], 1)

    def test_assembler_xfer_change(self):                           # A08-03
        core = E.PatchCore()
        core.tick(8.01)
        f1 = self._frags(core, 5000, 185)
        f2 = self._frags(core, 5001, 185)
        asm = A.PageAssembler()
        for f in f1[:5]:
            asm.feed(f)
        out = [asm.feed(f) for f in f2]
        self.assertEqual(out[-1], core.page_bytes(5001))
        self.assertEqual(asm.stats['xfer_change'], 1)

    def test_status_event_parse(self):
        core = E.PatchCore()
        core.tick(32.01)
        st = A.DevStatus.from_bytes(core.status_bytes())
        self.assertEqual((st.state, st.time_set, st.latest_page_seq), (2, 0, 5007))
        with self.assertRaises(A.ProtocolError):                    # A06-03
            A.DevStatus.from_bytes(b'\x00' * 19)
        core.start_measuring()
        e = core._create_event(E.EV_ARR, 7, 5006 * 1024 + 640, 435, 213)
        ev = A.EventRec.from_bytes(core.event_bytes(e))
        self.assertTrue(ev.crc_ok)
        self.assertEqual((ev.event_seq, ev.page_seq, ev.sample_ofs, ev.wave_start, ev.wave_pages), (1, 5006, 640, 5002, 9))
        e2 = core._create_event(E.EV_ARR, 7, 5001 * 1024, 435, 213)    # 記録開始の直後：前は送れない
        self.assertEqual((e2['wave_start'], e2['wave_pages']), (5000, 5))
        self.assertEqual(ev.title, '不整脈の疑い（休止）')
        self.assertEqual(ev.rr_ms, 435 * 1000 // 256)

    def test_commands(self):                                        # A05-01
        import datetime
        now = datetime.datetime(2026, 11, 21, 10, 15, 3)
        self.assertEqual(A.Command.set_time(now).to_bytes(), bytes([0x01, 26, 11, 21, 10, 15, 3]))
        self.assertEqual(A.Command.req_event(12).to_bytes(), b'\x20\x0c\x00\x00\x00')
        self.assertEqual(A.Command.req_pages(5121, 2).to_bytes(), b'\x30\x01\x14\x00\x00\x02\x00')
        self.assertEqual(A.Command.ack_setup(True).to_bytes(), b'\x11\x00')
        self.assertEqual(A.Command.set_patient_id('P01').to_bytes(), b'\x02\x03P01')
        self.assertEqual(A.Command.stop().to_bytes(), b'\x40')


# =============================================================================
# A03：アドバタイズ
# =============================================================================
class TestAdv(unittest.TestCase):
    def test_mfr(self):                                             # A03-01
        mfr = struct.pack('<HIBBIBB', 0xFFFF, 0xA1B2C3D4, 1, 0, 0, 90, 0x08)
        a = A.parse_adv('p', mfr, None, -50, 0, False)
        self.assertEqual((a.device_id, a.event, a.battery, a.status), ('A1B2C3D4', 1, 90, 0x08))
        self.assertIsNone(A.parse_adv('p', struct.pack('<HIBBIBB', 0x004C, 1, 1, 0, 0, 90, 0), None, -50, 0, False))

    def test_short_mfr(self):                                       # A03-04
        with self.assertRaises(A.ProtocolError):
            A.parse_adv('p', b'\xff\xff\x01', None, -50, 0, False)

    def test_emulator_name(self):
        core = E.PatchCore(device_id=0x0F3E2D1C)
        name = core.adv_name_now()
        self.assertEqual(name, 'ECGP-0F3E2D1C~0100005A')        # event 01・sub 00・status 00・電池 90%
        self.assertIsNone(A.parse_adv('p', None, name, -50, 0, False))     # accept_emulator OFF
        a = A.parse_adv('p', None, name, -50, 0, True)
        self.assertEqual((a.device_id, a.event, a.battery, a.emulator), ('0F3E2D1C', 1, 90, True))

    def test_expire_and_auto(self):                                 # A03-02・A03-03
        clock = A.FakeClock()
        st = A.Settings(None)
        sc = A.Scanner(st, clock)
        a = A.AdvInfo('p1', 'A1B2C3D4', 'n', A.EV_ARR, 7, 0, 90, 0x02, -50, 0.0)
        sc.on_adv(a)
        self.assertIsNone(sc.auto_target())                         # 未登録は自動接続しない
        st['registered'] = [{'device_id': 'A1B2C3D4'}]
        self.assertEqual(sc.auto_target().peer, 'p1')
        st['auto_connect_alert'] = False
        self.assertEqual(sc.auto_target().peer, 'p1')               # 未通知ビットで接続
        st['auto_fetch_unnotified'] = False
        self.assertIsNone(sc.auto_target())
        clock.advance(10.5)
        self.assertEqual(sc.devices(), [])


# =============================================================================
# A10：時刻補完・切り出し
# =============================================================================
def make_pages(n=10, start=5000, leadoff=(), time_from=None):
    core = E.PatchCore(start_seq=start)
    pages = {}
    for i in range(n):
        core.electrode_off = (start + i) in leadoff
        if time_from is not None and start + i == time_from:
            import datetime
            core._set_time(datetime.datetime(2026, 11, 21, 10, 0, 0))
            core.page_t0 = core.now
        core.now += 4.0
        core.tick(core.now)
        pages[start + i] = A.Page.from_bytes(core.page_bytes(start + i))
    return pages


class TestCutter(unittest.TestCase):
    def test_timefix(self):                                         # A10-01
        import datetime
        pages = make_pages(6, time_from=5003)
        t = A.fix_times(pages, 5003, datetime.datetime(2026, 11, 21, 10, 1))
        self.assertEqual(t[5003][0], datetime.datetime(2026, 11, 21, 10, 0, 0))
        self.assertEqual(t[5000][0], datetime.datetime(2026, 11, 21, 9, 59, 48))   # 3 ページ前＝12 秒前
        self.assertFalse(t[5000][1])
        self.assertEqual(t[5005][0], datetime.datetime(2026, 11, 21, 10, 0, 8))

    def test_timefix_midnight(self):
        import datetime
        pages = make_pages(3, time_from=5002)
        p = pages[5002]
        p.ts = (2026, 1, 1, 0, 0, 2)
        t = A.fix_times(pages, 5002, datetime.datetime(2026, 1, 1))
        self.assertEqual(t[5000][0], datetime.datetime(2025, 12, 31, 23, 59, 54))

    def test_setup_window(self):                                    # A10-02
        pages = make_pages(14, leadoff={5004})
        times = A.fix_times(pages, 0, __import__('datetime').datetime(2026, 11, 21))
        rec = A.Cutter.setup_window(pages, times)
        self.assertIsNotNone(rec)
        self.assertEqual(len(rec.ecg), 7680)
        self.assertEqual((rec.meta[0][0], rec.meta[0][2]), (5006, 4))   # 最新の連続 60 周期
        self.assertEqual(rec.meta[-1][0], 5013)
        self.assertTrue(all(r[3] == 0 for r in rec.meta))
        self.assertEqual(len(rec.meta), 60)
        self.assertEqual(list(rec.ecg[:128]), list(pages[5006].samples[512:640]))

    def test_setup_window_none_and_gap(self):                       # A10-03
        pages = make_pages(14, leadoff={5004})
        del pages[5009]
        times = A.fix_times(pages, 0, __import__('datetime').datetime(2026, 11, 21))
        self.assertIsNone(A.Cutter.setup_window(pages, times))

    def test_event_window(self):                                    # A10-04
        pages = make_pages(12)
        times = A.fix_times(pages, 0, __import__('datetime').datetime(2026, 11, 21))
        rec = A.Cutter.event_window(pages, times, 5005, 640)
        self.assertEqual(len(rec.ecg), 7680)
        self.assertTrue(3840 <= rec.detect_index < 3840 + 128)
        rec2 = A.Cutter.event_window(pages, times, 5000, 100)       # 先頭付近：範囲外は含めない
        self.assertLess(len(rec2.ecg), 7680)
        self.assertEqual(rec2.pages[0], 5000)


# =============================================================================
# A11・A12・A14・A16〜A19・ST：保存・解析・描画計算・エラー・モニタ・状態表示
# =============================================================================
class TestPureBlocks(unittest.TestCase):
    def test_exporter(self):                                        # A11-01〜04
        env = Env()
        pages = make_pages(9)
        times = A.fix_times(pages, 0, env.clock.local_datetime())
        rec = A.Cutter.setup_window(pages, times)
        ex = A.Exporter(env.ctx.store, env.clock, env.ctx.settings)
        raw = {s: b'\x00' * 2112 for s in rec.pages}
        p1 = ex.save(rec, 'A1B2C3D4', raw)
        p2 = ex.save(rec, 'A1B2C3D4', raw)
        self.assertTrue(p2['name'].endswith('_2'))
        lines = open(p1['csv'], encoding='utf-8').read().splitlines()
        self.assertEqual(lines[:2], ['ECG', '256'])
        self.assertEqual(len(lines), 7682)
        meta = open(p1['meta'], encoding='utf-8').read().splitlines()
        self.assertEqual(meta[0], 'page,time,cycle,electrode,accel,tilt_x,tilt_y,tilt_z,arr')
        self.assertEqual(len(meta), 61)
        self.assertEqual(os.path.getsize(p1['pages']) % 2112, 0)
        env.ctx.store.fail_next = True
        with self.assertRaises(A.StorageError):
            ex.save(rec, 'A1B2C3D4', raw)
        self.assertEqual([f for f in env.files() if f.endswith('.tmp')], [])
        x, fs, m, info = A.load_ecg_file(p1['csv'])                 # A11-05 相当：読み直し
        self.assertEqual((len(x), fs, len(m)), (7680, 256, 60))

    def test_no_app_judgement(self):                                # r10：本体 No.29（アプリは R 波検出・判定をしない）
        an = A.Analyzer.analyze(A.synth_ecg(30, 72, False, seed=3))
        self.assertEqual((an['beats'], an['hr'], an['verdict'], an['n_arr']), ([], None, '—', 0))
        an = A.Analyzer.analyze(A.synth_ecg(30, 72, True, seed=5))     # 波形からはがれを推定しない（meta だけ）
        self.assertEqual(an['leadoff'], [])
        meta = [(5000, '', k, 1 if 10 <= k < 14 else 0, 0, 0, 0, 0, 0) for k in range(60)]
        self.assertEqual(A.Analyzer.analyze(A.synth_ecg(30), meta)['leadoff'], [(1280, 1792)])
        ev = {'event_code': 3, 'sub_code': 7, 'during_motion': False, 'rr_ms': 1650, 'median_ms': 830, 'hr_bpm': 70,
              'time': '2026-11-21 10:20:00'}
        an = A.Analyzer.analyze(A.synth_ecg(30), None, 256, ev)
        self.assertEqual((an['verdict'], an['hr'], an['n_arr']), ('不整脈の疑い（休止）', 70, 1))
        self.assertFalse(hasattr(A.Analyzer, 'detect_r'))

    def test_geometry(self):                                        # A14-01〜03
        g = A.WaveGeometry()
        lay = g.layout(852, 393)
        self.assertEqual(lay.panel[2], 220)
        self.assertEqual(lay.header[3], 44)
        x = A.synth_ecg(30)
        env = g.envelope(x, (0, 0, 600, 200), base=0, scale=0.1)
        self.assertEqual(len(env), 600)
        meta = [(0, '', 0, 0, 1, 0, 0, 0, 0), (0, '', 1, 1, 2, 0, 0, 0, 0), (0, '', 2, 1, 0, 0, 0, 0, 0),
                (0, '', 3, 0, 2, 0, 0, 0, 1), (0, '', 4, 0, 1, 0, 0, 0, 1)]
        self.assertEqual(g.bands(meta), [(0, 1, 'motion'), (1, 3, 'leadoff'), (3, 4, 'shock'), (4, 5, 'motion')])
        self.assertEqual(g.arr_bands(meta), [(3, 5)])

    def test_error_guard(self):                                     # A16-01・02
        tr = A.Trace(None)
        h = A.ErrorHandler(tr)
        self.assertEqual(h.handle(A.CrcError('x')), 'retry')
        self.assertEqual(h.handle(A.StorageError('x')), 'abort_session')
        self.assertEqual(h.handle(A.ConnectError('x')), 'restart_scan')

        @A.guard
        def boom():
            raise RuntimeError('delegate')
        boom()
        self.assertTrue(any('delegate' in ln for ln in tr.lines))

    def test_error_handler_no_file_io(self):                        # r1 実機の fault：format_exc の中で落ちた
        import linecache
        tr = A.Trace(None)
        h = A.ErrorHandler(tr)
        orig = linecache.getline, linecache.updatecache
        linecache.getline = linecache.updatecache = lambda *a, **k: (_ for _ in ()).throw(AssertionError('file read'))
        try:
            try:
                raise TypeError("'tuple' object is not callable")
            except TypeError as e:
                h.handle(e, where='pump')
        finally:
            linecache.getline, linecache.updatecache = orig
        self.assertTrue(any(ln.split(' ', 1)[1].startswith('TB test_ecg_patch.py:') for ln in tr.lines), list(tr.lines))

    def test_pump_bad_item(self):                                   # キューに関数以外：記録して続行
        env = Env()
        env.mgr.q.put(((1, 2), ()))
        env.mgr.post(env.mgr.ui_event, 'after')
        env.mgr.pump()
        self.assertIn(('after',), list(env.mgr.ui_events))
        self.assertTrue(any('BAD_ITEM tuple' in ln for ln in env.ctx.trace.lines))

    def test_no_forbidden_ui_calls(self):                           # r5・r6 実機の結果から
        here = os.path.dirname(os.path.abspath(__file__))
        for rel in ('../iphone/ecg_patch_app.py', '../ipad/patch_emu.py'):
            src = open(os.path.join(here, rel), encoding='utf-8').read()
            self.assertNotIn('ui.delay(', src, rel)                 # S0D：ui.delay の繰返しでメモリ破損
            self.assertNotIn('@ui.in_background', src, rel)         # スクリプトのスレッドが止まっていて実行されない

    def test_settings_broken(self):                                 # A01-02
        import tempfile
        d = tempfile.mkdtemp()
        p = os.path.join(d, 's.json')
        open(p, 'w').write('{broken')
        st = A.Settings(p).load()
        self.assertIsNotNone(st.load_error)
        self.assertFalse(st['accept_emulator'])

    def test_monitor(self):                                         # A18-01・02・05
        clock = A.FakeClock()
        m = A.BleMonitor(clock)
        m.on_ble_event(A.BleEvent(0.0, 'op', 'connect'))
        m.on_ble_event(A.BleEvent(0.152, 'cb', 'didConnect'))
        self.assertEqual(m.snapshot()['connect_ms'], 152)
        for i in range(75):
            m.on_ble_event(A.BleEvent(1.0 + i / 75.0, 'rx', 'notify', ch='ECG', data=b'\x00' * 244))
        m.on_ble_event(A.BleEvent(2.01, 'rx', 'notify', ch='ECG', data=b'\x00' * 244))
        self.assertAlmostEqual(m.snapshot()['rate_kbs'], 18.3, delta=0.2)
        m.on_ble_event(A.BleEvent(3.0, 'cb', 'didDisconnect', err=('CBErrorDomain', 7, 'peripheralDisconnected')))
        self.assertTrue(any('想定外' in w[2] for w in m.snapshot()['warnings']))

    def test_trace_recorder_mask(self):                             # A19-01・02
        env = Env()
        rec = env.ctx.recorder
        rec.on_ble_event(A.BleEvent(1.0, 'tx', 'write', ch='CTL', data=b'\x02\x03P01'))
        rec.on_ble_event(A.BleEvent(1.1, 'cb', 'didDisconnect', err=('CBErrorDomain', 7, 'peripheralDisconnected')))
        rec.flush()
        lines = [json.loads(ln) for ln in open(env.ctx.store.path(rec.relpath), encoding='utf-8')]
        w = [d for d in lines if d['ev'] == 'write' and d.get('ch') == 'CTL'][-1]
        self.assertEqual(w['hex'], '02 **')
        self.assertEqual(lines[-1]['err']['code'], 7)

    def test_status_panel_steps(self):                              # ST-01・ST-02・ST-05・ST-07
        clock = A.FakeClock()
        sp = A.StatusPanelPresenter(clock)
        sp.on_event('bt_state', state='poweredOff')
        self.assertIsNone(sp.view_model().percent)
        sp.on_event('bt_state', state='poweredOn')
        self.assertEqual(sp.view_model().head, '待機中（パッチを探しています）')
        sp.on_event('rx_start', mode='setup', pages=8)
        n0 = len(sp.console)
        for p in range(0, 101):
            sp.on_event('progress', pct=p)
        added = sp.console[n0:]
        self.assertEqual(added, [' 受信中… 20%', ' 受信中… 40%', ' 受信中… 60%', ' 受信中… 80%'])
        sp.on_event('done', count=1)
        self.assertEqual(sp.view_model().dot, 'green')
        clock.advance(3.1)
        sp.tick(True)
        self.assertEqual(sp.view_model().head, '待機中（パッチを探しています）')

    def test_router(self):                                          # SC4-01・ALL-01（抜粋）
        r = A.ScreenRouter()
        r.on_event('init_done')
        self.assertEqual(r.current, 'SC2')
        r.on_event('patch_selected')
        r.on_orientation('landscape')
        self.assertEqual(r.current, 'SC4')
        r.on_orientation('portrait')
        self.assertEqual(r.current, 'SC3')
        r.on_event('alert')
        self.assertEqual(r.current, 'SC5')
        r.on_event('monitor')
        r.on_event('close')
        self.assertEqual(r.current, 'SC5')
        r.on_event('bogus')
        self.assertTrue(r.ignored)


# =============================================================================
# 結合：アプリ（SessionManager）× 模擬器（PatchCore）
# =============================================================================
class TestSetupSession(unittest.TestCase):
    def test_setup_completes_and_acks(self):                        # A13-01・SC3-02
        env = Env()
        env.run(40)                                                 # 10 ページ記録
        env.connect(patient_id='P0001')
        ok = env.run(30, until=lambda: env.core.state == E.S3 and env.mgr.state == 'SCANNING')
        self.assertTrue(ok, env.mgr.status_p.console[-6:])
        self.assertEqual(env.core.commands[:4], [0x01, 0x02, 0x10, 0x11])
        self.assertEqual(env.core.patient_id, 'P0001')
        csv = env.files('_setup30s.csv')
        self.assertEqual(len(csv), 1)
        self.assertEqual(len(open(csv[0]).read().splitlines()), 7682)
        meta = open(csv[0][:-4] + '_meta.csv').read().splitlines()
        self.assertEqual(len(meta), 61)
        self.assertFalse(meta[1].split(',')[1].startswith('~'))       # 基準ページの時刻で補完
        self.assertTrue(env.ctx.settings.is_registered('A1B2C3D4'))
        for k in ('C02', 'C03', 'C04', 'C05', 'C06', 'C07', 'C13', 'C15'):
            self.assertEqual(env.core.checks.items[k][0], 'OK', k)
        logs = env.files('.log')
        self.assertTrue(logs)
        self.assertNotIn('P0001', open(logs[0], encoding='utf-8').read())   # 19 章：患者 ID を記録しない
        self.assertIn('ACK_SETUP', open(logs[0], encoding='utf-8').read())

    def test_setup_electrode_ng_then_ok(self):                      # A13-02（途中で OK）
        env = Env()
        env.core.set_electrode(True)
        env.run(20)
        env.connect()
        env.run(12)
        self.assertEqual(env.mgr.state, 'SETUP')
        self.assertIn('電極 NG：貼付を確認してください', env.mgr.status_p.console)
        env.core.set_electrode(False)
        ok = env.run(60, until=lambda: env.core.state == E.S3 and env.mgr.state == 'SCANNING')
        self.assertTrue(ok)
        meta = open(env.files('_setup30s_meta.csv')[0]).read().splitlines()[1:]
        self.assertTrue(all(r.split(',')[3] == '0' for r in meta))

    def test_setup_timeout_prompt(self):                            # A13-02（120 秒）
        env = Env()
        env.core.set_electrode(True)
        env.run(8)
        env.connect()
        env.run(125)
        self.assertIn(('setup_timeout',), list(env.mgr.ui_events))
        self.assertNotIn(0x11, env.core.commands)

    def test_time_already(self):                                    # A13-03
        env = Env()
        env.core._set_time(env.clock.local_datetime())
        env.run(40)
        env.connect()
        env.run(30, until=lambda: env.core.state == E.S3 and env.mgr.state == 'SCANNING')
        self.assertNotIn(0x01, env.core.commands)
        self.assertEqual(env.core.commands[:2], [0x10, 0x11])

    def _resend_case(self, fault):
        env = Env()
        env.core.faults[fault] = 1
        env.run(40)
        env.connect()
        ok = env.run(40, until=lambda: env.core.state == E.S3 and env.mgr.state == 'SCANNING')
        self.assertTrue(ok, env.mgr.status_p.console[-6:])
        self.assertIn(0x30, env.core.commands)
        self.assertEqual(env.core.checks.items['C12'][0], 'OK')
        return env

    def test_drop_fragment_resend(self):                            # A08-02 → REQ_PAGES
        env = self._resend_case('drop_frag')
        self.assertTrue(any('再送を要求しました' in ln for ln in env.mgr.status_p.console))

    def test_crc_resend(self):                                      # CrcError → REQ_PAGES
        env = self._resend_case('bad_crc')
        log = open(env.files('.log')[0], encoding='utf-8').read()
        self.assertIn('crc=NG', log)

    def test_busy_retry(self):                                      # A05-04
        env = Env()
        env.core.faults['busy'] = 2
        env.run(40)
        env.connect()
        env.run(40, until=lambda: env.core.state == E.S3 and env.mgr.state == 'SCANNING')
        self.assertEqual(env.core.commands.count(0x01), 3)
        self.assertEqual(env.core.state, E.S3)

    def test_mtu23(self):
        env = Env(mtu=23)
        env.run(40)
        env.connect()
        ok = env.run(60, until=lambda: env.core.state == E.S3 and env.mgr.state == 'SCANNING')
        self.assertTrue(ok)

    def test_user_stop(self):                                       # 12.1（中止）
        env = Env()
        env.core.set_electrode(True)
        env.run(12)
        env.connect()
        env.run(10)
        env.mgr.post(env.mgr.user_stop)
        env.run(5)
        self.assertIn(0x40, env.core.commands)
        self.assertEqual(env.core.state, E.S3)
        self.assertTrue(env.files('_partial.csv'))
        self.assertEqual(env.mgr.state, 'SCANNING')

    def test_power_off(self):                                       # A07-02・16.2
        env = Env()
        env.core.set_electrode(True)
        env.run(12)
        env.connect()
        env.run(10)
        env.core.power_off()
        env.run(5)
        self.assertTrue(env.files('_partial.csv'))
        self.assertIn('ECG パッチの電源が切れました', env.mgr.status_p.console)
        self.assertEqual(env.mgr.state, 'SCANNING')

    def test_connect_timeout(self):                                 # A04-02
        env = Env()
        env.tr.no_discover_reply = True
        env.run(5)
        env.connect()
        env.run(17)
        self.assertEqual(env.mgr.state, 'SCANNING')
        self.assertIn('disconnect', env.tr.ops)

    def test_id_mismatch(self):                                     # A04-03
        env = Env()
        env.tr.emu_id_override = 0x12345678
        env.run(5)
        env.connect()
        env.run(3)
        self.assertNotIn(0x10, env.core.commands)
        self.assertTrue(any('一致しません' in ln for ln in env.mgr.status_p.console))

    def test_missing_char(self):                                    # A04-04
        env = Env()
        env.tr.chars.discard(A.CH_STREAM)
        env.run(5)
        env.connect()
        env.run(3)
        self.assertTrue(any('特性が足りません' in ln for ln in env.mgr.status_p.console))

    def test_emulator_rejected_by_default(self):                    # 20.2（accept_emulator OFF）
        env = Env(accept_emulator=False)
        env.run(5)
        self.assertEqual(env.mgr.scanner.table, {})
        self.assertTrue(any('模擬器を検出しました' in ln for ln in env.mgr.status_p.console))


class TestEventSession(unittest.TestCase):
    def _measuring_env(self, time_set=True):
        env = Env()
        env.register()
        env.core.start_measuring(set_time=time_set)
        env.run(24)                                                 # 検知の 15 秒前まで記録がある状態
        return env

    def test_event_flow_waiting(self):                              # A13-04・05・SC5-04
        env = self._measuring_env()
        env.core.trigger_arrhythmia('pause')
        ok = env.run(80, until=lambda: env.core.events and env.core.events[0]['notified'] and env.mgr.state == 'SCANNING')
        self.assertTrue(ok, env.mgr.status_p.console[-6:])
        self.assertIn(0x20, env.core.commands)
        self.assertIn(0x21, env.core.commands)
        self.assertIn('WAITING', env.core.checks.items['C09'][1])
        for k in ('C08', 'C09', 'C10', 'C11', 'C13'):
            self.assertEqual(env.core.checks.items[k][0], 'OK', k)
        ej = json.load(open(env.files('_event.json')[0], encoding='utf-8'))
        self.assertEqual((ej['event'], ej['sub'], ej['event_seq']), ('arrhythmia', 'pause', 1))
        self.assertTrue(ej['acked_at'])
        self.assertEqual(ej['pages_missing'], [])
        self.assertEqual(ej['samples'], 7680)
        self.assertTrue(3840 <= ej['detect_index'] < 3840 + 128)
        a = env.mgr.alert
        self.assertEqual([s for s, _ in a['steps']], ['done'] * 5)
        self.assertTrue(a['can_view'])
        self.assertEqual(a['title'], '不整脈の疑い（休止）')
        x, fs, meta, info = A.load_ecg_file(env.files('_event_arr.csv')[0])
        an = A.Analyzer.analyze(x, meta, fs, info.get('event'))
        self.assertEqual(an['verdict'], '不整脈の疑い（休止）')             # パッチの判定を表示（r10）

    def test_two_events(self):                                      # A13-04・SC5-06
        env = self._measuring_env()
        env.core.trigger_arrhythmia('pause')
        env.run(6)
        env.core.trigger_arrhythmia('early')
        ok = env.run(120, until=lambda: len(env.core.events) == 2 and all(e['notified'] for e in env.core.events)
                     and env.mgr.state == 'SCANNING')
        self.assertTrue(ok, env.mgr.status_p.console[-6:])
        self.assertEqual(env.core.commands.count(0x21), 2)
        self.assertEqual(len(env.files('_event.json')), 2)

    def test_event_time_unset(self):                                # A13-06（N-02）
        env = self._measuring_env(time_set=False)
        env.core.trigger_arrhythmia('pause')
        env.run(80, until=lambda: env.core.events and env.core.events[0]['notified'])
        cmds = env.core.commands
        self.assertEqual(cmds.count(0x01), 1)
        self.assertLess(cmds.index(0x01), cmds.index(0x20))

    def test_electrode_event(self):
        env = self._measuring_env()
        env.core.trigger_electrode_event(6.0)
        ok = env.run(80, until=lambda: env.core.events and env.core.events[0]['notified'] and env.mgr.state == 'SCANNING')
        self.assertTrue(ok)
        self.assertTrue(env.files('_event_elec.csv'))
        meta = open(env.files('_event_elec_meta.csv')[0]).read().splitlines()[1:]
        self.assertTrue(any(r.split(',')[3] == '1' for r in meta))

    def test_unregistered_not_auto(self):                           # 9.1 節の表
        env = Env()
        env.core.start_measuring()
        env.run(12)
        env.core.trigger_arrhythmia('pause')
        env.run(30)
        self.assertNotIn(0x20, env.core.commands)
        rows = env.mgr.patch_rows()
        self.assertTrue(rows and rows[0][1] == '未登録パッチの異常通知')

    def test_disconnect_during_event(self):                         # A07-03・SC5-07
        env = self._measuring_env()
        env.core.wait_extra = 0
        env.core.trigger_arrhythmia('pause')
        env.run(40, until=lambda: env.mgr.session is not None and isinstance(env.mgr.session, A.EventSession)
                and len(env.mgr.session.pages) >= 3)
        env.core.power_off()
        env.run(3)
        self.assertNotIn(0x21, env.core.commands)
        self.assertFalse(env.core.events[0]['notified'])
        self.assertTrue(env.files('_event_arr.csv'))
        self.assertTrue(env.mgr.alert['error'])

    def test_long_waiting(self):                                    # A13-07・13.10.4
        env = self._measuring_env()
        env.core.trigger_arrhythmia('pause')
        env.run(3)
        env.core.wait_extra = 25.0
        ok = env.run(120, until=lambda: env.core.events and env.core.events[0]['notified'])
        self.assertTrue(ok)
        self.assertNotIn(0x30, env.core.commands)                   # WAITING 中は REQ_PAGES を送らない
        self.assertIn('記録待ちが 20 秒を超えました', env.mgr.status_p.console)

    def test_long_waiting_text_stays(self):                         # 実機試験 No.9（r6 は一瞬で戻った）
        env = self._measuring_env()
        env.core.trigger_arrhythmia('pause')
        env.run(3)
        env.core.wait_extra = 30.0
        env.run(60, until=lambda: env.mgr.session is not None and isinstance(env.mgr.session, A.EventSession)
                and env.mgr.session.waiting and env.mgr.session.wait_t0 is not None
                and env.clock.now() - env.mgr.session.wait_t0 > 21)
        texts = []
        for _ in range(10):                                        # 2 秒間、表示が保たれること
            env.run(0.2)
            texts.append(dict(env.mgr.alert['steps'][1:2] and [env.mgr.alert['steps'][1]]))
        steps = [env.mgr.alert['steps'][1]]
        self.assertEqual(steps[0][0], 'warn')
        self.assertTrue(steps[0][1].startswith('記録済みの範囲だけ受信します'), steps)
        self.assertTrue(all(list(t.values())[0].startswith('記録済みの範囲だけ受信します') for t in texts), texts)


class TestR8(unittest.TestCase):
    """r6 の実機データから見つけた改良（K1〜K7）"""

    def test_k1_summary_rate(self):
        env = Env()
        env.run(40)
        env.connect()
        env.run(30, until=lambda: env.core.state == E.S3 and env.mgr.state == 'SCANNING')
        rows = open(env.ctx.store.path('ECG_patch/trace/ble_summary.csv'), encoding='utf-8').read().splitlines()
        hdr, last = rows[0].split(','), rows[-1].split(',')
        rate = float(last[hdr.index('rate_kbs')])
        self.assertTrue(20 <= rate <= 200, rate)                  # MockTransport は約 58 KB/s の枠
        self.assertEqual(last[0], '2026-11-21 10:15:40')          # 接続を始めた時刻（切断の時刻ではない）

    def test_k2_set_time_on_second_boundary(self):
        env = Env()
        env.run(40.35)                                              # 秒の途中（0.35 秒）で接続を始める
        env.connect()
        env.run(10, until=lambda: 0x01 in env.core.commands)
        diff = abs((env.core.patch_clock() - env.core.wall()).total_seconds())
        self.assertLess(diff, 0.15, diff)                         # r7 までは最大 1 秒ずれた
        self.assertEqual(env.core.checks.items['C05'][0], 'OK')

    def test_k3_wait_5s_after_last_ack(self):
        env = Env()
        env.register()
        env.core.start_measuring()
        env.run(24)
        env.core.trigger_arrhythmia('pause')
        env.run(80, until=lambda: env.core.events and env.core.events[0]['notified'])
        t_ack = env.clock.now()
        env.run(10, until=lambda: env.mgr.state == 'SCANNING')
        self.assertTrue(4.5 <= env.clock.now() - t_ack < 6.0)     # r10：本体 No.9・No.32 で 5 秒（r8・r9 の模擬器 1 秒はやめた）

    def test_k4_adv_and_list_logged(self):
        env = Env()
        env.run(3)                                                  # S2：装着確認の広告
        env.core.start_measuring()
        env.run(24)
        env.core.trigger_arrhythmia('pause')
        env.run(12)
        env.mgr.patch_rows()
        lines = '\n'.join(env.ctx.trace.lines)
        self.assertIn('accepted=True event=01', lines)
        self.assertIn('event=03 sub=07', lines)
        self.assertIn('LIST ECGP-A1B2C3D4 未登録パッチの異常通知（未登録）', lines)

    def test_k5_adv_trace_throttled(self):
        env = Env()
        env.run(20)                                                 # 広告は 0.1 秒ごと（200 回）
        rec = env.ctx.recorder
        n = sum(1 for d in rec.ring if d['ev'] == 'adv')
        self.assertLessEqual(n, 25, n)
        self.assertGreaterEqual(n, 15, n)
        iv = env.ctx.monitor.adv_interval_ms('A1B2C3D4')
        self.assertTrue(80 <= iv <= 120, iv)

    def test_k7_electrode_bits_per_cycle(self):
        core = E.PatchCore()
        core.tick(1.0)
        core.trigger_electrode_event(2.0)                            # 1.0〜3.0 秒 → page 5000 の周期 2〜5
        core.tick(4.01)
        p = A.Page.from_bytes(core.page_bytes(5000))
        self.assertEqual([p.cycle_electrode(k) for k in range(8)], [0, 0, 1, 1, 1, 1, 0, 0])
        core.tick(4.6)
        core.set_electrode(True)                                    # 4.6 秒から：page 5001（4.0〜8.0 秒）の周期 1 以降
        core.tick(8.02)
        p = A.Page.from_bytes(core.page_bytes(5001))
        self.assertEqual([p.cycle_electrode(k) for k in range(8)], [0, 1, 1, 1, 1, 1, 1, 1])
        core.tick(9.3)
        core.set_electrode(False)                                   # 9.3 秒まで：page 5002（8.0〜12.0 秒）の周期 0〜2
        core.tick(12.03)
        p = A.Page.from_bytes(core.page_bytes(5002))
        self.assertEqual([p.cycle_electrode(k) for k in range(8)], [1, 1, 1, 0, 0, 0, 0, 0])


class TestR9(unittest.TestCase):
    """r8 の実機試験（2026-10-30）で見つかった不具合"""

    def _event_without_time(self, frac=0.35):
        env = Env()
        env.clock.base += datetime.timedelta(seconds=frac)          # 接続が秒の途中になるようにずらす
        env.register()
        env.core.start_measuring(set_time=False)                   # 「計測開始（時刻なし）」
        env.run(24)
        n0 = len(env.core.commands)
        env.core.trigger_arrhythmia('pause')
        ok = env.run(80, until=lambda: env.core.events and env.core.events[0]['notified'])
        return env, env.core.commands[n0:], ok

    def test_no11_set_time_before_req_event(self):
        for frac in (0.05, 0.35, 0.8):                              # 秒のどこで Indicate が来ても
            env, cmds, ok = self._event_without_time(frac)
            self.assertTrue(ok, frac)
            self.assertIn(0x20, cmds)
            self.assertEqual(cmds.count(0x01), 1, (frac, cmds))
            self.assertLess(cmds.index(0x01), cmds.index(0x20), (frac, cmds))   # r8 は REQ_EVENT_WAVE が先
            diff = abs((env.core.patch_clock() - env.core.wall()).total_seconds())
            self.assertLess(diff, 0.15, (frac, diff))               # r8 は約 13 秒古い時刻を送った

    def test_set_time_busy_retry_fresh_time(self):
        env = Env()
        env.run(40.35)
        env.core.faults['busy'] = 2
        env.connect()
        env.run(15, until=lambda: env.core.state == E.S2 and env.core.commands.count(0x01) >= 3 and 0x02 in env.core.commands)
        self.assertGreaterEqual(env.core.commands.count(0x01), 3)
        diff = abs((env.core.patch_clock() - env.core.wall()).total_seconds())
        self.assertLess(diff, 0.15, diff)                           # r8 は再送でも最初の時刻（2 秒古い）を送った

    def test_real_patch_keeps_5s_after_last_ack(self):
        env = Env()
        env.register()
        env.core.start_measuring()
        env.run(24)
        env.core.trigger_arrhythmia('pause')
        env.run(20, until=lambda: env.mgr.conn is not None)
        env.mgr.conn.info.emulator = False                          # 本物のパッチとして扱う
        env.run(80, until=lambda: env.core.events and env.core.events[0]['notified'])
        t_ack = env.clock.now()
        env.run(10, until=lambda: env.mgr.state == 'SCANNING')
        self.assertGreaterEqual(env.clock.now() - t_ack, 4.5)      # 1 秒は模擬器だけ（藤田さんのメモ）

    def test_bottom_status_shows_remaining(self):
        env = Env()
        env.register()
        env.core.start_measuring()
        env.run(24)
        env.core.trigger_arrhythmia('pause')
        env.run(30, until=lambda: isinstance(env.mgr.session, A.EventSession) and env.mgr.session.waiting)
        heads = set()
        for _ in range(40):
            env.run(0.25)
            heads.add(env.mgr.status_p.head)
        self.assertGreaterEqual(len([h for h in heads if h.startswith('記録待ち（残り')]), 3, heads)

    def test_post_has_no_keyword_args(self):
        import re
        src = open(A.__file__, encoding='utf-8').read()
        bad = [m.group(0) for m in re.finditer(r'\.post\([^\n]*\b\w+=', src) if 'lambda' not in m.group(0)]
        self.assertEqual(bad, [])


class TestR10(unittest.TestCase):
    """本体仕様書 Rev1.2（No.44・No.26・No.52・7.7 節）と別紙 2（未決 6・8）への対応"""

    def _page(self):
        core = E.PatchCore(start_seq=5000)
        core.tick(4.01)
        return core.page_bytes(5000)

    def test_new_page_header_layout(self):
        b = self._page()
        H = A.PAGE_BODY
        self.assertEqual(struct.unpack_from('<H', b, H + 23)[0], 256)                     # fs（offset 23〜24）
        self.assertEqual(struct.unpack_from('<H', b, H + 25)[0], A.crc16_ccitt_false(b[:H + 25]))  # CRC（25〜26）
        p = A.Page.from_bytes(b)
        self.assertTrue(p.crc_ok)
        self.assertEqual(p.fs, 256)
        self.assertEqual(p.tilt, (A.TILT_NONE,) * 3)
        self.assertEqual(p.batt_text, '90%以上')

    def test_old_header_rejected(self):                              # 旧形式（CRC が offset 23）は受け付けない（仕様どおり）
        b = bytearray(self._page())
        H = A.PAGE_BODY
        b[H + 23:H + 27] = b'\xff\xff\xff\xff'
        struct.pack_into('<H', b, H + 23, A.crc16_ccitt_false(bytes(b[:H + 23])))
        self.assertFalse(A.Page.from_bytes(bytes(b)).crc_ok)

    def test_crc_covers_sampling_rate(self):
        b = bytearray(self._page())
        b[A.PAGE_BODY + 23] ^= 0x01                                   # fs を壊すと CRC NG（対象はスペア 0〜24）
        self.assertFalse(A.Page.from_bytes(bytes(b)).crc_ok)

    def test_session_logs_header_and_completes(self):
        env = Env()
        env.run(40)
        env.connect()
        ok = env.run(30, until=lambda: env.core.state == E.S3 and env.mgr.state == 'SCANNING')
        self.assertTrue(ok)
        log = ''.join(open(f, encoding='utf-8').read() for f in env.files('.log'))
        self.assertIn('page fs=256', log)
        meta = [f for f in env.files('_meta.csv')]
        self.assertEqual(len(open(meta[0], encoding='utf-8').read().splitlines()), 61)

    def test_adv_status_bits_appendix_b(self):
        core = E.PatchCore()
        core.set_electrode(True)
        st = core.adv_tuple()[2]
        self.assertEqual(st & A.ADV_ST_ELEC, A.ADV_ST_ELEC)          # bit0＝電極異常継続
        self.assertFalse(st & A.ADV_ST_TIMESET)
        info = A.parse_adv('p', None, 'ECGP-A1B2C3D4~0100%02X5A' % st, -40, 0, True)
        self.assertTrue(info.electrode_ng)
        self.assertFalse(info.time_set)
        mfr = A.MFR.pack(0xFFFF, 0xA1B2C3D6, 1, 0, 0, 90, 0x09)        # キット：電極 NG・記録中（0x09）
        info = A.parse_adv('p', mfr, 'ECGP-A1B2C3D6', -40, 0, False)
        self.assertTrue(info.electrode_ng)
        self.assertFalse(info.time_set)
        mfr = A.MFR.pack(0xFFFF, 0xA1B2C3D6, 3, 7, 0, 90, 0x0E)
        info = A.parse_adv('p', mfr, 'ECGP-A1B2C3D6', -40, 0, False)
        self.assertTrue(info.time_set and info.unnotified and not info.electrode_ng)

    def test_connect_while_electrode_ng(self):
        env = Env()
        env.core.set_electrode(True)
        env.run(10)
        rows = env.mgr.patch_rows()
        self.assertIn('電極 NG', rows[0][2])
        env.connect()
        ok = env.run(10, until=lambda: 0x10 in env.core.commands)
        self.assertTrue(ok, env.core.commands)

    def test_reconnect_with_button_after_setup(self):
        env = Env()
        env.run(40)
        env.connect()
        env.run(30, until=lambda: env.core.state == E.S3 and env.mgr.state == 'SCANNING')
        env.core.power_on_reset()                                     # キットの reset（S2 に戻る）
        env.run(3)
        n0 = env.core.commands.count(0x10)
        env.connect()                                                 # 15 秒の待ちの間でも「接続」で接続する
        ok = env.run(30, until=lambda: env.core.commands.count(0x10) > n0)
        self.assertTrue(ok)

    def test_debug_info_reread_after_1s(self):
        env = Env()
        env.run(40)
        env.connect()
        env.run(3, until=lambda: env.mgr.conn is not None and env.mgr.conn.ready)
        c_ = env.mgr.conn
        if 'DBG' not in [A.CH_SHORT.get(u) for u in c_.chars]:
            self.skipTest('MockTransport has no Debug Info')
        env.run(1.5)
        self.assertIsNone(c_.dbg_reread_at)


class TestFakeIos(unittest.TestCase):
    def test_smoke_fake_corebluetooth(self):
        """実際の CoreBluetoothTransport／PeripheralTransport・画面を偽 iOS 上で動かす（別プロセス）"""
        import subprocess
        import sys
        here = os.path.dirname(os.path.abspath(__file__))
        r = subprocess.run([sys.executable, '-W', 'ignore', os.path.join(here, 'smoke_fake_ios.py')],
                           capture_output=True, text=True, timeout=600)
        self.assertEqual(r.returncode, 0, r.stdout[-3000:] + r.stderr[-3000:])

    def test_diag_stages(self):
        """段階診断 S0〜S4 と、破損を検出したら BLE を始めないこと（別プロセス）"""
        import subprocess
        import sys
        here = os.path.dirname(os.path.abspath(__file__))
        r = subprocess.run([sys.executable, '-W', 'ignore', os.path.join(here, 'diag_fake_ios.py')],
                           capture_output=True, text=True, timeout=600)
        self.assertEqual(r.returncode, 0, r.stdout[-3000:] + r.stderr[-3000:])


if __name__ == '__main__':
    unittest.main()
