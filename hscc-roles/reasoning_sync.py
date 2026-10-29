"""Push the applied template's reasoning policy into every live profile config.

The concrete effort per tier is model vocabulary (one model takes
``low|medium|xhigh``, another ``high|max``), so the cluster template declares
it and ``hscc template apply`` records it at ``rolelib.REASONING_POLICY_PATH``.
This module then sets, in each profile's ``config.yaml``:

* ``agent.reasoning_effort``      = the policy value for the profile's tier
  (role spec ``reasoning_tier`` > orchestrators ``max`` > everyone ``standard``);
* ``delegation.reasoning_effort`` = the ``light`` value (subagents).

Only those two keys are ever touched, and only when the policy declares the
tier; a file is rewritten only when a value actually changes. Profiles are
never regenerated here (``generator.generate_profile`` rewrites a whole
config.yaml).
"""

from __future__ import annotations

import os

import yaml

import generator
import rolelib


def spec_tiers() -> dict:
    """``{profile name: tier}`` for every role spec that loads."""
    out = {}
    for path in rolelib.list_spec_files():
        try:
            spec = rolelib.load_spec(path)
        except (ValueError, OSError, yaml.YAMLError):
            continue
        out[spec["name"]] = generator.reasoning_tier(spec)
    return out


def sync_profile(pdir: str, tier: str, policy: dict, dry_run: bool = False) -> dict:
    name = os.path.basename(os.path.normpath(pdir))
    report = {"profile": name, "tier": tier, "changed": [], "status": "ok"}
    path = os.path.join(pdir, "config.yaml")
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError, UnicodeDecodeError) as exc:
        report["status"] = f"skipped: {type(exc).__name__}"
        return report
    if not isinstance(data, dict):
        report["status"] = "skipped: config is not a mapping"
        return report
    agent = data.get("agent") if isinstance(data.get("agent"), dict) else {}
    delegation = data.get("delegation") if isinstance(data.get("delegation"), dict) else {}
    new_agent, new_delegation = generator.reasoning_blocks(tier, policy, agent, delegation)
    if new_agent.get("reasoning_effort") != agent.get("reasoning_effort"):
        report["changed"].append(f"agent.reasoning_effort={new_agent['reasoning_effort']}")
    if new_delegation.get("reasoning_effort") != delegation.get("reasoning_effort"):
        report["changed"].append(f"delegation.reasoning_effort={new_delegation['reasoning_effort']}")
    if report["changed"] and not dry_run:
        if new_agent:
            data["agent"] = new_agent
        if new_delegation:
            data["delegation"] = new_delegation
        tmp = f"{path}.tmp-reasoning"
        with open(tmp, "w", encoding="utf-8") as fh:
            yaml.safe_dump(data, fh, default_flow_style=False, sort_keys=False,
                           allow_unicode=True)
        os.replace(tmp, path)
    return report


def sync_all(profiles_dir: str, policy: dict, tiers: dict | None = None,
             dry_run: bool = False) -> list:
    if not policy:
        return []
    tiers = spec_tiers() if tiers is None else tiers
    reports = []
    try:
        entries = sorted(os.listdir(profiles_dir))
    except OSError:
        return reports
    for entry in entries:
        pdir = os.path.join(profiles_dir, entry)
        if not os.path.isfile(os.path.join(pdir, "config.yaml")):
            continue
        tier = tiers.get(entry) or generator.reasoning_tier(entry)
        reports.append(sync_profile(pdir, tier, policy, dry_run=dry_run))
    return reports
