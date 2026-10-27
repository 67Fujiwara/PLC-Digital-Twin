"""起動: python -m twin configs/demo_line.yaml [--port 8080] [--fps 30] [--no-browser]"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import threading
import webbrowser


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="twin", description="PLC Digital Twin - Twin Core")
    ap.add_argument("config", nargs="?", default=os.path.join("configs", "demo_line.yaml"), help="装置定義YAML")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--fps", type=float, default=30.0, help="ブラウザ配信レート (Hz)")
    ap.add_argument("--no-browser", action="store_true", help="ブラウザを自動で開かない")
    ap.add_argument("--check", action="store_true", help="設定の検証のみ")
    ap.add_argument("--mock", action="store_true", help="PLC に接続せずモックで起動 (画面と設定の確認用)")
    args = ap.parse_args(argv)

    from .config import load_config
    try:
        cfg = load_config(args.config)
        if args.mock and cfg.plc_type != "mock":
            from .config import parse_config
            raw = dict(cfg.raw)
            raw["plc"] = dict(raw.get("plc") or {})
            raw["plc"]["driver"] = "mock"          # アドレス解釈は実機のまま、通信だけモック
            cfg = parse_config(raw)
            print("[info] --mock: PLC に接続せずモックで動作します (全デバイス 0)")
    except Exception as e:  # noqa: BLE001
        print(f"設定エラー: {e}", file=sys.stderr)
        return 2
    print(f"[info] 設定: {args.config}  PLC={cfg.plc_type} out={cfg.out.dev}{cfg.out.addr}x{cfg.out.words} "
          f"in={cfg.inp.dev}{cfg.inp.addr}x{cfg.inp.words} watch={cfg.watch_words}w objects={len(cfg.objects)}")
    if args.check:
        print("設定OK")
        return 0

    from .server.app import TwinApp
    app = TwinApp(cfg, args.host, args.port, args.fps, config_path=args.config)
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://localhost:{args.port}/")).start()
    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
