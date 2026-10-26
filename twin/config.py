"""装置定義YAMLの読み込みとタグ解決。

YAML構造 (抜粋):
  plc: {type: keyence|melsec|mock, ip:, port:, out: {dev: DM, addr: 1000, words: 64},
        in: {dev: DM, addr: 1100, words: 64}}
  cycle_ms: 10
  watch: [{dev: DM, addr: 100, words: 8}, {dev: MR, addr: 100, words: 2}]
  tags:
    out: [{name: CONV1_RUN, bit: 0.0}, {name: AX1_TARGET, word: 10, type: s32}]
    in:  [{name: CYL1_END, bit: 0.0}, {name: AX1_POS, word: 10, type: s32}]
  objects: [...]

タグ参照 (objects 内で使う文字列):
  out.NAME / in.NAME / watch.NAME   名前で参照
  out.5                              ブロック先頭からのビット番号 (word0 bit5)
  out.0.3                            word0 の bit3
  out.w10                            word10 (u16)
  out.w10:s32                        word10,11 を signed 32bit として
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Any

import yaml

DTYPES = ("u16", "s16", "u32", "s32")
BLOCKS = ("out", "in", "watch")


@dataclass
class BlockSpec:
    dev: str
    addr: int
    words: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Ref:
    """ブロック内の1ビット or 1ワード(複数ワード)を指す。"""
    block: str            # out / in / watch
    kind: str             # bit / word
    word: int
    bit: int = 0
    dtype: str = "u16"
    name: str = ""

    @property
    def nwords(self) -> int:
        return 2 if self.dtype in ("u32", "s32") else 1

    @property
    def bit_index(self) -> int:
        return self.word * 16 + self.bit

    def key(self) -> str:
        if self.kind == "bit":
            return f"{self.block}.{self.word}.{self.bit}"
        return f"{self.block}.w{self.word}:{self.dtype}"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Tag:
    name: str
    ref: Ref
    comment: str = ""
    address: str = ""     # PLC上の実アドレス表記 (表示用)

    def to_dict(self) -> dict:
        d = {"name": self.name, "comment": self.comment, "address": self.address}
        d.update(self.ref.to_dict())
        return d


@dataclass
class WatchSpec:
    dev: str
    addr: int
    words: int
    offset: int = 0       # watch ワード配列内の先頭オフセット
    bits: bool = False    # ビットデバイスか (表示をビット展開)
    names: list[str] = field(default_factory=list)


@dataclass
class TwinConfig:
    plc_type: str
    plc: dict
    out: BlockSpec
    inp: BlockSpec
    cycle_ms: float
    watch: list[WatchSpec]
    watch_words: int
    watch_every: int
    tags: dict[str, list[Tag]]
    objects: list[dict]
    record_seconds: float
    title: str
    raw: dict

    # --- タグ検索 -----------------------------------------------------
    def tag(self, block: str, name: str) -> Tag | None:
        for t in self.tags.get(block, []):
            if t.name == name:
                return t
        return None

    def resolve(self, ref: str | None) -> Ref | None:
        """文字列参照を Ref に解決する。None はそのまま返す。"""
        if ref is None:
            return None
        return resolve_ref(str(ref), self)

    def to_public(self) -> dict:
        """ブラウザへ渡す設定 (IPなど不要なものは落とす)。"""
        return {
            "title": self.title,
            "plc": {"type": self.plc_type, "ip": self.plc.get("ip", ""),
                    "port": self.plc.get("port", "")},
            "cycle_ms": self.cycle_ms,
            "out": self.out.to_dict(),
            "in": self.inp.to_dict(),
            "watch": [asdict(w) for w in self.watch],
            "watch_words": self.watch_words,
            "tags": {b: [t.to_dict() for t in ts] for b, ts in self.tags.items()},
            "objects": self.objects,
            "record_seconds": self.record_seconds,
        }


# ---------------------------------------------------------------------------
# アドレス表記 (表示用)
# ---------------------------------------------------------------------------
HEX_BIT_DEVICES = {"keyence": {"B"}, "melsec": {"X", "Y", "B", "SB", "DX", "DY"}}
KEYENCE_CH_DEVICES = {"MR", "R", "LR", "CR", "VB"}


def bit_address(plc_type: str, dev: str, word_addr: int, word_off: int, bit: int) -> str:
    """ワード単位で読んだビットデバイスの、各ビットの実アドレス表記。"""
    dev = dev.upper()
    if plc_type == "keyence" and dev in KEYENCE_CH_DEVICES:
        ch = word_addr // 100 + word_off
        return f"{dev}{ch}{bit:02d}"
    if dev in HEX_BIT_DEVICES.get(plc_type, set()):
        return f"{dev}{(word_addr + word_off * 16 + bit):X}"
    return f"{dev}{word_addr + word_off * 16 + bit}"


def word_address(plc_type: str, dev: str, word_addr: int, word_off: int) -> str:
    dev = dev.upper()
    if dev in HEX_BIT_DEVICES.get(plc_type, set()) or (plc_type == "melsec" and dev in ("W", "SW", "ZR")):
        return f"{dev}{(word_addr + word_off):X}"
    return f"{dev}{word_addr + word_off}"


def is_bit_device(plc_type: str, dev: str) -> bool:
    dev = dev.upper()
    if plc_type == "keyence":
        return dev in KEYENCE_CH_DEVICES | {"B"}
    if plc_type == "melsec":
        return dev in {"X", "Y", "M", "L", "F", "V", "B", "S", "SM", "SB", "DX", "DY"}
    return dev in {"M", "MR", "X", "Y"}


# ---------------------------------------------------------------------------
# 参照解決
# ---------------------------------------------------------------------------
_REF_NUM = re.compile(r"^(out|in|watch)\.(\d+)$")
_REF_WB = re.compile(r"^(out|in|watch)\.(\d+)\.(\d+)$")
_REF_W = re.compile(r"^(out|in|watch)\.w(\d+)(?::(u16|s16|u32|s32))?$")
_REF_NAME = re.compile(r"^(out|in|watch)\.([A-Za-z_][A-Za-z0-9_\-]*)$")


def resolve_ref(s: str, cfg: TwinConfig | None = None) -> Ref:
    s = s.strip()
    m = _REF_WB.match(s)
    if m:
        return Ref(m.group(1), "bit", int(m.group(2)), int(m.group(3)))
    m = _REF_NUM.match(s)
    if m:
        n = int(m.group(2))
        return Ref(m.group(1), "bit", n // 16, n % 16)
    m = _REF_W.match(s)
    if m:
        return Ref(m.group(1), "word", int(m.group(2)), 0, m.group(3) or "u16")
    m = _REF_NAME.match(s)
    if m:
        if cfg is None:
            raise ValueError(f"名前参照 {s!r} を解決するには設定が必要です")
        t = cfg.tag(m.group(1), m.group(2))
        if t is None:
            raise ValueError(f"タグ {s!r} は tags.{m.group(1)} に定義されていません")
        r = Ref(**asdict(t.ref))
        r.name = t.name
        return r
    raise ValueError(f"参照の書式が不正です: {s!r}")


def _parse_tag(block: str, d: dict, plc_type: str, spec: BlockSpec | None, watch: list[WatchSpec]) -> Tag:
    name = str(d["name"])
    comment = str(d.get("comment", ""))
    if "bit" in d:
        b = d["bit"]
        if isinstance(b, float):
            # YAML が 0.1 を float にしてしまう -> 文字列で "0.1" と書く推奨。救済:
            s = repr(b)
            w, bit = s.split(".")
            word, bit = int(w), int(bit)
        elif isinstance(b, str) and "." in b:
            w, bit = b.split(".")
            word, bit = int(w), int(bit)
        else:
            n = int(b)
            word, bit = n // 16, n % 16
        if bit > 15:
            raise ValueError(f"タグ {name}: bit は 0〜15 です ({b})")
        ref = Ref(block, "bit", word, bit, "u16", name)
    elif "word" in d:
        dtype = str(d.get("type", "u16"))
        if dtype not in DTYPES:
            raise ValueError(f"タグ {name}: type は {DTYPES} のいずれかです")
        ref = Ref(block, "word", int(d["word"]), 0, dtype, name)
    else:
        raise ValueError(f"タグ {name}: bit か word を指定してください")

    # 実アドレス表記
    address = ""
    if block in ("out", "in") and spec is not None:
        if ref.word >= spec.words:
            raise ValueError(f"タグ {name}: word {ref.word} はブロック長 {spec.words} を超えています")
        if ref.kind == "bit":
            address = f"{word_address(plc_type, spec.dev, spec.addr, ref.word)}.{ref.bit:X}"
        else:
            address = word_address(plc_type, spec.dev, spec.addr, ref.word)
    elif block == "watch":
        for w in watch:
            if w.offset <= ref.word < w.offset + w.words:
                off = ref.word - w.offset
                if ref.kind == "bit":
                    address = bit_address(plc_type, w.dev, w.addr, off, ref.bit) if w.bits \
                        else f"{word_address(plc_type, w.dev, w.addr, off)}.{ref.bit:X}"
                else:
                    address = word_address(plc_type, w.dev, w.addr, off)
                break
    return Tag(name, ref, comment, address)


def _normalize_keys(d: dict) -> dict:
    """YAML 1.1 は on/off/yes/no をブール値にする。キー True/False を 'on'/'off' に戻す。"""
    out = {}
    for k, v in d.items():
        if k is True:
            k = "on"
        elif k is False:
            k = "off"
        out[k] = _normalize_keys(v) if isinstance(v, dict) else v
    return out


def _parse_block(d: Any, default_dev: str, default_addr: int) -> BlockSpec:
    if d is None:
        return BlockSpec(default_dev, default_addr, 64)
    if isinstance(d, str):
        # "DM1000" 形式
        m = re.match(r"^([A-Za-z]+)(\d+)$", d.strip())
        if not m:
            raise ValueError(f"ブロック指定が不正です: {d!r}")
        return BlockSpec(m.group(1).upper(), int(m.group(2)), 64)
    return BlockSpec(str(d.get("dev", default_dev)).upper(), int(d.get("addr", default_addr)),
                     int(d.get("words", 64)))


def load_config(path: str) -> TwinConfig:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return parse_config(raw)


def parse_config(raw: dict) -> TwinConfig:
    plc = dict(raw.get("plc") or {})
    plc_type = str(plc.get("type", "mock")).lower()
    if plc_type not in ("keyence", "melsec", "mock"):
        raise ValueError(f"plc.type は keyence / melsec / mock のいずれかです: {plc_type}")
    default_dev = "D" if plc_type == "melsec" else "DM"
    out = _parse_block(plc.get("out"), default_dev, 1000)
    inp = _parse_block(plc.get("in"), default_dev, 1100)
    cycle_ms = float(raw.get("cycle_ms", 10))
    if cycle_ms < 1:
        raise ValueError("cycle_ms は 1 以上にしてください")

    watch: list[WatchSpec] = []
    off = 0
    for w in raw.get("watch") or []:
        dev = str(w["dev"]).upper()
        addr = w.get("addr", 0)
        if isinstance(addr, str):
            addr = int(addr, 16) if dev in HEX_BIT_DEVICES.get(plc_type, set()) | {"W", "ZR"} else int(addr)
        words = int(w.get("words", 1))
        bits = bool(w.get("bits", is_bit_device(plc_type, dev)))
        names = [str(n) for n in (w.get("names") or [])]
        watch.append(WatchSpec(dev, int(addr), words, off, bits, names))
        off += words
    watch_words = off
    watch_every = int(raw.get("watch_every", 10))

    tags_raw = raw.get("tags") or {}
    tags: dict[str, list[Tag]] = {"out": [], "in": [], "watch": []}
    for block in ("out", "in"):
        spec = out if block == "out" else inp
        for d in tags_raw.get(block) or []:
            tags[block].append(_parse_tag(block, d, plc_type, spec, watch))
    # watch は自動タグ生成 + 明示タグ
    for w in watch:
        for i in range(w.words):
            if w.bits:
                for b in range(16):
                    nm = w.names[i * 16 + b] if i * 16 + b < len(w.names) else bit_address(plc_type, w.dev, w.addr, i, b)
                    if nm:
                        tags["watch"].append(Tag(nm, Ref("watch", "bit", w.offset + i, b, "u16", nm),
                                                 "", bit_address(plc_type, w.dev, w.addr, i, b)))
            else:
                nm = w.names[i] if i < len(w.names) else word_address(plc_type, w.dev, w.addr, i)
                if nm:
                    tags["watch"].append(Tag(nm, Ref("watch", "word", w.offset + i, 0, "u16", nm),
                                             "", word_address(plc_type, w.dev, w.addr, i)))
    for d in tags_raw.get("watch") or []:
        tags["watch"].append(_parse_tag("watch", d, plc_type, None, watch))

    # 名前重複チェック
    for block, ts in tags.items():
        seen = set()
        for t in ts:
            if t.name in seen:
                raise ValueError(f"tags.{block} に同名タグがあります: {t.name}")
            seen.add(t.name)

    cfg = TwinConfig(
        plc_type=plc_type, plc=plc, out=out, inp=inp, cycle_ms=cycle_ms,
        watch=watch, watch_words=watch_words, watch_every=max(1, watch_every),
        tags=tags, objects=[_normalize_keys(o) for o in (raw.get("objects") or [])],
        record_seconds=float(raw.get("record_seconds", 120)),
        title=str(raw.get("title", "PLC Digital Twin")), raw=raw,
    )
    # objects の参照を事前検証 (エラーは起動時に出す)
    from .sim.behaviors import validate_objects  # 循環回避のため遅延import
    validate_objects(cfg)
    return cfg
