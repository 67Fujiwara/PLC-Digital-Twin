import pytest

from twin.config import parse_config, resolve_ref, Ref


def base(**kw):
    raw = {"plc": {"type": "keyence", "out": {"dev": "DM", "addr": 1000, "words": 64},
                   "in": {"dev": "DM", "addr": 1100, "words": 64}},
           "tags": {"out": [{"name": "RUN", "bit": "0.0"}, {"name": "TGT", "word": 10, "type": "s32"}],
                    "in": [{"name": "PE1", "bit": 2}, {"name": "POS", "word": 10, "type": "s32"}]},
           "objects": []}
    raw.update(kw)
    return raw


def test_tags_and_addresses():
    cfg = parse_config(base())
    t = cfg.tag("out", "RUN")
    assert t.ref == Ref("out", "bit", 0, 0, "u16", "RUN")
    assert t.address == "DM1000.0"
    assert cfg.tag("in", "PE1").address == "DM1100.2"
    assert cfg.tag("out", "TGT").address == "DM1010"


def test_ref_syntax():
    cfg = parse_config(base())
    assert resolve_ref("out.5", cfg) == Ref("out", "bit", 0, 5)
    assert resolve_ref("out.17", cfg) == Ref("out", "bit", 1, 1)
    assert resolve_ref("in.2.3", cfg) == Ref("in", "bit", 2, 3)
    assert resolve_ref("in.w10:s32", cfg) == Ref("in", "word", 10, 0, "s32")
    assert resolve_ref("out.RUN", cfg).name == "RUN"
    with pytest.raises(ValueError, match="定義されていません"):
        resolve_ref("out.NOPE", cfg)


def test_watch_auto_tags_keyence():
    cfg = parse_config(base(watch=[{"dev": "DM", "addr": 100, "words": 2, "names": ["A"]},
                                   {"dev": "MR", "addr": 100, "words": 2}]))
    names = [t.name for t in cfg.tags["watch"]]
    assert names[:2] == ["A", "DM101"]
    assert "MR100" in names and "MR115" in names and "MR200" in names and "MR215" in names
    assert cfg.watch_words == 4
    mr215 = cfg.tag("watch", "MR215")
    assert (mr215.ref.word, mr215.ref.bit) == (3, 15)


def test_watch_auto_tags_melsec_hex():
    raw = base(watch=[{"dev": "X", "addr": "10", "words": 1}, {"dev": "M", "addr": 32, "words": 1}])
    raw["plc"]["type"] = "melsec"
    raw["plc"]["out"] = {"dev": "D", "addr": 1000}
    raw["plc"]["in"] = {"dev": "D", "addr": 1100}
    cfg = parse_config(raw)
    names = [t.name for t in cfg.tags["watch"]]
    assert names[0] == "X10" and names[15] == "X1F" and names[16] == "M32"


def test_errors():
    with pytest.raises(ValueError, match="同名"):
        parse_config(base(tags={"out": [{"name": "A", "bit": 0}, {"name": "A", "bit": 1}], "in": []}))
    with pytest.raises(ValueError, match="超えて"):
        parse_config(base(tags={"out": [{"name": "A", "word": 64}], "in": []}))


def test_yaml_on_key_normalized(tmp_path):
    import yaml
    from twin.config import load_config
    raw = base(objects=[{"name": "c1", "type": "conveyor", "run": "out.RUN", "length": 1000},
                        {"name": "s1", "type": "sensor", "on": "c1", "at": 500, "output": "in.PE1"}])
    p = tmp_path / "a.yaml"
    p.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    cfg = load_config(str(p))
    assert cfg.objects[1]["on"] == "c1"


def test_object_validation():
    with pytest.raises(ValueError, match="コンベヤがありません"):
        parse_config(base(objects=[{"name": "s1", "type": "sensor", "on": "nope", "output": "in.PE1"}]))
    with pytest.raises(ValueError, match="in\\.\\* にしてください"):
        parse_config(base(objects=[{"name": "c1", "type": "conveyor", "run": "out.RUN"},
                                   {"name": "s1", "type": "sensor", "on": "c1", "output": "out.RUN"}]))
    with pytest.raises(ValueError, match="未対応の type"):
        parse_config(base(objects=[{"name": "x", "type": "robot"}]))
