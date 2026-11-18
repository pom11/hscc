"""Static checks for the GitHub Pages iOS install page.

The page is the only install surface most testers see, and it runs on a phone
with no build step. These assertions keep it self-contained (no CDN, no script)
and keep the `itms-services` URL and the required instructions from drifting
away from the release scripts.
"""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PAGE = REPO_ROOT / "ios-app" / "site" / "index.html"
INSTALL = REPO_ROOT / "ios-app" / "INSTALL.md"

ITMS = (
    "itms-services://?action=download-manifest&"
    "url=https://pom11.github.io/hscc/ios/manifest.plist"
)


class _Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.attrs = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs.extend((tag, name, value) for name, value in attrs)

    def handle_data(self, data):
        self.text.append(data)


def parse():
    page = _Page()
    page.feed(PAGE.read_text(encoding="utf-8"))
    page.close()
    return page


def test_page_exists_and_parses():
    page = parse()
    assert "html" in page.tags
    assert "body" in page.tags
    assert "".join(page.text).strip()


def test_install_button_uses_apple_ota_scheme_and_fixed_manifest():
    page = parse()
    install = [a for a in page.attrs if a[0] == "a" and a[1] == "href"]
    assert any(value == ITMS for _, _, value in install), ITMS


def test_page_is_phone_width_and_self_contained():
    page = parse()
    assert ("meta", "name", "viewport") in page.attrs
    # Works from the phone's network (or a local copy) with no CDN dependency.
    assert not [t for t in page.tags if t == "script"]
    assert not [t for t in page.tags if t == "link"]
    for tag, name, value in page.attrs:
        if name in {"src", "href"} and value:
            assert not value.startswith(("http://", "https://")) or tag == "a", (
                f"external {tag} resource: {value}"
            )


def test_instructions_cover_registration_safari_trust_widgets_and_update():
    text = " ".join(parse().text)
    normalized = " ".join(text.split()).lower()
    for phrase in (
        "device must be registered",
        "udid",
        "safari",
        "vpn & device management",
        "widgets",
        "live activities",
        "update",
        "unable to install",
    ):
        assert phrase in normalized, phrase


def test_install_md_is_short_numbered_and_matches_the_page():
    text = INSTALL.read_text(encoding="utf-8")
    assert "1." in text and "2." in text and "3." in text
    for phrase in ("UDID", "Safari", "VPN & Device Management", "widgets", "Live Activities"):
        assert phrase in text, phrase
    assert "pom11.github.io/hscc/ios/" in text
