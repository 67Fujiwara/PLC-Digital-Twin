import os

from twin.config import load_config, parse_config
from twin.importer import parse_labels, read_csv_text, build, main

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
S = os.path.join(ROOT, "docs", "samples")


def test_gxworks3_labels():
    cfg = load_config(os.path.join(ROOT, "configs", "melsec_iqr.yaml"))
    labels = parse_labels(read_csv_text(os.path.join(S, "gxworks3_global_labels.csv")))
    assert [l.name for l in labels][:2] == ["bConv1Run", "bCyl1Ext"]
    res = build(labels, "melsec", cfg.out, cfg.inp)
    out = {t["name"]: t for t in res.tags["out"]}
    inp = {t["name"]: t for t in res.tags["in"]}
    assert out["wWorkCount"] == {"name": "wWorkCount", "word": 12, "comment": "通過ワーク数"}
    assert out["dAx1Target"] == {"name": "dAx1Target", "word": 10, "type": "s32", "comment": "軸1目標位置"}
    assert out["bD1000bit3"]["bit"] == "0.3"
    assert inp["dAx1Pos"]["type"] == "s32" and inp["dAx1Pos"]["word"] == 10
    # M/X は watch (16 の倍数に揃える)、D100/D101 は連続 1 エントリ
    w = {(e["dev"], e["addr"]): e for e in res.watch}
    assert ("M", 0) in w and w[("M", 0)]["bits"] and w[("M", 0)]["names"][0] == "bTwinMode"
    assert ("M", 96) in w and w[("M", 96)]["names"][4] == "bPE1" and w[("M", 96)]["names"][5] == "bCylEnd"
    assert w[("M", 192)]["names"][8] == "bConv1Run"
    assert ("X", 16) in w and w[("X", 16)]["names"][0] == "bX10"
    assert w[("D", 100)]["words"] == 2 and w[("D", 100)]["names"] == ["iRecipeNo", "iRecipeSpeed"]
    assert any("自動割付" in why for _, why in res.skipped)
    # 生成結果が設定として読める
    raw = dict(cfg.raw); raw["tags"] = res.tags; raw["watch"] = res.watch; raw["objects"] = []
    cfg2 = parse_config(raw)
    assert cfg2.tag("watch", "bPE1").address == "M100"
    assert cfg2.tag("watch", "bX10").address == "X10"


def test_kvstudio_variables_and_comments():
    cfg = load_config(os.path.join(ROOT, "configs", "keyence_kv8000.yaml"))
    res = build(parse_labels(read_csv_text(os.path.join(S, "kvstudio_variables.csv"))), "keyence", cfg.out, cfg.inp)
    out = {t["name"]: t for t in res.tags["out"]}
    assert out["AX1_TARGET"]["type"] == "s32" and out["WORK_COUNT"]["word"] == 12
    w = {(e["dev"], e["addr"]): e for e in res.watch}
    assert w[("MR", 100)]["names"][0] == "PE1"                   # MR100 と MR200 は連続 (ch1, ch2) で 1 エントリ
    assert w[("MR", 100)]["words"] == 2
    assert w[("MR", 100)]["names"][16] == "CONV1_RUN" and w[("MR", 100)]["names"][17] == "CYL1_EXT"
    assert w[("MR", 1500)]["names"][3] == "STEP_LAMP"
    assert w[("DM", 100)]["words"] == 3 and w[("DM", 100)]["names"] == ["RECIPE_NO", "", "RECIPE_SPD"]
    raw = dict(cfg.raw); raw["tags"] = res.tags; raw["watch"] = res.watch; raw["objects"] = []
    cfg2 = parse_config(raw)
    assert cfg2.tag("watch", "STEP_LAMP").address == "MR1503"
    assert cfg2.tag("watch", "DM101") is None            # 名前のない位置は表示しない
    # ヘッダなしデバイスコメント
    res = build(parse_labels(read_csv_text(os.path.join(S, "kvstudio_device_comments.csv"))), "keyence", cfg.out, cfg.inp)
    w = {(e["dev"], e["addr"]): e for e in res.watch}
    assert w[("DM", 0)]["names"] == ["運転時間", "生産数"]
    assert w[("MR", 0)]["names"][0] == "TWIN_MODE" and w[("MR", 0)]["names"][17] == "CYL1_END"
    assert w[("R", 0)]["names"][0] == "実入力0"


def test_cli_and_include(tmp_path):
    import shutil
    import yaml
    cfgp = tmp_path / "line.yaml"
    shutil.copy(os.path.join(ROOT, "configs", "melsec_iqr.yaml"), cfgp)
    rc = main([os.path.join(S, "gxworks3_global_labels.csv"), "--config", str(cfgp)])
    assert rc == 0 and (tmp_path / "line.tags.yaml").is_file()
    raw = yaml.safe_load(cfgp.read_text(encoding="utf-8"))
    raw["include"] = ["line.tags.yaml"]
    raw["tags"]["out"].append({"name": "EXTRA", "bit": "3.15"})
    cfgp.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    cfg = load_config(str(cfgp))
    assert cfg.tag("out", "wWorkCount") is not None and cfg.tag("out", "EXTRA") is not None
    assert cfg.tag("watch", "iRecipeNo").address == "D100"
    assert cfg.objects                                       # 本体の objects は残る
