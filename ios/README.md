# ECG Patch — iPhone アプリ（Swift／Xcode 版）

Pythonista 版 `ecg_patch/iphone/ecg_patch_app.py`（r10）を、Xcode で作る iPhone のネイティブアプリ（SwiftUI＋CoreBluetooth）に移したものです。

- 版：`2026-11-22 Swift r10`（画面上部と設定画面に表示）
- 対象：iPhone、iOS 17 以上（iPhone 16・iOS 27.2 で動かす想定）
- 準拠：本体仕様書 Rev1.2（案）、付属資料 A 別紙 2 Rev0.5（案）、iPhone アプリ仕様書 001-C Rev0.7（案）。Python 版 r10 と同じ動作

## Python 版 r10 から移したもの

| 区分 | 内容 |
|---|---|
| 通信 | 広告（patch 形式の Manufacturer データ、模擬器の名前形式）、付属 B の Status ビット、接続、サービス・特性の検出、購読、Device Status・Event Log・ECG Stream、Debug Info（接続 1 秒以降の値を使う） |
| コマンド | SET_TIME（秒の変わり目で送る、再送時は送る時点の時刻で作り直す）、SET_PATIENT_ID、REQ_SETUP_WAVE、ACK_SETUP、REQ_EVENT_WAVE（WAITING）、ACK_EVENT、REQ_PAGES、STOP。結果コードの処理（BUSY は 1 秒後に最大 3 回） |
| 受信 | 断片の連結、ページヘッダ（Rev1.2：fs＝offset 23〜24、CRC＝25〜26）、CRC 異常・欠落の再送要求、時刻の補完（「~」付きの推定） |
| 切り出し・保存 | 装着確認＝電極正常の連続 30 秒、異常通知＝検知位置の前後 15 秒。`_setup30s.csv`・`_event_arr.csv`・`_event_elec.csv`・`_partial.csv`・`_meta.csv`・`_event.json`・`_pages.bin`・`session_*.log`（Python 版と同じ形式・同じフォルダ構成） |
| 記録 | `ecg_patch_trace.txt`（前回分は `_prev`）、`ECG_patch/trace/ble_trace_*.jsonl`、`ble_summary.csv` |
| 画面 | 縦向き（一覧・下部の接続状態・電極帯）、横向き（波形・パッチの判定）、異常通知（SC5）、設定（SC6）、BLE モニタ（SC7：下部を 1 秒長押し、または右上のアンテナ）、保存ファイル |
| 判定 | アプリは R 波検出・判定をせず、パッチの判定（Event Log）だけを表示（本体 No.29） |
| 切断 | ACK の後、パッチからの切断を最大 5 秒待つ（本体 No.9・No.32。模擬器も 5 秒） |

保存先はアプリの「書類」フォルダです。**ファイル App の「この iPhone 内 > ECG Patch」**から見たり、Mac に送ったりできます（`ECG/日付/…`、`ECG_patch/trace/…`、`ecg_patch_trace.txt`、`ecg_patch_settings.json`）。

Pythonista 版にあった回避策（作業スレッド、ctypes の型固定 objc_msgSend、ui.delay を使わない定期処理など）は Pythonista 固有の問題への対策なので、Swift 版には入れていません。CoreBluetooth の通知はメインキューで受け、状態機械もメインスレッドで動かします。

## iPhone 16 に入れて試す手順

Mac と Xcode が必要です。iPhone の iOS（27.2）に対応した Xcode を使ってください（iOS 27.2 のシミュレータが動いている Xcode なら対応しています）。

1. **プロジェクトを開く**
   GitHub の `aisilk/LEADSKIN` のブランチ `ecg-patch-ios-swift` を Mac に取り込みます（`git clone` するか、GitHub の「Code > Download ZIP」）。`ios/ECGPatch.xcodeproj` をダブルクリックして Xcode で開きます。
2. **署名（初回だけ）**
   左のプロジェクト一覧で「ECGPatch」→ TARGETS「ECGPatch」→「Signing & Capabilities」を開きます。「Team」で自分の Apple ID（Personal Team で可）を選びます。「Bundle Identifier が使えない」と出たら、`com.aisilk.ecgpatch` の末尾に名前を足して（例：`com.aisilk.ecgpatch.okano`）変えます。
3. **iPhone を Mac につなぐ**
   USB で接続し、iPhone に出る「このコンピュータを信頼」で「信頼」を押します。
4. **デベロッパモードを ON にする（初回だけ）**
   iPhone の「設定 > プライバシーとセキュリティ > デベロッパモード」を ON にし、再起動後に「オンにする」を押します。
5. **実行**
   Xcode 上部の実行先で接続した iPhone 16 を選び、▶（⌘R）を押します。
6. **初回の起動で「信頼されていない開発元」と出たとき**
   iPhone の「設定 > 一般 > VPN とデバイス管理」で、自分の Apple ID の開発元を「信頼」します。
7. **Bluetooth の許可**
   起動時の Bluetooth の確認で「許可」を押します。

Personal Team（無料の Apple ID）で入れたアプリは 7 日で起動できなくなります。その場合は、もう一度 ▶ で入れ直してください。藤田さんなど他の人の iPhone に配るときは、Apple Developer Program（有料）に登録して **TestFlight** で配布するのが簡単です。

## パッチ模擬器（iPad）での試験

1. iPad では Python 版の `ecg_patch/ipad/patch_emu.py`（r10）を Pythonista で動かします（Swift 版の模擬器はありません）。
2. iPhone のアプリで「設定 > 模擬器を受け入れる」を ON にします（最初に模擬器を見つけたときの確認で「受け入れる」を押しても ON になります）。
3. 試験項目は `ecg_patch/README.md` の 4 章（No.1〜No.14）と同じです。ACK の後の切断は、模擬器では約 5 秒後に iPhone から行います。

シミュレータには Bluetooth がないため、シミュレータでは画面の確認と単体テストだけができます。BLE の試験は実機の iPhone で行ってください。

## 単体テスト

Xcode の「Product > Test」（⌘U）で `ECGPatchTests` を実行します（シミュレータでも動きます）。試験の内容は次のとおりです。

- CRC、新しいページヘッダ（fs を壊すと CRC NG、旧形式は CRC NG）
- 断片の連結と欠落
- 付属 B の Status ビット（キットの 0x09・模擬器の名前）
- コマンドのバイト列
- 装着確認の切り出し（7,682 行・61 行）と異常通知の切り出し（検知位置が中央）
- アプリが判定しないこと
- 模擬パッチを使った装着確認の通し試験（SET_TIME → SET_PATIENT_ID → REQ_SETUP_WAVE → 保存 → ACK_SETUP → パッチ側から切断 → 登録）

## 構成

```
ios/
├─ ECGPatch.xcodeproj
├─ ECGPatch/
│  ├─ App/        ECGPatchApp.swift（起動）、AppModel.swift（組立て・画面への橋渡し）
│  ├─ Core/       Protocol（データ形式）、Records（時刻補完・切り出し・保存）、Sessions（送信・装着確認・異常通知）、
│  │              SessionManager（状態機械）、Monitor（BLE モニタ・トレース）、Support（時刻・保存先・記録・設定）、Transport
│  ├─ BLE/        CoreBluetoothTransport.swift
│  └─ UI/         ContentView（縦・横）、WaveformView（心電図）、OtherViews（SC5・SC6・SC7・ファイル）
└─ ECGPatchTests/ CoreTests.swift
```

## 注意

- このプロジェクトは Linux 上で作成したため、**Xcode でのビルドはまだ確認していません**。最初のビルドでエラーが出たら、そのメッセージを知らせてください。
- 医療機器としての検証は行っていません。試験・評価用です。
