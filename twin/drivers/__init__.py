"""PLCドライバ。plc.type に応じて生成する。"""
from __future__ import annotations

from .base import Driver, DriverError


def make_driver(plc: dict) -> Driver:
    t = str(plc.get("type", "mock")).lower()
    if t == "keyence":
        from .keyence import KeyenceHostLink
        return KeyenceHostLink(plc.get("ip", "192.168.0.10"), int(plc.get("port", 8501)),
                               timeout=float(plc.get("timeout", 1.0)))
    if t == "melsec":
        from .melsec import MelsecSLMP
        return MelsecSLMP(plc.get("ip", "192.168.3.39"), int(plc.get("port", 5000)),
                          timeout=float(plc.get("timeout", 1.0)),
                          network=int(plc.get("network", 0)), station=int(plc.get("station", 0xFF)))
    if t == "mock":
        from .mock import MockPLC
        return MockPLC(logic=plc.get("logic"), scan_ms=float(plc.get("scan_ms", 2.0)))
    raise DriverError(f"未対応の plc.type: {t}")


__all__ = ["Driver", "DriverError", "make_driver"]
