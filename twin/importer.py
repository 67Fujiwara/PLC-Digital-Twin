"""PLC のラベル (グローバル/ローカル変数) やデバイスコメントの CSV から tags / watch を自動生成する。

対応する CSV (列名で自動判別。文字コードは UTF-8 / Shift_JIS どちらでも可):
  - GX Works3  ラベルエディタ → 「CSV 書き出し」 (クラス, ラベル名, データ型, 割付（デバイス/ラベル）, コメント …)
  - GX Works3  デバイスコメント → 「CSV 書き出し」 (デバイス名, コメント)
  - KV STUDIO  変数 (KV-8000) → エクスポート (変数名, データ型, デバイス, コメント …)
  - KV STUDIO  デバイスコメント → エクスポート (デバイス, コメント)  ※ヘッダなしでも可
  - 手書き: 1列目 デバイス, 2列目 名前, 3列目 コメント

使い方:
  python -m twin.importer labels.csv --config configs/line_a.yaml --out configs/line_a.tags.yaml
  → line_a.yaml に  include: [line_a.tags.yaml]  を書くと tags / watch として読み込まれる。

割付ルール:
  - デバイスが out ブロック (例 DM1000〜1063) 内 → tags.out、in ブロック内 → tags.in
  - それ以外 → watch (連続するデバイスをまとめて 1 エントリ、各位置に名前を付ける)
  - データ型: BOOL/ビット → bit、INT → s16、DINT → s32、UINT/WORD → u16、UDINT/DWORD → u32
  - 割付なし (自動割付) のラベルは通信で読めないので除外し、一覧に出す
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import re
import sys
from dataclasses import dataclass, field

import yaml

from .config import (BlockSpec, KEYENCE_CH_DEVICES, SYSTEM_DEVICES, is_bit_device, is_hex_device,
                     load_config, TwinConfig)

NAME_KEYS = ("ラベル名", "変数名", "label name", "name", "名前", "変数")
DEV_KEYS = ("割付", "デバイス", "device", "assign")
TYPE_KEYS = ("データ型", "data type", "type", "型")
COMMENT_KEYS = ("コメント", "comment", "説明")
CLASS_KEYS = ("クラス", "class")

DTYPE_MAP = {
    "bool": "bit", "bit": "bit", "ビット": "bit", "b": "bit",
    "int": "s16", "int16": "s16", "signed16": "s16", "符号付き16": "s16",
    "dint": "s32", "int32": "s32", "signed32": "s32", "符号付き32": "s32",
    "uint": "u16", "word": "u16", "uint16": "u16", "word[unsigned]": "u16", "unsigned16": "u16", "符号なし16": "u16",
    "udint": "u32", "dword": "u32", "uint32": "u32", "dword[unsigned]": "u32", "unsigned32": "u32", "符号なし32": "u32",
    "word [unsigned]/bit string [16-bit]": "u16", "double word [unsigned]/bit string [32-bit]": "u32",
    "word [signed]": "s16", "double word [signed]": "s32",
}
DEV_RE = re.compile(r"^([A-Za-z]{1,3})([0-9A-Fa-f]+)(?:\.([0-9A-Fa-f]{1,2}))?$")


@dataclass
class Label:
    name: str
    device: str
    dtype: str          # bit/u16/s16/u32/s32
    comment: str = ""
    dev: str = ""
    num: int = 0
    bit: int | None = None   # ワードデバイスのビット指定 (DM1000.5 / D1000.3)
    row: int = 0


@dataclass
class ImportResult:
    tags: dict = field(default_factory=lambda: {"out": [], "in": []})
    watch: list = field(default_factory=list)
    skipped: list = field(default_factory=list)   # (name, reason)
    count: int = 0


# ---------------------------------------------------------------------------
# CSV 読込
# ---------------------------------------------------------------------------
def read_csv_text(path: str) -> str:
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "cp932", "utf-8", "utf-16"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _find_col(header: list[str], keys: tuple[str, ...]) -> int | None:
    low = [h.strip().lower() for h in header]
    for k in keys:
        for i, h in enumerate(low):
            if k.lower() in h:
                return i
    return None


def parse_labels(text: str) -> list[Label]:
    rows = list(csv.reader(io.StringIO(text)))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return []
    # ヘッダ行を探す (先頭 5 行以内で名前列かデバイス列を含む行)
    hdr_i = None
    for i, r in enumerate(rows[:5]):
        if _find_col(r, NAME_KEYS) is not None or _find_col(r, DEV_KEYS) is not None:
            hdr_i = i
            break
    labels: list[Label] = []
    if hdr_i is None:
        # ヘッダなし: [デバイス, 名前?, コメント?]
        for n, r in enumerate(rows):
            if not DEV_RE.match(r[0].strip()):
                continue
            dev = r[0].strip()
            name = r[1].strip() if len(r) > 1 and r[1].strip() else ""
            comment = r[2].strip() if len(r) > 2 else ""
            if not name:
                name, comment = (comment or dev), ""
            labels.append(Label(name, dev, "", comment, row=n + 1))
        return labels
    h = rows[hdr_i]
    ci_name, ci_dev, ci_type, ci_cmt, ci_cls = (_find_col(h, NAME_KEYS), _find_col(h, DEV_KEYS),
                                                _find_col(h, TYPE_KEYS), _find_col(h, COMMENT_KEYS),
                                                _find_col(h, CLASS_KEYS))
    # "デバイス名, コメント" 形式 (名前列なし) は名前 = コメント
    for n, r in enumerate(rows[hdr_i + 1:]):
        get = lambda i: (r[i].strip() if i is not None and i < len(r) else "")  # noqa: E731
        dev = get(ci_dev)
        name = get(ci_name)
        comment = get(ci_cmt)
        dtype = get(ci_type)
        if ci_name is None or (ci_name == ci_dev):
            name = ""
        if not dev and DEV_RE.match(name or ""):
            dev, name = name, ""
        if not name:
            name = comment or dev
        if not dev and not name:
            continue
        labels.append(Label(name, dev, dtype, comment if comment != name else "", row=hdr_i + n + 2))
    return labels


# ---------------------------------------------------------------------------
# デバイス解釈
# ---------------------------------------------------------------------------
def parse_device(plc_type: str, s: str) -> tuple[str, int, int | None] | None:
    """'DM1000' / 'DM1000.5' / 'MR100' / 'D1000.3' / 'X1F' -> (dev, number, bit_of_word)"""
    s = s.strip().upper().replace(" ", "")
    m = DEV_RE.match(s)
    if not m:
        return None
    dev, num, bit = m.group(1), m.group(2), m.group(3)
    is_hex = is_hex_device(plc_type, dev)
    try:
        n = int(num, 16) if is_hex else int(num)
        b = int(bit, 16) if bit is not None else None
    except ValueError:
        return None
    return dev, n, b


def norm_dtype(s: str) -> str:
    key = s.strip().lower().replace("　", " ")
    if not key:
        return ""
    if key in DTYPE_MAP:
        return DTYPE_MAP[key]
    for k, v in DTYPE_MAP.items():
        if key.startswith(k):
            return v
    if "bool" in key or "ビット" in key:
        return "bit"
    if "double" in key or "32" in key or "dint" in key or "dword" in key:
        return "u32" if ("unsigned" in key or "dword" in key or "符号なし" in key) else "s32"
    if "unsigned" in key or "word" in key or "符号なし" in key:
        return "u16"
    if "int" in key or "符号付き" in key:
        return "s16"
    return ""


def sanitize(name: str, used: set[str]) -> str:
    n = re.sub(r"[\s.]+", "_", name.strip()) or "tag"
    base, i = n, 2
    while n in used:
        n = f"{base}_{i}"
        i += 1
    used.add(n)
    return n


def build(labels: list[Label], plc_type: str, out: BlockSpec, inp: BlockSpec,
          include_system: bool = False, used: set[str] | None = None) -> ImportResult:
    res = ImportResult()
    used = set() if used is None else used
    watch_words: dict[tuple[str, int], dict] = {}   # (dev, word_addr) -> {"bits": bool, "names": {idx: name}}
    sys_skipped = 0
    for lb in labels:
        if not lb.device:
            res.skipped.append((lb.name, "デバイス割付なし (自動割付のラベルは通信で読めません)"))
            continue
        pd = parse_device(plc_type, lb.device)
        if pd is None:
            res.skipped.append((lb.name, f"デバイス表記を解釈できません: {lb.device}"))
            continue
        dev, num, wbit = pd
        if not include_system and dev in SYSTEM_DEVICES.get(plc_type, set()):
            sys_skipped += 1
            continue
        bitdev = is_bit_device(plc_type, dev)
        dtype = norm_dtype(lb.dtype)
        if bitdev:
            dtype = "bit"
        elif wbit is not None:
            dtype = "bit"
        elif not dtype:
            dtype = "u16"
        if dtype in ("u16", "s16", "u32", "s32") and not bitdev and dev not in ("DM", "EM", "FM", "ZF", "W", "TM", "CM", "D", "R", "ZR", "SD", "SW", "Z"):
            # T/C など
            pass
        name = sanitize(lb.name, used)
        res.count += 1
        # --- ブロック内か ---
        placed = False
        for block, spec in (("out", out), ("in", inp)):
            if dev == spec.dev and not bitdev and spec.addr <= num < spec.addr + spec.words:
                w = num - spec.addr
                if dtype == "bit":
                    res.tags[block].append({"name": name, "bit": f"{w}.{wbit or 0}", "comment": lb.comment})
                else:
                    d = {"name": name, "word": w}
                    if dtype != "u16":
                        d["type"] = dtype
                    if lb.comment:
                        d["comment"] = lb.comment
                    res.tags[block].append(d)
                placed = True
                break
        if placed:
            continue
        # --- watch ---
        if bitdev:
            if plc_type == "keyence" and dev in KEYENCE_CH_DEVICES:
                ch, bit = num // 100, num % 100
                if bit > 15:
                    res.skipped.append((lb.name, f"ビット番号が不正: {lb.device}"))
                    continue
                key = (dev, ch * 100)
            else:
                key = (dev, (num // 16) * 16)
                bit = num % 16
            e = watch_words.setdefault(key, {"bits": True, "names": {}})
            e["names"][bit] = name
        else:
            if dtype == "bit":
                res.skipped.append((lb.name, f"ワードデバイスのビット指定 ({lb.device}) は watch では未対応 (out/in ブロック内なら可)"))
                continue
            e = watch_words.setdefault((dev, num), {"bits": False, "names": {}, "dtype": dtype})
            e["names"][0] = name
            if dtype in ("u32", "s32"):
                watch_words.setdefault((dev, num + 1), {"bits": False, "names": {}, "dtype": "u16"})
    if sys_skipped:
        res.skipped.append((f"{sys_skipped} 件", f"システムデバイス {sorted(SYSTEM_DEVICES.get(plc_type, set()))} は除外 (--include-system で取込)"))
    # --- watch をデバイス種別ごとに連続範囲へまとめる (隙間 2 ワードまで結合, 1 エントリ 64 ワードまで) ---
    by_dev: dict[str, list[int]] = {}
    for (dev, addr) in watch_words:
        by_dev.setdefault(dev, []).append(addr)
    for dev, addrs in sorted(by_dev.items()):
        addrs.sort()
        bits = watch_words[(dev, addrs[0])]["bits"]
        step = 100 if (plc_type == "keyence" and dev in KEYENCE_CH_DEVICES) else (16 if bits else 1)
        groups: list[list[int]] = []
        for a in addrs:
            if groups and (a - groups[-1][-1]) // step <= 3 and (a - groups[-1][0]) // step < 64:
                groups[-1].append(a)
            else:
                groups.append([a])
        for g in groups:
            words = (g[-1] - g[0]) // step + 1
            names: list[str] = []
            for i in range(words):
                a = g[0] + i * step
                e = watch_words.get((dev, a))
                if bits:
                    names += [(e["names"].get(b, "") if e else "") for b in range(16)]
                else:
                    names.append(e["names"].get(0, "") if e else "")
            entry = {"dev": dev, "addr": g[0], "words": words, "bits": bits, "names": names}
            res.watch.append(entry)
    return res


def import_text(text: str, cfg: TwinConfig, include_system: bool = False,
                replaceable: set[str] | None = None) -> ImportResult:
    """CSV テキスト -> ImportResult。既存タグ名と重複しない名前を付ける (replaceable = 前回取込分は除く)。"""
    labels = parse_labels(text)
    used = {t.name for b in cfg.tags.values() for t in b} - (replaceable or set())
    return build(labels, cfg.plc_type, cfg.out, cfg.inp, include_system=include_system, used=used)


def to_yaml(res: ImportResult, source: str) -> str:
    head = (f"# {os.path.basename(source)} から自動生成 (python -m twin.importer)。\n"
            f"# 元の設定 YAML に  include: [{'<このファイル名>'}]  を書いて読み込む。再インポートで上書きされる。\n")
    return head + yaml.safe_dump({"tags": res.tags, "watch": res.watch}, allow_unicode=True, sort_keys=False, width=120)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="twin.importer", description="PLC ラベル/デバイスコメント CSV → tags/watch YAML")
    ap.add_argument("csv", help="GX Works3 / KV STUDIO から書き出した CSV")
    ap.add_argument("--config", required=True, help="装置の設定 YAML (plc.type, out/in ブロックを参照)")
    ap.add_argument("--out", help="出力 YAML (省略時は <config>.tags.yaml)")
    ap.add_argument("--dry-run", action="store_true", help="書き出さず内容だけ表示")
    ap.add_argument("--include-system", action="store_true", help="システムデバイス (KV: CM/CR, R: SM/SD) も取り込む")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    labels = parse_labels(read_csv_text(args.csv))
    if not labels:
        print("CSV からラベルを読み取れませんでした (列名: ラベル名/変数名, 割付/デバイス, データ型, コメント)", file=sys.stderr)
        return 2
    res = build(labels, cfg.plc_type, cfg.out, cfg.inp, include_system=args.include_system)
    out_path = args.out or (os.path.splitext(args.config)[0] + ".tags.yaml")
    text = to_yaml(res, args.csv)
    print(f"読込 {len(labels)} 行 → tags.out {len(res.tags['out'])}, tags.in {len(res.tags['in'])}, "
          f"watch {len(res.watch)} エントリ ({sum(w['words'] for w in res.watch)} ワード), 除外 {len(res.skipped)}")
    for name, why in res.skipped[:30]:
        print(f"  除外: {name}: {why}")
    if len(res.skipped) > 30:
        print(f"  ... 他 {len(res.skipped) - 30} 件")
    if args.dry_run:
        print(text)
        return 0
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(text)
    inc = os.path.basename(out_path)
    print(f"書出し: {out_path}")
    if out_path == os.path.splitext(args.config)[0] + ".tags.yaml":
        print("→ 設定ファイルと同じ名前の .tags.yaml なので、次回起動時に自動で読み込まれます")
    elif inc not in (cfg.raw.get("include") or []):
        print(f"→ {args.config} に次を追加してください:\n    include: [{inc}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
