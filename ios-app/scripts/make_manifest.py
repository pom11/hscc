#!/usr/bin/env python3
"""Generate Apple's over-the-air install manifest for an Ad Hoc iOS build.

`itms-services://?action=download-manifest&url=...` fetches a PROPERTY LIST
(not JSON, despite the old `manifest.json` name some tools use). Apple's
format is a top-level `items` array whose one entry carries:

  * `assets`   — the signed IPA (`software-package`), plus the 57 px and
                 512 px artwork the install sheet shows;
  * `metadata` — `bundle-identifier`, `bundle-version`, `kind` and `title`.

`bundle-version` is CFBundleVersion, NOT CFBundleShortVersionString. The
release script sets both to the release version, so the IPA on disk and this
manifest cannot disagree about what is being installed.

Stdlib only (plistlib), so the publish path needs no Python packages.

Usage:
    make_manifest.py --bundle-id com.example.ios --version 1.2.3 \
        --ipa-url https://github.com/o/r/releases/download/ios-v1.2.3/HSCC-1.2.3.ipa \
        --icon-57-url https://o.github.io/r/ios/icons/icon-57.png \
        --icon-512-url https://o.github.io/r/ios/icons/icon-512.png \
        --out /tmp/manifest.plist
"""

from __future__ import annotations

import argparse
import plistlib
import sys
from pathlib import Path

DEFAULT_TITLE = "HSCC"


def build_manifest(
    bundle_id: str,
    version: str,
    ipa_url: str,
    icon_57_url: str,
    icon_512_url: str,
    title: str = DEFAULT_TITLE,
) -> dict:
    """Return the manifest as a plist-ready dict (pure, so it is testable)."""
    for name, value in (
        ("bundle id", bundle_id),
        ("version", version),
        ("IPA URL", ipa_url),
        ("57 px icon URL", icon_57_url),
        ("512 px icon URL", icon_512_url),
    ):
        if not value or not str(value).strip():
            raise ValueError(f"{name} must not be empty")
    if not (ipa_url.startswith("https://") or ipa_url.startswith("http://")):
        raise ValueError("IPA URL must be an http(s) URL that iOS can reach")
    return {
        "items": [
            {
                "assets": [
                    {"kind": "software-package", "url": ipa_url},
                    {"kind": "display-image", "url": icon_57_url},
                    {"kind": "full-size-image", "url": icon_512_url},
                ],
                "metadata": {
                    "bundle-identifier": bundle_id,
                    "bundle-version": version,
                    "kind": "software",
                    "title": title,
                },
            }
        ]
    }


def render_manifest(
    bundle_id: str,
    version: str,
    ipa_url: str,
    icon_57_url: str,
    icon_512_url: str,
    title: str = DEFAULT_TITLE,
) -> bytes:
    """Serialize the manifest as an XML plist (Apple's OTA format)."""
    manifest = build_manifest(
        bundle_id=bundle_id,
        version=version,
        ipa_url=ipa_url,
        icon_57_url=icon_57_url,
        icon_512_url=icon_512_url,
        title=title,
    )
    return plistlib.dumps(manifest, fmt=plistlib.FMT_XML, sort_keys=False)


def write_manifest(out: Path, **kwargs) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(render_manifest(**kwargs))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-id", required=True)
    parser.add_argument("--version", required=True,
                        help="CFBundleVersion of the IPA (release script sets it)")
    parser.add_argument("--ipa-url", required=True)
    parser.add_argument("--icon-57-url", required=True)
    parser.add_argument("--icon-512-url", required=True)
    parser.add_argument("--title", default=DEFAULT_TITLE)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        write_manifest(
            args.out,
            bundle_id=args.bundle_id,
            version=args.version,
            ipa_url=args.ipa_url,
            icon_57_url=args.icon_57_url,
            icon_512_url=args.icon_512_url,
            title=args.title,
        )
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
