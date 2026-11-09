# -*- coding: utf-8 -*-
"""
ecg_diag_iphone.py  ―  iPhone アプリ（ecg_patch_app.py）の段階診断（r4）

r1・r3 は iPhone XS で、起動 3 秒後（作業スレッドへ最初の処理を渡した時点）に
Python のメモリ破損（関数が tuple に化ける）が起きた。r3 では BLE を呼ぶ前に起きている。
原因を推測で決めず、疑わしいものを 1 つずつ足して比べる（r17 レポート 4 章と同じ進め方）。

  使い方：ecg_patch_app.py と同じ「Pythonista 3」フォルダに置いて実行 → 段階を選ぶ →
          画面の判定（60 秒で OK／NG）を見る → 右上の × で閉じる → 次の段階へ。
  結果  ：ecg_diag_result.txt に 10 秒ごとに追記される（全段階分が 1 ファイルに残る）。
          落ちた場合も、最後の行と ecg_patch_trace_prev.txt で分かる。

  段階  S0 → S0D → S1 → S2 → S3 → S4 の順に、NG（または落ちる）になった段階を知らせてください。

  r4 の結果（iPhone XS）：アプリのコードを使わない S0（ui.delay で 0.2 秒ごとに予約し直す方式）でも
  破損が出た。r5 では定期処理を View の update()（update_interval）に変え、S0 をその方式にした。
  S0D は比較用の旧方式（ui.delay）。S0 が OK・S0D が NG なら ui.delay が原因と確定できる。
"""
import os
import sys
import time
import queue
import datetime
import threading

import ui
import dialogs
import console

DOCS = os.path.expanduser('~/Documents')
RESULT = os.path.join(DOCS, 'ecg_diag_result.txt')
DIAG_VERSION = '2026-11-09 r10'

STAGES = [
    ('S0', '基本：標準の画面部品＋作業スレッド、定期処理は View.update（r5 の方式）',
     'update'),
    ('S0D', '比較：S0 と同じで定期処理だけ ui.delay（r1〜r4 の方式。NG になる見込み）',
     'delay'),
    ('S1', 'アプリの画面（自作の描画なし）＋作業スレッド。ObjC クラスなし・BLE なし',
     dict(draw=False, objc_class=False, ble=False)),
    ('S2', 'S1 ＋ 自作の描画（波形・状態表示など）',
     dict(draw=True, objc_class=False, ble=False)),
    ('S3', 'S2 ＋ デリゲートの ObjC クラス作成（objc_util.create_objc_class）',
     dict(draw=True, objc_class=True, ble=False)),
    ('S4', 'S3 ＋ BLE 開始（通常の起動と同じ）',
     dict(draw=True, objc_class=True, ble=True)),
]


def append_result(line):
    with open(RESULT, 'a', encoding='utf-8') as f:
        f.write(line + '\n')
        f.flush()
        os.fsync(f.fileno())


def run_s0(block=True, method='update'):
    """アプリのコードを使わない基準：0.2 秒ごとに積んだ値を作業スレッドで確かめる。
    method='update'：View.update_interval（r5）、'delay'：ui.delay で予約し直す（r1〜r4）"""
    key = 'S0' if method == 'update' else 'S0D'
    q = queue.Queue()
    st = {'ok': 0, 'bad': 0, 'n': 0, 'running': True, 't0': time.time(), 'last': -1}
    closed = threading.Event()

    def probe(n, tag):
        if tag == ('probe', n, n * 7):
            st['ok'] += 1
        else:
            st['bad'] += 1

    def worker():
        while st['running']:
            try:
                item = q.get(timeout=0.05)
            except queue.Empty:
                continue
            fn, a = item if (type(item) is tuple and len(item) == 2) else (item, None)
            if not callable(fn) or type(a) is not tuple:
                st['bad'] += 1
                continue
            fn(*a)

    class V(ui.View):
        def __init__(self):
            self.background_color = 'white'
            self.tv = ui.TextView(editable=False, font=('Menlo', 14))
            self.add_subview(self.tv)
            self.started = False

        def layout(self):
            self.tv.frame = (10, 50, self.width - 20, self.height - 60)
            if not self.started:
                self.started = True
                if method == 'update':
                    self.update_interval = 0.2
                else:
                    ui.delay(self.loop, 0.2)

        def update(self):
            if method == 'update':
                self.loop()

        def loop(self):
            if not st['running']:
                return
            st['n'] += 1
            n = st['n']
            q.put((probe, (n, ('probe', n, n * 7))))
            el = time.time() - st['t0']
            verdict = 'NG' if st['bad'] else ('OK' if el >= 60 else '実行中')
            self.tv.text = '診断 %s   %s\n経過 %d 秒（60 秒で判定）\n検査 OK %d 回／破損 %d 回\n定期処理：%s' % (
                key, verdict, el, st['ok'], st['bad'], 'View.update' if method == 'update' else 'ui.delay')
            if int(el) // 10 != st['last']:
                st['last'] = int(el) // 10
                append_result('%s %s %s elapsed=%d probe_ok=%d bad=%d' % (
                    datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), DIAG_VERSION, key, el, st['ok'], st['bad']))
            if method == 'delay':
                ui.delay(self.loop, 0.2)

        def will_close(self):
            st['running'] = False
            closed.set()

    threading.Thread(target=worker, name='diag_worker', daemon=True).start()
    v = V()
    v.name = '診断 ' + key
    v.present('fullscreen')
    if block:
        closed.wait()
    return v, st


def load_app():
    """この診断と同じフォルダの ecg_patch_app.py を読む（名前が違う・版が違うときは理由を出して止める）"""
    import importlib.util
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, 'ecg_patch_app.py')
    if not os.path.exists(path):
        msg = '同じフォルダに ecg_patch_app.py がありません（%s）。名前に「 3」などが付いていないか確認してください。' % here
        append_result('ERROR ' + msg)
        console.alert('ファイルがありません', msg, 'OK', hide_cancel_button=True)
        return None
    spec = importlib.util.spec_from_file_location('ecg_patch_app', path)
    A = importlib.util.module_from_spec(spec)
    sys.modules['ecg_patch_app'] = A
    spec.loader.exec_module(A)
    if A.APP_VERSION.split()[-1] != DIAG_VERSION.split()[-1]:
        msg = '%s の版が %s です。%s を置いてください。' % (path, A.APP_VERSION, DIAG_VERSION)
        append_result('ERROR ' + msg)
        console.alert('版が違います', msg, 'OK', hide_cancel_button=True)
        return None
    return A


def main():
    here = os.path.abspath(__file__)
    if 'Mobile Documents' in here or 'CloudDocs' in here:
        append_result('WARN iCloud Drive から実行: %s' % here)
    items = ['%s  %s' % (k, t) for k, t, _ in STAGES]
    sel = dialogs.list_dialog('診断の段階（各 60 秒）', items)
    if not sel:
        return
    key = sel.split()[0]
    append_result('---- %s %s start %s' % (datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), DIAG_VERSION, key))
    flags = [f for k, _, f in STAGES if k == key][0]
    if isinstance(flags, str):
        run_s0(method=flags)
        return
    A = load_app()
    if A is None:
        return
    A.DIAG.update(flags)
    A.DIAG['stage'] = key
    A.DIAG['probe'] = True
    A.main()


if __name__ == '__main__':
    main()
