#!/usr/bin/env python3
"""Generators for the HSCC app release path (iOS + macOS).

This module owns the four artefacts that used to be hand-maintained and drift
apart from the binaries:

  * release notes — built from the ``changelog.d/`` fragments that touched
    ``ios-app/`` or ``macos-app/`` since the previous ``apps-v*`` tag, grouped
    Added / Changed / Fixed, with card ids stripped from the user-facing text;
  * ``/ios/version.json`` — the feed the iOS update check reads;
  * ``/macos/appcast.xml`` — a Sparkle 2 appcast, one item per release, with
    the notarized zip URL, length and EdDSA signature;
  * the two Pages pages (``/ios/`` and ``/macos/``) plus the iOS release
    history.

Everything here is pure except the small ``git``/file writers, so the tests in
``scripts/tests/test_apps_release.py`` pin the formats without a Mac app, a
keychain or the network.

Stdlib only.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import email.utils
import html
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable
import xml.etree.ElementTree as ET

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import changelog_fragments as cf  # noqa: E402

REPO = "pom11/hscc"
PAGES_BASE = "https://pom11.github.io/hscc"
IOS_INSTALL_URL = f"{PAGES_BASE}/ios/"
IOS_VERSION_URL = f"{PAGES_BASE}/ios/version.json"
MACOS_APPCAST_URL = f"{PAGES_BASE}/macos/appcast.xml"
IOS_MIN_OS = "26.0"
MACOS_MIN_OS = "14.0"
RELEASE_TAG_PREFIX = "apps-v"
LEGACY_TAG_PREFIXES = ("ios-v", "macos-widgets-v")
APP_PATH_PREFIXES = ("ios-app/", "macos-app/")
NOTE_KINDS = ("Added", "Changed", "Fixed")
SPARKLE_NS = "http://www.andymatuschak.org/xml-namespaces/sparkle"
DEFAULT_NOTES = "_No app-facing changes recorded._"

VERSION_RE = re.compile(r"^v?(?P<version>[0-9]+\.[0-9]+\.[0-9]+)$")
CARD_ID_RE = re.compile(r"\bt_[0-9a-f]{6,}\b")
SPACES_RE = re.compile(r"[ \t]{2,}")
EMPTY_PARENS_RE = re.compile(r"\(\s*[—,;:]?\s*\)")


# ── versions and build numbers ───────────────────────────────────────────────

def normalize_version(raw: str) -> str:
    m = VERSION_RE.match((raw or "").strip())
    if not m:
        raise ValueError(
            f"version {raw!r} is not MAJOR.MINOR.PATCH (e.g. 1.1.0, no leading v)")
    return m.group("version")


def version_key(raw: str) -> tuple[int, int, int]:
    return tuple(int(p) for p in normalize_version(raw).split("."))  # type: ignore[return-value]


def last_released_version(tags: Iterable[str]) -> str | None:
    """Highest version among ``apps-v*`` (and the pre-migration tags).

    The two legacy prefixes are read only so the FIRST ``apps-v`` release is
    still compared against the 1.0.0 that shipped under ``ios-v`` /
    ``macos-widgets-v``. New releases are always ``apps-v<version>``.
    """
    best = None
    for tag in tags:
        tag = tag.strip()
        for prefix in (RELEASE_TAG_PREFIX, *LEGACY_TAG_PREFIXES):
            if tag.startswith(prefix):
                raw = tag[len(prefix):]
                try:
                    key = version_key(raw)
                except ValueError:
                    continue
                if best is None or key > best[0]:
                    best = (key, normalize_version(raw))
                break
    return best[1] if best else None


def read_build_number(path: Path) -> int:
    text = path.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[0-9]+", text):
        raise ValueError(f"{path} must contain one integer build number, got {text!r}")
    return int(text)


def next_build_number(path: Path) -> int:
    return read_build_number(path) + 1


# ── release notes ────────────────────────────────────────────────────────────

def strip_card_ids(text: str) -> str:
    """Remove kanban ids from user-facing release notes (the card's rule)."""
    text = CARD_ID_RE.sub("", text)
    text = EMPTY_PARENS_RE.sub("", text)
    text = SPACES_RE.sub(" ", text)
    text = re.sub(r"\s+([,.;:])", r"\1", text)
    text = re.sub(r"[ \t]+$", "", text)
    return text


def _entry_lines(entry) -> list[str]:
    return [strip_card_ids(line).rstrip() for line in entry.body]


def render_notes(version: str, entries: Iterable) -> str:
    """Markdown release notes grouped Added / Changed / Fixed."""
    by_kind: dict[str, list] = {kind: [] for kind in NOTE_KINDS}
    for entry in entries:
        if entry.kind in by_kind:
            by_kind[entry.kind].append(entry)

    out = [f"# HSCC apps {normalize_version(version)}", ""]
    any_entry = False
    for kind in NOTE_KINDS:
        group = by_kind[kind]
        if not group:
            continue
        any_entry = True
        out.extend([f"## {kind}", ""])
        for entry in sorted(group, key=lambda e: e.sort_key()):
            out.extend(_entry_lines(entry))
            out.append("")
    if not any_entry:
        out.extend([DEFAULT_NOTES, ""])
    return "\n".join(out).rstrip("\n") + "\n"


def notes_url(version: str, platform: str) -> str:
    return f"{PAGES_BASE}/{platform}/notes/{normalize_version(version)}.html"


def _inline_markdown(text: str) -> str:
    text = html.escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)",
                  r'<a href="\2">\1</a>', text)
    return text


def render_notes_body(markdown: str) -> str:
    """A small, dependency-free Markdown subset -> HTML body fragment."""
    body: list[str] = []
    in_list = False

    def close_list():
        nonlocal in_list
        if in_list:
            body.append("</ul>")
            in_list = False

    for raw in markdown.splitlines():
        line = raw.rstrip()
        if not line.strip():
            close_list()
            continue
        m = re.match(r"^(#{1,3})\s+(.*)$", line)
        if m:
            close_list()
            level = len(m.group(1))
            body.append(f"<h{level}>{_inline_markdown(m.group(2))}</h{level}>")
            continue
        if line.startswith(("- ", "* ")):
            if not in_list:
                body.append("<ul>")
                in_list = True
            body.append(f"<li>{_inline_markdown(line[2:])}</li>")
            continue
        close_list()
        body.append(f"<p>{_inline_markdown(line)}</p>")
    close_list()
    return "".join(body)


def render_notes_html(version: str, markdown: str) -> str:
    """A full standalone release-notes page for the Pages site."""
    body = render_notes_body(markdown)
    title = f"HSCC {normalize_version(version)} — release notes"
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>{html.escape(title)}</title>\n"
        "<style>body{margin:0;padding:28px 20px 56px;background:#000;color:#fff;"
        "font:17px/1.5 -apple-system,BlinkMacSystemFont,\"SF Pro Text\","
        "Helvetica,Arial,sans-serif}main{max-width:640px;margin:0 auto}"
        "h1,h2{color:#FF5722}a{color:#FF5722}code{font:0.93em ui-monospace,"
        "SFMono-Regular,Menlo,monospace}.foot{margin-top:36px;color:#777;"
        "font-size:.86rem}</style>\n</head>\n<body>\n<main>\n"
        f"{body}\n"
        f"<p class=\"foot\"><a href=\"{PAGES_BASE}/\">HSCC</a> · "
        f"{html.escape(normalize_version(version))}</p>\n"
        "</main>\n</body>\n</html>\n"
    )


def format_released_at(moment: _dt.datetime | None = None) -> str:
    moment = moment or _dt.datetime.now(_dt.timezone.utc)
    return moment.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def rfc2822(moment: _dt.datetime | None = None) -> str:
    moment = moment or _dt.datetime.now(_dt.timezone.utc)
    return email.utils.format_datetime(moment.astimezone(_dt.timezone.utc),
                                       usegmt=True)


# ── iOS feed and history ─────────────────────────────────────────────────────

def version_json(version: str, build: int, released_at: str, *,
                 notes: str | None = None, install_url: str = IOS_INSTALL_URL,
                 min_os: str = IOS_MIN_OS) -> dict:
    build = int(build)
    if build <= 0:
        raise ValueError("build must be a positive integer")
    if not install_url.startswith("https://"):
        raise ValueError("install_url must be https")
    return {
        "version": normalize_version(version),
        "build": build,
        "released_at": released_at,
        "notes_url": notes or notes_url(version, "ios"),
        "install_url": install_url,
        "min_os": min_os,
    }


def load_history(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [row for row in data if isinstance(row, dict) and row.get("version")]


def update_history(history: list[dict], entry: dict) -> list[dict]:
    rows = [row for row in history if row.get("version") != entry["version"]]
    rows.append(entry)
    rows.sort(key=lambda row: int(row.get("build") or 0), reverse=True)
    return rows


def history_html(rows: list[dict]) -> str:
    if not rows:
        return "<li>No previous releases.</li>"
    out = []
    for row in rows:
        version = html.escape(str(row["version"]))
        build = html.escape(str(row.get("build", "")))
        href = html.escape(str(row.get("notes_url")
                               or notes_url(str(row["version"]), "ios")))
        out.append(f'<li><a href="{href}">{version}</a> '
                   f'<span>build {build}</span></li>')
    return "\n".join(out)


def render_ios_page(template: str, version: str, build: int,
                    notes_markdown: str, history: list[dict]) -> str:
    text = template
    replacements = {
        "__HSCC_VERSION__": normalize_version(version),
        "__HSCC_BUILD__": str(int(build)),
        "__HSCC_NOTES__": render_notes_body(notes_markdown),
        "__HSCC_HISTORY__": history_html(
            [row for row in history if row.get("version") != normalize_version(version)]),
        "__HSCC_NOTES_URL__": notes_url(version, "ios"),
        "__HSCC_INSTALL_URL__": IOS_INSTALL_URL,
    }
    for token, value in replacements.items():
        text = text.replace(token, value)
    leftover = sorted(set(re.findall(r"__HSCC_[A-Z0-9_]+__", text)))
    if leftover:
        raise ValueError(f"iOS page template still has {', '.join(leftover)}")
    return text


def render_macos_page(template: str, version: str, build: int,
                      notes_markdown: str) -> str:
    text = template
    replacements = {
        "__HSCC_VERSION__": normalize_version(version),
        "__HSCC_BUILD__": str(int(build)),
        "__HSCC_NOTES__": render_notes_body(notes_markdown),
        "__HSCC_NOTES_URL__": notes_url(version, "macos"),
        "__HSCC_APPCAST__": MACOS_APPCAST_URL,
    }
    for token, value in replacements.items():
        text = text.replace(token, value)
    leftover = sorted(set(re.findall(r"__HSCC_[A-Z0-9_]+__", text)))
    if leftover:
        raise ValueError(f"macOS page template still has {', '.join(leftover)}")
    return text


# ── Sparkle 2 appcast ────────────────────────────────────────────────────────

def _sparkle(tag: str) -> str:
    return f"{{{SPARKLE_NS}}}{tag}"


def appcast_item(version: str, build: int, zip_url: str, length: int,
                 signature: str, *, pub_date: str | None = None,
                 notes: str | None = None,
                 min_os: str = MACOS_MIN_OS) -> dict:
    if not zip_url.startswith("https://"):
        raise ValueError("appcast enclosure url must be https")
    if not signature or not signature.strip():
        raise ValueError("appcast item needs a non-empty EdDSA signature")
    return {
        "version": normalize_version(version),
        "build": str(int(build)),
        "pub_date": pub_date or rfc2822(),
        "notes_url": notes or notes_url(version, "macos"),
        "zip_url": zip_url,
        "length": str(int(length)),
        "signature": signature.strip(),
        "min_os": min_os,
    }


def appcast_xml(items: Iterable[dict], *, title: str = "HSCC Widgets") -> str:
    ET.register_namespace("sparkle", SPARKLE_NS)
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = title
    ET.SubElement(channel, "link").text = MACOS_APPCAST_URL
    ET.SubElement(channel, "description").text = (
        "HSCC Widgets releases for the in-app Sparkle 2 updater.")
    ET.SubElement(channel, "language").text = "en"

    for item in sorted(items, key=lambda i: int(i["build"]), reverse=True):
        node = ET.SubElement(channel, "item")
        ET.SubElement(node, "title").text = f"HSCC Widgets {item['version']}"
        ET.SubElement(node, "pubDate").text = item["pub_date"]
        ET.SubElement(node, "description").text = (
            f"Release notes: {item['notes_url']}")
        ET.SubElement(node, "link").text = item["notes_url"]
        ET.SubElement(node, _sparkle("version")).text = item["build"]
        ET.SubElement(node, _sparkle("shortVersionString")).text = item["version"]
        ET.SubElement(node, _sparkle("minimumSystemVersion")).text = item["min_os"]
        enclosure = ET.SubElement(node, "enclosure", {
            "url": item["zip_url"],
            "length": item["length"],
            "type": "application/octet-stream",
        })
        enclosure.set(_sparkle("edSignature"), item["signature"])

    ET.indent(rss, space="  ")
    body = ET.tostring(rss, encoding="unicode")
    return '<?xml version="1.0" encoding="utf-8"?>\n' + body + "\n"


def parse_appcast(text: str) -> list[dict]:
    if not text or not text.strip():
        return []
    root = ET.fromstring(text)
    rows = []
    for item in root.findall("./channel/item"):
        enclosure = item.find("enclosure")
        if enclosure is None:
            continue
        rows.append({
            "version": (item.findtext(_sparkle("shortVersionString")) or "").strip(),
            "build": (item.findtext(_sparkle("version")) or "").strip(),
            "pub_date": (item.findtext("pubDate") or "").strip(),
            "notes_url": (item.findtext("link") or "").strip(),
            "zip_url": enclosure.get("url", ""),
            "length": enclosure.get("length", ""),
            "signature": enclosure.get(_sparkle("edSignature"), ""),
            "min_os": (item.findtext(_sparkle("minimumSystemVersion"))
                       or MACOS_MIN_OS).strip(),
        })
    return [row for row in rows if row["version"] and row["build"]]


def upsert_appcast(existing: str, item: dict) -> str:
    rows = [row for row in parse_appcast(existing)
            if row["build"] != item["build"] and row["version"] != item["version"]]
    rows.append(item)
    return appcast_xml(rows)


# ── project.yml version bump ─────────────────────────────────────────────────

def set_project_version(path: Path, version: str, build: int) -> None:
    version = normalize_version(version)
    text = path.read_text(encoding="utf-8")
    changed = 0
    for key, value in (("MARKETING_VERSION", f'"{version}"'),
                       ("CURRENT_PROJECT_VERSION", f'"{int(build)}"')):
        pattern = re.compile(rf'^(\s*){key}:\s*"[^"]*"\s*$', re.MULTILINE)
        text, n = pattern.subn(rf'\g<1>{key}: {value}', text)
        if n != 1:
            raise ValueError(f"{path}: expected exactly one {key} line, found {n}")
        changed += n
    if changed != 2:
        raise ValueError(f"{path}: version bump did not apply cleanly")
    path.write_text(text, encoding="utf-8")


# ── fragments that touched the apps ──────────────────────────────────────────

def git_app_fragments(repo_root: Path, since_tag: str | None) -> set[str] | None:
    """Fragment basenames committed together with ``ios-app/`` or ``macos-app/``.

    ``None`` means "no previous apps tag": every current fragment is in scope.
    """
    if not since_tag:
        return None
    cmd = ["git", "-C", str(repo_root), "log", "--format=%x1e%H", "--name-only"]
    cmd.append(f"{since_tag}..HEAD")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git log {since_tag}..HEAD failed: {proc.stderr.strip()}")
    names: set[str] = set()
    for record in proc.stdout.split("\x1e"):
        lines = [line for line in record.splitlines() if line.strip()]
        if not lines:
            continue
        paths = lines[1:]
        if not any(p.startswith(APP_PATH_PREFIXES) for p in paths):
            continue
        for path in paths:
            if path.startswith("changelog.d/") and path.endswith(".md"):
                if "/archive/" not in path and not Path(path).name.lower().startswith("readme"):
                    names.add(Path(path).name)
    return names


def load_app_entries(fragments_dir: Path, since_tag: str | None,
                     repo_root: Path = REPO_ROOT) -> list:
    entries = cf.load_entries(fragments_dir)
    names = git_app_fragments(repo_root, since_tag)
    if names is None:
        return entries
    return [entry for entry in entries if entry.source in names]


# ── writers ──────────────────────────────────────────────────────────────────

def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def publish_ios(pages_dir: Path, ios_dir: Path, version: str, build: int, *,
                notes_markdown: str | None = None, bundle_id: str,
                ipa_url: str, released_at: str | None = None) -> dict:
    version = normalize_version(version)
    released_at = released_at or format_released_at()
    if not notes_markdown:
        notes_markdown = f"# HSCC apps {version}\n\n{DEFAULT_NOTES}\n"
    ios_pages = pages_dir / "ios"
    notes_path = ios_pages / "notes" / f"{version}.html"
    _write(notes_path, render_notes_html(version, notes_markdown))

    history_path = ios_pages / "releases.json"
    history = load_history(history_path)
    entry = {
        "version": version,
        "build": int(build),
        "released_at": released_at,
        "notes_url": notes_url(version, "ios"),
    }
    history = update_history(history, entry)
    _write(history_path, json.dumps(history, indent=2, sort_keys=True) + "\n")

    template = (ios_dir / "site" / "index.html").read_text(encoding="utf-8")
    _write(ios_pages / "index.html",
           render_ios_page(template, version, build, notes_markdown, history))

    feed = version_json(version, build, released_at)
    _write(ios_pages / "version.json", json.dumps(feed, indent=2) + "\n")
    return {"notes": notes_path, "history": history_path,
            "index": ios_pages / "index.html",
            "version_json": ios_pages / "version.json", "feed": feed}


def publish_macos(pages_dir: Path, macos_dir: Path, version: str, build: int, *,
                  notes_markdown: str | None = None, zip_url: str,
                  length: int, signature: str,
                  released_at: str | None = None) -> dict:
    version = normalize_version(version)
    released_at = released_at or format_released_at()
    if not notes_markdown:
        notes_markdown = f"# HSCC apps {version}\n\n{DEFAULT_NOTES}\n"
    macos_pages = pages_dir / "macos"
    notes_path = macos_pages / "notes" / f"{version}.html"
    _write(notes_path, render_notes_html(version, notes_markdown))

    item = appcast_item(version, build, zip_url, length, signature,
                        notes=notes_url(version, "macos"))
    appcast_path = macos_pages / "appcast.xml"
    existing = (appcast_path.read_text(encoding="utf-8")
                if appcast_path.exists() else "")
    _write(appcast_path, upsert_appcast(existing, item))

    template = (macos_dir / "site" / "index.html").read_text(encoding="utf-8")
    _write(macos_pages / "index.html",
           render_macos_page(template, version, build, notes_markdown))
    return {"notes": notes_path, "appcast": appcast_path,
            "index": macos_pages / "index.html", "item": item}


# ── CLI ──────────────────────────────────────────────────────────────────────

def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="apps_release.py",
        description="Generate HSCC app release notes, feeds and Pages files.")
    sub = ap.add_subparsers(dest="command", required=True)

    notes = sub.add_parser("notes", help="render release notes from fragments")
    notes.add_argument("--version", required=True)
    notes.add_argument("--since", default=None,
                       help="previous apps-v tag (omit for every current fragment)")
    notes.add_argument("--fragments-dir", default=str(cf.FRAGMENTS_DIR))
    notes.add_argument("--out", required=True)

    ios = sub.add_parser("publish-ios", help="write /ios page, history and feed")
    ios.add_argument("--pages-dir", required=True)
    ios.add_argument("--ios-dir", required=True)
    ios.add_argument("--version", required=True)
    ios.add_argument("--build", required=True, type=int)
    ios.add_argument("--notes-md", default=None)
    ios.add_argument("--bundle-id", required=True)
    ios.add_argument("--ipa-url", required=True)
    ios.add_argument("--released-at", default=None)

    mac = sub.add_parser("publish-macos", help="write /macos page and appcast")
    mac.add_argument("--pages-dir", required=True)
    mac.add_argument("--macos-dir", required=True)
    mac.add_argument("--version", required=True)
    mac.add_argument("--build", required=True, type=int)
    mac.add_argument("--notes-md", default=None)
    mac.add_argument("--zip-url", required=True)
    mac.add_argument("--length", required=True, type=int)
    mac.add_argument("--signature", required=True)
    mac.add_argument("--released-at", default=None)

    bump = sub.add_parser("set-project-version",
                          help="set MARKETING_VERSION + CURRENT_PROJECT_VERSION")
    bump.add_argument("--project", required=True)
    bump.add_argument("--version", required=True)
    bump.add_argument("--build", required=True, type=int)

    sub.add_parser("last-released",
                   help="print the highest release version among local tags")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "notes":
            entries = load_app_entries(Path(args.fragments_dir), args.since)
            _write(Path(args.out), render_notes(args.version, entries))
            print(f"wrote {args.out} ({len(entries)} fragment entries)")
            return 0
        if args.command == "publish-ios":
            notes = (Path(args.notes_md).read_text(encoding="utf-8")
                     if args.notes_md else None)
            result = publish_ios(Path(args.pages_dir), Path(args.ios_dir),
                                 args.version, args.build, notes_markdown=notes,
                                 bundle_id=args.bundle_id, ipa_url=args.ipa_url,
                                 released_at=args.released_at)
            print(f"wrote {result['index']}, {result['version_json']}, "
                  f"{result['notes']} and {result['history']}")
            return 0
        if args.command == "publish-macos":
            notes = (Path(args.notes_md).read_text(encoding="utf-8")
                     if args.notes_md else None)
            result = publish_macos(Path(args.pages_dir), Path(args.macos_dir),
                                   args.version, args.build, notes_markdown=notes,
                                   zip_url=args.zip_url, length=args.length,
                                   signature=args.signature,
                                   released_at=args.released_at)
            print(f"wrote {result['index']}, {result['appcast']} and {result['notes']}")
            return 0
        if args.command == "set-project-version":
            set_project_version(Path(args.project), args.version, args.build)
            print(f"{args.project}: {normalize_version(args.version)} "
                  f"({int(args.build)})")
            return 0
        if args.command == "last-released":
            proc = subprocess.run(["git", "-C", str(REPO_ROOT), "tag", "-l"],
                                  capture_output=True, text=True)
            if proc.returncode != 0:
                print(proc.stderr.strip(), file=sys.stderr)
                return 2
            print(last_released_version(proc.stdout.splitlines()) or "0.0.0")
            return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"apps_release: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
