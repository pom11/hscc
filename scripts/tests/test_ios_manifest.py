"""Tests for ios-app/scripts/make_manifest.py (Apple OTA manifest.plist).

The manifest is the one file iOS reads before it trusts the IPA: a typo in
`bundle-identifier`, `bundle-version` or a URL produces "Unable to install"
with no useful detail on the phone. These tests pin the exact Apple shape and
the URL/version wiring the release + gh-pages scripts rely on.
"""

from __future__ import annotations

import importlib.util
import plistlib
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "ios-app" / "scripts" / "make_manifest.py"

spec = importlib.util.spec_from_file_location("make_manifest", MODULE_PATH)
assert spec and spec.loader
manifest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manifest)


BUNDLE_ID = "dev.example.ios"
VERSION = "1.2.3"
IPA = "https://github.com/o/r/releases/download/ios-v1.2.3/HSCC-1.2.3.ipa"
ICON57 = "https://o.github.io/r/ios/icons/icon-57.png"
ICON512 = "https://o.github.io/r/ios/icons/icon-512.png"


def parsed(**overrides):
    kwargs = dict(
        bundle_id=BUNDLE_ID,
        version=VERSION,
        ipa_url=IPA,
        icon_57_url=ICON57,
        icon_512_url=ICON512,
    )
    kwargs.update(overrides)
    return plistlib.loads(manifest.render_manifest(**kwargs))


def test_manifest_is_a_plist_with_one_item():
    data = manifest.render_manifest(
        bundle_id=BUNDLE_ID,
        version=VERSION,
        ipa_url=IPA,
        icon_57_url=ICON57,
        icon_512_url=ICON512,
    )
    assert data.lstrip().startswith(b"<?xml")
    doc = plistlib.loads(data)
    assert list(doc) == ["items"]
    assert len(doc["items"]) == 1


def test_software_package_points_at_release_asset():
    item = parsed()["items"][0]
    software = [a for a in item["assets"] if a["kind"] == "software-package"]
    assert software == [{"kind": "software-package", "url": IPA}]


def test_metadata_carries_identity_and_version():
    meta = parsed()["items"][0]["metadata"]
    assert meta == {
        "bundle-identifier": BUNDLE_ID,
        "bundle-version": VERSION,
        "kind": "software",
        "title": "HSCC",
    }
    # Apple requires strings; a numeric version is a silent plist type error.
    assert all(isinstance(v, str) for v in meta.values())


def test_display_images_are_57_and_512():
    assets = parsed()["items"][0]["assets"]
    assert {"kind": "display-image", "url": ICON57} in assets
    assert {"kind": "full-size-image", "url": ICON512} in assets


def test_custom_title_is_used():
    assert parsed(title="HSCC Nightly")["items"][0]["metadata"]["title"] == "HSCC Nightly"


def test_empty_required_value_is_rejected():
    for field in ("bundle_id", "version", "ipa_url", "icon_57_url", "icon_512_url"):
        try:
            manifest.build_manifest(
                **{
                    **dict(
                        bundle_id=BUNDLE_ID,
                        version=VERSION,
                        ipa_url=IPA,
                        icon_57_url=ICON57,
                        icon_512_url=ICON512,
                    ),
                    field: " ",
                }
            )
        except ValueError:
            pass
        else:  # pragma: no cover - failure path
            raise AssertionError(f"{field} should be rejected when blank")


def test_cli_writes_a_valid_manifest(tmp_path):
    out = tmp_path / "ios" / "manifest.plist"
    subprocess.run(
        [
            sys.executable,
            str(MODULE_PATH),
            "--bundle-id", BUNDLE_ID,
            "--version", VERSION,
            "--ipa-url", IPA,
            "--icon-57-url", ICON57,
            "--icon-512-url", ICON512,
            "--out", str(out),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert plistlib.loads(out.read_bytes()) == parsed()
