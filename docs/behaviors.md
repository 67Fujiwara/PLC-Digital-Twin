# 挙動ライブラリ（YAML `objects`）

参照の書き方: `out.NAME` / `in.NAME`（tags で命名）、`out.5`（ビット番号）、`out.0.3`（word.bit）、`out.w10:s32`（ワード）。
PLC 出力（out / watch）は読むだけ、PLC 入力（in）は書くだけ。書き先に `out.*` を指定すると起動時にエラーになる。

| type | 主なキー | 出力（in へ書く） |
|---|---|---|
| `conveyor` | `run`, `length`, `speed` または `speed_ref`+`rated_hz`+`rated_speed`+`hz_scale`, `accel`, `decel`, `next`, `reverse` | `encoder`（u32 パルス, `pulses_per_mm`） |
| `sensor` | `on`(コンベヤ), `at`, `width`, `delay`, `invert` / `cylinder`+`min`+`max` | `output` |
| `stopper` | `on`, `at`, `up` または `down`, `time` | — |
| `cylinder` | `extend`, `retract`(省略時は単動), `stroke`, `time`, `retract_time`, `sensor_band`, `push:{conv, at, width, to}` | `end_sensor`, `home_sensor` |
| `emitter` | `onto`, `at`, `every`, `trigger`, `enable`, `length`, `max`, `initial` | — |
| `axis` | `target`(s32), `start`, `home`, `jog_fwd`, `jog_rev`, `speed`, `accel`, `decel`, `unit_scale`, `lo`, `hi`, `tol`, `initial` | `pos`(s32), `busy`, `inpos` |
| `camera` | `on`, `at`(トリガ線), `fov`, `trigger`(省略時はワーク前端通過で自動撮像), `delay`, `slots`, `ack`, `done_time`, `x_unit`, `y_unit` | `done`, `count`, `enc`(撮像時エンコーダ u32), `results`(先頭ワード。NG ごとに X(s16), Y(u16) の 2 ワード) |
| `airblow` | `on`, `at`, `nozzles`(連数), `blow`(`out.wN` の bit0〜 or ビット参照のリスト), `pitch`, `y0`, `band`, `reach` | — |
| `blinker` | `period` | `output`（通信確認用） |
| `counter` | `step` | `output`（毎周期加算、通信確認用） |

ワークは X（搬送方向、前端位置）と Y（幅方向、片側エッジから中心まで）を持つ。コンベヤの `width`（既定 300mm）の中で
`emitter` の `y: random` がランダム配置し、`ng_rate` で不良を混ぜる。Y が重ならないワーク同士は追突せず並走する。

## カメラ → エンコーダ追従 → 多連エアブローの考え方

```
     トリガ線 (camera.at)             ノズル (airblow.at)
  ───────┬────────────────────────────────┬──────────→ X
   視野  │  NG 中心 X (負=上流)           │
   fov   │◄──────── 追従距離 ──────────►│
```

1. ワーク前端がトリガ線を通過（または PLC の `trigger`）で撮像。`delay` 後に `done` ON、`count` と各 NG の (X, Y)、撮像時の `enc` を出力。
   X はトリガ線基準（下流が正）、Y はコンベヤ片側エッジ基準。
2. PLC は `done` の立上りで (X, Y, enc) を追従テーブルに登録し、`ack` を返す（次の結果はキューで待つ）。
3. 毎スキャン `現在位置 = camera.at + X + (現在エンコーダ − enc) ÷ pulses_per_mm` を計算し、
   `airblow.at − 先行量` に達したら `ノズル番号 = Y ÷ pitch` のビットを `blow_time` だけ ON。
4. ノズルの帯 `band`（Y）× `reach`（X）に中心がかかるワークが排出される。NG なら「排出」、OK なら「誤排出」として集計し、
   末端まで流れた NG は「見逃し」。ライン図の見出しに常時表示する。

モック PLC 側の手本実装は `twin/drivers/demo_logic.py`（追従テーブル・ノズル選択・ブロー時間）。

## 例

```yaml
objects:
  - {name: conv1, type: conveyor, run: out.CONV1_RUN, length: 2000, next: conv2,
     speed_ref: out.INV_HZ, rated_hz: 60, rated_speed: 300, accel: 0.5, decel: 0.3}
  - {name: supply, type: emitter, onto: conv1, every: 3.0, length: 60}
  - {name: PE1, type: sensor, on: conv1, at: 1500, width: 10, output: in.PE1, delay: 0.01}
  - {name: ST1, type: stopper, on: conv1, at: 1560, up: out.ST1_UP, time: 0.1}
  - {name: CYL1, type: cylinder, extend: out.CYL1_EXT, stroke: 100, time: 0.4,
     end_sensor: in.CYL1_END, home_sensor: in.CYL1_HOME,
     push: {conv: conv1, at: 1520, width: 100, to: null}}
  - {name: AX1, type: axis, target: out.AX1_TARGET, start: out.AX1_START, speed: 400, accel: 0.3,
     pos: in.AX1_POS, busy: in.AX1_BUSY, inpos: in.AX1_INPOS, unit_scale: 100}
  # 画像検査 + 4 連エアブロー
  - {name: CAM1, type: camera, on: conv1, at: 600, fov: 300, delay: 0.05, slots: 4,
     done: in.CAM_DONE, ack: out.CAM_ACK, count: in.CAM_COUNT, enc: in.CAM_ENC, results: in.w32}
  - {name: BLOW, type: airblow, on: conv1, at: 1200, nozzles: 4, blow: out.w2, band: 50, reach: 40}
```

## 新しい挙動を足す

`twin/sim/behaviors.py` に `Behavior` のサブクラス（`type`, `step(dt, io, world)`, `state()`）を書いて `REGISTRY` に登録する。
数十行で 1 部品。`world.works` / `world.spawn()` / `world.works_on(conv)` でワークを操作できる。
