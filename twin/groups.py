"""デバイスのブロック (グループ) 分け。

- 自動: tags に group があればそれ (クロスリファレンス CSV のプログラム名など)。無ければ
  「PLC → PC」「PC → PLC」+ 監視デバイスをデバイス種別ごとにアドレスの隙間で分けたまとまり
  (KV の割付慣習: MR300〜 異常, MR800〜 SOL 排斥REQ, DM2000〜 レシピ … がそのまま出る)
- 手動: 画面で名前変更・移動・並べ替えしたものを <設定>.groups.yaml に保存し、次回も使う
"""
from __future__ import annotations

import os
import re
from collections import Counter

import yaml

from .config import TwinConfig, Tag, KEYENCE_CH_DEVICES

GAP_WORDS = 20      # ワードデバイス: この隙間 (ワード) で別ブロック
GAP_CH = 2          # KV リレー: この隙間 (ch) で別ブロック
GAP_BITS = 48       # 三菱ビット: この隙間 (点) で別ブロック
STOP = {"No", "TP", "SV", "PV", "FF", "PB", "PL", "SYS", "Sys", "to", "from", "ms", "ON", "OFF"}


def groups_path(config_path: str) -> str:
    return os.path.splitext(config_path)[0] + ".groups.yaml"


def _addr_num(plc_type: str, t: Tag, cfg: TwinConfig) -> tuple[str, float]:
    """並べ替え・隙間判定用の (デバイス種別, 連続番号)。"""
    r = t.ref
    for w in cfg.watch:
        if w.offset <= r.word < w.offset + w.words:
            off = r.word - w.offset
            if w.bits:
                if plc_type in ("keyence", "mock") and w.dev in KEYENCE_CH_DEVICES:
                    return w.dev, (w.addr // 100 + off) + r.bit / 16.0          # ch 単位
                return w.dev, (w.addr + off * 16 + r.bit) / 16.0                # 16 点 = 1
            return w.dev, float(w.addr + off)
    return "?", float(r.word)


def _tokens(name: str) -> list[str]:
    base = re.sub(r"[\[【(（].*?[\]】)）]", "", name)
    out = []
    for tok in re.split(r"[_\s　/\-]+", base):
        tok = re.sub(r"^\d+|\d+$", "", tok)              # 前後の数字を落とす (01SOL → SOL, MR301 → MR)
        tok = re.sub(r"^(MR|DM|LR|CR|CM|R|B|W|T|C|D|M|X|Y)$", "", tok)
        if len(tok) >= 2 and tok not in STOP:
            out.append(tok)
    return out


def _hint(tags: list[Tag]) -> str:
    """ブロック内の名前からよく出る語を 1 つ拾ってヒントにする。"""
    words: Counter = Counter()
    for t in tags:
        for tok in set(_tokens(t.name)):
            words[tok] += 1
    if not words:
        return ""
    best = max(words.items(), key=lambda kv: (kv[1], len(kv[0])))
    return best[0] if best[1] >= max(2, len(tags) // 3) else ""


def auto_groups(cfg: TwinConfig) -> list[dict]:
    groups: list[dict] = []
    # 明示 group (クロスリファレンスなど)
    explicit: dict[str, list[str]] = {}
    rest: list[Tag] = []
    for b in ("out", "in", "watch"):
        for t in cfg.tags.get(b, []):
            g = getattr(t, "group", "") or ""
            if g:
                explicit.setdefault(g, []).append(f"{b}.{t.name}")
            elif b == "out":
                explicit.setdefault("PLC → PC", []).append(f"{b}.{t.name}")
            elif b == "in":
                explicit.setdefault("PC → PLC", []).append(f"{b}.{t.name}")
            else:
                rest.append(t)
    for name in ("PLC → PC", "PC → PLC"):
        if name in explicit:
            groups.append({"name": name, "keys": explicit.pop(name), "auto": True})
    for name, keys in explicit.items():
        groups.append({"name": name, "keys": keys, "auto": True})
    # 監視デバイス: 種別ごとにアドレス順 → 隙間で分割
    by_dev: dict[str, list[tuple[float, Tag]]] = {}
    for t in rest:
        dev, n = _addr_num(cfg.plc_type, t, cfg)
        by_dev.setdefault(dev, []).append((n, t))
    for dev in sorted(by_dev):
        items = sorted(by_dev[dev], key=lambda x: x[0])
        is_rel = cfg.plc_type in ("keyence", "mock") and dev in KEYENCE_CH_DEVICES
        is_bit = any(t.ref.kind == "bit" for _, t in items)
        gap = GAP_CH if is_rel else (GAP_BITS / 16.0 if is_bit else GAP_WORDS)
        cur: list[Tag] = []
        last = None
        for n, t in items:
            if cur and last is not None:
                split = n - last > gap
                if not split and is_rel and int(n) // 10 != int(last) // 10 and n - last > 1:
                    split = True                       # MR1xxx → MR2xxx の境目
                if not split and not is_rel and not is_bit and int(n) // 1000 != int(last) // 1000 and n - last > 1:
                    split = True                       # DM1999 → DM2000 の境目
                if not split and int(n) != int(last) and len(cur) >= 4:
                    h = _hint(cur)
                    if h and h not in _tokens(t.name) and _tokens(t.name):
                        split = True                   # 名前の系統が変わった (異常 → SOL 排斥REQ)
                if split:
                    groups.append(_mk(dev, cur))
                    cur = []
            cur.append(t)
            last = n
        if cur:
            groups.append(_mk(dev, cur))
    return groups


def _mk(dev: str, tags: list[Tag]) -> dict:
    a0, a1 = tags[0].address, tags[-1].address
    rng = a0 if a0 == a1 else f"{a0}〜{_short(a1, a0)}"
    hint = _hint(tags)
    return {"name": f"{rng}{'  ' + hint if hint else ''}", "keys": [f"{t.ref.block}.{t.name}" for t in tags], "auto": True}


def _short(a1: str, a0: str) -> str:
    """同じデバイス種別なら番号だけ (DM2000〜2099)"""
    m0, m1 = re.match(r"^([A-Z]+)", a0), re.match(r"^([A-Z]+)(.*)$", a1)
    return m1.group(2) if (m0 and m1 and m0.group(1) == m1.group(1)) else a1


def load_groups(config_path: str | None, cfg: TwinConfig) -> list[dict]:
    """保存済みがあればそれを優先し、保存に無いタグは自動分けで補う。"""
    auto = auto_groups(cfg)
    if not config_path or not os.path.isfile(groups_path(config_path)):
        return auto
    with open(groups_path(config_path), "r", encoding="utf-8") as f:
        saved = yaml.safe_load(f) or {}
    valid = {f"{b}.{t.name}" for b in cfg.tags for t in cfg.tags[b]}
    out: list[dict] = []
    seen: set[str] = set()
    for g in saved.get("groups") or []:
        keys = [k for k in (g.get("keys") or []) if k in valid and k not in seen]
        seen.update(keys)
        if keys or g.get("keep"):
            out.append({"name": str(g.get("name", "")), "keys": keys, "auto": False, "collapsed": bool(g.get("collapsed", False))})
    # 未分類 (取込で増えた分) は自動分けの結果から
    for g in auto:
        keys = [k for k in g["keys"] if k not in seen]
        if keys:
            seen.update(keys)
            out.append({"name": g["name"], "keys": keys, "auto": True})
    return out


def save_groups(config_path: str, groups: list[dict]) -> str:
    p = groups_path(config_path)
    data = {"groups": [{"name": g.get("name", ""), "keys": list(g.get("keys") or []),
                        **({"collapsed": True} if g.get("collapsed") else {}),
                        **({"keep": True} if g.get("keep") else {})}
                       for g in groups if (g.get("keys") or g.get("keep"))]}   # 空のブロックは「新しいブロック」直後だけ残す
    with open(p, "w", encoding="utf-8") as f:
        f.write("# 画面で整理したデバイスのブロック分け。tag キーは <out|in|watch>.<名前>\n")
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False, width=200)
    return p
