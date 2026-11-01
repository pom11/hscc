"""Manifest + import-contract tests for the comfyui-sparkrun pack.

These are the tests that matter most for the repo suite: the pack lives in a
repo whose own venv has NO torch and NO ComfyUI, and ``scripts/run_tests.sh``
collects it. If the pack imported ``comfy`` or ``torch`` at module level, the
suite would not "fail" — it would fail to collect, and the pack would look
broken to whoever ran the suite next. So the import guard is asserted
structurally (an AST scan) rather than by hoping an ImportError shows up.
"""

from __future__ import annotations

import ast
import importlib
import os

import pytest
from pack_loader import PACK_DIR, load_pack, pack_file, probe_import

HEAVY_MODULES = {"comfy", "comfy_api", "comfy_execution", "torch", "numpy", "PIL"}
PACK_MODULES = ("__init__.py", "nodes.py", "catalog.py", "media.py", "http_client.py")


def _module_level_imports(path):
    """Every module name imported at module level (not inside a function)."""
    tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
    found = set()
    for node in tree.body:  # top level only — nested imports are the guard
        if isinstance(node, ast.Import):
            found.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
    return found


@pytest.mark.parametrize("stem", ["__init__", "nodes", "catalog", "media", "http_client"])
def test_pack_file_imports_without_comfy_or_torch(stem):
    """The pack loads in THIS interpreter (no comfy, no torch). If this fails,
    the repo suite fails to collect the whole pack. A fresh exec under a
    throwaway name — the cached suite modules are not touched."""
    assert probe_import(stem) is not None


@pytest.mark.parametrize("name", PACK_MODULES)
def test_no_heavy_import_at_module_level(name):
    """Structural guard: heavy modules may only be imported INSIDE a function."""
    heavy = HEAVY_MODULES & _module_level_imports(pack_file(name))
    assert not heavy, f"{name} imports {sorted(heavy)} at module level — the repo " \
                      f"suite has no torch/ComfyUI and could not collect the pack"


def test_no_sys_path_mutation_anywhere_in_the_pack():
    """The pack must not insert its own dir on sys.path: this pack owns a
    ``nodes.py``, and ComfyUI already has an importable ``nodes`` module -
    shadowing it would break every other installed pack.

    Checked on the AST (a ``sys.path`` attribute access), not by grepping the
    text: these docstrings deliberately EXPLAIN this rule and mention sys.path
    in prose, so a substring guard fails on the very sentences that document
    the requirement.
    """
    for name in PACK_MODULES:
        tree = ast.parse(open(pack_file(name), encoding="utf-8").read(), filename=name)

        def is_sys_path(node):
            return (isinstance(node, ast.Attribute) and node.attr == "path"
                    and isinstance(node.value, ast.Name) and node.value.id == "sys")

        hits = [n.lineno for n in ast.walk(tree) if is_sys_path(n)]
        assert not hits, f"{name} touches sys.path at line(s) {hits}"


def test_node_class_mappings_shape():
    init = load_pack("__init__")
    assert set(init.NODE_CLASS_MAPPINGS) == {
        "SparkrunImage", "SparkrunImageEdit", "SparkrunVideo"}
    assert set(init.NODE_DISPLAY_NAME_MAPPINGS) == set(init.NODE_CLASS_MAPPINGS)
    for name, cls in init.NODE_CLASS_MAPPINGS.items():
        assert callable(cls.INPUT_TYPES), name
        assert isinstance(cls.RETURN_TYPES, tuple) and cls.RETURN_TYPES
        assert hasattr(cls, cls.FUNCTION), f"{name} declares FUNCTION={cls.FUNCTION}"
        assert cls.CATEGORY == "sparkrun"


def test_classic_layout_matches_what_0_43_loader_reads():
    """ComfyUI 0.43.2 registers a pack if it exposes NODE_CLASS_MAPPINGS **or**
    ``comfy_entrypoint`` (nodes.py, V1 vs V3 branch) and reads no
    ``comfy.plugin.json``. The pack takes the first branch, so the mappings must
    be plain module attributes — not produced by a call."""
    init = load_pack("__init__")
    assert isinstance(init.NODE_CLASS_MAPPINGS, dict)
    assert not hasattr(init, "comfy_entrypoint")
    assert init.WEB_DIRECTORY is None  # no web assets => no phantom extension dir


@pytest.mark.parametrize("stem,kind,expected", [
    ("SparkrunImage", "image", ("IMAGE", "DICT")),
    ("SparkrunImageEdit", "image", ("IMAGE", "DICT")),
    ("SparkrunVideo", "video", ("VIDEO", "DICT")),
])
def test_return_types_and_kind_wiring(stem, kind, expected):
    init = load_pack("__init__")
    cls = init.NODE_CLASS_MAPPINGS[stem]
    assert cls.RETURN_TYPES == expected
    assert cls.KIND == kind
    assert cls.RETURN_NAMES == ("image" if kind == "image" else "video", "meta")


def test_every_required_widget_is_a_comfy_tuple():
    """INPUT_TYPES must speak ComfyUI's shape: {section: {name: (type, opts)}}.
    A widget declared as a bare string is rejected by /object_info at runtime,
    so it is caught here instead."""
    init = load_pack("__init__")
    for stem, cls in init.NODE_CLASS_MAPPINGS.items():
        spec = cls.INPUT_TYPES()
        assert "required" in spec, stem
        for section in ("required", "optional"):
            for widget, decl in (spec.get(section) or {}).items():
                assert isinstance(decl, tuple) and len(decl) in (1, 2), \
                    f"{stem}.{widget} is not a (type, opts) tuple: {decl!r}"
                first = decl[0]
                assert isinstance(first, (str, list)), f"{stem}.{widget} type {first!r}"


def test_video_node_exposes_polling_controls():
    cls = load_pack("__init__").NODE_CLASS_MAPPINGS["SparkrunVideo"]
    req = cls.INPUT_TYPES()["required"]
    for widget in ("poll_interval", "poll_timeout"):
        assert widget in req, f"video node lost its {widget} control"
        default = req[widget][1]["default"]
        assert default > 0
    # polling must be fast enough to be usable but never a busy-loop
    assert req["poll_interval"][1]["min"] >= 0.1


def test_base_url_default_uses_the_documented_placeholder():
    """The committed default must carry the repo's LAN placeholder, never a real
    host — address_guard enforces this repo-wide, and this test says why the
    placeholder belongs here specifically."""
    init = load_pack("__init__")
    assert "10.0.0.x" in init.DEFAULT_BASE_URL
    assert init.DEFAULT_BASE_URL.endswith(":4000")


def test_base_url_default_follows_the_environment(monkeypatch):
    """An operator setting SPARKRUN_PROXY_BASE_URL must reach the widget default,
    which is why a placeholder in the source is not a functional limitation. The
    value is read at import time, so this asserts against a fresh exec — under a
    throwaway module name, so the modules the rest of the suite holds are not
    left pointing at a phantom proxy."""
    monkeypatch.setenv("SPARKRUN_PROXY_BASE_URL", "http://127.0.0.1:4123")
    fresh = probe_import("nodes")
    assert fresh.DEFAULT_BASE_URL == "http://127.0.0.1:4123"
    assert load_pack("nodes").DEFAULT_BASE_URL != "http://127.0.0.1:4123"


def test_pack_ships_the_declared_files():
    for name in ("__init__.py", "nodes.py", "catalog.py", "media.py",
                 "http_client.py", "README.md", "requirements.txt", "install.sh"):
        assert os.path.isfile(os.path.join(PACK_DIR, name)), f"missing {name}"


def test_no_comfy_plugin_json_is_shipped():
    """0.43.2's loader reads no such file (grep over the whole 0.43.2 tree finds
    zero references), so shipping one would imply a contract the host ignores."""
    assert not [f for f in os.listdir(PACK_DIR) if f.endswith(".plugin.json")]


# ── the M3 contract, bound to its owner ──────────────────────────────────────

_MEDIA_REGISTRATION = os.path.normpath(
    os.path.join(PACK_DIR, os.pardir, "hscc-cluster", "media_registration.py"))


def test_mode_enums_match_media_registration():
    """The pack's mode strings must be the SAME strings hscc emits into
    ``model_info.mode``.

    The pack cannot import the module normally (``hscc-cluster`` is a
    hyphenated dir, and the repo's isolation rule is one pytest process per
    plugin dir), so the contract owner is loaded by file path and compared —
    the same by-path loading precedent as ``hscc_daemon/verify.py`` for
    ``install_payload``. Skipped only when the file is genuinely absent from
    this checkout; a VALUE drift fails the suite.
    """
    if not os.path.isfile(_MEDIA_REGISTRATION):
        pytest.skip(f"{_MEDIA_REGISTRATION} not in this checkout (media M3 unmerged)")
    spec = importlib.util.spec_from_file_location(
        "media_registration__comfyui_sparkrun_probe", _MEDIA_REGISTRATION)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    catalog = load_pack("catalog")
    assert catalog.KIND_TO_MODE == dict(mod.KIND_TO_MODE), (
        "the pack's kind->mode pair drifted from the registration contract")
    assert set(catalog.MODES) == set(mod.MEDIA_MODES)
    # The owner's own vocabulary: media modes are exactly the enum values.
    assert set(mod.MEDIA_MODES) == set(mod.KIND_TO_MODE.values())
