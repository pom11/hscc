"""Tests for the landing page at the gh-pages root (t_107669bd).

Four things are pinned here, because each one is a promise the page makes to a
public audience:

  1. **Publishing the root must not touch /ios/ or /macos/.** Those subtrees are
     the only install path for the two apps. The plant/verify tests drive
     ``scripts/publish_site.sh`` against a throwaway remote that already has a
     marker under each subtree and fail if either marker disappears, changes, or
     if the root page is missing.
  2. **The two release scripts must not wipe the root page.** One leg runs the
     real ``macos-app/scripts/publish_macos.sh`` against a throwaway remote that
     has a root page in it; the other asserts the shared preservation mechanism
     (reuse the branch, stage only your own subtree) is still what both scripts
     do, and that neither has acquired a destructive git operation.
  3. **The page renders site/COPY.md, not a paraphrase of it.** The card's
     sentences and every template row must appear in the page.
  4. **The command list cannot drift from the CLI** — scripts/gen_site_commands.py
     — and the page stays self-contained (no CDN, no web font, no private
     address) and usable at phone width.
"""

from __future__ import annotations

import html as html_mod
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

SITE = REPO / "site"
INDEX_HTML = SITE / "index.html"
SITE_CSS = SITE / "site.css"
SITE_ICON = SITE / "assets" / "hscc-icon.png"
APP_ICON = REPO / "ios-app" / "Assets.xcassets" / "AppIcon.appiconset" / "AppIcon.png"
COPY_MD = SITE / "COPY.md"
PUBLISH_SITE = SCRIPTS / "publish_site.sh"
IOS_PUBLISH = REPO / "ios-app" / "scripts" / "publish_pages.sh"
MACOS_PUBLISH = REPO / "macos-app" / "scripts" / "publish_macos.sh"

HELP_FIXTURE = Path(__file__).parent / "fixtures" / "hscc_help_landing.txt"

BEGIN = "<!-- COMMANDS:BEGIN"
END = "<!-- COMMANDS:END -->"


# ── helpers ──────────────────────────────────────────────────────────────────

def git(*args, cwd=None, check=True) -> str:
    """git with a fixed identity, so the test never depends on the operator."""
    out = subprocess.run(
        ["git", "-c", "user.name=site-test", "-c", "user.email=site@test.invalid",
         *args],
        cwd=str(cwd) if cwd else None, capture_output=True, text=True, check=False,
    )
    if check and out.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed ({out.returncode}): {out.stderr.strip()}"
        )
    return out.stdout


def make_remote_with_pages(tmp_path: Path, pages: dict[str, str]) -> Path:
    """A bare 'remote' whose gh-pages branch already holds `pages`.

    This is the state the task calls CRITICAL: /ios/ and /macos/ are already
    being served when the root page is published.
    """
    bare = tmp_path / "remote.git"
    bare.mkdir()
    git("init", "--quiet", "--bare", str(bare))
    seed = tmp_path / "seed"
    seed.mkdir()
    git("init", "--quiet", str(seed))
    for rel, body in pages.items():
        target = seed / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    git("add", "-A", cwd=seed)
    git("commit", "--quiet", "-m", "seed gh-pages", cwd=seed)
    git("push", "--quiet", str(bare), "HEAD:refs/heads/gh-pages", cwd=seed)
    return bare


def remote_files(bare: Path, branch: str = "gh-pages") -> dict[str, bytes]:
    """Every tracked path on the remote branch, with its raw bytes."""
    names = git("ls-tree", "-r", "--name-only", branch, cwd=bare).split()
    out: dict[str, bytes] = {}
    for n in names:
        proc = subprocess.run(
            ["git", "show", f"{branch}:{n}"], cwd=str(bare),
            capture_output=True, check=True,
        )
        out[n] = proc.stdout
    return out


def b(text: str) -> bytes:
    return text.encode("utf-8")


def run_publish(bare: Path, env_extra: dict[str, str] | None = None):
    env = {"HSCC_PAGES_REMOTE": str(bare), "HSCC_PAGES_BRANCH": "gh-pages"}
    env.update(env_extra or {})
    import os
    full = {**os.environ, **env}
    return subprocess.run(
        ["bash", str(PUBLISH_SITE)], capture_output=True, text=True,
        cwd=str(REPO), env=full,
    )


# ── 1. the root publish preserves /ios/ and /macos/ ──────────────────────────

PLANTED = {
    ".nojekyll": "",
    "ios/index.html": "<!doctype html><title>ios install</title>",
    "ios/manifest.plist": "<plist>DO NOT TOUCH ME</plist>",
    "ios/keep-marker.txt": "ios-marker-9137",
    "ios/icons/icon-57.png": "not-a-real-png-but-must-survive",
    "macos/index.html": "<!doctype html><title>macos install</title>",
    "macos/appcast.xml": "<rss>DO NOT TOUCH ME</rss>",
    "macos/keep-marker.txt": "macos-marker-4820",
}


def test_publish_site_keeps_every_ios_and_macos_file(tmp_path):
    """Plant/verify: markers under /ios/ and /macos/ survive a root publish."""
    bare = make_remote_with_pages(tmp_path, PLANTED)

    proc = run_publish(bare)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    after = remote_files(bare)
    for rel, body in PLANTED.items():
        assert rel in after, f"{rel} was deleted by the root publish"
        assert after[rel] == b(body), f"{rel} was rewritten by the root publish"
    assert after["ios/keep-marker.txt"] == b("ios-marker-9137")
    assert after["macos/keep-marker.txt"] == b("macos-marker-4820")

    # ...and the root page is actually there, with the repo's own content.
    assert after["index.html"] == INDEX_HTML.read_bytes()
    assert after["site.css"] == SITE_CSS.read_bytes()
    assert after["assets/hscc-icon.png"] == SITE_ICON.read_bytes()
    assert after["ios/icons/icon-57.png"] == b("not-a-real-png-but-must-survive")
    assert ".nojekyll" in after


def test_publish_site_creates_the_branch_when_there_is_none(tmp_path):
    """No branch means nothing to preserve — but the page still has to land."""
    bare = tmp_path / "remote.git"
    bare.mkdir()
    git("init", "--quiet", "--bare", str(bare))

    proc = run_publish(bare)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    after = remote_files(bare)
    assert "index.html" in after and "site.css" in after
    # It must not have invented the app subtrees either.
    assert not [p for p in after if p.startswith(("ios/", "macos/"))]


def test_publish_site_twice_is_stable_and_still_preserves(tmp_path):
    bare = make_remote_with_pages(tmp_path, PLANTED)
    first = run_publish(bare)
    assert first.returncode == 0, first.stdout + first.stderr
    mid = remote_files(bare)

    second = run_publish(bare)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "no change to publish" in second.stdout
    assert remote_files(bare) == mid


def test_publish_site_refuses_a_page_that_loads_app_subtree_assets(tmp_path):
    """The self-containment guard fires, and it publishes nothing."""
    planted = dict(PLANTED)
    planted["index.html"] = "<html><body><img src=\"ios/icons/icon-57.png\">"
    bare = make_remote_with_pages(tmp_path, planted)

    fake_site = tmp_path / "site"
    shutil.copytree(SITE, fake_site)
    page = fake_site / "index.html"
    page.write_text(page.read_text(encoding="utf-8")
                    + '<img src="macos/appcast.xml">\n', encoding="utf-8")

    proc = run_publish(bare, {"HSCC_SITE_DIR": str(fake_site)})
    assert proc.returncode != 0
    assert "subtree this" in (proc.stdout + proc.stderr) or "self-contained" in (
        proc.stdout + proc.stderr)
    # The failed run left the remote exactly as it was.
    assert remote_files(bare) == {k: b(v) for k, v in planted.items()}


def test_publish_site_script_never_uses_a_destructive_git_operation():
    """The guard is cheap enough to also just be forbidden in the script."""
    text = PUBLISH_SITE.read_text(encoding="utf-8")
    # The header comments say out loud what the script refuses to do, so scan
    # only the lines that can actually run.
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    for banned in ("git rm", "git clean", "push --force", "push -f",
                   "git checkout -B", "rm -rf \"$PAGES_DIR/"):
        assert banned not in code, f"publish_site.sh runs {banned!r}"
    assert 'add -- "${ROOT_FILES[@]}" "${ROOT_DIRS[@]}"' in text, (
        "publish_site.sh must stage its own root paths, not `git add -A`")


# ── 2. the release scripts must keep the root page ───────────────────────────

def test_macos_release_script_keeps_the_root_page(tmp_path):
    """Behavioural: run the real publish_macos.sh against a throwaway remote.

    `gh` is stubbed (the script only needs `gh auth status` to succeed) so no
    GitHub call happens; everything else — the worktree, the subtree staging,
    the push — is the production code path.
    """
    if shutil.which("gh") is None:
        pytest.skip("gh is not installed; the script's tool probe would abort")

    root = tmp_path / "repo"
    (root / "macos-app" / "scripts").mkdir(parents=True)
    (root / "macos-app" / "site").mkdir(parents=True)
    (root / "scripts").mkdir(parents=True)
    shutil.copy(MACOS_PUBLISH, root / "macos-app" / "scripts" / "publish_macos.sh")
    shutil.copy(REPO / "macos-app" / "site" / "index.html",
                root / "macos-app" / "site" / "index.html")
    for name in ("apps_release.py", "changelog_fragments.py"):
        shutil.copy(SCRIPTS / name, root / "scripts" / name)
    zip_path = tmp_path / "HSCC-Widgets-9.9.9.zip"
    zip_path.write_bytes(b"zip-bytes-for-the-length-field")

    planted = dict(PLANTED)
    planted["index.html"] = "<!doctype html><title>the landing page</title>"
    bare = make_remote_with_pages(tmp_path, planted)

    git("init", "--quiet", str(root))
    git("add", "-A", cwd=root)
    git("commit", "--quiet", "-m", "release fixture", cwd=root)
    git("remote", "add", "origin", str(bare), cwd=root)

    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "gh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (stub / "gh").chmod(0o755)

    import os
    env = {**os.environ, "PATH": f"{stub}{os.pathsep}{os.environ['PATH']}",
           "TMPDIR": str(tmp_path)}
    proc = subprocess.run(
        ["bash", str(root / "macos-app" / "scripts" / "publish_macos.sh"),
         "9.9.9", "999", str(zip_path), "dummysignature"],
        capture_output=True, text=True, cwd=str(root), env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    after = remote_files(bare)
    assert after["index.html"] == b(planted["index.html"]), (
        "publish_macos.sh rewrote or dropped the landing page at the root")
    assert after["ios/keep-marker.txt"] == b("ios-marker-9137")
    assert after["macos/keep-marker.txt"] == b("macos-marker-4820")
    # The release itself landed.
    assert after["macos/index.html"].startswith(b("<!doctype html>"))
    assert b("9.9.9") in after["macos/appcast.xml"]


@pytest.mark.parametrize("script", [IOS_PUBLISH, MACOS_PUBLISH],
                         ids=["ios", "macos"])
def test_release_scripts_share_the_preservation_mechanism(script):
    """Static guard for the mechanism both release legs rely on.

    Reusing an existing gh-pages branch and staging only your own subtree is
    what keeps the OTHER publishers' files alive. If either half of that is
    edited away — an orphan checkout that no longer checks for the branch, a
    `git add -A`, a `git clean` — the root page (and the other app's subtree)
    starts getting wiped on release.
    """
    text = script.read_text(encoding="utf-8")
    assert "git ls-remote --exit-code --heads origin gh-pages" in text, (
        f"{script.name} no longer checks whether gh-pages exists before "
        "choosing the orphan path")
    assert "reusing existing gh-pages" in text
    assert "checkout --quiet --orphan gh-pages" in text

    before_orphan = text.split("git rm -rf")[0]
    assert "checkout --quiet --orphan" in before_orphan, (
        f"{script.name}: the orphan checkout must sit in the branch-missing "
        "else-branch, before the tree is emptied")

    adds = re.findall(r"git -C \"\$PAGES_DIR\" add ([^\n]+)", text)
    assert adds, f"{script.name} stages nothing — how does it publish?"
    subtree = script.parent.parent.name          # ios-app / macos-app
    own = subtree.split("-")[0]                  # ios / macos
    for add in adds:
        assert own in add, f"{script.name} stages {add!r}, not its own subtree"
        assert "-A" not in add and "." not in add.replace(".nojekyll", "")

    for banned in ("git clean", "push --force", "push -f", "push --delete"):
        assert banned not in text, f"{script.name} contains {banned!r}"


# ── 3. the page renders the copy card ────────────────────────────────────────

def normalize(text: str) -> str:
    """Compare copy without markup noise.

    `**bold**` and `backticks` are markdown conventions, `—` is how the card
    separates a bullet's lead-in from its explanation, and the page renders
    both as structure (a card title, a <code> span) rather than characters.
    Case is ignored for the same reason: a card title may start a sentence.
    Markdown links keep their visible text and drop their target; an autolink
    (<https://…>) has the URL as its visible text, so it keeps the URL — which
    is what the page must show for those two links.
    """
    text = html_mod.unescape(text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)      # [text](url)
    text = re.sub(r"<(https?://[^>]+)>", r"\1", text)          # <url>
    text = re.sub(r"<[^>]+>", " ", text)                       # html tags
    text = text.replace("**", "").replace("`", "").replace("—", " ")
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    return re.sub(r"\s+", " ", text).strip().lower()


@pytest.fixture(scope="module")
def page_text() -> str:
    return normalize(INDEX_HTML.read_text(encoding="utf-8"))


def copy_sections() -> dict[str, str]:
    """The numbered copy sections of site/COPY.md, keyed by their heading.

    Split on EVERY `## ` heading so the trailing "REVIEW notes" block (which is
    reviewer text, not page copy) cannot leak into section 7.
    """
    text = COPY_MD.read_text(encoding="utf-8")
    parts = re.split(r"^## ([^\n]+)$", text, flags=re.M)
    out: dict[str, str] = {}
    for i in range(1, len(parts) - 1, 2):
        if re.match(r"\d+\.", parts[i]):
            out[parts[i].strip()] = parts[i + 1]
    return out


def copy_bullets(section: str) -> list[str]:
    """Each `- ...` bullet of a section, continuations joined."""
    lines = section.splitlines()
    out: list[str] = []
    for line in lines:
        if line.startswith("- "):
            out.append(line[2:].strip())
        elif line.startswith("  ") and out:
            out[-1] += " " + line.strip()
        elif not line.strip():
            continue
    return out


def test_every_copy_bullet_appears_in_the_page(page_text):
    sections = copy_sections()
    wanted: list[str] = []
    for key in ("1. What it is (ELI5)", "2. Why", "3. What it does",
                "6. How it looks"):
        assert key in sections, f"site/COPY.md no longer has section {key!r}"
        wanted += copy_bullets(sections[key])
    assert len(wanted) >= 15, "the copy card changed shape; teach this test"

    missing = [w for w in wanted if normalize(w) not in page_text]
    assert not missing, "these copy lines are not in the page verbatim: " + \
        "\n  ".join(missing)


def test_the_copy_prose_of_the_other_sections_appears(page_text):
    sections = copy_sections()
    for key in ("4. Templates", "7. Get it"):
        body = sections[key]
        for para in re.split(r"\n\s*\n", body):
            para = para.strip()
            # Tables, fenced blocks and rules have their own tests below.
            if not para or para.startswith(("|", "```", "<!--", "`hscc")):
                continue
            if set(para) <= {"-"}:      # a `---` rule carries no copy
                continue
            if para.startswith("## "):
                continue
            assert normalize(para) in page_text, (
                f"section {key!r} paragraph is not in the page: {para[:70]}...")


def test_every_shipped_template_row_appears_in_the_page(page_text):
    sections = copy_sections()
    rows = [r for r in sections["4. Templates"].splitlines()
            if r.startswith("| `")]
    assert len(rows) == 23, (
        f"expected the 23 shipped templates, found {len(rows)} — the copy card "
        "changed and the page table did not follow")
    for row in rows:
        name, desc = [c.strip() for c in row.strip().strip("|").split("|")[0:2]]
        line = normalize(f"{name} {desc}")
        assert line in page_text, f"template row not rendered verbatim: {row[:80]}"


def test_install_commands_are_the_real_ones(page_text):
    for line in ("brew tap pom11/hscc", "brew install hscc",
                 "hscc bootstrap --yes",
                 "brew install --cask pom11/hscc/hscc-widgets"):
        assert normalize(line) in page_text, f"install line missing: {line}"


def test_the_ad_hoc_gate_is_stated_next_to_the_ios_link(page_text):
    """REVIEW line from the copy card: the page must not imply self-serve."""
    assert "ios/" in INDEX_HTML.read_text(encoding="utf-8")
    assert normalize("It's an Ad Hoc build, so your device has to be registered "
                     "with the maintainer first") in page_text


# ── 4. commands cannot drift, page stays self-contained ──────────────────────

def test_page_command_region_matches_the_generated_one():
    """The committed list is what the real CLI generates, byte for byte."""
    import gen_site_commands as g

    assert g.main(["--help-file", str(HELP_FIXTURE), "--page", str(INDEX_HTML),
                   "--quiet"]) == 0, "site/index.html drifted from `hscc --help`"


def test_live_cli_and_fixture_agree_on_the_command_list():
    """A fixture cannot hide drift from a CLI change, so compare both parses."""
    import gen_site_commands as g

    if shutil.which("hscc") is None:
        pytest.skip("hscc is not installed on this host")
    live = g.parse_help(g.fetch_help(None))
    fixture = g.parse_help(HELP_FIXTURE.read_text(encoding="utf-8"))
    assert [(x["name"], x["sigs"]) for x in live] == \
        [(x["name"], x["sigs"]) for x in fixture], (
        "the installed CLI changed its command surface; regenerate the page "
        "(python3 scripts/gen_site_commands.py --write) and refresh "
        "scripts/tests/fixtures/hscc_help_landing.txt")


def test_generator_fails_when_the_page_is_missing_a_command(tmp_path):
    """The drift test is not vacuous: removing one command breaks it."""
    import gen_site_commands as g

    page = tmp_path / "index.html"
    page.write_text(INDEX_HTML.read_text(encoding="utf-8").replace(
        '<li><code>hscc triggers</code>', '<li><code>hscc triggerz</code>'),
        encoding="utf-8")
    assert g.main(["--help-file", str(HELP_FIXTURE), "--page", str(page),
                   "--quiet"]) == 1


def test_generator_refuses_to_invent_copy_for_an_unknown_command(tmp_path):
    """A new CLI command must not be answered with a made-up sentence."""
    import gen_site_commands as g

    grown = HELP_FIXTURE.read_text(encoding="utf-8").replace(
        "  triggers             Show trigger-engine rules and recent firings",
        "  triggers             Show trigger-engine rules and recent firings\n"
        "  frobnicate           Brand new command nobody has described",
    )
    fixture = tmp_path / "grown.txt"
    fixture.write_text(grown, encoding="utf-8")
    proc_out = []
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        rc = g.main(["--help-file", str(fixture), "--page", str(INDEX_HTML),
                     "--quiet"])
    proc_out.append(buf.getvalue())
    assert rc == 1
    assert "frobnicate" in proc_out[0], "the error must name the undescribed command"


def test_page_has_no_remote_or_framework_references():
    text = INDEX_HTML.read_text(encoding="utf-8")
    css = SITE_CSS.read_text(encoding="utf-8")
    assert "<script" not in text, "the landing page must not need javascript"
    assert "@import" not in css
    assert "<link" in text and "stylesheet" in text
    for tag in re.findall(r"<link[^>]*>", text):
        assert "site.css" in tag or "hscc-icon.png" in tag, tag
    for url in re.findall(r"https?://[^\s\"'()<>]+", text + css):
        assert re.match(r"https?://(github\.com|pom11\.github\.io)/", url), (
            f"unexpected external reference: {url}")
    assert "fonts." not in text + css and "@font-face" not in css


def test_page_and_css_carry_no_private_address():
    """Same detector as the commit-time and pytest gates, not a second regex."""
    import address_guard as ag

    offenders = ag.scan_paths(SITE, ["index.html", "site.css", "COPY.md",
                                     "assets/hscc-icon.png"])
    assert not offenders, f"private address in the site files: {offenders}"


def test_page_is_usable_at_phone_width():
    text = INDEX_HTML.read_text(encoding="utf-8")
    css = SITE_CSS.read_text(encoding="utf-8")
    assert 'name="viewport" content="width=device-width, initial-scale=1"' in text
    assert "max-width: 760px" in css
    assert "@media (max-width: 619px)" in css, (
        "the phone layout is what makes this readable at 360px")
    # Nothing may be wider than a phone's content box minus padding.
    for width in re.findall(r"(?<![-\w])width:\s*(\d+)px", css):
        assert int(width) <= 112, f"fixed width {width}px overflows a 360px phone"
    for prop in re.findall(r"min-width:\s*(\d+)px", css):
        assert int(prop) >= 620, f"min-width {prop}px gates the desktop layout"


def test_the_icon_is_the_app_icon_not_a_redraw():
    assert SITE_ICON.read_bytes() == APP_ICON.read_bytes(), (
        "site/assets/hscc-icon.png must stay a byte-for-byte copy of the app "
        "icon: cp ios-app/Assets.xcassets/AppIcon.appiconset/AppIcon.png "
        "site/assets/hscc-icon.png")


def test_page_has_exactly_the_contracted_sections():
    """The copy card contracts seven sections in order; the page renders them."""
    order = ["What it is", "Why", "What it does", "Templates", "Commands",
             "How it looks", "Get it"]
    text = INDEX_HTML.read_text(encoding="utf-8")
    heads = re.findall(r"<h2[^>]*>([^<]+)</h2>", text)
    assert heads == order, f"section order changed: {heads}"
    assert "REVIEW" not in text, "reviewer notes are not page copy"


def test_commands_markers_survive_in_both_files():
    for path in (COPY_MD, INDEX_HTML):
        body = path.read_text(encoding="utf-8")
        assert BEGIN in body and END in body, f"{path.name} lost its COMMANDS markers"
        assert body.index(BEGIN) < body.index(END)
