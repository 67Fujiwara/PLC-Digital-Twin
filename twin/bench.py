"""通信往復時間の計測ツール。どこまで周期を詰められるかを数字で出す。

  python -m twin.bench configs/a.yaml                  # YAML の plc 設定で TCP を計測
  python -m twin.bench configs/a.yaml --udp            # UDP でも計測して比較 (KV: 8500, R: UDP 用の接続設定が必要)
  python -m twin.bench configs/a.yaml --words 16,32,64 # ブロック長ごとの往復時間

出力: 読み/書き/往復 (avg, p99, max) と、推奨 cycle_ms (通常 / 読み書き並列時)。
"""
from __future__ import annotations

import argparse
import math
import sys
import threading
import time

from .config import load_config
from .drivers import make_driver, DriverError


def stats(xs: list[float]) -> tuple[float, float, float]:
    """(avg, p90, max) ms"""
    xs = sorted(xs)
    return (sum(xs) / len(xs) * 1000, xs[min(len(xs) - 1, int(len(xs) * 0.9))] * 1000, xs[-1] * 1000)


def bench(plc: dict, out, inp, words: int, n: int) -> dict | None:
    drv = make_driver(plc)
    try:
        drv.connect()
    except DriverError as e:
        print(f"  接続失敗: {e}")
        return None
    zeros = [0] * words
    try:
        for _ in range(5):                                   # ウォームアップ
            drv.read_words(out.dev, out.addr, words)
            drv.write_words(inp.dev, inp.addr, zeros)
        r, w, rw = [], [], []
        for _ in range(n):
            t0 = time.perf_counter()
            drv.read_words(out.dev, out.addr, words)
            t1 = time.perf_counter()
            drv.write_words(inp.dev, inp.addr, zeros)
            t2 = time.perf_counter()
            r.append(t1 - t0); w.append(t2 - t1); rw.append(t2 - t0)
        # 並列 (2 接続)
        par = None
        wdrv = drv.clone()
        if wdrv is not drv:
            try:
                wdrv.connect()
                p = []
                for _ in range(n):
                    t0 = time.perf_counter()
                    th = threading.Thread(target=wdrv.write_words, args=(inp.dev, inp.addr, zeros))
                    th.start()
                    drv.read_words(out.dev, out.addr, words)
                    th.join()
                    p.append(time.perf_counter() - t0)
                par = stats(p)
            except DriverError as e:
                print(f"  並列計測不可 (2 本目の接続): {e}")
            finally:
                wdrv.close()
    finally:
        drv.close()
    return {"read": stats(r), "write": stats(w), "rw": stats(rw), "par": par}


def rec(p90_ms: float) -> float:
    return max(0.5, math.ceil((p90_ms + 0.5) / 0.5) * 0.5)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="twin.bench", description="PLC 通信の往復時間を計測")
    ap.add_argument("config")
    ap.add_argument("-n", type=int, default=200, help="計測回数 (既定 200)")
    ap.add_argument("--words", default="", help="ブロック長の一覧 (例 16,32,64)。省略時は YAML の out.words")
    ap.add_argument("--udp", action="store_true", help="UDP でも計測")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    sizes = [int(x) for x in args.words.split(",") if x.strip()] or [cfg.out.words]
    protos = ["tcp"] + (["udp"] if args.udp else [])
    if cfg.plc.get("protocol", "tcp") == "udp" and "udp" not in protos:
        protos = ["udp"]
    print(f"PLC: {cfg.plc_type} {cfg.plc.get('ip', '')}  out={cfg.out.dev}{cfg.out.addr} in={cfg.inp.dev}{cfg.inp.addr}  n={args.n}")
    print(f"{'proto':5} {'words':>5} | {'読み avg/p90/max':>22} | {'書き avg/p90/max':>22} | {'往復 avg/p90/max':>22} | 推奨周期 | 並列時 往復 p90 → 推奨")
    best = None
    for proto in protos:
        plc = dict(cfg.plc)
        plc["protocol"] = proto
        if proto == "udp" and cfg.plc_type == "keyence" and int(cfg.plc.get("port", 8501)) == 8501:
            plc["port"] = 8500
        for wd in sizes:
            res = bench(plc, cfg.out, cfg.inp, wd, args.n)
            if res is None:
                continue
            f = lambda t: f"{t[0]:6.2f}/{t[1]:6.2f}/{t[2]:6.2f}"  # noqa: E731
            par = f"{res['par'][1]:6.2f} → {rec(res['par'][1]):4.1f}ms" if res["par"] else "-"
            print(f"{proto:5} {wd:5d} | {f(res['read'])} | {f(res['write'])} | {f(res['rw'])} | {rec(res['rw'][1]):5.1f}ms | {par}")
            seq_c = rec(res["rw"][1])
            par_c = rec(res["par"][1]) if res["par"] else None
            use_par = par_c is not None and par_c < seq_c
            cand = par_c if use_par else seq_c
            if best is None or cand < best[0]:
                best = (cand, proto, wd, use_par)
    if best:
        c, proto, wd, par = best
        print(f"\n最速候補: cycle_ms: {c}  (protocol: {proto}, words: {wd}{', parallel_rw: true' if par else ''})")
        print("  ※ PLC のスキャンタイムより短くしても新しい値は得られません。KV STUDIO / GX Works3 でスキャンタイムも確認してください。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
