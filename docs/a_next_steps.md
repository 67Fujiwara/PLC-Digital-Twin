# Line A（Phoenix 画像検査 + SOL 32 連 排斥）を実機で回すまでの手順

a.csv のデバイスコメントから読み取った構成:

| 役割 | デバイス |
|---|---|
| 入力 X000〜X00F | R000〜R015（ロジックが直接参照） |
| 出力 Y010〜Y01F | R500〜R515 |
| 画像検査からの排斥要求 SOL1-01〜SOL32 | MR800〜MR815, MR900〜MR915（[from Ins]） |
| 検査完了 / 検査番号 | B020（Eye1 検査完了）, W0800（to Phoenix） |
| SOL ごとの X 座標範囲 / ON ディレイ / 吹付時間 | DM17700〜, DM17900〜, T10〜T34 |
| 運転条件 | LR000 排斥許可, LR004 検査不可ワーク排斥, LR005 強制排斥 |

## ステップ 1: 実機につないで「見る」（ラダー変更なし、今日できる）

```
python -m twin.bench configs/a.yaml          # 往復時間 → 周期の当たりを付ける
python -m twin configs/a.yaml --cycle auto   # 既存デバイスをそのまま監視
```

KV STUDIO 側で「上位リンク通信（ポート 8501）」が有効なら、ラダーを 1 行も変えずに MR800〜 の排斥要求、
DM5120〜 の検査数、LR の運転条件、R000〜 の入出力が画面に出る。波形に SOL REQ と Y 出力を並べると、
今の装置の「要求から吹付けまでの時間」が 10ms 単位で測れる。これが実機側の基準データになる。

## ステップ 2: PLC に I/O 層（TWIN_MODE）を入れる

ロジックが R000〜/R500〜 を直接参照しているので、KV STUDIO の「デバイス一括置換」で内部リレーに付け替える。

| 置換前 | 置換後 | 備考 |
|---|---|---|
| R000〜R015 | MR100〜MR115 | 入力（ロジック側の参照だけ置換） |
| R500〜R515 | MR200〜MR215 | 出力（OUT 命令の先も置換） |
| SOL 出力 Y（コメント未登録。実際の Y 番号を確認） | MR300〜MR315, MR400〜MR415 | SOL01〜32 |

その上でプログラム先頭に I/O 層を追加する（TWIN_MODE = MR000）。

```
; ===== I/O 層 =====
; --- 入力 ---
LDB MR000
MOV  R000   MR100          ; 実機: X000〜X00F
LD  MR000
MOV  DM1100 MR100          ; ツイン: PC からの仮想入力 (in word0)
; --- 排斥要求 (画像検査 or PC) ---
LD  MR000
MOV  DM1102 MR800          ; ツイン: in word2 → SOL1-01〜 (MR800ch)
MOV  DM1103 MR900          ; ツイン: in word3 → SOL25〜 (MR900ch)
; 実機時は Phoenix からの B/W リンク → MR800 の既存処理をそのまま
LD  MR000
MOV  DM1101 ...            ; 必要なら Eye1 検査完了 (B020) を in word1 bit0 から
; --- 出力 ---
LDB MR000
MOV  MR200  R500           ; 実機: Y010〜Y01F
MOV  MR300  <SOL Y ch1>    ; 実機: SOL01〜16
MOV  MR400  <SOL Y ch2>    ; 実機: SOL17〜32
LD  MR000
MOV  MR200  DM1000         ; ツイン: out word0
MOV  MR300  DM1002         ; ツイン: out word2
MOV  MR400  DM1003         ; ツイン: out word3
```

TWIN_MODE（MR000）は VT のスイッチに割り付けておくと切替が楽。安全回路（非常停止・出力許可）は I/O 層より上流に置く。

## ステップ 3: ツインで流す

```
python -m twin configs/a_twin.yaml --mock    # まず PC だけ (疑似ラダー twin/drivers/line_a_logic.py)
python -m twin configs/a_twin.yaml           # 実機 + TWIN_MODE ON
```

`configs/a_twin.yaml` の TODO を装置の実寸に合わせる:

- `conv.width` = SOL ピッチ × 32、`camera.lane_pitch` = SOL ピッチ（DM17700〜 の X 座標範囲と同じ区切り）
- `camera.at` / `airblow.at` = カメラ中心と SOL の搬送方向位置、`conv.speed` = 実速度
- 吹付時間・ON ディレイは PLC 側の DM17900〜 / T10〜 がそのまま効くので YAML には書かない

画面の「NG 排出 / 誤排出 / 見逃し」で ON ディレイと吹付時間の当たりを取り、X 座標範囲の境界（隣のレーンへの巻き込み）を
供給 `y: random` で何百個も流して確かめる。実機では「ステップ 1 で取った波形」と重ねて差があるところだけ調整する。

## ステップ 4: 以降

- 品種ごとの `objects` 差分は YAML を複製（`include` で共通部を共有）
- 実機記録（CSV 保存）とツイン記録の自動比較
- 3D ビューア（three.js）は必要になってから
