import suggest_template


def _templates(tmp_path, names):
    d = tmp_path / "templates"
    d.mkdir()
    for n in names:
        (d / f"{n}.yaml").write_text("name: x\n")
    return d


def test_picks_matching_size(tmp_path):
    td = _templates(tmp_path, ["basic-1-node", "basic-2-node", "basic-3-node", "basic-4-node"])
    assert suggest_template.pick_template(3, template_dir=td) == "basic-3-node"


def test_over_four_falls_back_to_four(tmp_path):
    td = _templates(tmp_path, ["basic-4-node"])
    assert suggest_template.pick_template(9, template_dir=td) == "basic-4-node"


def test_zero_or_none_treated_as_one(tmp_path):
    td = _templates(tmp_path, ["basic-1-node"])
    assert suggest_template.pick_template(0, template_dir=td) == "basic-1-node"
    assert suggest_template.pick_template(None, template_dir=td) == "basic-1-node"


def test_missing_template_returns_none(tmp_path):
    td = _templates(tmp_path, ["basic-1-node"])  # no basic-3
    assert suggest_template.pick_template(3, template_dir=td) is None


def test_suggest_from_cluster_dict(tmp_path):
    td = _templates(tmp_path, ["basic-2-node"])
    res = suggest_template.suggest({"hosts": ["a", "b"]}, template_dir=td)
    assert res["hosts"] == 2
    assert res["template"] == "basic-2-node"
    assert "apply basic-2-node --confirm" in res["note"]


def test_suggest_handles_none_cluster(tmp_path):
    td = _templates(tmp_path, ["basic-1-node"])
    res = suggest_template.suggest(None, template_dir=td)
    assert res["hosts"] == 0
    assert res["template"] == "basic-1-node"  # 0 hosts -> size 1
