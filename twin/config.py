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

import os
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
    group: str = ""       # ブロック名 (クロスリファレンスのプログラム名など。空なら自動分け)

    def to_dict(self) -> dict:
        d = {"name": self.name, "comment": self.comment, "address": self.address, "group": self.group}
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
    groups: list[str] = field(default_factory=list)   # names と同じ並びでブロック名 (任意)
    source: str = ""      # "import" = CSV 取込 (.tags.yaml) 由来
    error: str = ""       # 読めなかったときの理由 (実行時に付く。空なら正常)


@dataclass
class TwinConfig:
    plc_type: str
    plc: dict
    out: BlockSpec
    inp: BlockSpec
    cycle_ms: float
    cycle_auto: bool          # True: 起動時に往復時間を実測して最速周期を決める
    spin_ms: float            # 周期直前のスピン待ち時間 (精度 vs CPU)
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
                    "port": self.plc.get("port", ""), "driver": self.plc.get("driver", "")},
            "cycle_ms": self.cycle_ms,
            "cycle_auto": self.cycle_auto,
            "parallel_rw": bool(self.plc.get("parallel_rw", False)),
            "protocol": str(self.plc.get("protocol", "tcp")),
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
HEX_WORD_DEVICES = {"keyence": {"W"}, "melsec": {"W", "SW", "ZR"}}
KEYENCE_CH_DEVICES = {"MR", "R", "LR", "CR", "VB"}
SYSTEM_DEVICES = {"keyence": {"CM", "CR"}, "melsec": {"SM", "SD", "SB", "SW"}, "mock": {"CM", "CR"}}


def is_hex_device(plc_type: str, dev: str) -> bool:
    dev = dev.upper()
    return dev in HEX_BIT_DEVICES.get(plc_type, set()) or dev in HEX_WORD_DEVICES.get(plc_type, set())


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
    if is_hex_device(plc_type, dev):
        if plc_type == "keyence" and dev == "W":
            return f"{dev}{(word_addr + word_off):04X}"
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
_REF_NAME = re.compile(r"^(out|in|watch)\.(\S+)$")


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
    return Tag(name, ref, comment, address, str(d.get("group", "")))


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


def sidecar_path(path: str) -> str:
    """画面から編集した監視デバイスの保存先: <設定名>.watch.yaml"""
    base, _ = os.path.splitext(path)
    return base + ".watch.yaml"


def tags_sidecar_path(path: str) -> str:
    """CSV 取込で生成した tags/watch の保存先: <設定名>.tags.yaml (存在すれば自動で include)"""
    base, _ = os.path.splitext(path)
    return base + ".tags.yaml"


def _merge_include(raw: dict, base_dir: str) -> dict:
    """include: [a.yaml, ...] の tags / watch / objects を取り込む (本体側が後ろに付く)。"""
    incs = raw.get("include") or []
    if isinstance(incs, str):
        incs = [incs]
    tags = {"out": [], "in": [], "watch": []}
    watch: list = []
    objects: list = []
    for inc in incs:
        p = inc if os.path.isabs(inc) else os.path.join(base_dir, inc)
        if not os.path.isfile(p):
            raise ValueError(f"include のファイルがありません: {p}")
        with open(p, "r", encoding="utf-8") as f:
            sub = yaml.safe_load(f) or {}
        for b in tags:
            tags[b] += list((sub.get("tags") or {}).get(b) or [])
        watch += list(sub.get("watch") or [])
        objects += list(sub.get("objects") or [])
    own = raw.get("tags") or {}
    raw["tags"] = {b: tags[b] + list(own.get(b) or []) for b in tags}
    raw["watch"] = watch + list(raw.get("watch") or [])
    raw["objects"] = objects + list(raw.get("objects") or [])
    return raw


def load_config(path: str) -> TwinConfig:
    """読込順: 本体 YAML + include → <名前>.watch.yaml があれば watch を置換 → <名前>.tags.yaml (CSV 取込) を追加。"""
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    ts = tags_sidecar_path(path)
    incs = raw.get("include") or []
    incs = [incs] if isinstance(incs, str) else list(incs)
    raw["include"] = [i for i in incs if os.path.basename(i) != os.path.basename(ts)]   # .tags.yaml は自動読込 (二重防止)
    raw = _merge_include(raw, os.path.dirname(os.path.abspath(path)))
    sc = sidecar_path(path)
    if os.path.isfile(sc):
        with open(sc, "r", encoding="utf-8") as f:
            side = yaml.safe_load(f) or {}
        if isinstance(side, dict) and "watch" in side:
            raw["watch"] = list(side["watch"] or [])
    ts = tags_sidecar_path(path)
    if os.path.isfile(ts):
        with open(ts, "r", encoding="utf-8") as f:
            imp = yaml.safe_load(f) or {}
        for b in ("out", "in", "watch"):
            raw["tags"][b] = list((imp.get("tags") or {}).get(b) or []) + raw["tags"][b]
        for w in imp.get("watch") or []:
            w = dict(w)
            w["source"] = "import"
            raw["watch"].append(w)
    return parse_config(raw)


def save_watch(path: str, watch: list) -> str:
    """UI から保存。CSV 取込由来 (source: import) は .tags.yaml 側にあるので除く。"""
    watch = [dict(w) for w in watch if w.get("source") != "import"]
    for w in watch:
        w.pop("source", None)
    sc = sidecar_path(path)
    with open(sc, "w", encoding="utf-8") as f:
        f.write("# 画面 (監視デバイス) から保存した追加監視。元の YAML の watch より優先される。\n")
        yaml.safe_dump({"watch": watch}, f, allow_unicode=True, sort_keys=False)
    return sc


def parse_config(raw: dict) -> TwinConfig:
    plc = dict(raw.get("plc") or {})
    plc_type = str(plc.get("type", "mock")).lower()
    if plc_type not in ("keyence", "melsec", "mock"):
        raise ValueError(f"plc.type は keyence / melsec / mock のいずれかです: {plc_type}")
    default_dev = "D" if plc_type == "melsec" else "DM"
    out = _parse_block(plc.get("out"), default_dev, 1000)
    inp = _parse_block(plc.get("in"), default_dev, 1100)
    cyc = raw.get("cycle_ms", 10)
    cycle_auto = isinstance(cyc, str) and cyc.strip().lower() in ("auto", "max", "fastest", "最速")
    cycle_ms = 10.0 if cycle_auto else float(cyc)
    if cycle_ms < 0.5:
        raise ValueError("cycle_ms は 0.5 以上 (または auto) にしてください")

    watch: list[WatchSpec] = []
    off = 0
    for w in raw.get("watch") or []:
        dev = str(w["dev"]).upper()
        addr = w.get("addr", 0)
        if isinstance(addr, str):
            addr = int(addr, 16) if is_hex_device(plc_type, dev) else int(addr)
        words = int(w.get("words", 1))
        bits = bool(w.get("bits", is_bit_device(plc_type, dev)))
        names = [str(n) for n in (w.get("names") or [])]
        groups_ = [str(n) for n in (w.get("groups") or [])]
        watch.append(WatchSpec(dev, int(addr), words, off, bits, names, groups_, str(w.get("source", ""))))
        off += words
    watch_words = off
    watch_every = int(raw.get("watch_every", 2))

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
                        g = w.groups[i * 16 + b] if i * 16 + b < len(w.groups) else ""
                        tags["watch"].append(Tag(nm, Ref("watch", "bit", w.offset + i, b, "u16", nm),
                                                 "", bit_address(plc_type, w.dev, w.addr, i, b), g))
            else:
                nm = w.names[i] if i < len(w.names) else word_address(plc_type, w.dev, w.addr, i)
                if nm:
                    g = w.groups[i] if i < len(w.groups) else ""
                    tags["watch"].append(Tag(nm, Ref("watch", "word", w.offset + i, 0, "u16", nm),
                                             "", word_address(plc_type, w.dev, w.addr, i), g))
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
        plc_type=plc_type, plc=plc, out=out, inp=inp, cycle_ms=cycle_ms, cycle_auto=cycle_auto,
        spin_ms=float(raw.get("spin_ms", 1.5)),
        watch=watch, watch_words=watch_words, watch_every=max(1, watch_every),
        tags=tags, objects=[_normalize_keys(o) for o in (raw.get("objects") or [])],
        record_seconds=float(raw.get("record_seconds", 120)),
        title=str(raw.get("title", "PLC Digital Twin")), raw=raw,
    )
    # objects の参照を事前検証 (エラーは起動時に出す)
    from .sim.behaviors import validate_objects  # 循環回避のため遅延import
    validate_objects(cfg)
    return cfg
