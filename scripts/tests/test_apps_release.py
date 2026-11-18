"""Tests for scripts/apps_release.py — notes, feeds, appcast and Pages.

The release path's formats are pinned here so a change cannot silently produce
an invalid appcast, an iOS feed with the wrong install URL, notes that leak
card ids, or a build number that goes backwards.
"""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import apps_release as ar  # noqa: E402
import changelog_fragments as cf  # noqa: E402


def entry(kind, body, task="t_aaaa1111", index=0):
    return cf.Entry(kind=kind, task=task, order=0, index=index,
                    body=body, source=f"{task}.md")


# ── notes ────────────────────────────────────────────────────────────────────

def test_notes_group_added_changed_fixed_and_drop_card_ids():
    entries = [
        entry("Added", ["- **Widgets update themselves.** (t_aaaa1111)"], "t_aaaa1111"),
        entry("Fixed", ["- The menu bar item no longer doubles. (t_bbbb2222)"], "t_bbbb2222"),
        entry("Changed", ["- `release_apps.sh` is the one command. t_cccc3333"], "t_cccc3333"),
        entry("Verified", ["- 99 tests green (t_dddd4444)"], "t_dddd4444"),
    ]
    text = ar.render_notes("1.1.0", entries)
    assert text.startswith("# HSCC apps 1.1.0")
    assert text.index("## Added") < text.index("## Changed") < text.index("## Fixed")
    assert "Verified" not in text
    assert "t_aaaa1111" not in text and "t_bbbb2222" not in text
    assert "t_cccc3333" not in text and "t_dddd4444" not in text
    assert "Widgets update themselves" in text
    assert "no longer doubles" in text
    assert "`release_apps.sh` is the one command." in text


def test_notes_without_app_entries_says_so():
    text = ar.render_notes("1.1.0", [entry("Docs", ["- docs only"])])
    assert ar.DEFAULT_NOTES in text


def test_notes_html_is_escaped_and_has_a_heading():
    html = ar.render_notes_html("1.1.0", "# HSCC apps 1.1.0\n\n## Fixed\n\n"
                                "- **safe** <script>alert(1)</script>\n")
    assert "<h1>HSCC apps 1.1.0</h1>" in html
    assert "<strong>safe</strong>" in html
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "<li>" in html


# ── versions and builds ──────────────────────────────────────────────────────

def test_last_release_reads_apps_tags_and_the_legacy_baseline():
    tags = ["v2.9.0", "ios-v1.0.0", "macos-widgets-v1.0.0", "apps-v1.2.0",
            "apps-v1.10.0", "apps-vnot-a-version"]
    assert ar.last_released_version(tags) == "1.10.0"
    assert ar.last_released_version(["ios-v1.0.0"]) == "1.0.0"
    assert ar.last_released_version([]) is None


def test_next_build_number_is_monotonic(tmp_path):
    path = tmp_path / "BUILD_NUMBER"
    path.write_text("7\n", encoding="utf-8")
    assert ar.read_build_number(path) == 7
    assert ar.next_build_number(path) == 8
    path.write_text("8\n", encoding="utf-8")
    assert ar.next_build_number(path) == 9


def test_bad_build_number_is_refused(tmp_path):
    path = tmp_path / "BUILD_NUMBER"
    path.write_text("7.1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        ar.read_build_number(path)


def test_set_project_version_touches_exactly_the_two_lines(tmp_path):
    path = tmp_path / "project.yml"
    path.write_text(
        "settings:\n  base:\n"
        '    MARKETING_VERSION: "0.1.0"\n'
        '    CURRENT_PROJECT_VERSION: "1"\n'
        '    PRODUCT_BUNDLE_IDENTIFIER: com.hscc.mac\n',
        encoding="utf-8")
    ar.set_project_version(path, "1.1.0", 2)
    text = path.read_text(encoding="utf-8")
    assert 'MARKETING_VERSION: "1.1.0"' in text
    assert 'CURRENT_PROJECT_VERSION: "2"' in text
    assert 'MARKETING_VERSION: "0.1.0"' not in text
    assert "PRODUCT_BUNDLE_IDENTIFIER: com.hscc.mac" in text


def test_set_project_version_refuses_a_missing_key(tmp_path):
    path = tmp_path / "project.yml"
    path.write_text('settings:\n  base:\n    MARKETING_VERSION: "1.0.0"\n',
                    encoding="utf-8")
    with pytest.raises(ValueError):
        ar.set_project_version(path, "1.1.0", 2)


# ── iOS feed ─────────────────────────────────────────────────────────────────

def test_version_json_has_exactly_the_update_check_keys():
    feed = ar.version_json("1.1.0", 2, "2026-11-18T10:00:00Z")
    assert set(feed) == {"version", "build", "released_at", "notes_url",
                         "install_url", "min_os"}
    assert feed["version"] == "1.1.0"
    assert feed["build"] == 2
    assert feed["install_url"] == "https://pom11.github.io/hscc/ios/"
    assert feed["notes_url"] == "https://pom11.github.io/hscc/ios/notes/1.1.0.html"
    assert feed["min_os"] == "26.0"


def test_publish_ios_writes_feed_page_notes_and_monotonic_history(tmp_path):
    pages = tmp_path / "pages"
    for version, build in (("1.0.0", 1), ("1.1.0", 2)):
        result = ar.publish_ios(
            pages, REPO / "ios-app", version, build,
            notes_markdown=f"# HSCC apps {version}\n\n## Fixed\n\n- build {build}\n",
            bundle_id="ro.parsec.hscc.ios",
            ipa_url=f"https://github.com/pom11/hscc/releases/download/"
                    f"apps-v{version}/HSCC-{version}.ipa",
            released_at=f"2026-11-18T10:0{build}:00Z")
        assert result["version_json"].is_file()

    feed = json.loads((pages / "ios" / "version.json").read_text(encoding="utf-8"))
    assert feed["version"] == "1.1.0" and feed["build"] == 2
    history = json.loads((pages / "ios" / "releases.json").read_text(encoding="utf-8"))
    assert [row["build"] for row in history] == [2, 1]
    assert (pages / "ios" / "notes" / "1.1.0.html").is_file()
    index = (pages / "ios" / "index.html").read_text(encoding="utf-8")
    assert 'Version 1.1.0 (build 2)' in index
    assert "build 2" in index and "build 1" in index
    assert "__HSCC_" not in index


# ── Sparkle appcast ──────────────────────────────────────────────────────────

def test_appcast_is_valid_xml_with_sparkle_fields_and_correct_url():
    item = ar.appcast_item(
        "1.1.0", 2,
        "https://github.com/pom11/hscc/releases/download/apps-v1.1.0/"
        "HSCC-Widgets-1.1.0.zip",
        123456, "c2lnbmF0dXJl", pub_date="Wed, 18 Nov 2026 10:00:00 GMT")
    xml = ar.appcast_xml([item])
    root = ET.fromstring(xml)
    assert root.tag == "rss"
    ns = {"sparkle": ar.SPARKLE_NS}
    node = root.find("./channel/item", ns)
    assert node is not None
    assert node.findtext("sparkle:version", namespaces=ns) == "2"
    assert node.findtext("sparkle:shortVersionString", namespaces=ns) == "1.1.0"
    assert node.findtext("sparkle:minimumSystemVersion", namespaces=ns) == "14.0"
    enclosure = node.find("enclosure")
    assert enclosure.get("url").endswith("/apps-v1.1.0/HSCC-Widgets-1.1.0.zip")
    assert enclosure.get("length") == "123456"
    assert enclosure.get(f"{{{ar.SPARKLE_NS}}}edSignature") == "c2lnbmF0dXJl"
    assert node.findtext("link") == ar.notes_url("1.1.0", "macos")


def test_appcast_keeps_one_item_per_release_newest_first():
    first = ar.appcast_item("1.0.0", 1, "https://example.invalid/a.zip", 1,
                            "sig1", pub_date="Wed, 18 Nov 2026 09:00:00 GMT")
    second = ar.appcast_item("1.1.0", 2, "https://example.invalid/b.zip", 2,
                             "sig2", pub_date="Wed, 18 Nov 2026 10:00:00 GMT")
    xml = ar.upsert_appcast(ar.appcast_xml([first]), second)
    rows = ar.parse_appcast(xml)
    assert [row["build"] for row in rows] == ["2", "1"]
    # Re-publishing the same build replaces, never duplicates.
    xml2 = ar.upsert_appcast(xml, second)
    assert len(ar.parse_appcast(xml2)) == 2


def test_publish_macos_writes_page_notes_and_appcast(tmp_path):
    pages = tmp_path / "pages"
    result = ar.publish_macos(
        pages, REPO / "macos-app", "1.1.0", 2,
        notes_markdown="# HSCC apps 1.1.0\n\n## Added\n\n- Sparkle updates\n",
        zip_url="https://github.com/pom11/hscc/releases/download/apps-v1.1.0/"
                "HSCC-Widgets-1.1.0.zip",
        length=999, signature="c2ln")
    assert result["appcast"].is_file()
    assert (pages / "macos" / "notes" / "1.1.0.html").is_file()
    index = (pages / "macos" / "index.html").read_text(encoding="utf-8")
    assert "brew install --cask pom11/hscc/hscc-widgets" in index
    assert "Sparkle updates" in index
    assert "__HSCC_" not in index
    assert "apps-v1.1.0/HSCC-Widgets-1.1.0.zip" in result["appcast"].read_text()


def test_appcast_requires_a_signature_and_https():
    with pytest.raises(ValueError):
        ar.appcast_item("1.1.0", 2, "https://example.invalid/a.zip", 1, "")
    with pytest.raises(ValueError):
        ar.appcast_item("1.1.0", 2, "http://example.invalid/a.zip", 1, "sig")
