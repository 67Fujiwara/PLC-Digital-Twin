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
| `blinker` | `period` | `output`（通信確認用） |
| `counter` | `step` | `output`（毎周期加算、通信確認用） |

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
```

## 新しい挙動を足す

`twin/sim/behaviors.py` に `Behavior` のサブクラス（`type`, `step(dt, io, world)`, `state()`）を書いて `REGISTRY` に登録する。
数十行で 1 部品。`world.works` / `world.spawn()` / `world.works_on(conv)` でワークを操作できる。
