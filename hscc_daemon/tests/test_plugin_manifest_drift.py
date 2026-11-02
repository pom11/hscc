"""GAP-A-7: plugin.yaml `provides_tools` must not drift from the registry.

Audit A GAP-A-7: ``hscc-cluster/plugin.yaml`` declared 13 tools while
``__init__.py`` registered 23 (orch_message, apply_template, list_templates,
fleet_stats, … all missing). Under-declaration is not cosmetic: Hermes reads
``provides_tools`` to advertise a plugin's tools (``hermes tools``,
plugin_catalog, deferred-platform tool registration), so the manifest is a
contract — and a manifest that lies is exactly the class of gap audits kept
rediscovering (docs/audits GAP-A-7; the deployed-payload twin is GAP-F-8,
covered by test_verify_cluster_tools.py).

The rule asserted here, per plugin dir that carries BOTH a plugin.yaml and an
__init__.py:

    set(provides_tools) == set(names registered by register(ctx))

in both directions — under-declaration (the audit finding) and
over-declaration (which would make ``verify.check_plugins`` demand tools the
registry cannot deploy). The registry is the source of truth: a table that
exists but is NOT summed into the register() loop is not a registration, so
the parse resolves names through the tables the loop actually concatenates.

Parses only — never imports the plugin (hyphenated dirs aren't importable)
and never touches ~/.hscc, the fleet, or the network. The name/manifest
parsers are imported from ``hscc_daemon.verify`` itself: one implementation,
so the drift test cannot pass on a private parser while the shipped check
uses a diverging one (the single-implementation rule the address guard and
guard_posture tests enforce elsewhere).

Skipped (not silently vacuous) when the tree is not the repo layout: the
deployed layout's plugins dir also carries third-party plugins this repo
makes no promises about.
"""

import re
from pathlib import Path

import pytest

from hscc_daemon.verify import _manifest_tools, _registered_tool_names

REPO = Path(__file__).resolve().parents[2]

# The repo layout is the only layout this contract is written for: the
# deployed tree under ~/.hermes/plugins is a mix of this repo's payloads and
# third-party plugins, and its drift story is check_plugins' deployed-payload
# leg (test_verify_cluster_tools.py), not this file.
if not (REPO / "scripts" / "run_tests.sh").is_file():
    pytest.skip("not the repo layout — manifest-vs-registry is a repo promise",
                allow_module_level=True)


def _plugin_dirs():
    """Repo dirs carrying a plugin.yaml (the manifest side of the contract)."""
    return sorted(d for d in REPO.iterdir()
                  if d.is_dir() and not d.name.startswith((".", "_"))
                  and (d / "plugin.yaml").is_file())


def _registered_names(init_text):
    """Every tool name register(ctx) registers: the `_*_TOOLS` tables it
    concatenates (via verify's own parser) plus any direct
    ``ctx.register_tool(name="…")`` call (the sparkrun-hermes shape — a single
    tool registered without a table).

    A table defined but never summed into the register() loop is NOT a
    registration and is reported by a separate assertion below, so "what the
    tables say" and "what the loop registers" cannot quietly diverge here."""
    names = set(_registered_tool_names(init_text))
    names.update(re.findall(
        r"""register_tool\(\s*\n?\s*name\s*=\s*["']([\w.\-]+)["']""", init_text))
    return names


def _tables_reached_by_register_loop(init_text):
    """Names of every `_*_TOOLS = [` table defined in the file, and which of
    them appear in the register() loop's concatenated expression."""
    defined = set(re.findall(r"^(\w*_TOOLS)\s*=\s*\[", init_text, re.MULTILINE))
    m = re.search(r"def register\(ctx.*?(?=\ndef |\Z)", init_text, re.DOTALL)
    body = m.group(0) if m else ""
    # Only the concatenation counts as registration; a table named in a
    # comment in the body would false-satisfy, so match `+ TABLE` /
    # `(TABLE` shapes in the loop's expression, not prose.
    reached = {t for t in defined if re.search(
        r"[+(]\s*" + t + r"\b", body) or re.search(r"\b" + t + r"\s*\+", body)}
    return defined, reached


ALL_PLUGIN_DIRS = _plugin_dirs()
MANIFESTED = [d for d in ALL_PLUGIN_DIRS if (d / "plugin.yaml").is_file()
              and (d / "__init__.py").is_file()]


def test_repo_census_nonempty():
    """Guard the guard: this file's contract only means something if the
    census actually found the plugin dirs (a bad path anchor would 'pass' by
    checking nothing)."""
    assert {d.name for d in MANIFESTED} >= {"hscc-cluster", "sparkrun-hermes"}, \
        f"expected the two tool-registering plugins, found: {[d.name for d in MANIFESTED]}"


@pytest.mark.parametrize("plugin_dir", MANIFESTED,
                         ids=lambda d: d.name)
def test_manifest_matches_registered_tools(plugin_dir):
    """`provides_tools` == the tools register(ctx) registers — both ways."""
    init_text = (plugin_dir / "__init__.py").read_text()
    registered = _registered_names(init_text)
    declared = _manifest_tools(str(plugin_dir))

    missing = sorted(registered - declared)   # GAP-A-7: manifest lies low
    extra = sorted(declared - registered)     # manifest promises the impossible
    assert not missing, (
        f"{plugin_dir.name}/plugin.yaml under-declares {len(missing)} registered "
        f"tool(s): {missing}. The __init__.py registry is the source of truth; "
        "add them to provides_tools (hermes advertises tools from this list)."
    )
    assert not extra, (
        f"{plugin_dir.name}/plugin.yaml over-declares {len(extra)} tool(s) no "
        f"register() call produces: {extra}. Either register them or drop them "
        "from provides_tools — verify.check_plugins demands every declared tool "
        "in the deployed payload."
    )


@pytest.mark.parametrize("plugin_dir", MANIFESTED,
                         ids=lambda d: d.name)
def test_every_tools_table_reaches_the_register_loop(plugin_dir):
    """The table-vs-loop gap that would make the manifest test above lie:
    a `_*_TOOLS` table defined but not concatenated into register() registers
    nothing, so the manifest must NOT list its tools. Pin every defined table
    into the loop — that is the invariant the manifest mirrors."""
    defined, reached = _tables_reached_by_register_loop(
        (plugin_dir / "__init__.py").read_text())
    unreachable = sorted(defined - reached)
    assert not unreachable, (
        f"{plugin_dir.name}/__init__.py defines tool tables the register() "
        f"loop never concatenates: {unreachable}. They register nothing, so "
        "either wire them into the loop or delete them (the manifest is "
        "asserted against what actually registers)."
    )


def test_cluster_manifest_is_exactly_the_23_registered_tools():
    """The concrete GAP-A-7 receipt: 23 tools, the 10 that were missing are
    named. If this count changes intentionally, the per-table tests above are
    the generic contract — update the number here knowingly, never by drift."""
    declared = _manifest_tools(str(REPO / "hscc-cluster"))
    assert len(declared) == len(_registered_names(
        (REPO / "hscc-cluster" / "__init__.py").read_text())) == 23
    for previously_missing in ("orch_message", "apply_template", "list_templates",
                               "preview_template", "fleet_stats", "cluster_verify",
                               "cluster_throughput", "autoscale_advice",
                               "discovery_status", "nas_status"):
        assert previously_missing in declared, \
            f"GAP-A-7 regression: {previously_missing} dropped from the manifest"
