"""The errno-65 /usr/bin/curl fallback, and the mirror rule that keeps it honest.

WHY THIS FILE EXISTS (t_391b084e)
---------------------------------
macOS binds Privacy > Local Network to the *signing identity* of the executable,
so an adhoc-signed interpreter — a Homebrew keg's ``Python.app``, a python.org
Framework build — is denied on EVERY private-range connect no matter how often
the Python entries are re-toggled. ``urllib`` then reports
``OSError(65, 'No route to host')`` for a host that is up and answering, while
Apple-signed ``/usr/bin/curl`` (which keeps its own TCC grant) reaches the same
URL. The symptom this pins: ``hscc cluster monitor --live`` rendering every
backend ``down`` on a healthy fleet.

THREE PLUGINS CARRY THE SAME BLOCK VERBATIM:

    hscc_daemon/cluster_live.py            _http_get          (monitor --live)
    hscc-bootstrap/doctor.py               _http_get_default  (config probes)
    hscc-cluster/cluster_template.py       _http_get_default  (template probes)

It is deliberately NOT one shared import: each HSCC plugin deploys standalone
into ``~/.hermes/plugins`` and the hyphenated dirs are not importable package
names, so none of the three can import another (see
docs/DESIGN-probe-before-write.md, 'One probe contract for the whole fleet').
The price of copying is drift, so the copy is PINED:
``test_mirrored_block_is_identical`` extracts the delimited block from all three
source files and fails if any byte differs. Edit all three in one commit.

Nothing here touches the network or execs a real curl: ``urllib.request.urlopen``
and the module's ``_lan_curl_run`` seam are both stubbed.
"""

import ast
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[1]
for _p in (str(_REPO_ROOT), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import cluster_template as ct  # noqa: E402  (this plugin's own copy)

BEGIN = "# ── BEGIN mirrored block: lan_curl_fallback"
END = "# ── END mirrored block: lan_curl_fallback"

#: The three copies, by (label, path). Loaded as TEXT for the mirror test; the
#: hscc-bootstrap copy is never imported here — see _load_by_path below.
MIRROR_FILES = {
    "hscc_daemon/cluster_live.py": _REPO_ROOT / "hscc_daemon" / "cluster_live.py",
    "hscc-bootstrap/doctor.py": _REPO_ROOT / "hscc-bootstrap" / "doctor.py",
    "hscc-cluster/cluster_template.py": _HERE.parents[0] / "cluster_template.py",
}


def _block_of(path: Path) -> str:
    """The mirrored block, delimiters included, exactly as the file holds it."""
    text = path.read_text()
    assert BEGIN in text, f"{path.name}: mirrored block is MISSING"
    assert END in text, f"{path.name}: mirrored block is unterminated"
    start = text.index(BEGIN)
    end = text.index(END, start)
    return text[start:text.index("\n", end) + 1]


# ── the mirror rule ────────────────────────────────────────────────────────

def test_mirrored_block_is_identical_across_the_three_plugins():
    """The one-copy-per-plugin rule, enforced: any byte of drift fails here.

    Three plugins, one block, no cross-plugin import (hyphenated plugin dirs are
    not importable). A drifted copy is a real bug class — the denied-host
    workaround would silently apply on one surface and not another, which is
    precisely the 'monitor says down, doctor says fine' shape nobody can debug.
    """
    blocks = {label: _block_of(path) for label, path in MIRROR_FILES.items()}
    reference = blocks["hscc_daemon/cluster_live.py"]
    for label, block in blocks.items():
        assert block == reference, (
            f"{label} has drifted from hscc_daemon/cluster_live.py — the "
            "errno-65 fallback block must be copied verbatim into all three "
            "plugins in the same commit")
    # sanity: the pins that make the block what it is
    for needle in ("errno", "65", "_CURL_BIN = \"/usr/bin/curl\"",
                   "LAN via curl fallback (Local Network permission)",
                   "def _lan_curl_fallback("):
        assert needle in reference, f"block lost its {needle!r} anchor"


def test_probe_mirrors_stay_compatible_with_doctor():
    """doctor's and hscc-cluster's ``_http_get_default`` bodies are byte-equal
    apart from the def line — pinning the 'keep the two byte-compatible' line in
    docs/DESIGN-probe-before-write.md for the fallback era: Bearer auth, the 10s
    timeout, and the errno-65 retry call must not drift apart."""
    def body(path):
        tree = ast.parse(path.read_text())
        fn = next(n for n in tree.body
                  if isinstance(n, ast.FunctionDef) and n.name == "_http_get_default")
        seg = ast.get_source_segment(path.read_text(), fn) or ""
        doc = ast.get_docstring(fn, clean=False) or ""
        doc_lines = {l.strip() for l in doc.splitlines()}
        lines = seg.splitlines()[1:]          # def line: typing syntax differs
        return "\n".join(l for l in lines
                         if l.strip() and l.strip() not in doc_lines)

    mine = body(MIRROR_FILES["hscc-cluster/cluster_template.py"])
    theirs = body(MIRROR_FILES["hscc-bootstrap/doctor.py"])
    assert mine == theirs, (
        "the two _http_get_default mirrors drifted (docstrings aside):\n"
        f"--- hscc-bootstrap/doctor.py\n{theirs}\n"
        f"--- hscc-cluster/cluster_template.py\n{mine}")
    for needle in ('headers["Authorization"] = f"Bearer {api_key}"',
                   "urllib.request.urlopen(req, timeout=10)",
                   "_lan_curl_fallback(url, 10, e, api_key=api_key)"):
        assert needle in mine, f"hscc-cluster's probe lost {needle!r}"
        assert needle in theirs, f"doctor's probe lost {needle!r}"


# ── the fallback itself, on THIS plugin's copy ─────────────────────────────

class _CurlStub:
    """Records every argv the fallback hands the curl seam; returns canned rc."""

    def __init__(self, rc=0, out=b'{"data": []}', err=b""):
        self.rc, self.out, self.err = rc, out, err
        self.argvs = []

    def __call__(self, argv, timeout):
        self.argvs.append(list(argv))
        return self.rc, self.out, self.err


def _errno65(host="10.0.0.1"):
    return urllib.error.URLError(OSError(65, "No route to host", host))


def _urlopen_raises(exc):
    def _fake(*a, **kw):
        raise exc
    return _fake


@pytest.fixture
def curl_seam(monkeypatch):
    """Stub the curl seam on THIS module's copy and refill the notice ticket."""
    def _install(rc=0, out=b'{"data": []}', err=b""):
        stub = _CurlStub(rc, out, err)
        monkeypatch.setattr(ct, "_lan_curl_run", stub)
        ct._lan_curl_reset_notice()
        return stub
    return _install


class TestLanCurlFallback:
    def test_errno65_on_private_host_falls_back_and_returns_body(self, curl_seam,
                                                                 monkeypatch):
        stub = curl_seam(out=b'{"data": [{"id": "worker-model"}]}')
        monkeypatch.setattr(urllib.request, "urlopen", _urlopen_raises(_errno65()))
        got = ct._http_get_default("http://10.0.0.1:4000/v1/models")
        assert got == '{"data": [{"id": "worker-model"}]}'
        assert stub.argvs == [["/usr/bin/curl", "-sS", "--fail", "-L",
                               "--max-time", "10",
                               "http://10.0.0.1:4000/v1/models"]]

    @pytest.mark.parametrize("url", [
        "https://api.example.com/v1/models",   # public: not the TCC story
        "http://localhost:4000/v1/models",     # TCC does not gate loopback
        "http://127.0.0.1:4000/v1/models",
    ])
    def test_errno65_off_a_private_host_propagates_unchanged(self, curl_seam,
                                                             monkeypatch, url):
        stub = curl_seam()
        monkeypatch.setattr(urllib.request, "urlopen", _urlopen_raises(_errno65()))
        with pytest.raises(urllib.error.URLError) as ei:
            ct._http_get_default(url)
        assert ei.value.reason.errno == 65
        assert stub.argvs == []

    def test_errno113_does_not_fall_back(self, curl_seam, monkeypatch):
        """113 is Linux EHOSTUNREACH = genuinely no route; it stays an error."""
        stub = curl_seam()
        exc = urllib.error.URLError(OSError(113, "No route to host", "10.0.0.1"))
        monkeypatch.setattr(urllib.request, "urlopen", _urlopen_raises(exc))
        with pytest.raises(urllib.error.URLError) as ei:
            ct._http_get_default("http://10.0.0.1:4000/v1/models")
        assert ei.value.reason.errno == 113
        assert stub.argvs == []

    def test_httperror_from_urlopen_is_untouched(self, curl_seam, monkeypatch):
        stub = curl_seam()
        exc = urllib.error.HTTPError("http://10.0.0.1:4000/v1/models", 401,
                                     "unauthorized", None, None)
        monkeypatch.setattr(urllib.request, "urlopen", _urlopen_raises(exc))
        with pytest.raises(urllib.error.HTTPError) as ei:
            ct._http_get_default("http://10.0.0.1:4000/v1/models")
        assert ei.value.code == 401
        assert stub.argvs == []

    def test_curl_exit_22_raises_the_httperror_family(self, curl_seam, monkeypatch):
        """Auth/path errors must stay 'probe error', not 'unreachable'."""
        curl_seam(rc=22, err=b"curl: (22) The requested URL returned error: 401\n")
        monkeypatch.setattr(urllib.request, "urlopen", _urlopen_raises(_errno65()))
        with pytest.raises(urllib.error.HTTPError) as ei:
            ct._http_get_default("http://10.0.0.1:4000/v1/models")
        assert ei.value.code == 401

    def test_probe_reports_error_not_unreachable_over_the_fallback(self, curl_seam,
                                                                   monkeypatch):
        """The probe-before-write gate must keep its three-way verdict: a 401
        through curl is still 'error' (reachable, wrong auth/path)."""
        curl_seam(rc=22, err=b"curl: (22) The requested URL returned error: 401\n")
        monkeypatch.setattr(urllib.request, "urlopen", _urlopen_raises(_errno65()))
        status, detail = ct._probe_served_models("http://10.0.0.1:4000/v1")
        assert status == "error"
        assert "401" in detail[0]

    def test_probe_recovers_models_over_the_fallback(self, curl_seam, monkeypatch):
        """The reported failure class, end to end: probe-before-write works
        again on a TCC-denied interpreter instead of refusing every write."""
        curl_seam(out=json.dumps({"data": [{"id": "worker-model"}]}).encode())
        monkeypatch.setattr(urllib.request, "urlopen", _urlopen_raises(_errno65()))
        assert ct._probe_served_models("http://10.0.0.1:4000/v1") == \
            ("ok", ["worker-model"])

    def test_api_key_never_reaches_argv_and_travels_in_a_0600_file(self, curl_seam,
                                                                   monkeypatch):
        """SECURITY: `ps` shows every argv token of a child process, so the
        Bearer value must live in the -H @file only, never in argv."""
        key = "sk-SECRET-donotputinargv"
        stub = curl_seam()
        captured = {}

        def spy(argv, timeout):
            stub.argvs.append(list(argv))
            for tok in argv:
                assert key not in tok, f"api_key leaked into argv token {tok!r}"
            i = argv.index("-H") + 1
            assert argv[i].startswith("@"), "header must come from a file"
            path = argv[i][1:]
            captured["body"] = open(path).read()
            captured["mode"] = oct(os.stat(path).st_mode & 0o777)
            captured["path"] = path
            return 0, b'{"data": []}', b""

        monkeypatch.setattr(ct, "_lan_curl_run", spy)
        monkeypatch.setattr(urllib.request, "urlopen", _urlopen_raises(_errno65()))
        ct._http_get_default("http://10.0.0.1:4000/v1/models", api_key=key)
        assert captured["body"] == f"Authorization: Bearer {key}\n"
        assert captured["mode"] == "0o600"
        assert not os.path.exists(captured["path"])   # unlinked after the request
        assert stub.argvs[0][-1] == "http://10.0.0.1:4000/v1/models"

    def test_notice_printed_exactly_once_across_fallbacks(self, curl_seam,
                                                          monkeypatch, capsys):
        stub = curl_seam()
        monkeypatch.setattr(urllib.request, "urlopen", _urlopen_raises(_errno65()))
        for i in range(4):
            ct._http_get_default(f"http://10.0.0.{i}:4000/v1/models")
        assert len(stub.argvs) == 4
        assert capsys.readouterr().err.count(ct.LAN_CURL_FALLBACK_NOTICE) == 1

    def test_curl_missing_surfaces_original_error_with_hint(self, monkeypatch):
        def no_curl(argv, timeout):
            raise FileNotFoundError(2, "No such file or directory", argv[0])
        monkeypatch.setattr(ct, "_lan_curl_run", no_curl)
        ct._lan_curl_reset_notice()
        monkeypatch.setattr(urllib.request, "urlopen", _urlopen_raises(_errno65()))
        with pytest.raises(urllib.error.URLError) as ei:
            ct._http_get_default("http://10.0.0.1:4000/v1/models")
        msg = str(ei.value)
        assert "No route to host" in msg               # original kept, not replaced
        assert "curl fallback unavailable" in msg      # + the one-line hint
