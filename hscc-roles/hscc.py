#!/usr/bin/env python3
"""HSCC Roles — author + generate role-specialized Hermes profiles.

Usage: hscc-roles <command> [args]

Commands:
  generate                 Build/refresh all role profiles from roles/*.yaml
  create <name> <desc...>  Author a new role spec from a description
  list                     List role specs and whether their profile exists
  validate                 Validate every role spec (load + required fields)
  autonomy [on|off]        Show or set the fleet autonomy flag
  orch <project|general>   Ensure a project's orchestrator profile exists
                           (idempotent) and print its resolved identity
  orch-all [--registry P]  Ensure an orchestrator for EVERY registry project
                           plus the `general` catch-all (idempotent, bulk)
  provision-check         Report each profile's EFFECTIVE memory provider /
                           char limit / compaction cap / summarization endpoint
                           (provisioning verifier, read-only)
"""
import json
import os
import subprocess
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rolelib
import generator
import author
import autonomy
import orchestrators
import _theme
from _theme import escape, make_console, panel, status_panel, table


def _base_identity():
    with open(rolelib.BASE_IDENTITY_PATH) as f:
        return f.read()


def _usage(msg: str):
    """Print a usage/error line to stderr via the themed console (plain on a
    pipe). Human-facing only — never used on a machine (--json) path."""
    make_console(file=sys.stderr).print(f"[error]{escape(msg)}[/error]")


def cmd_generate():
    base = _base_identity()
    out = []
    failures = []
    for spec_file in rolelib.list_spec_files():
        try:
            spec = rolelib.load_spec(spec_file)
            changed = generator.generate_profile(spec, base)
            out.append({"role": spec["name"], "changed": changed})
        except Exception as e:
            failures.append({"file": os.path.basename(spec_file), "error": str(e)})
    result = {"generated": out}
    if failures:
        result["failures"] = failures
    # --json byte-identity: the machine payload stays RAW print(json.dumps),
    # never routed through a Console (no ANSI / no reformat) — bootstrap and
    # scripts capture + json.load() this.
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 1 if failures else 0


def cmd_create(argv):
    if len(argv) < 2:
        _usage("Usage: hscc-roles create <name> <description...>")
        return 1
    name = argv[0]
    desc = " ".join(argv[1:])
    path = author.create_role(name, desc)
    make_console().print(panel(
        "hscc-roles create",
        f"[ok]created[/ok] role [accent]{escape(name)}[/accent]\n"
        f"[label]spec:[/label] {escape(path)}\n"
        f"[dim]next: run `hscc-roles generate` to build the profile[/dim]",
    ))
    return 0


def cmd_list():
    rows = []
    for spec_file in rolelib.list_spec_files():
        spec = rolelib.load_spec(spec_file)
        pdir = os.path.join(rolelib.PROFILES_DIR, spec["name"])
        rows.append({"role": spec["name"],
                     "profile_exists": os.path.isdir(pdir)})
    tb = table("hscc-roles — role specs")
    tb.add_column("role", style="accent")
    tb.add_column("profile")
    for r in rows:
        tb.add_row(escape(r["role"]), "yes" if r["profile_exists"] else "no")
    make_console().print(tb)
    return 0


def cmd_validate():
    errs = []
    for spec_file in rolelib.list_spec_files():
        try:
            rolelib.load_spec(spec_file)
        except ValueError as e:
            errs.append(str(e))
    if errs:
        make_console().print(status_panel(
            "\n".join(escape(e) for e in errs), "error", title="validate"))
    else:
        make_console().print(status_panel(
            "all role specs valid", "ok", title="validate"))
    return 1 if errs else 0


def cmd_autonomy(argv):
    if argv:
        autonomy.set_state(argv[0])
    state = "on" if autonomy.is_on() else "off"
    make_console().print(panel(
        "autonomy",
        f"fleet autonomy is [accent]{state}[/accent]",
    ))
    return 0


def cmd_orch(argv):
    """Ensure + report a project's orchestrator profile (single project).

    Usage: hscc.py orch <project|general> [--registry PATH]
    Idempotent — re-running never clobbers an existing profile's memory/sessions.
    """
    registry = None
    rest = list(argv)
    if "--registry" in rest:
        i = rest.index("--registry")
        if i + 1 >= len(rest):
            _usage("Usage: hscc.py orch <project|general> [--registry PATH]")
            return 1
        registry = rest[i + 1]
        del rest[i:i + 2]
    if len(rest) != 1:
        _usage("Usage: hscc.py orch <project|general> [--registry PATH]")
        return 1
    project = rest[0] or None
    base = _base_identity()
    try:
        result = orchestrators.ensure_orchestrator(project, base_identity=base,
                                                    path=registry)
        # --json byte-identity: raw print(json.dumps), never themed.
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except orchestrators.OrchestratorError as e:
        make_console().print(status_panel(
            escape(str(e)), "error", title="orch"))
        return 1


def cmd_orch_all(argv):
    """Ensure an orchestrator for EVERY registry project + the `general` catch-all.

    Usage: hscc.py orch-all [--registry PATH]

    Idempotent — re-running never clobbers an existing profile's memory/sessions
    (``ensure_orchestrator`` guarantees this). One invocation covers the whole
    registry so bootstrap can provision the full per-project orchestrator fleet
    without enumerating projects itself.

    stdout is PURE JSON (callers may capture + ``json.load()`` it): per-profile
    results (profile/session/board/changed) plus any failures. Warnings for a
    missing/unreadable registry go to stderr, not stdout. A failure on one
    project never aborts the rest — every project is attempted, failures are
    collected, and the exit code is non-zero iff at least one failed.
    """
    registry = None
    rest = list(argv)
    if "--registry" in rest:
        i = rest.index("--registry")
        if i + 1 >= len(rest):
            _usage("Usage: hscc.py orch-all [--registry PATH]")
            return 1
        registry = rest[i + 1]
        del rest[i:i + 2]

    base = _base_identity()

    # Which projects to provision: every registry project plus `general`.
    # A missing/unreadable registry yields an empty list — we still ensure
    # `general` (the project-less default must work on a bare machine) and
    # warn rather than dying. Warnings go to stderr via the themed console
    # (plain on a pipe), never stdout — stdout stays pure JSON.
    try:
        projects = orchestrators.list_registry_projects(registry)
    except Exception as e:  # defensive: never abort the whole command on registry
        projects = []
        make_console(file=sys.stderr).print(
            f"[warn]{escape(f'warn: registry unreadable; ensuring only `general`: {e}')}[/warn]")
    if not projects:
        make_console(file=sys.stderr).print(
            "[warn]warn: no registry projects found; ensuring only `general`[/warn]")

    targets = projects + ["general"]
    ensured = []
    failures = []
    for project in targets:
        try:
            result = orchestrators.ensure_orchestrator(
                project, base_identity=base, path=registry)
            ensured.append({
                "project": result["project"],
                "profile": result["profile"],
                "session": result["session"],
                "board": result["board"],
                "changed": result["changed"],
            })
        except orchestrators.OrchestratorError as e:
            failures.append({"project": project, "error": str(e)})
        except Exception as e:  # non-orchestrator errors also isolated per project
            failures.append({"project": project, "error": f"{type(e).__name__}: {e}"})

    report = {"requested": targets, "ensured": ensured}
    if failures:
        report["failures"] = failures
    # --json byte-identity: raw print(json.dumps), never themed.
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 1 if failures else 0


class EffectiveConfigError(Exception):
    """A profile's effective config could not be resolved via Hermes' loader."""


# The four provisioning fields provision-check reports. Each names a dotted
# path into the config dict (as produced by Hermes' load_config / the profile's
# raw config.yaml) and a stable report key.
_FIELD_DEFS = [
    ("memory_provider", ("memory", "provider")),
    ("memory_char_limit", ("memory", "memory_char_limit")),
    ("threshold_tokens", ("compression", "threshold_tokens")),
    ("summarization_base_url", ("auxiliary", "compression", "base_url")),
]

# Human label for each report key (dotted config path), for the table view.
_FIELD_LABELS = {
    "memory_provider": "memory.provider",
    "memory_char_limit": "memory.memory_char_limit",
    "threshold_tokens": "compression.threshold_tokens",
    "summarization_base_url": "auxiliary.compression.base_url",
}

_EFFECTIVE_LOADER = r'''
import json
import os
import sys

# The profile whose EFFECTIVE config we resolve. HERMES_HOME is set to the
# profile dir so Hermes' own get_config_path() targets <profile-dir>/config.yaml
# — a profile's config does NOT inherit the flat ~/.hermes/config.yaml.
home = sys.argv[1]
os.environ["HERMES_HOME"] = home

# hermes_cli lives under ~/.hermes/hermes-agent (never on a normal sys.path).
# Add it exactly the way hscc_daemon/autodown.py does, honouring
# HERMES_AGENT_PATH. Run in a FRESH subprocess, so a failure here can never
# poison the parent's in-process config cache or env.
hermes_path = os.path.expanduser(os.environ.get("HERMES_AGENT_PATH", "~/.hermes/hermes-agent"))
if hermes_path not in sys.path:
    sys.path.insert(0, hermes_path)

from hermes_cli.config import load_config  # noqa: E402

cfg = load_config() or {}
mem = cfg.get("memory") if isinstance(cfg.get("memory"), dict) else {}
comp = cfg.get("compression") if isinstance(cfg.get("compression"), dict) else {}
aux = cfg.get("auxiliary") if isinstance(cfg.get("auxiliary"), dict) else {}
ccomp = aux.get("compression") if isinstance(aux.get("compression"), dict) else {}

print(json.dumps({
    "memory_provider": mem.get("provider"),
    "memory_char_limit": mem.get("memory_char_limit"),
    "threshold_tokens": comp.get("threshold_tokens"),
    "summarization_base_url": ccomp.get("base_url"),
}, ensure_ascii=False))
'''


def _provision_profile_names(registry=None):
    """The profile names provision-check iterates — exactly the set orch-all
    provisions: an orchestrator profile per registry project (``<P>-orch``)
    plus the ``general`` catch-all (``general-orch``). Degrades to just the
    catch-all on a missing/unreadable registry, mirroring orch-all."""
    projects = orchestrators.list_registry_projects(registry)
    return [f"{p}-orch" for p in projects] + [orchestrators.GENERAL_PROFILE]


def _nested_get(data, keys):
    cur = data
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def _read_profile_raw(profile_dir):
    """Read a profile's raw config.yaml as a dict (the on-disk side of the
    comparison). None when the file is absent; the parsed mapping otherwise.
    This is a plain read of what's ON DISK — effective resolution is
    _load_effective_config's job, never here."""
    cfg_path = os.path.join(profile_dir, "config.yaml")
    if not os.path.isfile(cfg_path):
        return None
    with open(cfg_path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data if isinstance(data, dict) else {}


def _load_effective_config(profile_dir):
    """Resolve a profile's EFFECTIVE config by invoking Hermes' own
    ``load_config()`` with ``HERMES_HOME=<profile_dir>``, in an isolated
    subprocess so no in-process env/cache state leaks between profiles and a
    crash can never poison the parent. Returns a dict of the four effective
    values. Raises :class:`EffectiveConfigError` on any failure so the caller
    reports the profile explicitly rather than skipping it."""
    try:
        res = subprocess.run(
            [sys.executable, "-c", _EFFECTIVE_LOADER, profile_dir],
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.SubprocessError as e:
        raise EffectiveConfigError(f"could not run Hermes loader: {e}") from e
    # Hermes may print warnings to stdout before the JSON; take the LAST
    # non-empty line as the payload.
    payload = ""
    for line in reversed(res.stdout.splitlines()):
        if line.strip():
            payload = line
            break
    if res.returncode != 0 or not payload:
        err = (res.stderr or "").strip()
        raise EffectiveConfigError(
            f"Hermes loader failed: {err or res.stdout.strip() or 'empty output'}")
    try:
        return json.loads(payload)
    except ValueError as e:
        raise EffectiveConfigError(
            f"loader returned unparseable output: {payload!r}") from e


def _profile_report(name, profile_dir):
    """Build the provision-check report for one profile.

    Reports BOTH the on-disk raw value and the EFFECTIVE value for each field,
    flagging a per-field discrepancy when a raw value the operator set on disk
    did NOT take effect (effective differs) — the silently-defaulted case
    (e.g. the memory block falling back to the 2200-char default). A field with
    no raw value simply runs whatever effective resolves to; that is not a
    discrepancy, it is a not-pinned field the report still surfaces."""
    raw = _read_profile_raw(profile_dir)
    effective = _load_effective_config(profile_dir)
    fields = {}
    for key, path in _FIELD_DEFS:
        raw_value = _nested_get(raw, path) if raw is not None else None
        eff_value = effective.get(key)
        fields[key] = {
            "raw": raw_value,
            "effective": eff_value,
            "discrepancy": raw_value is not None and raw_value != eff_value,
        }
    any_disc = any(f["discrepancy"] for f in fields.values())
    return {
        "profile": name,
        "config": os.path.join(profile_dir, "config.yaml") if raw is not None else None,
        "fields": fields,
        "discrepancy": any_disc,
    }


def cmd_provision_check(argv):
    """Report each profile's EFFECTIVE memory/compaction provisioning.

    Usage: hscc.py provision-check [--registry PATH] [--profile NAME] [--json]

    For every profile in the provisioning set (a ``<P>-orch`` per registry
    project plus the ``general-orch`` catch-all — the same set orch-all
    provisions) this resolves <profile>/config.yaml and reports the values
    Hermes would ACTUALLY use: memory.provider, memory.memory_char_limit,
    compression.threshold_tokens (compaction cap) and the aggregate
    auxiliary.compression base_url (the summarization endpoint), by invoking
    Hermes' own load_config() with HERMES_HOME set to the profile dir. It never
    reimplements a parser and never trusts the flat config — a profile's config
    does not inherit it, so a value can read correct on disk while the profile
    silently runs the default. Each field reports BOTH its on-disk raw value
    and the effective value, flagging a discrepancy when the two differ. A
    profile that cannot be resolved is reported explicitly, not skipped.

    --json keeps stdout PURE JSON (raw print(json.dumps), never themed).
    """
    as_json = "--json" in argv
    rest = [a for a in argv if a != "--json"]
    registry = None
    profile_filter = None
    usage = "Usage: hscc.py provision-check [--registry PATH] [--profile NAME] [--json]"
    if "--registry" in rest:
        i = rest.index("--registry")
        if i + 1 >= len(rest):
            _usage(usage)
            return 1
        registry = rest[i + 1]
        del rest[i:i + 2]
    if "--profile" in rest:
        i = rest.index("--profile")
        if i + 1 >= len(rest):
            _usage(usage)
            return 1
        profile_filter = rest[i + 1]
        del rest[i:i + 2]
    if rest:
        _usage(usage)
        return 1
    if not os.path.isdir(rolelib.PROFILES_DIR):
        report = {"error": f"no profiles dir at {rolelib.PROFILES_DIR}"}
        if as_json:
            print(json.dumps(report, indent=2, ensure_ascii=False))
        else:
            make_console().print(status_panel(
                escape(report["error"]), "error", title="provision-check"))
        return 1

    profiles = []
    for name in _provision_profile_names(registry):
        if profile_filter and name != profile_filter:
            continue
        profile_dir = os.path.join(rolelib.PROFILES_DIR, name)
        if not os.path.isdir(profile_dir):
            profiles.append({
                "profile": name,
                "config": None,
                "error": "missing profile dir",
            })
            continue
        try:
            profiles.append(_profile_report(name, profile_dir))
        except EffectiveConfigError as e:
            profiles.append({
                "profile": name,
                "config": os.path.join(profile_dir, "config.yaml"),
                "error": str(e),
            })

    if as_json:
        # --json byte-identity: raw print(json.dumps), never themed.
        print(json.dumps(profiles, indent=2, ensure_ascii=False))
        return 1 if any(p.get("error") or p.get("discrepancy") for p in profiles) else 0

    for p in profiles:
        if p.get("error"):
            make_console().print(status_panel(
                escape(f"{p['profile']}: {p['error']}"),
                "error", title="provision-check"))
            continue
        tb = table(f"provision-check — {escape(p['profile'])}")
        tb.add_column("field", style="label")
        tb.add_column("raw (on-disk)")
        tb.add_column("effective (Hermes)")
        tb.add_column("note")
        for key, _path in _FIELD_DEFS:
            f = p["fields"][key]
            note = "DISCREPANCY" if f["discrepancy"] else ""
            tb.add_row(
                escape(_FIELD_LABELS[key]),
                escape(_fmt_raw(f["raw"])),
                escape(_fmt_raw(f["effective"])),
                note,
            )
        make_console().print(tb)
    return 1 if any(p.get("error") or p.get("discrepancy") for p in profiles) else 0


def _fmt_raw(v):
    """Render a raw/effective value for the human table: None -> '(unset)'."""
    if v is None:
        return "(unset)"
    return str(v)


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        make_console().print(panel("hscc-roles — usage", str(__doc__)))
        return 0
    cmd = sys.argv[1]
    if cmd == "generate":
        return cmd_generate()
    if cmd == "create":
        return cmd_create(sys.argv[2:])
    if cmd == "list":
        return cmd_list()
    if cmd == "validate":
        return cmd_validate()
    if cmd == "autonomy":
        return cmd_autonomy(sys.argv[2:])
    if cmd == "orch":
        return cmd_orch(sys.argv[2:])
    if cmd == "orch-all":
        return cmd_orch_all(sys.argv[2:])
    if cmd == "provision-check":
        return cmd_provision_check(sys.argv[2:])
    make_console(file=sys.stderr).print(
        f"[error]Unknown command: {escape(cmd)}[/error]")
    make_console().print(panel("hscc-roles — usage", str(__doc__)))
    return 1


if __name__ == "__main__":
    sys.exit(main())
