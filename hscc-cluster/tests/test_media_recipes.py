"""Offline fixture gate for the staged media recipes (hscc-cluster/recipes/media/).

Why a hand-rolled gate instead of importing sparkrun: hscc-cluster deploys as a
standalone plugin dir and cannot ``import sparkrun`` in-process (the reason
``sparkrun_bridge.py`` shells out — see its module docstring), and CI has no
sparkrun install at all. The authoritative gate stays the operator-side
``sparkrun recipe validate`` on the installed overlay copy (recipes/README.md);
this suite is the pre-flight that catches, offline and per-commit:

  * YAML that does not parse, or top-level keys outside sparkrun's
    ``_KNOWN_KEYS`` allow-list (``core/recipe.py`` — an unknown key is a
    load-time ERROR there, so it must be an error here too);
  * the media invariants the design contract locks in
    (docs/design/media-serving.md §1/§5/§6/§7 + the §8 operator rulings):
    digest-pinned container, no ``:latest``, distribution disabled,
    local-cache serving, readiness inference off, LAN-only explicit port,
    the quantized-tier rule (never plain BF16 as the served default), the
    no-client-caps ruling (no ``--scheduler-rpc-timeout``), and
    output-path scratch under the already-mounted cache root.

Cross-checks that would be a tautology against our own yaml are instead pinned
against sparkrun / upstream SOURCE of truth (textual, like the route/URL-
constant rule elsewhere in this repo — a pinned constant is prose until it is
checked against the source segment). Two of these checks earned their keep the
day they were written: the staged commands originally carried
``--mem-fraction-static``, and the v0.5.22 diffusion parser is a STRICT parser
(``ServerArgs.from_cli_args`` ends its dynamic-flag extraction with
``raise SystemExit("error: unrecognized arguments: …")``, server_args.py:3609
@v0.5.22) — a srt/LLM-only flag in a diffusion command template is a boot
crash, not a warning, and sparkrun never strips or appends flags for recipes
with an explicit ``command:`` (runtimes/sglang.py:371-384).
Every source-backed check is SKIPPED with a loud reason when the source is not
available, so the suite stays green on CI boxes without a sparkrun checkout or
the cached upstream fixture.
"""

import os
import re
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
MEDIA_DIR = REPO / "hscc-cluster" / "recipes" / "media"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── sparkrun schema facts (mirrored from sparkrun core/recipe.py) ────────────
# If sparkrun ever widens/narrows its allow-list, the operator-side
# `sparkrun recipe validate` stays authoritative; this set is deliberately the
# LOAD-PATH keys only, which is what a malformed staged file trips first.
_KNOWN_KEYS = {
    "sparkrun_version", "recipe_version", "name", "description", "model",
    "model_revision", "runtime", "runtime_version", "mode", "min_nodes",
    "max_nodes", "container", "containers", "defaults", "env", "command",
    "runtime_config", "cluster_only", "solo_only", "benchmark", "metadata",
    "pre_exec", "post_exec", "post_commands", "mods", "stop_after_post",
    "builder", "builder_config", "executor", "executor_config", "scheduler",
    "distribution_config", "layout", "cluster_config", "runtime_cache",
    "readiness", "capabilities", "unsupported_capabilities",
}

_MEDIA_RECIPES = sorted(MEDIA_DIR.glob("*.yaml"))


def _load_all():
    cases = []
    for path in _MEDIA_RECIPES:
        try:
            doc = yaml.safe_load(path.read_text())
        except yaml.YAMLError as exc:  # pragma: no cover - failure mode
            pytest.fail(f"{path.name}: YAML does not parse: {exc}")
        cases.append((path, doc))
    return cases


ALL = _load_all()


def _ids():
    return [p.name for p, _ in ALL]


# ── structural: every key the loader would reject ────────────────────────────

def test_at_least_one_media_recipe_exists():
    assert _MEDIA_RECIPES, f"no media recipes found under {MEDIA_DIR}"


@pytest.mark.parametrize("path,doc", ALL, ids=_ids())
def test_top_level_keys_known(path, doc):
    unknown = set(doc) - _KNOWN_KEYS
    assert not unknown, f"{path.name}: unknown top-level keys {sorted(unknown)}"


@pytest.mark.parametrize("path,doc", ALL, ids=_ids())
def test_required_fields(path, doc):
    assert doc.get("recipe_version") == "2", f"{path.name}: recipe_version must be '2'"
    for key in ("name", "description", "model", "runtime", "container", "command"):
        assert doc.get(key), f"{path.name}: missing required key {key!r}"
    assert doc["runtime"] == "sglang", \
        f"{path.name}: media units are SGLang Diffusion-only (§8 Q2: no vLLM-Omni)"
    assert isinstance(doc["command"], str) and doc["command"].strip()
    assert doc["command"].strip().startswith("sglang serve"), \
        f"{path.name}: media units launch via `sglang serve`"


# ── pinning rules (recipes/README.md 'Pin everything') ───────────────────────

@pytest.mark.parametrize("path,doc", ALL, ids=_ids())
def test_container_pinned_by_digest(path, doc):
    container = doc["container"]
    assert "@sha256:" in container, f"{path.name}: container must be digest-pinned"
    assert not container.split("@")[0].endswith(":latest")
    m = re.fullmatch(r"[^@\s]+@sha256:([0-9a-f]{64})", container)
    assert m, f"{path.name}: malformed digest pin {container!r}"


@pytest.mark.parametrize("path,doc", ALL, ids=_ids())
def test_distribution_disabled(path, doc):
    dist = doc.get("distribution_config", {})
    assert dist.get("models", {}).get("enabled") is False, \
        f"{path.name}: models must NOT be downloaded/distributed (staging is operator-side)"
    assert dist.get("containers", {}).get("enabled") is False, \
        f"{path.name}: image must be pre-pulled, not distributed"


@pytest.mark.parametrize("path,doc", ALL, ids=_ids())
def test_served_from_local_cache_mount(path, doc):
    """flash-next rule: weights come from the node-local HF cache mount, never NAS."""
    cmd = doc["command"]
    assert "--model-path {model}" in cmd, \
        f"{path.name}: model must flow through {{model}} (single source of truth = `model:` key)"
    # no host paths outside the mount convention smuggled into the command
    for p in re.findall(r"(?<![\w{])/(?:mnt|home|Users|Volumes)[\w./-]*", cmd):
        pytest.fail(f"{path.name}: raw host path in command: {p}")


@pytest.mark.parametrize("path,doc", ALL, ids=_ids())
def test_ports_lan_bound_explicit(path, doc):
    defaults = doc["defaults"]
    port = defaults.get("port")
    assert isinstance(port, int) and not isinstance(port, bool) and 30000 <= port <= 30999, \
        f"{path.name}: explicit LAN-range port required in defaults (§7)"
    assert defaults.get("host") == "0.0.0.0"  # binds inside the LAN (§7); proxy :4000 is the only ingress
    cmd = doc["command"]
    assert "--port {port}" in cmd and "--host {host}" in cmd, \
        f"{path.name}: port/host must flow through the config chain, not be hardcoded"
    assert "--strict-ports true" in cmd, \
        f"{path.name}: strict ports — a silently shifted port breaks the proxy route"


@pytest.mark.parametrize("path,doc", ALL, ids=_ids())
def test_ports_unique_across_media(path, doc):
    ports = [d["defaults"]["port"] for _, d in ALL]
    assert len(ports) == len(set(ports)), f"duplicate media ports across recipes: {ports}"


@pytest.mark.parametrize("path,doc", ALL, ids=_ids())
def test_readiness_rules(path, doc):
    """No media unit serves /v1/chat/completions — inference probe must be off."""
    readiness = doc.get("readiness", {})
    assert readiness.get("inference") is False, \
        f"{path.name}: readiness.inference must be false (diffusion servers have no chat endpoint)"


@pytest.mark.parametrize("path,doc", ALL, ids=_ids())
def test_solo_and_no_mods(path, doc):
    assert doc.get("solo_only") is True, f"{path.name}: media units run solo (multi-node is cycle-2)"
    assert doc.get("mods") == [], f"{path.name}: mods must be the empty list (no builder on a pinned digest)"


# ── design-contract invariants (docs/design/media-serving.md) ────────────────

@pytest.mark.parametrize("path,doc", ALL, ids=_ids())
def test_gpu_fraction_declared_as_defaults_only(path, doc):
    """§5 needs an honest declared fraction — as a DEFAULTS KEY only.

    The diffusion CLI is a STRICT parser (unknown args → SystemExit,
    server_args.py:3609 @v0.5.22) and has no memory-fraction field at all, so
    emitting the fraction as a CLI flag there is a boot crash, not a budget
    (this exact bug was caught by the flag-surface check below on the first
    run of this suite). The fraction stays a sparkrun-side claim: scheduler
    budget + VRAM check visibility + the §5 per-node fraction sum.
    """
    frac = doc["defaults"].get("gpu_memory_utilization")
    assert isinstance(frac, (int, float)) and not isinstance(frac, bool) and 0 < frac <= 0.8, \
        f"{path.name}: honest §5 fraction required in defaults (per-node sum rule caps at 0.8)"
    assert "--mem-fraction-static" not in doc["command"], \
        f"{path.name}: the diffusion parser SystemExits on this flag — fraction is defaults-only"


@pytest.mark.parametrize("path,doc", ALL, ids=_ids())
def test_quantized_tier_not_bare_bf16(path, doc):
    """§8 Q1 (never-plain-BF16): each unit must carry an explicit quantized
    story — a quantization mechanism in the command, or a model repo whose id
    IS a published quantized checkpoint (self-describing repos need no flag:
    the guide says omit --quantization for modelopt-nvfp4)."""
    cmd = doc["command"]
    markers = ("nvfp4", "fp8", "int8", "modelopt")
    quantized = (
        "--quantization" in cmd
        or "--component-weights-paths" in cmd
        or "--component-paths" in cmd
        or "-weights-path" in cmd  # documented alias, e.g. --transformer-weights-path
        or any(m in doc["model"].lower() for m in markers)
    )
    assert quantized, \
        f"{path.name}: no quantization mechanism found — violates the never-plain-BF16 ruling"


@pytest.mark.parametrize("path,doc", ALL, ids=_ids())
def test_no_client_caps_in_recipe(path, doc):
    """§8 Q5 ruling: no client caps; --scheduler-rpc-timeout deliberately unset."""
    assert "--scheduler-rpc-timeout" not in doc["command"], \
        f"{path.name}: scheduler-rpc-timeout is deliberately unset (§8 Q5)"


@pytest.mark.parametrize("path,doc", ALL, ids=_ids())
def test_output_scratch_under_cache_root(path, doc):
    cmd = doc["command"]
    m = re.search(r"--output-path\s+(\S+)", cmd)
    assert m, f"{path.name}: §2 requires an explicit --output-path scratch dir"
    assert m.group(1).startswith("/cache/huggingface/"), \
        f"{path.name}: scratch must live under the already-mounted cache root (no new bind mount)"
    assert "--input-save-path" in cmd, f"{path.name}: edits/uploads need --input-save-path (§7 upload handling)"


@pytest.mark.parametrize("path,doc", ALL, ids=_ids())
def test_staging_list_in_header(path, doc):
    """The card's HARD CONSTRAINT: the recipe states the EXACT HF repo ids to stage."""
    text = path.read_text()
    header = "\n".join(text.splitlines()[:80])
    assert "STAGING LIST" in header, f"{path.name}: header must carry the operator staging list"
    for repo in _repos_of(doc):
        assert repo in text, f"{path.name}: {repo} not named in the file"


def _repos_of(doc):
    repos = [doc["model"]]
    cmd = doc["command"]
    for flag in ("--component-weights-paths", "--component-paths"):
        for m in re.finditer(rf"{flag}[\w.]*\s+(\S+)", cmd):
            val = m.group(1)
            if "{" not in val:
                repos.append(val)
    for m in re.finditer(r"\s--[\w-]+-weights-path\s+(\S+)", cmd):
        val = m.group(1)
        if "{" not in val:
            repos.append(val)
    return repos


# ── cross-checks against sparkrun / upstream SOURCE (skipped without it) ─────

SPARKRUN_SRC = Path(os.environ.get("SPARKRUN_SRC", str(Path.home() / "sparkrun" / "src" / "sparkrun")))
SA_FIXTURE = Path(os.environ.get("SGLANG_SA_FIXTURE", ""))  # cached v0.5.22 diffusion arg parser, if any

#: One diffusion image across the whole media tier (single pre-pull, §5
#: headroom math identical unit-to-unit). Diverging is possible, but only via
#: a deliberate edit HERE plus a design-doc note — not silently in one yaml.
EXPECTED_DIFFUSION_IMAGE = "lmsysorg/sglang@sha256:e3a4f06001a29542fc4a8638536d9ffc5eb9c9942b08603c09b19a56eae4a1ae"


def _skip_unless(cond, reason):
    if not cond:
        pytest.skip(reason)


def test_media_units_share_one_digest():
    """Every media recipe pins the same diffusion image digest."""
    for path, doc in ALL:
        assert doc["container"] == EXPECTED_DIFFUSION_IMAGE, \
            f"{path.name}: container diverges from the fleet's single media image"


def test_hf_cache_root_matches_sparkrun_runtime_base():
    """/cache/huggingface is sparkrun's OWN default cache root (runtimes/base.py);
    our output-path scratch dir nests under a mount that exists by right."""
    _skip_unless(SPARKRUN_SRC.is_dir(), f"no sparkrun checkout at {SPARKRUN_SRC}")
    src = (SPARKRUN_SRC / "runtimes" / "base.py").read_text()
    assert '"/cache/huggingface"' in src, \
        "sparkrun's default HF cache root changed — media scratch paths need review"


def test_fraction_key_is_known_so_defaults_stay_warning_free():
    """``gpu_memory_utilization`` must remain a KNOWN sglang config key even
    though the media templates never emit its flag — otherwise every launch
    logs an 'unmapped config key' warning against the §5 budget knob
    (core/launcher.py's report_unmapped_config_keys counts a key consumed when
    the runtime's flag map lists it)."""
    _skip_unless(SPARKRUN_SRC.is_dir(), f"no sparkrun checkout at {SPARKRUN_SRC}")
    src = (SPARKRUN_SRC / "runtimes" / "sglang.py").read_text()
    assert '"gpu_memory_utilization": "--mem-fraction-static"' in src, \
        ("sparkrun's sglang flag map changed for gpu_memory_utilization — the media "
         "recipes' claim that the key stays 'known' (no unmapped-key warning) needs review")


def test_diffusion_cli_accepts_every_flag_the_recipes_use():
    """Every `--flag` rendered by a media recipe must survive the v0.5.22
    SGLang *diffusion* parser (the diffusion surface, NOT srt — a srt-only
    flag parses fine against the LLM server and SystemExits on the diffusion
    one, server_args.py:3609).

    The parser's grammar has two halves:
      * ``add_argument("--x", ...)`` — the static surface;
      * dynamic component maps, consumed in ``from_cli_args`` via
        ``_extract_dynamic_component_map`` (server_args.py:3354-3427) — the
        ``--component-*`` families plus their ``--<component>-path`` /
        ``--<component>-weights-path`` alias suffixes. A flag matching those
        prefixes/suffixes is accepted WITHOUT being an add_argument.
    """
    _skip_unless(SA_FIXTURE.is_file(),
                 "v0.5.22 diffusion arg-parser fixture not cached (SGLANG_SA_FIXTURE)")
    fixture = SA_FIXTURE.read_text()

    defined = set(re.findall(r'add_argument\(\s*"--([\w][\w-]*)"', fixture))
    # dynamic-map prefixes, spelled in the _extract_* classmethods
    dyn_prefixes = set(re.findall(r'"--(component[\w-]*\.)"', fixture))
    dyn_prefixes |= {p.rstrip(".") for p in dyn_prefixes}
    # alias suffixes (e.g. --transformer-weights-path, --<component>-path)
    alias_suffixes = set(re.findall(r'alias_suffix="([\w-]+)"', fixture))

    def accepted(flag: str) -> bool:
        if flag in defined:
            return True
        for prefix in dyn_prefixes:  # --component-<family>.<component>
            if flag == prefix or flag.startswith(prefix + "."):
                return True
        for suffix in alias_suffixes:  # --<component>{-weights-path|-path}
            # `flag` is captured WITHOUT its leading dashes; the parser accepts
            # any `--<stem><suffix>` whose stem is a single token — that IS the
            # dynamic alias grammar (server_args.py:3369-3376), so mirroring
            # it here means a bogus `--whatever-path` would parse too, exactly
            # as it would in the real CLI.
            if flag.endswith(suffix):
                stem = flag[: -len(suffix)].strip("-")
                if stem and re.fullmatch(r"[\w][\w-]*", stem):
                    return True
        return False

    used = set()
    for _, doc in ALL:
        for m in re.finditer(r"\s--([\w][\w.-]*)", doc["command"]):
            used.add(m.group(1))
    offenders = sorted(f for f in used if not accepted(f))
    assert not offenders, (
        f"flags not in the v0.5.22 diffusion parser surface: {offenders} — "
        "the diffusion CLI SystemExits on unrecognized arguments (boot crash)"
    )


def test_diffusion_parser_is_recorded_as_strict():
    """The invariant the fraction rule above rests on: unknown args are FATAL
    on the diffusion parser. If upstream ever softens that, this test fails
    and the defaults-only fraction rule gets revisited deliberately."""
    _skip_unless(SA_FIXTURE.is_file(),
                 "v0.5.22 diffusion arg-parser fixture not cached (SGLANG_SA_FIXTURE)")
    assert "unrecognized arguments" in SA_FIXTURE.read_text(), \
        "diffusion parser strictness changed upstream — re-check the fraction rule"


def test_no_real_infrastructure_addresses():
    """Belt-and-braces next to scripts/address_guard.py: staged recipes carry
    LAN placeholders only (docs/design/media-serving.md §7)."""
    pat = re.compile(r"\b(?:100\.64\.|10\.0\.0\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.)\d")
    for path, _ in ALL:
        hits = pat.findall(path.read_text())
        assert not hits, f"{path.name}: real-looking private address found: {hits}"
