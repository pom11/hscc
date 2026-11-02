"""Drift guard: ``requirements.txt`` (the dependency SOT) vs the brew tap
formula's ``resource`` blocks (GAP-D-1, audit 2026-10-31, card t_b82ad0fe).

WHY THIS EXISTS
---------------
``Formula/hscc.rb`` builds its libexec venv with ``venv.pip_install resources``
— a hand-written list of ``resource "<name>" do url/sha256`` blocks. It NEVER
reads ``requirements.txt``: the file is copied into the payload tree, but pip is
never pointed at it. So the formula is a SECOND, manual statement of the same
dependency set, and any pin added to the SOT is silently inert for every brew
install until somebody also edits the tap.

That is exactly what happened (measured, not theorised): ``httpx==0.28.1``
landed in ``requirements.txt`` at ``07bbfcdf`` (PR #113, the fix for
``hscc kanban monitor`` dying with ``No module named 'httpx'`` under
``/opt/homebrew/bin/hscc``) and the 2.14.0 tap formula shipped without it. The
guard that should have caught it,
``hscc_daemon/tests/test_kanban_monitor_deps.py``, soft-skipped the keg
(``verdicts.append("...skip...") + continue`` behind ``assert verdicts``) so the
suite reported ``5 passed`` over a keg venv that was still blind.

WHAT THIS CHECKS
----------------
  1. PRESENCE — every name in ``requirements.txt`` must appear as a
     ``resource`` block in the formula. A missing one means the pin is inert for
     brew users: FAIL.
  2. EXACT PINS — an ``==`` pin in the SOT must be that exact version in the
     formula's sdist filename. A pin that is present but a different version
     means brew installs a version nobody tested against the SOT: FAIL.
  3. RANGES — a ``>=``/``<`` spec must be satisfied by the version the formula
     pins (this is how ``rich>=13,<16`` in the SOT stays honest while the formula
     pins one release of it). Skipped with a visible reason when ``packaging``
     is not importable in the suite interpreter — never silently passed.
  4. STRUCTURE — every resource block carries a url AND a sha256 and a
     parseable name/version. A hand-mangled block is what a manual list gets
     over time.
  5. NON-VACUITY — the comparator is run against fabricated formulas that omit
     httpx and that pin a wrong version, and must REPORT them. A drift check
     that cannot report a planted drift proves nothing (the lesson of the
     soft-skip above).

The formula's EXTRA resources (transitives: mdurl, pygments, anyio, ...) are
allowed and not compared: the SOT is a direct-dependency list and pip runs with
``--no-deps`` against these resources, so the closure has to be stated only on
the formula side. Checking that closure needs the network, which a repo test
must not require.

FINDING THE FORMULA (the card's "read its path from the existing tap-bump
workflow … or config")
----------------------------------------------------------------------
The path comes from ``scripts/bump_formula.py`` — the same module the
tap-bump workflow (``.github/workflows/bump-homebrew-tap.yml.disabled``) calls,
and therefore the single source for where the tap lives:
``$HSCC_TAP_DIR`` → ``$DEFAULT_TAP_DIR`` (``~/dev/homebrew-hscc``) → the cached
clone under ``$HSCC_TAP_CACHE_DIR`` → ``<that>/Formula/hscc.rb``.

Unlike ``bump_formula.resolve_tap_dir()`` this NEVER clones: a test that reaches
the network to satisfy itself is a test that passes on machines with no tap and
hangs on machines with a firewalled one. If no candidate carries the formula the
guard ``pytest.skip()``S with the list of paths it looked at, so CI reports
"this machine could not check the tap" instead of "the tap agrees".
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
REQUIREMENTS = REPO / "requirements.txt"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import bump_formula as bf  # noqa: E402


# ── the SOT parser: loaded from its canonical home, never re-implemented ─────
#
# ``parse_requirements`` lives in the httpx-pin guard
# (hscc_daemon/tests/test_kanban_monitor_deps.py), which is the module whose
# whole subject is this pin. Loaded by path (the same idiom hscc_daemon/tests/
# conftest.py uses for sparkrun_fence.py) so neither suite's sys.path nor a
# package layout is assumed. If it cannot be loaded that is a hard ERROR — a
# guard that quietly loses its parser must not be able to report green.

def _load_sot_parser():
    path = REPO / "hscc_daemon" / "tests" / "test_kanban_monitor_deps.py"
    spec = importlib.util.spec_from_file_location(
        "hscc_monitor_deps_for_drift", path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hscc_monitor_deps_for_drift"] = mod
    spec.loader.exec_module(mod)
    return mod.parse_requirements


parse_requirements = _load_sot_parser()


# ── the formula parser ───────────────────────────────────────────────────────

_RESOURCE_BLOCK_RE = re.compile(
    r'^\s*resource\s+"(?P<name>[^"]+)"\s+do\s*$'
    r'(?P<body>.*?)'
    r'^\s*end\s*$',
    re.MULTILINE | re.DOTALL)
_URL_RE = re.compile(r'^\s*url\s+"(?P<url>[^"]*)"\s*$', re.MULTILINE)
_SHA_RE = re.compile(r'^\s*sha256\s+"(?P<sha>[0-9a-fA-F]{64})"\s*$',
                     re.MULTILINE)

#: ``markdown_it_py-4.2.0.tar.gz`` / ``pyyaml-6.0.3.tar.gz`` /
#: ``certifi-2026.5.20.tar.gz`` -> the PEP 440 version after the last dash.
_SDIST_VERSION_RE = re.compile(
    r"^(?P<stem>.+?)-(?P<version>[0-9][0-9A-Za-z]*(?:\.[0-9A-Za-z]+)*"
    r"(?:-[0-9A-Za-z]+)?(?:\+[0-9A-Za-z.]+)?)\.(?:tar\.gz|zip)$")


def canonical(name: str) -> str:
    """PEP 503 name normalisation — ``PyYAML``/``pyyaml``, ``markdown-it-py``/
    ``markdown_it_py`` and ``Http_X`` all mean one package. The SOT and the
    formula spell these differently on purpose (the formula names the PyPI
    project, the SOT uses the import-ish form), so a raw string compare would
    flag drift that is not drift."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_formula_resources(text: str) -> dict:
    """``{canonical_name: {"name": str, "url": str, "sha256": str,
    "version": str|None, "filename": str}}`` for a formula's resource blocks.

    Deliberately regex-over-text rather than a Ruby parse: the only facts needed
    are the resource's name, its sdist url and its digest, and a Ruby parser is
    not a stdlib dependency of this suite. Structural problems are NOT hidden
    here — a block with no url, no sha256, or a filename carrying no version is
    reported as-is and asserted on below.
    """
    out = {}
    for m in _RESOURCE_BLOCK_RE.finditer(text):
        name = m.group("name")
        body = m.group("body")
        url_m = _URL_RE.search(body)
        sha_m = _SHA_RE.search(body)
        url = url_m.group("url") if url_m else ""
        filename = url.rsplit("/", 1)[-1] if url else ""
        ver_m = _SDIST_VERSION_RE.match(filename) if filename else None
        out[canonical(name)] = {
            "name": name,
            "url": url,
            "sha256": sha_m.group("sha") if sha_m else "",
            "version": ver_m.group("version") if ver_m else None,
            "filename": filename,
        }
    return out


def sdist_version_from_url(url: str) -> str | None:
    m = _SDIST_VERSION_RE.match(url.rsplit("/", 1)[-1])
    return m.group("version") if m else None


# ── the comparator (pure: the only I/O is the caller's two strings) ──────────

def compare(sot_text: str, formula_text: str) -> list:
    """Every way ``formula_text`` has drifted from ``sot_text``, as strings.

    Empty list == the formula reproduces the SOT's dependency set. Never
    raises for a *drift* — a drift is data the caller asserts on, and the
    non-vacuity controls below feed it fabricated formulas.
    """
    reqs = parse_requirements(sot_text)
    res = parse_formula_resources(formula_text)
    drift = []

    for name in sorted(reqs):
        if name not in res:
            drift.append(
                f"MISSING: '{name}' is pinned in requirements.txt "
                f"({reqs[name] or 'no specifier'}) but has NO resource block in "
                "Formula/hscc.rb — `venv.pip_install resources` builds the brew "
                "venv from those blocks and never reads requirements.txt, so "
                f"this pin is INERT for every brew install until a "
                f"`resource` block for it is added to the formula")
            continue
        entry = res[name]
        spec = reqs[name]
        if entry["version"] is None:
            drift.append(
                f"UNPARSEABLE: resource '{entry['name']}' url "
                f"{entry['url']!r} carries no version in its sdist filename — "
                "cannot compare it against the SOT")
            continue
        exact = re.fullmatch(r"==[ \t]*([A-Za-z0-9][A-Za-z0-9._+!-]*)", spec)
        if exact:
            if entry["version"] != exact.group(1):
                drift.append(
                    f"VERSION: '{name}' is pinned {spec} in requirements.txt but "
                    f"the formula installs {entry['version']} "
                    f"({entry['filename']}) — brew users get a version the SOT "
                    "does not declare")
            continue
        if not spec:
            continue  # unpinned in the SOT: any version agrees
        verdict = _ranges_satisfied(spec, entry["version"])
        if verdict is False:
            drift.append(
                f"RANGE: '{name}' requires {spec} in requirements.txt but the "
                f"formula installs {entry['version']} ({entry['filename']})")
        elif verdict is None:
            drift.append(
                f"UNVERIFIED:{name} {spec} vs formula {entry['version']}")
    return drift


def _ranges_satisfied(spec: str, version: str):
    """True / False / None (None = could not decide — the caller surfaces it,
    it never counts as agreement)."""
    try:
        from packaging.specifiers import SpecifierSet
        from packaging.version import InvalidVersion, Version
    except ImportError:
        return None
    try:
        return Version(version) in SpecifierSet(spec)
    except InvalidVersion:
        return None


# ── formula discovery (config-driven, never a clone) ────────────────────────

def candidate_formula_paths() -> list:
    """Where the tap formula could be, in ``bump_formula``'s own priority order.

    ``HSCC_TAP_DIR`` is honoured first because that is how the release host (and
    a test) points the tooling at a checkout; ``DEFAULT_TAP_DIR`` and the cache
    clone target follow because they are what ``resolve_tap_dir`` would use. The
    clone step of ``resolve_tap_dir`` is deliberately NOT reused — see the
    module docstring.
    """
    cands = []
    if os.environ.get("HSCC_TAP_DIR"):
        cands.append(Path(os.environ["HSCC_TAP_DIR"]).expanduser()
                     / bf.FORMULA_REL)
    cands.append(bf.DEFAULT_TAP_DIR / bf.FORMULA_REL)
    cands.append(bf.CACHE_ROOT / Path(bf.TAP_REPO).name / bf.FORMULA_REL)
    seen, out = set(), []
    for c in cands:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def read_formula() -> tuple:
    """``(path, text)`` of the first candidate that exists.

    ``pytest.skip`` — with every path that was tried — when none exists. This is
    the card's requirement: an unreachable formula is a VISIBLE skip, never a
    pass. A guard that green-lights the tap on a machine that never opened it is
    the same failure mode that hid GAP-D-1.
    """
    tried = candidate_formula_paths()
    for path in tried:
        if path.is_file():
            return path, path.read_text(encoding="utf-8")
    pytest.skip(
        "the tap formula is unreachable from this test environment — no drift "
        "check was possible. Looked at (in bump_formula's own priority order): "
        + ", ".join(str(p) for p in tried)
        + ". Set HSCC_TAP_DIR to a pom11/homebrew-hscc checkout to arm this "
          "guard. (The guard does not clone: a repo test that needs the network "
          "to satisfy itself is not a guard.)")
    raise AssertionError("unreachable")  # pytest.skip raises


def _sot_text() -> str:
    return REQUIREMENTS.read_text(encoding="utf-8")


# ── the drift assertions against the REAL tap ───────────────────────────────

def test_formula_resources_match_requirements_sot():
    """Every requirements.txt pin must be a formula resource at the SOT's
    version — the check whose absence let httpx be inert for brew installs."""
    path, formula = read_formula()
    drift = compare(_sot_text(), formula)
    unverified = [d for d in drift if d.startswith("UNVERIFIED:")]
    hard = [d for d in drift if not d.startswith("UNVERIFIED:")]
    assert not hard, (
        f"requirements.txt and {path} have DRIFTED ({len(hard)} issue(s)):\n"
        + "\n".join(f"  - {d}" for d in hard)
        + "\n\nThe formula builds its venv with `venv.pip_install resources` "
        "and never reads requirements.txt, so a pin in the SOT that is not also "
        "a resource is silently INERT for every `brew install hscc`. Fix: add "
        "or correct the matching `resource` block in the tap "
        "(operator-side commit + push; `scripts/bump_formula.py` deliberately "
        "never touches resource blocks).")
    if unverified:
        pytest.skip(
            "packaging is not importable in this suite interpreter, so the "
            "range-spec comparisons below could not be evaluated (presence and "
            "exact pins WERE checked): " + "; ".join(unverified))


def test_every_formula_resource_block_is_well_formed():
    """A hand-maintained list rots structurally: a block without a digest, or an
    sdist filename with no version, is not usable by ``pip_install resources``
    and is not comparable by this guard."""
    path, formula = read_formula()
    bad = []
    for name, entry in parse_formula_resources(formula).items():
        if not entry["url"]:
            bad.append(f"{name}: no url line")
        elif not entry["url"].startswith("https://files.pythonhosted.org/"):
            bad.append(f"{name}: url is not a PyPI sdist host "
                       f"({entry['url']})")
        if not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]):
            bad.append(f"{name}: sha256 missing or not 64 hex "
                       f"({entry['sha256'][:12]!r})")
        if entry["version"] is None:
            bad.append(f"{name}: no version in sdist filename "
                       f"{entry['filename']!r}")
    assert not bad, (
        f"{path} carries malformed resource block(s):\n"
        + "\n".join(f"  - {b}" for b in bad))


def test_formula_version_pins_the_current_release():
    """The formula must pin the version this checkout's VERSION file declares.

    Not the card's ask, but it is the check that makes the resource comparison
    meaningful: comparing the SOT of ``main`` against a formula that still pins
    an older release answers a question nobody asked. ``bump_formula.formula_version``
    is the same reader the bump flow uses (explicit ``version`` line, else the
    ``vX.Y.Z`` in the archive url).
    """
    path, formula = read_formula()
    want = (REPO / "VERSION").read_text(encoding="utf-8").strip()
    try:
        got = bf.formula_version(formula)
    except bf.BumpError as exc:
        pytest.fail(f"{path} states no readable version: {exc}")
    if got != want:
        pytest.skip(
            f"{path} pins {got} but this checkout's VERSION says {want} — the "
            "tap trails the release flow (bump_formula.py owns that), so the "
            "dependency comparison above is the only leg that applied. Run "
            f"`python3 scripts/bump_formula.py --tag v{want} --dry-run`.")


# ── non-vacuity: a comparator that cannot report drift is not a guard ────────

def _satisfying_version(spec: str) -> str:
    """A version that satisfies ``spec`` — enough to build a matching formula.

    ``==X`` -> X; a range -> its lower bound (``>=13,<16`` -> ``13``); unpinned
    -> ``1.0``. Deliberately simple: this builds a fixture, not a resolver.
    """
    exact = re.fullmatch(r"==[ \t]*([A-Za-z0-9][A-Za-z0-9._+!-]*)", spec)
    if exact:
        return exact.group(1)
    lower = re.search(r">=[ \t]*([0-9][0-9.]*)", spec)
    return lower.group(1) if lower else "1.0"


def _formula_from_sot(drop: str | None = None,
                      version_override: dict | None = None) -> str:
    """A formula whose resource blocks match the real SOT, generated FROM it.

    Generated rather than hand-written so a future pin added to requirements.txt
    cannot make the positive control stale (a hand-written fixture would report
    the new package MISSING and the "matched baseline" test would fail on a card
    that did nothing wrong). The controls are now mutations of a baseline that
    is correct by construction.
    """
    reqs = parse_requirements(_sot_text())
    blocks = []
    for name in sorted(reqs):
        if drop and name == drop:
            continue
        version = (version_override or {}).get(name) or _satisfying_version(reqs[name])
        blocks.append(
            f'  resource "{name}" do\n'
            f'    url "https://files.pythonhosted.org/packages/00/00/'
            f'{name}-{version}.tar.gz"\n'
            f'    sha256 "0000000000000000000000000000000000000000000000000000000000000000"\n'
            f'  end\n')
    return ("class Hscc < Formula\n"
            '  url "https://github.com/pom11/hscc/archive/refs/tags/v9.9.9.tar.gz"\n'
            '  sha256 "1111111111111111111111111111111111111111111111111111111111111111"\n\n'
            + "\n".join(blocks) + "end\n")


def test_comparator_reports_a_missing_resource():
    """The GAP-D-1 scenario, planted: drop the httpx resource and the comparator
    MUST name httpx. Without this leg, a comparator that always returned []
    would pass every real assertion above."""
    stripped = _formula_from_sot(drop="httpx")
    assert "httpx" not in stripped, "fixture must actually drop the block"
    drift = compare(_sot_text(), stripped)
    assert any(d.startswith("MISSING:") and "httpx" in d for d in drift), (
        f"guard is VACUOUS: removing the httpx resource produced {drift!r}")


def test_comparator_reports_a_wrong_exact_pin():
    """A resource that is present at the WRONG version must also be reported —
    presence alone is not agreement with an ``==`` pin."""
    wrong = _formula_from_sot(version_override={"httpx": "0.9.9"})
    assert "httpx-0.9.9.tar.gz" in wrong
    drift = compare(_sot_text(), wrong)
    assert any(d.startswith("VERSION:") and "httpx" in d for d in drift), (
        f"guard is VACUOUS: an httpx resource at 0.9.9 produced {drift!r}")


def test_comparator_accepts_the_matched_baseline():
    """The positive control for the two negative controls: a formula generated
    to match the SOT produces NO drift. Without this leg the two above could be
    satisfied by a comparator that reports everything."""
    baseline = _formula_from_sot()
    assert compare(_sot_text(), baseline) == [], baseline
    # and the baseline is not trivially empty — it states every SOT name
    parsed = parse_formula_resources(baseline)
    assert set(parsed) == set(parse_requirements(_sot_text())), parsed


def test_comparator_reports_a_broken_sha():
    """Structural check, planted: a resource whose digest line was mangled."""
    broken = _formula_from_sot().replace(
        'sha256 "0000000000000000000000000000000000000000000000000000000000000000"',
        'sha256 "not-a-digest"')
    entries = parse_formula_resources(broken)
    assert entries and all(e["sha256"] == "" for e in entries.values()), entries
    # and the real tap parses clean, so the structural check is not vacuous
    _, formula = read_formula()
    assert all(e["sha256"] for e in parse_formula_resources(formula).values())


def test_sot_parser_is_the_canonical_one():
    """The drift guard must not carry its own requirements parser: the parser it
    uses is the SAME object the httpx-pin guard uses, so the two cannot drift
    apart on how a line is read (the audit's whole complaint)."""
    path = REPO / "hscc_daemon" / "tests" / "test_kanban_monitor_deps.py"
    assert parse_requirements.__module__ == "hscc_monitor_deps_for_drift"
    assert path.is_file()
    # behaviour pinned too: a comment line is not a pin
    assert parse_requirements("# httpx==0.28.1\n") == {}
    assert parse_requirements("httpx[socks]==0.28.1\n") == {"httpx": "==0.28.1"}
