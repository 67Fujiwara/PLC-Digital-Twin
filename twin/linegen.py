"""ライン設定 (カメラ台数・画素・射撃台数・段数・距離・速度) から objects / tags を生成する。

YAML の line: か <設定>.line.yaml (画面の「ライン設定」で保存) に書く。例:
  line:
    conveyor: {speed: 300, width: 640, length: 1500}
    camera:   {count: 2, px_x: 1024, px_y: 512, mm_per_px: 0.3125, at: 400, delay_ms: 30, coord: px}
    eject:    {count: 32, rows: 2, row_gap: 60, distance: 300, band: 18, reach: 30, blow_ms: 60}
    supply:   {every: 0.6, jitter: 0.6, length: 30, width: 30, ng_rate: 0.3}

座標の向き: X = 短手 (幅方向)、Y = 流れ方向。カメラ出力 (coord: px) は X 画素 (x_offset 基準), Y 画素 (視野上流端 = 0)。
距離 (eject.distance) は「視野の下流端 → 1 段目ノズル」。2 段目は row_gap だけ下流で、1 段目の間に千鳥配置。

I/O 割付 (PLC 側 I/O 層と合わせる):
  out: 0.0 CONV_RUN / word 2〜 SOL01〜 (bit 順、16 点ごとに次のワード)
  in : 1.n CAMn_DONE / word 4,5 ENC (u32) / word 10+2n,11+2n CAMn の REQ (レーンビット) / word 20+n CAMn_COUNT /
       word 32+8n〜 CAMn の NG 座標 (X,Y) × 4 スロット
"""
from __future__ import annotations

import math

DEFAULT_LINE = {
    "conveyor": {"speed": 300.0, "width": 640.0, "length": 1500.0},
    "camera": {"count": 1, "px_x": 2048, "px_y": 1024, "mm_per_px": 0.3125, "at": 400.0, "delay_ms": 30, "coord": "px"},
    "eject": {"count": 32, "rows": 1, "row_gap": 60.0, "distance": 300.0, "band": 18.0, "reach": 30.0, "blow_ms": 60, "lead": 15.0},
    "supply": {"every": 0.6, "jitter": 0.6, "length": 30.0, "width": 30.0, "ng_rate": 0.3, "max": 60},
}


def merged(line: dict | None) -> dict:
    out = {k: dict(v) for k, v in DEFAULT_LINE.items()}
    for k, v in (line or {}).items():
        if isinstance(v, dict) and k in out:
            out[k].update(v)
    return out


def generate(line: dict | None, words: int = 64) -> dict:
    """-> {"objects": [...], "tags": {"out": [...], "in": [...]}, "params": {...}, "alloc": [...]}"""
    L = merged(line)
    conv, cam, ej, sup = L["conveyor"], L["camera"], L["eject"], L["supply"]
    width = float(conv["width"]); speed = float(conv["speed"]); length = float(conv["length"])
    ncam = max(1, int(cam["count"])); px_x, px_y = int(cam["px_x"]), int(cam["px_y"]); mpp = float(cam["mm_per_px"])
    fov = px_y * mpp
    cover = px_x * mpp
    cam_at = float(cam["at"])                         # 視野中心 (流れ方向)
    fov_end = cam_at + fov / 2
    n = max(1, int(ej["count"])); rows = 2 if int(ej["rows"]) >= 2 else 1
    row_gap = float(ej["row_gap"]); dist = float(ej["distance"])
    pitch = width / n
    eject_at = fov_end + dist                         # 1 段目
    tags_out = [{"name": "CONV_RUN", "bit": "0.0", "comment": "搬送コンベヤ運転"}]
    tags_in = [{"name": "ENC", "word": 4, "type": "u32", "comment": "搬送エンコーダ (10 pls/mm)"},
               {"name": "PE_IN", "bit": "0.0", "comment": "入口ワーク検出"}]
    alloc = [("out", "DM1000.0", "CONV_RUN"), ("in", "DM1100.0", "PE_IN"), ("in", "DM1104-1105", "ENC (u32)")]
    sol_refs = []
    for i in range(n):
        w, b = 2 + i // 16, i % 16
        nm = f"SOL{i + 1:02d}"
        tags_out.append({"name": nm, "bit": f"{w}.{b}", "comment": f"射撃 {i + 1} ({'2段目' if rows == 2 and i % 2 == 1 else '1段目'})"})
        sol_refs.append(f"out.{nm}")
    alloc.append(("out", f"DM{1000 + 2}.0〜", f"SOL01〜SOL{n:02d} (16 点ごとに次のワード)"))
    objects = [
        {"name": "conv", "type": "conveyor", "run": "out.CONV_RUN", "length": length, "width": width, "speed": speed,
         "encoder": "in.ENC", "pulses_per_mm": 10},
        {"name": "supply", "type": "emitter", "onto": "conv", "every": float(sup["every"]), "jitter": float(sup["jitter"]),
         "length": float(sup["length"]), "width": float(sup["width"]), "y": "random", "ng_rate": float(sup["ng_rate"]),
         "max": int(sup["max"])},
        {"name": "PE_IN", "type": "sensor", "on": "conv", "at": 100.0, "output": "in.PE_IN"},
    ]
    cams = []
    for i in range(ncam):
        nm = f"CAM{i + 1}"
        x_off = i * cover
        tags_in.append({"name": f"{nm}_DONE", "bit": f"1.{i}", "comment": f"カメラ {i + 1} 結果有効"})
        tags_in.append({"name": f"{nm}_REQ1", "word": 10 + 2 * i, "comment": f"カメラ {i + 1} 排斥要求 レーン 1〜16 (bit0 = SOL01)"})
        tags_in.append({"name": f"{nm}_REQ2", "word": 11 + 2 * i, "comment": f"カメラ {i + 1} 排斥要求 レーン 17〜32"})
        tags_in.append({"name": f"{nm}_COUNT", "word": 20 + i, "comment": f"カメラ {i + 1} NG 個数"})
        for k in range(4):
            tags_in.append({"name": f"{nm}_X{k + 1}", "word": 32 + 8 * i + 2 * k, "type": "s16", "comment": f"NG{k + 1} X 画素 (幅方向)"})
            tags_in.append({"name": f"{nm}_Y{k + 1}", "word": 33 + 8 * i + 2 * k, "comment": f"NG{k + 1} Y 画素 (流れ方向, 上流端=0)"})
        alloc.append(("in", f"DM1101.{i}", f"{nm}_DONE"))
        alloc.append(("in", f"DM{1110 + 2 * i}-{1111 + 2 * i}", f"{nm} 排斥要求 (bit0 = SOL01)"))
        alloc.append(("in", f"DM{1120 + i}", f"{nm}_COUNT"))
        alloc.append(("in", f"DM{1132 + 8 * i}〜", f"{nm} NG X,Y 画素 × 4"))
        objects.append({"name": nm, "type": "camera", "on": "conv", "at": cam_at, "pixels": [px_x, px_y], "mm_per_px": mpp,
                        "x_offset": x_off, "coord": str(cam["coord"]), "trigger_offset": fov / 2,
                        "lane_window": float(sup["length"]) * 1.5, "delay": float(cam["delay_ms"]) / 1000.0,
                        "done_time": 0.05, "slots": 4, "done": f"in.{nm}_DONE", "count": f"in.{nm}_COUNT",
                        "results": f"in.w{32 + 8 * i}", "lanes": n, "lane_pitch": pitch, "lane_out": f"in.{nm}_REQ1"})
        cams.append(nm)
    objects.append({"name": "SOL", "type": "airblow", "on": "conv", "at": eject_at, "nozzles": n, "pitch": pitch,
                    "band": float(ej["band"]), "reach": float(ej["reach"]), "rows": rows, "row_gap": row_gap,
                    "blow": sol_refs})
    params = {"speed": speed, "ppm": 10.0, "lanes": n, "rows": rows, "row_gap": row_gap,
              "trigger_to_eject": dist,                     # トリガ線 (視野下流端) → 1 段目
              "blow_s": float(ej["blow_ms"]) / 1000.0, "lead": float(ej.get("lead", 15.0)),
              "cams": ncam, "req_words": [10 + 2 * i for i in range(ncam)], "done_bits": [(1, i) for i in range(ncam)],
              "enc_word": 4, "sol_word": 2, "run_bit": (0, 0)}
    return {"objects": objects, "tags": {"out": tags_out, "in": tags_in}, "params": params, "alloc": alloc,
            "derived": {"fov_mm": fov, "cover_mm": cover, "pitch_mm": pitch, "eject_at": eject_at, "fov_end": fov_end,
                        "cam_coverage_mm": cover * ncam, "travel_ms_row1": dist / speed * 1000 if speed else 0,
                        "travel_ms_row2": (dist + row_gap) / speed * 1000 if speed else 0}}
