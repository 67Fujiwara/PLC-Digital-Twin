# PLC Digital Twin

PLC 実機 + PC + タッチパネルだけで、装置の動作を PC 上で確認するツール。
PLC の本番プログラムをそのまま動かし、PC が 10ms 周期で仮想 I/O を交換して機械の挙動（コンベヤ・シリンダ・センサ・軸）を計算し、
ブラウザにデバイスの ON/OFF と数値をリアルタイム表示する。実機に載せたときの修正をゼロに近づけることが目的。

```
[VT5 / GOT2000] ──Ethernet──┐
                            ├──[HUB]──[PLC 実機]  ← 本番プログラム + I/O 層 (TWIN_MODE)
[PC: Twin Core (Python)] ───┘    10ms 周期で DM ブロック一括 R/W
        │ WebSocket 30Hz
        ▼
[ブラウザ]  デバイス LED / 数値 / 波形 (10ms) / ライン図 / イベント / 仮想センサの手動操作
```

対応 PLC: キーエンス KV-8000 / KV-7000 / KV-Nano（上位リンク 8501）、三菱 MELSEC iQ-R（SLMP 3E バイナリ）。
依存は Python 3.8 以上と PyYAML だけ。ブラウザ側はビルド不要。

## 起動

```bash
pip install -r requirements.txt
python -m twin configs/demo_line.yaml          # モック PLC でデモ (実機不要)
python -m twin configs/keyence_kv8000.yaml     # KV-8000 実機 (IP は YAML で)
python -m twin configs/melsec_iqr.yaml         # iQ-R 実機
```

ブラウザが自動で `http://localhost:8080/` を開く（`--no-browser` で抑止、`--port` で変更）。
同じ LAN の他の PC からも `http://<PCのIP>:8080/` で閲覧できる。

設定の検証だけ: `python -m twin configs/xxx.yaml --check`

## 画面

| 領域 | 内容 |
|---|---|
| ヘッダ | PLC 接続状態、実周期、ジッタ (avg / p99 / max)、読み・計算・書きの所要 ms、配信 fps、遅延、記録秒数 |
| PLC → PC | PLC の仮想出力ビットを LED 表示、監視数値を 10 進 / hex / スパークライン表示。変化した瞬間に点滅 |
| PC → PLC | 仮想センサ。スイッチで **強制 ON/OFF**（挙動シミュレーションより優先、📌 表示）、ワードは値を入力して Enter |
| 監視デバイス | 任意デバイス（DM0〜, MR000〜, SD520… 100ms 周期）。画面の欄に `DM100 8` や `D0-D15` と打って追加・✕で削除・「保存」で次回起動にも反映。書込みも可能 |
| 表示設定 | ヘッダの「表示設定」で見たいデバイスだけにチェック。ブラウザごとに記憶 |
| ライン | コンベヤを上から見た 2D 図。ワーク（NG は赤）、センサ、ストッパ、カメラ視野、多連エアブローのノズル、シリンダ、軸。NG 排出 / 誤排出 / 見逃しを集計表示 |
| 波形 | ロジアナ風。デバイスをクリックして追加。10ms 分解能、2〜120 秒幅、クリックで停止、ホバーで値読み |
| イベント | 全ビットの立上り▲ / 立下り▽ を時刻 (ms) 付きで記録。数値変化も記録可 |
| CSV 保存 | 記録バッファ（既定 120 秒、`record_seconds`）を全タグ + 生ワードで保存。実機の記録と比較できる |

## 装置を定義する（YAML だけ）

```yaml
plc:
  type: keyence                   # keyence / melsec / mock
  ip: 192.168.0.10
  port: 8501
  out: {dev: DM, addr: 1000, words: 64}   # PLC → PC
  in:  {dev: DM, addr: 1100, words: 64}   # PC → PLC
cycle_ms: 10
watch:
  - {dev: DM, addr: 0, words: 8}
  - {dev: MR, addr: 0, words: 2}
tags:
  out:
    - {name: CONV1_RUN, bit: "0.0", comment: 供給コンベヤ運転}
    - {name: AX1_TARGET, word: 10, type: s32}
  in:
    - {name: PE1, bit: "0.2"}
    - {name: AX1_POS, word: 10, type: s32}
objects:
  - {name: conv1, type: conveyor, run: out.CONV1_RUN, length: 2000, speed: 300}
  - {name: PE1, type: sensor, on: conv1, at: 1500, output: in.PE1}
```

- `tags` でブロック内のビット/ワードに名前を付ける（`bit: "0.2"` = word 0 の bit 2。引用符を付ける）。
- `objects` の挙動は [docs/behaviors.md](docs/behaviors.md)。コンベヤ・センサ・ストッパ・シリンダ・供給・サーボ軸に加え、
  画像検査カメラ（NG の X,Y 座標とエンコーダ値を出力）と多連エアブロー（Y 座標で連を選んでピンポイント排出）がある。
  挙動を書かずに `tags` だけでも「デバイスモニタ + 手動強制」として使える。
- PLC 側に入れる I/O 層（TWIN_MODE 切替）は [docs/plc_io_layer.md](docs/plc_io_layer.md)。

## 開発の進め方（設計メモ §9）

1. PLC に I/O 層と DM ブロックを入れ、TWIN_MODE で切り替わることを確認
2. `configs/commtest.yaml` の `type`/`ip` を実機に変えて起動し、ヘッダの周期・ジッタ・読/書 ms を実測（← 設計成否が決まる）
3. 装置の YAML（tags + objects）を書く
4. ラダーを作り込みながら、波形とイベントでタイミングを確認。仮想センサの強制で異常系も試す
5. 実機でも同じ Core を動かし（TWIN_MODE OFF、watch で実 I/O を監視）、CSV を重ねて差分箇所だけ調整

## 10ms を守るための実装

- 1 周期の通信は「読み 1 回 + 書き 1 回」に固定（ビット単位アクセスなし）。`watch` は 10 周期に 1 回
- 10ms ループは専用プロセス（`multiprocessing`）。WebSocket 配信・記録は親プロセス
- TCP 常時接続 + `TCP_NODELAY`。Windows は `timeBeginPeriod(1)` + 高優先度、Linux は `nice -10`
- 周期の直前 1.5ms はスピン待ちで精度を出す。周期ジッタを常時表示

## 構成

```
twin/
  __main__.py        CLI
  config.py          YAML 読込・タグ解決
  drivers/           keyence.py (上位リンク) / melsec.py (SLMP) / mock.py (+ demo_logic.py)
  sim/               kernel.py (ワーク・IO バス) / behaviors.py (挙動ライブラリ)
  core/loop.py       10ms ループ (専用プロセス) / timing.py
  server/            http.py (HTTP+WebSocket, 依存なし) / app.py (配信・記録・REST)
  web/               index.html / app.js / style.css (ビルド不要)
configs/             demo_line / keyence_kv8000 / melsec_iqr / commtest
docs/                plc_io_layer.md / behaviors.md / protocol.md
tests/               pytest (ドライバのフレーム、挙動、サーバ結合)
tools/e2e_check.cjs  Playwright によるブラウザ UI スモークテスト
```

## テスト

```bash
pip install pytest pytest-timeout
python -m pytest -q
# UI (Playwright が入っている環境で、サーバ起動中に)
node tools/e2e_check.cjs http://localhost:8080 ui.png
```

## スクリプトからの操作

```bash
curl -X POST localhost:8080/api/cmd -d '{"type":"force_bit","block":"in","word":0,"bit":2,"value":1}'
curl -o rec.csv localhost:8080/api/record.csv
```

プロトコルは [docs/protocol.md](docs/protocol.md)。
