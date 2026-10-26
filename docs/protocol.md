# Twin Core ⇄ ブラウザ プロトコル（WebSocket, JSON）

接続先: `ws://<host>:8080/ws`（HTTP と同じポート）。

## サーバ → ブラウザ

| type | 内容 |
|---|---|
| `hello` | `config`（タグ、ブロック、objects、周期）。接続直後に 1 回 |
| `history` | 直近の記録サンプル（最大 3000 = 30 秒）。波形の初期表示用 |
| `frame` | 30Hz。`samples`: 前回以降の 10ms サンプル配列、`sim`: 挙動状態（ワーク位置など）、`stat`: 周期・ジッタ・通信時間、`forces`: 強制中の一覧、`logs` |
| `pong` | `ping` への応答 |

サンプル: `{seq, wall, out:[64], in:[64], watch:[n], p: 周期ms, r: 読みms, w: 書きms}`

## ブラウザ → サーバ

| type | 引数 | 動作 |
|---|---|---|
| `force_bit` | `block:"in", word, bit, value: 0/1/null` | 仮想センサを強制。`null` で解除。Sim の出力より優先 |
| `force_word` | `block:"in", word, dtype, value/null` | ワード強制 |
| `release_all` | — | 全強制解除 |
| `write_watch` | `word, value` | 監視デバイスのワードへ PLC 書込み（1 回） |
| `write_watch_bit` | `word, bit, value` | 監視デバイスのビットへ PLC 書込み（ワード単位で RMW） |
| `write_dev` | `dev, addr, value` or `values` | 任意デバイスへ直接書込み |
| `sim_reset` | — | 挙動シミュレーションを初期状態へ |
| `sim_enable` | `value` | 挙動シミュレーションの停止/再開 |
| `emit` | `name` | エミッタからワークを 1 個投入 |
| `remove_works` | — | ワーク全消去 |
| `ping` | `t` | 遅延計測 |

## REST

| path | 内容 |
|---|---|
| `GET /api/config` | hello と同じ設定 JSON |
| `GET /api/stat` | 最新の周期統計 |
| `GET /api/record.csv` | 記録バッファ（`record_seconds` 分）を CSV で取得。列 = タグ名 + 生ワード |
| `POST /api/cmd` | 上記コマンドを JSON で投入（スクリプトからの操作用） |
