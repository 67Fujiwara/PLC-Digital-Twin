"""PLC のデバイスメモリを全域スキャンして「使われているデバイス」を見つける。

上位リンク / SLMP で取れるのは値だけ (コメント・変数名はプロジェクト側にあり通信では読めない)。
- 全読込: 既定の範囲を一括読みし、値 != 0 のデバイスを一覧にする
- 差分読込: 前回スキャンと比べて値が変わったデバイスを一覧にする (TP 操作 → 差分で「どのデバイスが動いたか」が分かる)

ループの接続とは別の接続 (driver.clone()) を使い、10ms 周期を乱さない。
範囲は YAML の scan: [{dev, addr, words}] で上書きできる。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .config import TwinConfig, bit_address, word_address, is_bit_device, KEYENCE_CH_DEVICES, SYSTEM_DEVICES
from .drivers import Driver, DriverError

# 既定スキャン範囲 (dev, 先頭番号, ワード数)。ビットデバイスはワード (16 点) 単位。
# システムデバイス (KV: CR/CM, 三菱: SM/SD/SB/SW) と T/C は含めない (件数が多いだけで装置の動きには関係ない)。
# YAML の scan: に書いた場合も SYSTEM_DEVICES は読み飛ばす。
DEFAULT_SCAN = {
    "keyence": [("R", 0, 600), ("MR", 0, 1000), ("LR", 0, 1000), ("B", 0, 1024),
                ("DM", 0, 65535), ("W", 0, 16384)],
    "melsec": [("X", 0, 512), ("Y", 0, 512), ("M", 0, 2048), ("L", 0, 2048), ("B", 0, 2048),
               ("D", 0, 32768), ("W", 0, 32768), ("R", 0, 32768)],
    "mock": [("R", 0, 600), ("MR", 0, 1000), ("LR", 0, 1000), ("B", 0, 1024),
             ("DM", 0, 65535), ("W", 0, 16384)],
}
CHUNK = {"keyence": 1000, "melsec": 960, "mock": 1000}


@dataclass
class ScanRange:
    dev: str
    addr: int          # 先頭番号 (リレーは ch*100 / 16 の倍数)
    words: int
    bits: bool


@dataclass
class ScanResult:
    mode: str
    started: float
    finished: float = 0.0
    total_words: int = 0
    read_words: int = 0
    errors: list = field(default_factory=list)
    rows: list = field(default_factory=list)      # 表示用 (nonzero / changed)
    values: dict = field(default_factory=dict)     # (dev, word_index) -> value  (全値のスナップショット)
    done: bool = False


def ranges_for(cfg: TwinConfig) -> list[ScanRange]:
    raw = cfg.raw.get("scan")
    out: list[ScanRange] = []
    if raw:
        for r in raw:
            dev = str(r["dev"]).upper()
            if dev in SYSTEM_DEVICES.get(cfg.plc_type, set()):
                continue
            out.append(ScanRange(dev, int(r.get("addr", 0)), int(r.get("words", 1)), is_bit_device(cfg.plc_type, dev)))
    else:
        for dev, addr, words in DEFAULT_SCAN.get(cfg.plc_type, DEFAULT_SCAN["mock"]):
            out.append(ScanRange(dev, addr, words, is_bit_device(cfg.plc_type, dev)))
    return out


def _plc_addr(plc_type: str, r: ScanRange, word_index: int) -> int:
    """ワード index → PLC 上の先頭番号。"""
    if r.bits:
        if plc_type in ("keyence", "mock") and r.dev in KEYENCE_CH_DEVICES:
            return r.addr + word_index * 100
        return r.addr + word_index * 16
    return r.addr + word_index


class Scanner:
    def __init__(self, cfg: TwinConfig, make_conn):
        """make_conn: 呼ぶと接続済みの Driver を返す関数 (clone して connect)。"""
        self.cfg = cfg
        self.make_conn = make_conn
        self.last: ScanResult | None = None       # 直近のスナップショット (差分の基準)
        self.current: ScanResult | None = None
        self.busy = False

    def run(self, mode: str, progress=None) -> ScanResult:
        plc_type = self.cfg.plc_type
        chunk = CHUNK.get(plc_type, 960)
        ranges = ranges_for(self.cfg)
        res = ScanResult(mode=mode, started=time.time(), total_words=sum(r.words for r in ranges))
        self.current = res
        self.busy = True
        drv: Driver | None = None
        try:
            drv = self.make_conn()
            prev = self.last.values if (mode == "diff" and self.last is not None) else None
            names = self._name_map()
            for r in ranges:
                off = 0
                while off < r.words:
                    n = min(chunk, r.words - off)
                    base = _plc_addr(plc_type, r, off)
                    try:
                        vals = drv.read_words(r.dev, base, n)
                    except DriverError as e:
                        res.errors.append(f"{r.dev}{base} x{n}: {e}")
                        # 範囲外などは半分に割って続ける (最小 16 ワードで諦める)
                        if n > 16:
                            chunk_local = n // 2
                            try:
                                vals = drv.read_words(r.dev, base, chunk_local)
                                n = chunk_local
                            except DriverError:
                                off += n
                                res.read_words += n
                                continue
                        else:
                            off += n
                            res.read_words += n
                            continue
                    for i, v in enumerate(vals):
                        key = (r.dev, off + i)
                        res.values[key] = v
                        pv = prev.get(key, 0) if prev is not None else 0
                        if (prev is None and v != 0) or (prev is not None and v != pv):
                            wa = _plc_addr(plc_type, r, off + i)
                            res.rows.append(self._row(plc_type, r, off + i, wa, v, pv if prev is not None else None, names))
                    off += n
                    res.read_words += n
                    if progress is not None:
                        progress(self.status())
        except DriverError as e:
            res.errors.append(f"接続: {e}")
        finally:
            if drv is not None:
                try:
                    drv.close()
                except Exception:  # noqa: BLE001
                    pass
            res.finished = time.time()
            res.done = True
            self.busy = False
            if not res.errors or res.values:
                self.last = res
        return res

    def _name_map(self) -> dict[str, str]:
        m: dict[str, str] = {}
        for b in ("out", "in", "watch"):
            for t in self.cfg.tags.get(b, []):
                if t.address:
                    m[t.address.upper()] = t.name
        return m

    def _row(self, plc_type: str, r: ScanRange, word_index: int, plc_addr: int, v: int, pv: int | None, names: dict) -> dict:
        if r.bits:
            label = self._word_label(plc_type, r, word_index)
            bits_on = [b for b in range(16) if v >> b & 1]
            changed_bits = [b for b in range(16) if pv is not None and ((v ^ pv) >> b & 1)]
            bit_names = {b: names.get(bit_address(plc_type, r.dev, r.addr, word_index, b).upper(), "") for b in range(16)}
            return {"dev": r.dev, "addr": label, "word": word_index, "bits": True, "value": v, "prev": pv,
                    "on": bits_on, "changed": changed_bits,
                    "labels": {b: bit_address(plc_type, r.dev, r.addr, word_index, b) for b in range(16)},
                    "names": {b: n for b, n in bit_names.items() if n}}
        label = word_address(plc_type, r.dev, r.addr, word_index)
        return {"dev": r.dev, "addr": label, "word": word_index, "bits": False, "value": v, "prev": pv,
                "name": names.get(label.upper(), "")}

    def _word_label(self, plc_type: str, r: ScanRange, word_index: int) -> str:
        if plc_type in ("keyence", "mock") and r.dev in KEYENCE_CH_DEVICES:
            return f"{r.dev}{(r.addr // 100) + word_index}xx"
        return f"{bit_address(plc_type, r.dev, r.addr, word_index, 0)}〜"

    def status(self) -> dict:
        c = self.current
        if c is None:
            return {"busy": False, "has_prev": self.last is not None}
        return {"busy": self.busy, "mode": c.mode, "progress": (c.read_words / c.total_words) if c.total_words else 1.0,
                "read_words": c.read_words, "total_words": c.total_words, "rows": len(c.rows), "errors": c.errors,
                "done": c.done, "elapsed": round((c.finished or time.time()) - c.started, 2),
                "has_prev": self.last is not None}
