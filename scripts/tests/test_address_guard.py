"""Tests for scripts/address_guard.py — the shared detector (t_ec2c2f95).

The hook tests in ``hscc_daemon/tests/test_precommit_address_hook.py`` cover the
commit-time TRIGGER end to end (real ``git commit`` in a throwaway repo). These
cover the detector's own contract, which both triggers share:

  * pattern verdicts — real LAN, real tailnet, sanctioned fixtures, placeholders
  * the STAGED blob is what gets judged, never the working tree
  * renames / spaces / binary content do not derail the batched read
  * the CLI exit codes the hook depends on (0 clean, 1 leak, 2 cannot-run)
  * the failure text carries offending ``file:line`` AND both placeholders
"""

import subprocess
import io
import re
import shutil
import sys
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    "address_guard_under_test", REPO / "scripts" / "address_guard.py")
assert _spec is not None and _spec.loader is not None
address_guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(address_guard)

def _addr(*parts):
    """Assemble a forbidden-shaped fixture address at runtime.

    The repo's OWN gate scans every tracked file, including this test, so a
    real-shaped address may not appear here as a contiguous literal — exactly
    the rule this guard enforces on everyone else. The parts are harmless alone.
    """
    return "".join(parts)


REAL_LAN = _addr("192", ".168.88.244")            # the live LAN's NAS host
REAL_TAILNET = _addr("100.64", ".55.7")           # CGNAT, outside the fixture block
LAN_PLACEHOLDER = "10.0.0.244"
TAILNET_PLACEHOLDER = "100.64.0.1"


# ── pattern verdicts (shared by pytest gate and hook) ────────────────────────

# The delimiter classes the t_1b7b3166 probe cross-multiplied (26 x 26 = 676
# cells, both interpreters). Pinning the WHOLE cross product rather than a sample:
# the shipped boundary used to be `\b`, which turned an address touching any word
# character on either side into a silent miss, and a sampled matrix is exactly how
# that regression would survive review again. `""` means the absence of a
# character, so that row/column pins start- and end-of-line.
#
# `uni_cjk` / `uni_latin1` / `uni_cyrillic` / `uni_ordm` are here because `\w` is
# Unicode: the old boundary hid unspaced CJK prose (`服务器<addr>`), not just
# `LABEL_<addr>` identifier tokens. The address values stay assembled by `_addr`
# because the repo's own gate scans this file.
_DELIM_CLASSES = (
    ("letter_ascii", "z"),
    ("digit", "5"),
    ("underscore", "_"),
    ("uni_cjk", "\u4e2d"),
    ("uni_latin1", "\u00e9"),
    ("uni_cyrillic", "\u0438"),
    ("uni_ordm", "\u00aa"),
    ("space", " "),
    ("tab", "\t"),
    ("comma", ","),
    ("equals", "="),
    ("amp", "&"),
    ("colon", ":"),
    ("slash", "/"),
    ("dquote", '"'),
    ("squote", "'"),
    ("rbracket", "]"),
    ("dash", "-"),
    ("dot", "."),
    ("semi", ";"),
    ("at", "@"),
    ("nul", "\x00"),
    ("ctrl_0e", "\x0e"),
    ("ctrl_01", "\x01"),
    ("newline", "\n"),
    ("eol", ""),
)


def _matrix_cases():
    """(param, id) for every delimiter cell, expected verdict computed from the
    rule the fix implements: a match unless a DIGIT abuts either side.

    The expectation is written as the *rule*, not as a captured table, so this
    suite says what the boundary is for and the audit says what it measured.
    """
    cases = []
    for pre_name, pre in _DELIM_CLASSES:
        for post_name, post in _DELIM_CLASSES:
            text = pre + REAL_LAN + post
            digit_abuts = pre.isdigit() or post.isdigit()
            cases.append(
                pytest.param(text, None if digit_abuts else REAL_LAN,
                             id=f"{pre_name}_{post_name}"))
    return cases


_VERDICT_CASES = [
    (f"mount {REAL_LAN}:/models", REAL_LAN),                       # live LAN
    (f"subnet {REAL_LAN}/24", REAL_LAN),                            # export list
    (f"ping {_addr('192', '.168.88.1')}", _addr("192", ".168.88.1")),   # LAN gateway
    (f"api https://{REAL_TAILNET}:8787", REAL_TAILNET),             # CGNAT, not fixture
    (f"host {_addr('100', '.65.1.1')}", _addr("100", ".65.1.1")),   # CGNAT above 100.64
    (f"host {_addr('100.127', '.9.9')}", _addr("100.127", ".9.9")),  # top of CGNAT
    (f"use {LAN_PLACEHOLDER}", None),                                # LAN placeholder
    (f"use {TAILNET_PLACEHOLDER}", None),                            # tailnet placeholder
    ("fixture 100.64.0.3 and 100.64.0.254", None),                   # sanctioned block
    ("172.16.4.5 and 8.8.8.8", None),                                # out of scope
    ("10.0.0.244/24 export", None),                                  # placeholder subnet
    ("192.168.89.244 is a different subnet", None),                  # not the live LAN
    # ── whole-line shapes t_3e6d3db7 §1b claimed as risky; all non-word on both
    # sides, so these matched BEFORE the fix too and must keep matching.
    ('{"host":"' + REAL_LAN + '","x":1}', REAL_LAN),                # minified JSON
    ("nas," + REAL_LAN + ",6379", REAL_LAN),                         # CSV column
    ("key=" + REAL_LAN + "&x=1", REAL_LAN),                          # query string
    ("https://" + REAL_LAN + ":8787/v1", REAL_LAN),                  # URL
    # ── the class the `\b` boundary actually hid (t_1b7b3166) ──
    ("NAS_" + REAL_LAN, REAL_LAN),                                   # label token
    ("host" + REAL_LAN, REAL_LAN),                                   # concatenated label
    (REAL_LAN + "backup", REAL_LAN),                                 # trailing label
    ("\u670d\u52a1\u5668" + REAL_LAN, REAL_LAN),                     # unspaced CJK prose
    (REAL_LAN + "\u7684\u914d\u7f6e", REAL_LAN),                     # trailing CJK prose
    ("h\u00f6st" + REAL_LAN, REAL_LAN),                              # accented label
    ("\x0e" + REAL_LAN + "N", REAL_LAN),                             # marshal framing
    # ── truncation guard: a longer digit RUN is not a dotted quad ──
    ("1" + REAL_LAN, None),                                          # leading digit run
    (REAL_LAN + "1", None),                                          # trailing digit run
    (_addr("192", ".168.88.2444"), None),                            # 4-digit last octet
]


@pytest.mark.parametrize("text,expect_hit", _VERDICT_CASES + _matrix_cases())
def test_find_in_text_verdicts(text, expect_hit):
    hits = address_guard.find_in_text(text)
    if expect_hit is None:
        assert hits == [], f"false positive on {text!r}"
    else:
        assert hits and hits[0][1] == expect_hit, f"missed {expect_hit!r} in {text!r}"


def test_the_new_boundary_is_a_strict_superset_of_the_old_word_boundary():
    """Detection can only have widened, never narrowed.

    The one property that makes a boundary change safe to review: every string the
    OLD pattern matched must still be matched. The old form is rebuilt FROM THE
    SHIPPED PATTERN at runtime (swap the two digit-exclusions back for `\\b`) —
    never written out as a second literal, which would break
    `test_forbidden_pattern_is_defined_in_exactly_one_tracked_file` in
    hscc_daemon/tests and give the repo a competing implementation.
    """
    shipped = address_guard.FORBIDDEN.pattern
    assert shipped.startswith("(?<![0-9])(?:") and shipped.endswith(")(?![0-9])"), (
        "the boundary changed shape again; this superset proof no longer describes it")
    old = re.compile(shipped.replace("(?<![0-9])", "\\b", 1).replace("(?![0-9])", "\\b", 1))
    assert old.pattern != shipped

    probes = [pre + REAL_LAN + post
              for pre, _ in _DELIM_CLASSES for post, _ in _DELIM_CLASSES]
    probes += [
        "mount " + REAL_LAN + ":/models", "subnet " + REAL_LAN + "/24",
        "nas," + REAL_LAN + ",6379", "key=" + REAL_LAN + "&x=1",
        '{"h":"' + REAL_LAN + '"}', REAL_LAN,
        "host " + REAL_TAILNET, "NAS_" + REAL_TAILNET,
    ]
    for probe in probes:
        if old.search(probe):
            assert address_guard.FORBIDDEN.search(probe), (
                f"a previously-detected occurrence is now missed: {probe!r}")
    # And it is a STRICT superset: at least the CJK-adjacency class is new.
    assert not old.search("\u670d\u52a1\u5668" + REAL_LAN), (
        "the old pattern already caught unspaced CJK prose, so this card's premise "
        "is stale — re-measure the delimiter matrix")
    assert address_guard.FORBIDDEN.search("\u670d\u52a1\u5668" + REAL_LAN)


def test_pattern_text_is_bytes_transportable_and_verdict_identical():
    """The guard applies the pattern to `str`; the redactor ships its `.pattern`
    as bytes over a child-process protocol and re-compiles it there. Those two
    forms must not be able to disagree, and they would if the boundary used `\\d`
    (Unicode-aware in str, ASCII-only in bytes). Pinned over the whole delimiter
    corpus and the fixture block, not just one example.
    """
    shipped = address_guard.FORBIDDEN
    twin = re.compile(shipped.pattern.encode("utf-8"))
    corpus = [pre + REAL_LAN + post
              for pre, _ in _DELIM_CLASSES for post, _ in _DELIM_CLASSES]
    corpus += [pre + "100.64.0.1" + post
               for pre, _ in _DELIM_CLASSES for post, _ in _DELIM_CLASSES]
    corpus += ["host " + REAL_TAILNET, "1" + REAL_LAN, REAL_LAN + "1"]
    for probe in corpus:
        assert bool(shipped.search(probe)) == bool(twin.search(probe.encode())), \
            f"str and bytes forms disagree on a delimiter context: {probe!r}"
    # The behaviour that `[0-9]` buys over `\\d`: `\\d` is Unicode-aware in str mode,
    # so a `\\d` boundary would let a non-ASCII digit abut the address and silently
    # suppress the match — in str mode only, which is exactly the str/bytes split this
    # guard and the redactor straddle. Arabic-Indic five beside the address must
    # therefore still be a hit.
    assert shipped.search("\u0665" + REAL_LAN), (
        "the boundary is Unicode-aware: a non-ASCII digit neighbour now hides an "
        "address in str mode while bytes mode would still catch it")


def test_the_sanctioned_forms_stay_accepted_in_every_context():
    """Placeholders and the fixture block stay allowed — including when they abut
    a word character, which is precisely the context the new boundary opens up.

    Widening the boundary is only safe if the sanctioned spellings do not get
    swept up with it: `10.0.0.x` and `100.64.0.1` are the documented convention
    this repo tells people to write, so a boundary that flags `NAS_100.64.0.1`
    would be reverted by whoever hits it first.
    """
    sanctioned = [
        "10.0.0" + ".x", "10.0.0" + ".x/24 export", "host10.0.0" + ".x",
        "100.64.0.1", "api https://100.64.0.1:8787", "NAS_100.64.0.1",
        "node100.64.0.1x", "100.64.0.1backup", "100.64.0.3 and 100.64.0.254",
        "\u670d\u52a1\u5668100.64.0.1", "5100.64.0.19",
    ]
    for text in sanctioned:
        assert address_guard.find_in_text(text) == [], f"false positive: {text!r}"
    # every class on both sides, around the fixture host itself
    for pre, _ in _DELIM_CLASSES:
        for post, _ in _DELIM_CLASSES:
            assert address_guard.find_in_text(pre + "100.64.0.1" + post) == []
    # ...and around a real-shaped one, where the same sweep MUST now fire. Built
    # from REAL_TAILNET, not spelled out: the widened boundary flags exactly this
    # `LABEL_<addr>` shape, so a literal here would be caught by the gate this file
    # is testing — which is the proof the fix works, and why fixtures stay assembled.
    assert address_guard.find_in_text("NAS_" + REAL_TAILNET) == [(1, REAL_TAILNET)], \
        "a real CGNAT host with a label prefix must be caught"
    assert address_guard.find_in_text(REAL_TAILNET + "\u7684\u914d\u7f6e"), \
        "trailing CJK prose must be caught on a tailnet address too"


def test_reports_the_line_number_of_every_hit():
    text = "line one\nnothing here\nhost " + REAL_LAN + "\nand " + REAL_TAILNET + "\n"
    assert address_guard.find_in_text(text) == [(3, REAL_LAN), (4, REAL_TAILNET)]


def test_scan_blob_carries_file_line_and_address():
    got = address_guard.scan_blob("docs/audit.md", f"# t\nhost {REAL_LAN}\n".encode())
    assert got == [f"docs/audit.md:2: {REAL_LAN}"]


def test_scan_blob_skips_binaries_and_skip_suffixes():
    """A blob the policy does NOT cover stays silent (t_3e6d3db7 narrowed this).

    The old guard skipped *every* NUL-bearing blob, which is how a committed
    `.pyc` escaped all three gates. Only two classes are silent now: a declared
    asset (SKIP_SUFFIXES) and an unknown-extension binary. A build artefact is
    refused instead — see test_scan_blob_refuses_a_pyc_carrying_a_real_address.
    """
    dirty = b"\x00binary " + REAL_LAN.encode() + b"\x00"
    assert address_guard.scan_blob("x.bin", dirty) == []          # unknown extension
    assert address_guard.scan_blob("logo.png", REAL_LAN.encode()) == []


def test_skip_suffixes_unchanged_from_the_original_gate():
    """The gate's original noise list must not silently shrink."""
    assert address_guard.SKIP_SUFFIXES == (
        ".png", ".jpg", ".jpeg", ".pdf", ".ico", ".zip", ".gz", ".xcuserstate")


# ── build artefacts: refused on the name, not the content (t_3e6d3db7) ───────

def _pyc_like(addr):
    """A `.pyc`-shaped blob: NUL-framed, with the address in a string constant.

    What a real one looks like on the matters-here axis: the compiled constants
    section holds the source's string literals verbatim, so the address is in the
    blob in exactly the form the text scan cannot see. NUL bytes either side so
    the OLD guard would have returned [] and stayed silent about it.

    This is the WEAK mutant. The strong one — `test_a_genuine_compiled_pyc_...`,
    which compiles real source at runtime — is what proves the refusal is not
    merely a content-scan in disguise.
    """
    return b"\xe3\r\r\n\x00\x00\x00\x00" + b"\x00" + addr.encode() + b"\x00\x00"


@pytest.fixture()
def genuine_leaky_pyc(tmp_path):
    """A REAL `.pyc`, compiled by this interpreter, from source that carried an
    address — assembled at runtime so the repo's own guard stays clean.

    Why this fixture exists rather than a hand-built blob: a mutant made by
    splicing the dotted string into NUL filler is MATCHED by the current pattern
    (both boundaries land on non-word bytes) and so proves nothing. A genuinely
    compiled `.pyc` is not: marshal writes the string constant with no delimiter
    before the next interned name, so the character immediately AFTER the address
    is a word character, the trailing `\\b` fails, and FORBIDDEN finds 0 while a
    naive `192\\.168\\.88\\.\\d{1,3}` still finds 1. Measured, not assumed: leading
    char in the compiled blob is U+000E (non-word), trailing is 'N' (word), and
    relaxing the trailing boundary is what restores the match.
    """
    src = tmp_path / "leaky_source.py"
    src.write_text("NAS = " + repr(REAL_LAN) + "\n", encoding="utf-8")
    import py_compile
    py_compile.compile(str(src), cfile=str(tmp_path / "leaky.pyc"), doraise=True)
    blob = (tmp_path / "leaky.pyc").read_bytes()
    assert b"\0" in blob, "a real .pyc is NUL-bearing — that is the whole premise"
    return blob


def test_a_genuine_compiled_pyc_is_now_visible_to_the_pattern(genuine_leaky_pyc):
    """RE-PINNED by t_1b7b3166. This test used to assert the OPPOSITE — read that
    before "fixing" it.

    t_3e6d3db7 wrote this as a hair trigger: `FORBIDDEN` had `\\b` on both sides, so
    marshal's undelimited framing (a word character immediately AFTER the constant)
    made a genuinely compiled `.pyc` invisible to the shipping pattern while a naive
    scan still saw it. That blindness was the whole stated basis for "refuse the
    type, never scan the bytes".

    t_1b7b3166 measured the delimiter cross-product (26x26, both interpreters) and
    found that `\\b` was a *delimiter assumption*, not a safety property, and that it
    also hid unspaced CJK prose and any `LABEL_<addr>` token in ordinary text. The
    boundary is now digit-exclusion, which stops a truncated digit run and nothing
    else. Measured consequence, re-pinned here: the same compiled blob is now seen
    by `FORBIDDEN` in text, bytes and latin-1 form.

    So the refusal is NO LONGER justified by "the scanner cannot read this blob". It
    is justified by the two things that remain true, both pinned next door:
    `test_a_deflated_container_member_is_unreachable_at_any_boundary` (a compressed
    member does not contain the bytes, so no pattern can ever reach them) and
    `test_the_nul_gate_not_the_pattern_is_now_the_blind_layer` (an unlisted
    extension is skipped before any decode). If a future edit makes the FIRST
    assertion below go red — the pattern stops seeing a compiled blob — that is not
    a regression to revert: it means the boundary went back to assuming prose-style
    delimiters, and the t_3e6d3db7 argument needs re-measuring before it is
    re-adopted. See docs/audits/address-guard-boundary-t_1b7b3166.md.
    """
    blob = genuine_leaky_pyc
    assert REAL_LAN.encode() in blob, "the address is in the blob"
    text = blob.decode("utf-8", "ignore")
    # Pattern assembled at runtime for the same reason the addresses are: the repo's
    # own single-implementation pin scans this file and must not find a second copy
    # of the detector's literal.
    naive = re.compile(_addr("192", r"\.168\.88\.\d{1,3}"))
    assert naive.findall(text), "naive scan should see it"
    # (1) the shipping detector now sees it too, in every decode form the guard or
    # the redactor could apply. `bytes` mode matters: the redactor ships
    # `.pattern` over a child-process protocol, so str and bytes must agree.
    assert [a for _, a in address_guard.find_in_text(text)] == [REAL_LAN], (
        "FORBIDDEN no longer matches a compiled blob — the boundary has regressed to "
        "assuming prose-style delimiters, which is the t_1b7b3166 defect. Re-measure "
        "the delimiter matrix before re-adopting any 'the scanner is blind here' claim")
    # The pattern TEXT must be bytes-compilable and agree with text mode. That is the
    # documented reason the boundary spells `[0-9]` instead of `\d`: `\d` is
    # Unicode-aware in str mode and ASCII-only in bytes mode, so a `\d` boundary
    # would make the verdict depend on which the caller happens to hold. The redactor
    # ships `.pattern` as bytes over a child-process protocol and re-compiles it on
    # the far side, so the two forms must not be able to disagree.
    as_bytes = re.compile(address_guard.FORBIDDEN.pattern.encode("utf-8"))
    assert as_bytes.search(blob), "bytes form must agree with the text form"
    assert address_guard.FORBIDDEN.search(blob.decode("latin-1")), \
        "the lossless decode must not hide it either"
    # (2) the refusal still fires, and fires on the NAME before any of this is read
    got = address_guard.scan_blob("hscc-provision/__pycache__/hscc.cpython-313.pyc", blob)
    assert got == ["hscc-provision/__pycache__/hscc.cpython-313.pyc:0: "
                   "build artefact must not be tracked"]
    # (3) ...and an unlisted extension is STILL silent on bytes the pattern can read,
    # which relocates the honest limit of this card from the pattern to the NUL gate.
    assert address_guard.scan_blob("vendor/blob.bin", blob) == []


def test_a_deflated_container_member_is_unreachable_at_any_boundary(tmp_path):
    """The blindness that is REAL and irreducible — and no boundary can fix it.

    t_3e6d3db7 declined extract-and-scan partly for cost. This adds the harder
    reason, measured, and it is the reason the name-based refusal stays load-bearing
    after t_1b7b3166 widened the pattern: a `.whl`/`.zip`/`.jar` member written with
    DEFLATE does not CONTAIN the address bytes at all. Before that fix the compiled
    blob was the one class where a content scan provably lost to a name refusal; now
    the compressed container is, and unlike the boundary this cannot be repaired by
    any regex, because there are no bytes to match.

    The stored (uncompressed) sibling is included as the control that makes the
    deflated case mean something: same content, same size class, different container
    format, and readability flips. Readability is a property of the format, not of
    the pattern — so the pattern is the wrong layer to fix this at.
    """
    import zipfile

    def container(compression):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression) as zf:
            zf.writestr("pkg/config.py", "NAS = " + repr(REAL_LAN) + "\n")
            zf.writestr("pkg/pad.txt", "nothing here\n" * 120)
        return buf.getvalue()

    deflated = container(zipfile.ZIP_DEFLATED)
    stored = container(zipfile.ZIP_STORED)
    naive = re.compile(_addr("192", r"\.168\.88\.\d{1,3}"))

    # The deflated member genuinely does not carry the bytes — this is the premise.
    assert REAL_LAN.encode() not in deflated, (
        "the fixture stopped being compressible, so the premise of this case is gone")
    assert not naive.findall(deflated.decode("utf-8", "ignore")), \
        "a naive scan cannot see it either, which is the point"
    assert not address_guard.find_in_text(deflated.decode("utf-8", "ignore")), \
        "the pattern must not see what is not there"
    # The stored control DOES carry them, and the pattern sees them.
    assert REAL_LAN.encode() in stored
    assert [a for _, a in address_guard.find_in_text(stored.decode("utf-8", "ignore"))] \
        == [REAL_LAN]
    # Either way the guard refuses, on the NAME, without needing to read a byte.
    for rel in ("dist/pkg-1.0-py3-none-any.whl", "dist/stored-1.0-py3-none-any.whl"):
        assert address_guard.scan_blob(rel, deflated) == [
            f"{rel}:0: build artefact must not be tracked"]


def test_the_nul_gate_not_the_pattern_is_now_the_blind_layer():
    """Where a leak can still ride through unread, stated honestly.

    Before t_1b7b3166 this input was blind twice over — the pattern could not match
    it AND `scan_blob` never decoded it — which is why the old prose blamed the
    pattern for a gap the gate was responsible for. With the digit boundary the
    pattern CAN match these bytes; the silence is now entirely the gate's.

    And note what the gate keys on: the CONTENT (a NUL byte), not the extension.
    Renaming the blob to `.txt` does not make it readable to the guard, so the
    honest limit of the policy is "NUL-bearing and not on the hatch", which is
    narrower than the old `SKIP_SUFFIXES` prose implies and worth pinning before
    someone 'fixes' the gate by widening the suffix list.
    """
    leaky = b"\x00\x00host " + REAL_LAN.encode() + b"\x00\x00"
    # the pattern, applied to the same bytes decoded, has no trouble at all
    assert [a for _, a in address_guard.find_in_text(
        leaky.decode("utf-8", "ignore"))] == [REAL_LAN]
    # the gate declines to look, whatever the extension is
    assert address_guard.scan_blob("vendor/blob.bin", leaky) == []
    assert address_guard.scan_blob("docs/notes.txt", leaky) == [], (
        "the NUL gate keys on the extension, not the content — the policy is then "
        "wider than documented and a renamed binary becomes unreadable-by-accident")
    # strip the NULs and the very same address is reported: the gate, not the pattern
    assert address_guard.scan_blob("docs/notes.txt", b"host " + REAL_LAN.encode()) == [
        f"docs/notes.txt:1: {REAL_LAN}"]


def test_scan_blob_refuses_a_pyc_carrying_a_real_address():
    """THE regression this card exists for.

    Before the policy, `b"\\0" in data` returned [] and the address rode through
    the hook, the pytest gate and the CI backstop — all three call this function.
    """
    got = address_guard.scan_blob("scripts/__pycache__/hscc.cpython-313.pyc",
                                  _pyc_like(REAL_LAN))
    assert got, "a committed .pyc must not be invisible to the guard"
    assert got[0].startswith("scripts/__pycache__/hscc.cpython-313.pyc:0:")


@pytest.mark.parametrize("rel", [
    "pkg/mod.pyc",                    # the case that actually happened here
    "pkg/__pycache__/mod.cpython-313.pyc",
    "vendor/thing.pyo",
    "native/helper.so",
    "native/helper.dylib",
    "native/helper.dll",
    "build/obj.o",
    "build/lib.a",
    "java/Thing.class",
    "dist/pkg-1.0-py3-none-any.whl",
])
def test_scan_blob_refuses_build_artefacts_even_when_clean(rel):
    """Content-independent: the offence is that the path is tracked at all.

    The guard cannot tell a clean .pyc from a leaking one without the
    extract-and-scan machinery the policy declines to build, so a rule enforced
    only when a leak is provable is a rule that leaks.
    """
    assert address_guard.scan_blob(rel, b"harmless text") == [
        f"{rel}:0: build artefact must not be tracked"]
    assert address_guard.scan_blob(rel, _pyc_like(REAL_TAILNET))


def test_scan_blob_refuses_an_artefact_regardless_of_case_and_separators():
    assert address_guard.is_build_artefact("PKG/MOD.PYC")
    assert address_guard.is_build_artefact("pkg\\__pycache__\\mod.pyc")
    assert address_guard.is_build_artefact("pkg/__pycache__/x.PYC")
    # and the negative controls that make the list mean something
    assert not address_guard.is_build_artefact("docs/pycall.md")
    assert not address_guard.is_build_artefact("data/alpine.software.json")
    assert not address_guard.is_build_artefact("native/source.c")
    assert not address_guard.is_build_artefact("assets/logo.png")


def test_legitimate_binary_asset_is_still_accepted():
    """The tracked binary population must survive the new rule untouched.

    Measured at 3c20990c the tracked tree has exactly two NUL-bearing files, both
    .png, and this is the guard against a policy that "fixes" the .pyc gap by
    breaking a real asset.
    """
    assert not address_guard.is_build_artefact("assets/hscc.png")
    assert address_guard.scan_blob("assets/hscc.png", b"\x89PNG\r\n\x1a\n\x00\x00") == []
    assert address_guard.scan_blob("vendor/blob.dat", b"\x00\x01\x02") == []


def test_a_compiled_artefact_suffix_is_never_waivable_by_the_hatch(monkeypatch):
    """The hatch cannot reach the tier whose readability the reviewer cannot see.

    Round-1 review (t_3e6d3db7) found the claim "widening the hatch can never hide
    a leak" was FALSE while the hatch could waive a `.pyc`, and justified the fix by
    saying the text scan is blind to a compiled blob. t_1b7b3166 re-measured that
    after replacing the `\\b` boundary with digit-exclusion: a genuine compiled blob
    is now READABLE (see
    `test_a_genuine_compiled_pyc_is_now_visible_to_the_pattern`). The rule is
    unchanged and still load-bearing, on the blindness that survives any boundary:
    a DEFLATE-compressed container member carries no scan bytes at all, and whether a
    given blob is readable depends on the container format and on whichever
    interpreter emitted it -- neither visible to whoever edits this list. The fix
    stays structural, not wording: the suffix tier in `is_build_artefact` ignores the
    hatch, so every compiled shape below stays refused from the hatch alone.
    """
    monkeypatch.setattr(address_guard, "ALLOWED_BINARY_PATHS", frozenset(
        ["native/bundled.so", "pkg/leak.pyc", "pkg/leak.pyo", "native/x.pyd",
         "build/obj.o", "build/lib.a", "native/x.dylib", "native/x.dll",
         "java/Thing.class", "dist/pkg-1.0-py3-none-any.whl"]))
    for rel in address_guard.ALLOWED_BINARY_PATHS:
        assert address_guard.scan_blob(rel, b"\x00clean\x00") == [
            f"{rel}:0: build artefact must not be tracked"], (
            f"{rel} is on the hatch and got through: a compiled artefact the text"
            " scan cannot read is now tracked and unscanned")


def test_a_genuinely_compiled_pyc_on_the_hatch_is_still_refused(
        monkeypatch, genuine_leaky_pyc):
    """The exact probe the round-1 review ran, inverted into a requirement.

    Before the two-tier hatch, `scan_blob` returned `[]` here — silent — because the
    hatch waived the refusal. It must still name the path.

    The *reason* it must has changed, and this test is where that change is recorded.
    t_3e6d3db7 pinned the first assertion below as `== []` ("the case is worthless
    unless the text scan is still blind"), because with `\\b` on both sides marshal's
    word-char neighbour broke the trailing boundary. t_1b7b3166 replaced that boundary
    with digit-exclusion and re-measured the blob: it is now visible. So this case is
    no longer justified by scanner blindness — it is justified because the hatch waives
    the *name* refusal, and a hatch that can waive the name refusal of an artefact the
    guard cannot reliably read (see the deflated-container and NUL-gate pins) is a
    hatch that can hide a leak. The first assertion is now an equality the other way,
    with a message that says which measurement to redo if it flips.
    """
    monkeypatch.setattr(address_guard, "ALLOWED_BINARY_PATHS", frozenset({"pkg/leak.pyc"}))
    assert [a for _, a in address_guard.find_in_text(
        genuine_leaky_pyc.decode("utf-8", errors="ignore"))] == [REAL_LAN], (
        "FORBIDDEN stopped seeing a compiled blob: the boundary went back to assuming "
        "prose-style delimiters. Re-run the delimiter cross-product in "
        "docs/audits/address-guard-boundary-t_1b7b3166.md before re-arguing the "
        "two-tier rule from 'the scanner cannot read this'")
    got = address_guard.scan_blob("pkg/leak.pyc", genuine_leaky_pyc)
    assert got == ["pkg/leak.pyc:0: build artefact must not be tracked"], (
        "the hatch let a compiled artefact through — it is now tracked and readable"
        " only by luck of the container format, which is not a control")


def test_hatch_waives_only_the_segment_refusal_and_still_scans(monkeypatch):
    """What the hatch *does* buy: a `__pycache__`-segment path is tracked, decoded
    and text-scanned even though it is full of NUL bytes and would be a skipped
    asset by extension. "This path may be tracked", never "this path may be unread".
    """
    monkeypatch.setattr(address_guard, "ALLOWED_BINARY_PATHS",
                        frozenset({"vendor/__pycache__/fixture.dat", "docs/reference.pdf"}))
    # clean -> tracked, no verdict
    assert address_guard.scan_blob("vendor/__pycache__/fixture.dat", b"\x00clean\x00") == []
    # leaking + NUL-bearing -> the address is still found (the scan applies to a
    # path the hatch has waived, NULs and all)
    leaky = b"\x00\x00host " + REAL_LAN.encode() + b"\x00\x00"
    assert address_guard.scan_blob("vendor/__pycache__/fixture.dat", leaky) == [
        f"vendor/__pycache__/fixture.dat:1: {REAL_LAN}"]
    # and the waiver is exactly as wide as the path on the hatch
    assert address_guard.scan_blob("vendor/__pycache__/other.dat", b"\x00clean\x00") == [
        "vendor/__pycache__/other.dat:0: build artefact must not be tracked"]


def test_hatch_does_not_bypass_the_skip_suffix_shortcut_silently(monkeypatch):
    """A hatch entry must not turn a declared asset into an unread, unscanned file.

    Same guarantee, the other hole the review's logic exposes: `SKIP_SUFFIXES` is a
    second way a path escapes the scan. An allowlisted path is decoded even when its
    extension is on that list, so a reviewer adding one entry can never — by
    accident or on purpose — get "tracked AND never read" for a path they listed.
    """
    monkeypatch.setattr(address_guard, "ALLOWED_BINARY_PATHS",
                        frozenset({"docs/reference.pdf"}))
    leaky = "remount " + REAL_LAN + ":/models\n"
    assert address_guard.scan_blob("docs/reference.pdf", leaky.encode()) == [
        f"docs/reference.pdf:1: {REAL_LAN}"]
    # without the hatch the same path is a declared asset and stays unread
    assert address_guard.scan_blob("docs/other.pdf", leaky.encode()) == []


def test_scan_paths_and_scan_blob_agree_on_a_hatched_path(tmp_path, monkeypatch):
    """`scan_paths` has its own pre-read skip shortcut; it must honour the hatch.

    The two gates re-implement the same silence rules in two places, which is
    exactly where the hook and the CI job drift apart. Pin the pair on the one
    input where they could disagree: an allowlisted path with a skipped extension.
    """
    monkeypatch.setattr(address_guard, "ALLOWED_BINARY_PATHS",
                        frozenset({"docs/reference.pdf"}))
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "reference.pdf").write_text(
        "remount " + REAL_LAN + ":/models\n", encoding="utf-8")
    expected = [f"docs/reference.pdf:1: {REAL_LAN}"]
    assert address_guard.scan_paths(tmp_path, ["docs/reference.pdf"]) == expected
    assert address_guard.scan_blob(
        "docs/reference.pdf",
        (tmp_path / "docs" / "reference.pdf").read_bytes()) == expected


def test_scan_paths_refuses_a_tracked_artefact_even_if_unreadable(tmp_path):
    """The tracked gate must not depend on reading the bytes.

    A path in HEAD's index is a tracked artefact whether or not the working tree
    carries it (deleted-but-tracked) or it cannot be opened. The name is verdict
    enough, so the pre-read skip shortcuts must not run for it.
    """
    rel = "hscc-provision/__pycache__/hscc.cpython-313.pyc"
    assert address_guard.scan_paths(tmp_path, [rel]) == [
        f"{rel}:0: build artefact must not be tracked"]
    # present-but-unreadable via the injected reader, same verdict
    def boom(_p):
        raise OSError("EIO")
    (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / rel).write_bytes(_pyc_like(REAL_LAN))
    assert address_guard.scan_paths(tmp_path, [rel], read=boom) == [
        f"{rel}:0: build artefact must not be tracked"]


def test_cli_blocks_a_staged_pyc_carrying_a_real_address(tmp_path):
    """The card's acceptance case, through the CLI the hook actually calls."""
    r = _repo(tmp_path)
    pyc = r / "scripts" / "__pycache__"
    pyc.mkdir(parents=True)
    (pyc / "hscc.cpython-313.pyc").write_bytes(_pyc_like(REAL_LAN))
    _git(r, "add", "-A")
    proc = subprocess.run([sys.executable, str(REPO / "scripts" / "address_guard.py"),
                           "--staged", "--repo", str(r)], capture_output=True)
    assert proc.returncode == 1
    err = proc.stderr.decode()
    assert "hscc.cpython-313.pyc" in err
    assert "git rm --cached" in err


def test_cli_tracked_flags_a_committed_pyc_even_after_a_scrubbed_worktree(tmp_path):
    """`--tracked` must name the artefact from the INDEX, not the working tree.

    Same failure mode as the 2026-08-30 audit, one class over: the file may be
    gone (or clean) on disk while HEAD still carries the blob.
    """
    r = _repo(tmp_path)
    pyc = r / "pkg" / "__pycache__"
    pyc.mkdir(parents=True)
    (pyc / "m.cpython-313.pyc").write_bytes(_pyc_like(REAL_TAILNET))
    _git(r, "add", "-A")
    assert _git(r, "commit", "-q", "-m", "oops", "--no-verify").returncode == 0
    # Working tree scrubbed: the bytes are gone, the tracked path is not.
    shutil.rmtree(pyc)
    proc = subprocess.run([sys.executable, str(REPO / "scripts" / "address_guard.py"),
                           "--tracked", "--repo", str(r)], capture_output=True)
    assert proc.returncode == 1, proc.stderr.decode()
    assert "pkg/__pycache__/m.cpython-313.pyc" in proc.stderr.decode()


def test_report_names_the_artefact_and_the_removal_command():
    text = address_guard.report(["pkg/__pycache__/m.pyc:0: build artefact must not be tracked"])
    assert "pkg/__pycache__/m.pyc" in text
    assert "git rm --cached" in text, "the fix for an artefact is untracking, not scrubbing"
    assert "PUBLIC" in text
    # Mixed input: both classes explained, and an address still gets placeholders.
    mixed = address_guard.report([f"a.md:3: {REAL_LAN}", "m.pyc:0: build artefact must not be tracked"])
    assert "a.md:3" in mixed and "10.0.0.x" in mixed and "100.64.0.1" in mixed
    assert "git rm --cached" in mixed


# ── staged content, not working-tree content ─────────────────────────────────

def _git(repo, *args, input_bytes=None):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e.invalid",
                           "-C", str(repo), *args], capture_output=True, input=input_bytes)


def _repo(tmp_path):
    r = tmp_path / "guard"
    r.mkdir()
    assert _git(r, "init", "-q", "-b", "main").returncode == 0
    (r / "a.md").write_text("clean\n", encoding="utf-8")
    _git(r, "add", "-A")
    assert _git(r, "commit", "-q", "-m", "seed").returncode == 0
    return r


def test_staged_paths_excludes_deletions_and_includes_new_files(tmp_path):
    r = _repo(tmp_path)
    (r / "new.md").write_text("new\n", encoding="utf-8")
    (r / "a.md").unlink()
    _git(r, "add", "-A")
    assert address_guard.staged_paths(r) == ["new.md"]


def test_staged_blobs_reads_the_index_not_the_worktree(tmp_path):
    r = _repo(tmp_path)
    (r / "a.md").write_text(f"host {REAL_LAN}\n", encoding="utf-8")
    _git(r, "add", "a.md")
    (r / "a.md").write_text("scrubbed 10.0.0.x\n", encoding="utf-8")   # operator scrubs
    assert address_guard.staged_blobs(r, ["a.md"])["a.md"].decode() == f"host {REAL_LAN}\n"
    assert address_guard.scan_staged(r) == [f"a.md:1: {REAL_LAN}"]


def test_staged_blobs_survives_awkward_path_names(tmp_path):
    """Spaces, unicode, and a NEWLINE in the name (git allows it).

    A newline-in-path file is the only known way to make a line-framed
    `cat-file --batch` stream skip a file; it goes through the per-path
    `cat-file blob` path instead and must still be caught.
    """
    r = _repo(tmp_path)
    (r / "docs").mkdir()
    spaced = "docs/a b.md"
    uni = "docs/наас.md"
    newline_name = "docs/we\nird.md"
    for name in (spaced, uni, newline_name):
        (r / name).write_text(f"host {REAL_TAILNET}\n", encoding="utf-8")
    _git(r, "add", "-A")
    got = sorted(address_guard.scan_staged(r))
    assert got == sorted([f"{spaced}:1: {REAL_TAILNET}",
                          f"{uni}:1: {REAL_TAILNET}",
                          f"{newline_name}:1: {REAL_TAILNET}"]), got


def test_scan_staged_is_empty_on_a_clean_tree(tmp_path):
    r = _repo(tmp_path)
    assert address_guard.staged_paths(r) == []
    assert address_guard.scan_staged(r) == []


def test_scan_staged_on_a_repo_without_head(tmp_path):
    """First commit (no HEAD yet): the whole index is the staged tree."""
    r = tmp_path / "fresh"
    r.mkdir()
    assert _git(r, "init", "-q", "-b", "main").returncode == 0
    (r / "leak.md").write_text(f"host {REAL_LAN}\n", encoding="utf-8")
    _git(r, "add", "-A")
    assert address_guard.scan_staged(r) == ["leak.md:1: " + REAL_LAN]


def test_scan_tracked_matches_the_pytest_gate_population(tmp_path):
    r = _repo(tmp_path)
    (r / "sub").mkdir()
    (r / "sub" / "b.md").write_text(f"{REAL_TAILNET}\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "second")
    (r / "untracked.md").write_text("not tracked\n", encoding="utf-8")
    offenders = address_guard.scan_tracked(r)
    assert offenders == ["sub/b.md:1: " + REAL_TAILNET]
    assert "untracked.md" not in "\n".join(address_guard.tracked_paths(r))


# ── report text + CLI ────────────────────────────────────────────────────────

def test_report_names_offenders_and_placeholders():
    text = address_guard.report([f"a.md:7: {REAL_LAN}"])
    assert "a.md:7" in text
    assert "10.0.0.x" in text and "100.64.0.1" in text
    assert "PUBLIC" in text


def test_cli_exit_codes(tmp_path):
    r = _repo(tmp_path)

    def run(args, repo=r):
        return subprocess.run([sys.executable, str(REPO / "scripts" / "address_guard.py"),
                               *args, "--repo", str(repo)], capture_output=True)

    assert run(["--staged"]).returncode == 0                       # clean
    (r / "leak.md").write_text(f"host {REAL_LAN}\n", encoding="utf-8")
    _git(r, "add", "leak.md")
    proc = run(["--staged"])
    assert proc.returncode == 1                                    # leak
    assert f"leak.md:1: {REAL_LAN}" in proc.stderr.decode()
    (r / "leak.md").unlink()
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "x", "--no-verify")
    assert run(["--tracked"]).returncode == 0                       # clean tracked
    assert run(["--staged"], repo=tmp_path).returncode == 2         # not a repo -> cannot run


def test_cli_failure_text_is_actionable(tmp_path):
    r = _repo(tmp_path)
    (r / "notes.txt").write_text(f"subnet {REAL_LAN}/24\n", encoding="utf-8")
    _git(r, "add", "notes.txt")
    proc = subprocess.run([sys.executable, str(REPO / "scripts" / "address_guard.py"),
                           "--staged", "--repo", str(r)], capture_output=True)
    err = proc.stderr.decode()
    assert "notes.txt:1" in err and "10.0.0.x" in err and "100.64.0.1" in err


# keep pytest import light: imported late so the module-under-test load order is explicit
import pytest  # noqa: E402
