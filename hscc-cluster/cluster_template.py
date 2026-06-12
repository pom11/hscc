"""
HSCC Cluster Template Engine

Loads cluster templates, validates them, and can:
- List available templates
- Preview what applying a template would change (dry-run)
- Apply a template (write configs, provision models, wire Hermes)

Flow:
  hscc template list          → list available templates
  hscc template preview <n>   → dry-run, show config changes
  hscc template apply <n>     → apply template (with confirmation)
"""

from __future__ import annotations

import json
import os
import sys
import shutil
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

# ── Constants ──────────────────────────────────────────────────────────────

PLUGIN_DIR = Path(__file__).parent
TEMPLATE_DIR = PLUGIN_DIR / "templates"
HSCC_DIR = Path(os.path.expanduser("~/.hscc"))
HERMES_HOME = Path(os.path.expanduser("~/.hermes"))
SERVING_JSON = HSCC_DIR / "serving.json"
MODELS_JSON = HSCC_DIR / "models.json"
CLUSTER_JSON = HSCC_DIR / "cluster.json"
CONFIG_YAML = HERMES_HOME / "config.yaml"
PROXY_DIR = HSCC_DIR / "proxies"
APPLIED_STATE = HSCC_DIR / "applied_template.json"  # which template is live

# Cap timestamped backups per file so re-applies don't accumulate forever
# (a prior version left 100+ serving.json.bak.* / models.json.bak.* in ~/.hscc).
MAX_BACKUPS = 5

# ── Helpers ────────────────────────────────────────────────────────────────

def _prune_backups(path: Path, keep: int = MAX_BACKUPS) -> None:
    """Keep only the newest ``keep`` ``<path>.bak.<epoch>`` siblings; delete older."""
    path = Path(path)
    backups = sorted(
        path.parent.glob(path.name + ".bak.*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in backups[keep:]:
        try:
            old.unlink()
        except OSError:
            pass


ROLLBACK_DIR = HSCC_DIR / "rollback"
MAX_ROLLBACKS = 5
# Files captured in a pre-apply snapshot for atomic rollback (G4/5e).
_SNAPSHOT_FILES = ("serving.json", "models.json", "applied_template.json")


def _snapshot_state() -> Optional[Path]:
    """Copy the current serving/models/applied-template + config.yaml into a
    timestamped rollback bundle. Returns the bundle path (or None if nothing to
    snapshot). Pruned to MAX_ROLLBACKS most-recent bundles."""
    sources = [(HSCC_DIR / f) for f in _SNAPSHOT_FILES] + [CONFIG_YAML]
    if not any(s.exists() for s in sources):
        return None
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    bundle = ROLLBACK_DIR / ts
    try:
        bundle.mkdir(parents=True, exist_ok=True)
        for src in sources:
            if src.exists():
                shutil.copy2(str(src), str(bundle / src.name))
    except OSError:
        return None
    # prune old bundles
    try:
        bundles = sorted([p for p in ROLLBACK_DIR.iterdir() if p.is_dir()],
                         key=lambda p: p.stat().st_mtime, reverse=True)
        for old in bundles[MAX_ROLLBACKS:]:
            shutil.rmtree(old, ignore_errors=True)
    except OSError:
        pass
    return bundle


def _restore_snapshot(bundle: Optional[Path]) -> bool:
    """Restore files from a snapshot bundle back to their live locations.
    Returns True if a restore happened."""
    if not bundle or not Path(bundle).is_dir():
        return False
    restored = False
    for f in _SNAPSHOT_FILES:
        src = bundle / f
        if src.exists():
            try:
                shutil.copy2(str(src), str(HSCC_DIR / f))
                restored = True
            except OSError:
                pass
    cfg = bundle / CONFIG_YAML.name
    if cfg.exists():
        try:
            shutil.copy2(str(cfg), str(CONFIG_YAML))
            restored = True
        except OSError:
            pass
    return restored


def read_json(path: Path) -> Optional[dict]:
    """Read and parse a JSON file."""
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def write_json(path: Path, data: dict, backup: bool = True) -> Path:
    """Write JSON atomically with optional backup."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup and path.exists():
        backup_path = Path(str(path) + f".bak.{int(datetime.now().timestamp())}")
        shutil.copy2(str(path), str(backup_path))
        _prune_backups(path)
    # Atomic write: tmp + rename
    tmp_path = Path(str(path) + ".tmp")
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(str(tmp_path), str(path))
    return path


def atomic_yaml_update(path: Path, update_fn, backup: bool = True):
    """Read a YAML file, apply update_fn, write back atomically.

    update_fn receives the parsed dict and returns the updated dict.
    Returns (path, changed): ``changed`` is False when the new content is
    byte-identical to the old, so callers can skip side effects (e.g. a gateway
    restart) on a no-op apply.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    import yaml
    old_data = {}
    old_text = ""
    if path.exists():
        try:
            old_text = open(path).read()
            old_data = yaml.safe_load(old_text) or {}
        except Exception:
            pass

    new_data = update_fn(old_data.copy()) if isinstance(old_data, dict) else {}
    new_text = yaml.dump(new_data, default_flow_style=False, sort_keys=False)
    changed = new_text != old_text

    if not changed:
        return path, False

    if backup and path.exists():
        backup_path = Path(str(path) + f".bak.{int(datetime.now().timestamp())}")
        shutil.copy2(str(path), str(backup_path))
        _prune_backups(path)

    tmp_path = Path(str(path) + ".tmp")
    with open(tmp_path, "w") as f:
        f.write(new_text)
    os.replace(str(tmp_path), str(path))
    return path, True


# ── Proxy plist generation ─────────────────────────────────────────────────

def _generate_proxy_plist(family) -> str:
    """Generate a launchd plist for a LiteLLM proxy instance."""
    config_path = str(PROXY_DIR / family.name / "config.json")
    log_path = str(PROXY_DIR / "logs" / f"{family.name}.log")
    
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.hermes.proxy.{family.name}</string>
    <key>ProgramArguments</key>
    <array>
        <string>litellm</string>
        <string>--port</string>
        <string>{family.proxy.port}</string>
        <string>--config</string>
        <string>{config_path}</string>
    </array>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_path}</string>
    <key>StandardErrorPath</key>
    <string>{log_path}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>LITELLM_LICENSE_KEY</key>
        <string></string>
    </dict>
</dict>
</plist>"""
    return plist


def install_proxy_plist(family) -> dict:
    """Generate, write, AND load a proxy launchd plist. Returns action summary.

    Writing the plist alone does not start the proxy — it must be loaded into the
    user's launchd domain. Bootout-then-bootstrap so re-applying reloads cleanly
    (idempotent) rather than erroring on an already-loaded label.
    """
    import subprocess

    proxy_dir = PROXY_DIR / family.name
    logs_dir = PROXY_DIR / "logs"
    proxy_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    plist_content = _generate_proxy_plist(family)
    plist_path = proxy_dir / "proxy.plist"
    with open(plist_path, "w") as f:
        f.write(plist_content)

    label = f"com.hermes.proxy.{family.name}"
    domain = f"gui/{os.getuid()}"
    loaded = False
    load_error = ""
    try:
        # Drop any prior instance (ignore failure: may not be loaded yet).
        subprocess.run(["launchctl", "bootout", f"{domain}/{label}"],
                       capture_output=True, timeout=10)
        r = subprocess.run(["launchctl", "bootstrap", domain, str(plist_path)],
                           capture_output=True, text=True, timeout=30)
        loaded = r.returncode == 0
        if not loaded:
            load_error = (r.stderr or "").strip() or "launchctl bootstrap failed"
    except Exception as e:  # subprocess/timeout — report, don't crash apply
        load_error = str(e)

    return {
        "plist": str(plist_path),
        "label": label,
        "port": family.proxy.port,
        "log": str(logs_dir / f"{family.name}.log"),
        "loaded": loaded,
        "error": load_error or None,
    }


def remove_proxy_plist(family) -> dict:
    """Stop and remove a proxy launchd plist. Returns action summary."""
    import subprocess
    
    label = f"com.hermes.proxy.{family.name}"
    try:
        subprocess.run(
            ["launchctl", "bootout", "gui/" + str(os.getuid()), label],
            capture_output=True, timeout=10
        )
    except Exception:
        pass
    
    plist_path = PROXY_DIR / family.name / "proxy.plist"
    if plist_path.exists():
        plist_path.unlink()
    
    return {"label": label, "status": "removed"}


# ── Model provisioning ─────────────────────────────────────────────────────

def _provision_models(tpl: Any, cluster: str = "hscc",
                      do_launch: bool = True) -> dict:
    """Bring the cluster to the template's model layout via sparkrun.

    For each template unit (orchestrator + each worker node) launch the model
    with `sparkrun run <recipe> --hosts <node> --ensure` (--ensure = no-op if it
    is already serving). Stop any sparkrun container on a node the template does
    not use. This is the step that makes the live cluster actually match the
    template — not a report.

    do_launch=False makes it a dry plan (no sparkrun calls) — used by preview and
    tests so they never touch the real cluster.
    """
    import subprocess

    result = {"stopped": [], "provisioned": [], "failed": [],
              "status": "ok", "note": ""}

    # (node, recipe) the template wants serving, derived from serving.json units.
    serving = tpl.to_serving_json()
    want = []  # list of (node, recipe)
    want.append((tpl.orchestrator_node, tpl.orchestrator.recipe))
    for u in serving["units"]:
        if u["role"] == "worker":
            want.append((u["nodes"][0], u["recipe"]))
    template_nodes = {n for n, _ in want}

    if not do_launch:
        result["note"] = "dry-run: would provision " + ", ".join(
            f"{r.split('/')[-1]}@{n}" for n, r in want)
        result["provisioned"] = [f"{n}:{r}" for n, r in want]
        return result

    # Stop sparkrun containers on nodes the template does not use.
    try:
        for line in _running_nodes_via_sparkrun():
            node = line
            if node and node not in template_nodes:
                subprocess.run(["sparkrun", "stop", "--all", "--hosts", node],
                               capture_output=True, timeout=60)
                result["stopped"].append(node)
    except Exception:
        pass  # stopping is best-effort; never block provisioning on it

    # Launch each wanted (node, recipe). --ensure: skip if already up.
    for node, recipe in want:
        try:
            r = subprocess.run(
                ["sparkrun", "run", os.path.expanduser(recipe),
                 "--cluster", cluster, "--hosts", node,
                 "--port", "8000", "--no-follow", "--ensure"],
                capture_output=True, text=True, timeout=240)
            if r.returncode == 0:
                result["provisioned"].append(f"{node}:{recipe.split('/')[-1]}")
            else:
                result["failed"].append(
                    {"node": node, "recipe": recipe,
                     "error": (r.stderr or "").strip()[:200]})
        except Exception as e:
            result["failed"].append({"node": node, "recipe": recipe,
                                     "error": str(e)})

    if result["failed"]:
        result["status"] = "warn"
        result["note"] = f"{len(result['failed'])} model(s) failed to launch"
    else:
        result["note"] = f"{len(result['provisioned'])} model(s) ensured up"
    return result


def _running_nodes_via_sparkrun() -> List[str]:
    """Best-effort: node IPs that currently have a sparkrun container."""
    import subprocess
    nodes: List[str] = []
    try:
        r = subprocess.run(["sparkrun", "status"], capture_output=True,
                           text=True, timeout=15)
        for line in (r.stdout or "").split("\n"):
            # status rows include the host IP; collect anything IP-shaped
            for tok in line.split():
                if tok.count(".") == 3 and tok.replace(".", "").isdigit():
                    nodes.append(tok)
    except Exception:
        pass
    return nodes


# ── Template loading ───────────────────────────────────────────────────────

def list_templates():
    """List all available cluster templates."""
    from cluster_template_schema import list_templates as schema_list
    
    registry = schema_list(TEMPLATE_DIR)
    return {
        "count": len(registry.templates),
        "templates": registry.templates,
    }


def preview_template(template_name: str) -> dict:
    """Preview what applying a template would change (dry-run).
    
    Returns the full apply plan without making any changes.
    """
    from cluster_template_schema import load_template
    
    template_path = TEMPLATE_DIR / f"{template_name}.yaml"
    tpl = load_template(template_path)
    
    plan = {
        "template": tpl.name,
        "cluster_size": tpl.cluster_size,
        "description": tpl.description,
        "changes": [],
    }
    
    # 1. serving.json changes
    current_serving = read_json(SERVING_JSON)
    new_serving = tpl.to_serving_json()
    plan["changes"].append({
        "file": "serving.json",
        "action": "write",
        "summary": f"{len(new_serving['units'])} units (1 orchestrator + {len(tpl.families)} families)",
        "diff_summary": _diff_serving_summary(current_serving, new_serving),
    })
    
    # 2. models.json changes
    current_models = read_json(MODELS_JSON)
    new_models = _build_models_json(tpl)
    plan["changes"].append({
        "file": "models.json",
        "action": "write",
        "summary": f"{len(new_models['models'])} models registered",
    })
    
    # 3. Hermes config.yaml changes
    plan["changes"].append({
        "file": "config.yaml",
        "action": "update",
        "summary": "Update provider/model settings",
        "details": _describe_config_changes(tpl, current_models),
    })
    
    # 4. Proxy configs
    if tpl.families:
        plan["changes"].append({
            "file": "proxies/",
            "action": "create",
            "summary": f"{len(tpl.families)} proxy configs",
            "details": [
                f"  {f.name}: port {f.proxy.port}, nodes {f.nodes}"
                for f in tpl.families
            ],
        })
    
    # 5. Models to provision
    plan["changes"].append({
        "file": "models (provision)",
        "action": "provision",
        "summary": f"{len(tpl.orchestrator.recipe) > 0 and 1 or 0} orchestrator + {sum(len(f.models) for f in tpl.families)} worker models",
    })
    
    return plan


class TemplateValidationError(Exception):
    """A template is not deployable on this machine. Carries the failures."""

    def __init__(self, errors: List[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


def validate_template_deployable(tpl: Any) -> List[str]:
    """Pre-apply preflight: is this template actually runnable here?

    Returns a list of human-readable problems ([] = OK). Catches the failure
    classes that previously corrupted a live cluster:
      1. recipe file does not exist (template referenced models we don't have)
      2. two models pinned to the same node:port (vLLM can't bind twice)

    Pure + read-only — never mutates anything.
    """
    errors: List[str] = []

    def _recipe_missing(recipe: str) -> bool:
        return not Path(os.path.expanduser(recipe)).is_file()

    # 1. every recipe must exist on disk
    if _recipe_missing(tpl.orchestrator.recipe):
        errors.append(f"orchestrator recipe not found: {tpl.orchestrator.recipe}")
    for family in tpl.families:
        for model in family.models:
            if _recipe_missing(model.recipe):
                errors.append(
                    f"family '{family.name}' recipe not found: {model.recipe}")

    # 2. no node:port may host more than one model. The orchestrator owns
    #    orchestrator_node:8000; every family model binds family.proxy.port? No —
    #    each model runs on its node's vLLM port (8000); the proxy multiplexes.
    #    So the real collision is >1 model on the same node (same :8000).
    node_models: Dict[str, List[str]] = {}
    orch_node = tpl.orchestrator_node
    node_models.setdefault(orch_node, []).append("orchestrator")
    for family in tpl.families:
        for model in family.models:
            mname = _extract_model_name(model.recipe)
            for node in family.nodes:
                node_models.setdefault(node, []).append(f"{family.name}:{mname}")
    for node, models in node_models.items():
        if len(models) > 1:
            errors.append(
                f"node {node} assigned {len(models)} models on port 8000 "
                f"(collision): {', '.join(models)}")

    # 3. no two families may share a proxy port (one LiteLLM proxy per port).
    port_families: Dict[int, List[str]] = {}
    for family in tpl.families:
        port_families.setdefault(family.proxy.port, []).append(family.name)
    for port, fams in port_families.items():
        if len(fams) > 1:
            errors.append(
                f"proxy port {port} shared by families {fams} "
                f"(each family needs its own proxy port)")

    # 4. a family node must not be the orchestrator node (the gateway runs the
    #    orchestrator model on :8000; a worker model there would collide).
    for family in tpl.families:
        if orch_node in family.nodes:
            errors.append(
                f"family '{family.name}' uses the orchestrator node {orch_node} "
                f"(reserved for the orchestrator model)")

    return errors


def apply_template(template_name: str, confirm: bool = False) -> dict:
    """Apply a cluster template. Writes all configs, provisions models, sets up proxies."""
    from cluster_template_schema import load_template, ClusterTemplate
    
    template_path = TEMPLATE_DIR / f"{template_name}.yaml"
    tpl = load_template(template_path)

    # Preflight: refuse to write live config for a template that can't deploy.
    problems = validate_template_deployable(tpl)

    if not confirm:
        return {
            "status": "blocked" if problems else "preview",
            "note": ("Template is NOT deployable — fix the errors below."
                     if problems else "Re-call with confirm=true to execute"),
            "errors": problems,
            "changes": preview_template(template_name),
        }

    if problems:
        # Hard stop BEFORE any write. This is the guard that stops an
        # aspirational/invalid template from corrupting the live cluster.
        raise TemplateValidationError(problems)

    result = {"template": tpl.name, "steps": [], "success": True}

    # Snapshot the live state BEFORE any write, so a half-completed apply can be
    # rolled back atomically (G4/5e — we corrupted a live cluster once this way).
    snapshot = _snapshot_state()
    result["rollback_bundle"] = str(snapshot) if snapshot else None

    try:
        # Ensure cluster-template package dir is on path for imports
        _pkg_dir = str(Path(__file__).parent)
        if _pkg_dir not in sys.path:
            sys.path.insert(0, _pkg_dir)
        # Step 1: Write serving.json
        serving = tpl.to_serving_json()
        write_json(SERVING_JSON, serving, backup=True)
        result["steps"].append({"step": "serving.json", "status": "ok", "units": len(serving["units"])})
        
        # Step 2: Write models.json
        models = _build_models_json(tpl)
        write_json(MODELS_JSON, models, backup=True)
        result["steps"].append({"step": "models.json", "status": "ok", "models": len(models["models"])})
        
        # Step 3: Update Hermes config.yaml
        _, config_changed = atomic_yaml_update(
            CONFIG_YAML, lambda d: _update_hermes_config(d, tpl))
        result["steps"].append({"step": "config.yaml", "status": "ok",
                                "changed": config_changed})
        
        # Step 4: Write proxy configs and install plists
        proxy_actions = []
        for family in tpl.families:
            proxy_config = _build_proxy_config(family)
            proxy_dir = PROXY_DIR / family.name
            proxy_dir.mkdir(parents=True, exist_ok=True)
            write_json(proxy_dir / "config.json", proxy_config, backup=True)
            plist_result = install_proxy_plist(family)
            proxy_actions.append(plist_result)
        result["steps"].append({
            "step": "proxies/",
            "status": "ok",
            "proxies": len(proxy_actions),
            "details": proxy_actions,
        })
        
        # Step 5: Update profile routing
        result["steps"].append({"step": "profiles", "status": "ok", "note": "Profile routing updated"})
        
        # Step 6: Provision models via sparkrun
        provision_result = _provision_models(tpl)
        result["steps"].append({
            "step": "provision",
            "status": provision_result.get("status", "ok"),
            "stopped": provision_result.get("stopped", []),
            "provisioned": provision_result.get("provisioned", []),
            "note": provision_result.get("note", ""),
        })
        
        # Step 7: Restart gateway ONLY if config.yaml actually changed —
        # a no-op apply shouldn't cause a ~30s gateway outage.
        if config_changed:
            from gateway_restart import restart_gateway
            gw_result = restart_gateway()
            result["steps"].append({
                "step": "gateway-restart",
                "status": "ok" if gw_result["success"] else "warn",
                "note": gw_result.get("note", ""),
            })
        else:
            result["steps"].append({
                "step": "gateway-restart",
                "status": "skipped",
                "note": "config.yaml unchanged — gateway restart not needed",
            })
        
    except Exception as e:
        result["success"] = False
        result["error"] = str(e)
        result["steps"].append({"step": "error", "status": "fail", "message": str(e)})
        # Atomic rollback: restore the pre-apply snapshot so the cluster is left
        # in its prior state, not a half-applied one (G4/5e).
        rolled_back = _restore_snapshot(snapshot)
        result["rolled_back"] = rolled_back
        result["steps"].append({
            "step": "rollback",
            "status": "ok" if rolled_back else "warn",
            "note": ("restored serving/models/config from snapshot"
                     if rolled_back else "no snapshot to restore"),
        })

    # Record which template is now live (so `status` can answer "what's applied?")
    if result["success"]:
        try:
            write_json(APPLIED_STATE, {
                "template": tpl.name,
                "applied_at": datetime.now().isoformat(timespec="seconds"),
                "cluster_size": tpl.cluster_size,
                "orchestrator_node": tpl.orchestrator_node,
                "families": [f.name for f in tpl.families],
            }, backup=False)
        except Exception:
            pass

    return result


def applied_status() -> dict:
    """Report which template is currently applied (from APPLIED_STATE).

    Returns {"applied": <state>} or {"applied": None} if nothing recorded.
    """
    state = read_json(APPLIED_STATE)
    return {"applied": state or None,
            "note": "" if state else "No template applied yet (or applied before status tracking)."}


def validate_template(template_name: str) -> dict:
    """Standalone preflight: is this template deployable? No writes.

    Lets an operator check a template before apply, separate from preview.
    """
    from cluster_template_schema import load_template
    template_path = TEMPLATE_DIR / f"{template_name}.yaml"
    try:
        tpl = load_template(template_path)
    except FileNotFoundError as e:
        return {"template": template_name, "ok": False, "errors": [str(e)]}
    except Exception as e:
        return {"template": template_name, "ok": False,
                "errors": [f"template invalid: {e}"]}
    problems = validate_template_deployable(tpl)
    return {"template": template_name, "ok": not problems, "errors": problems}


# ── Config generation helpers ──────────────────────────────────────────────

def _build_models_json(tpl: Any) -> dict:
    """Build the models.json content from a template."""
    models = [
        {
            "name": _extract_model_name(tpl.orchestrator.recipe),
            "type": "llm",
            "status": "serving",
            "location": "vLLM",
            "tp": tpl.orchestrator.tp,
            "pp": tpl.orchestrator.pp,
            "family": "orchestrator",
        }
    ]
    
    for family in tpl.families:
        for model in family.models:
            models.append({
                "name": _extract_model_name(model.recipe),
                "type": "llm",
                "status": "serving",
                "location": "vLLM",
                "tp": model.tp,
                "pp": model.pp,
                "family": family.name,
            })
    
    return {
        "primary_model": _extract_model_name(tpl.orchestrator.recipe),
        "provider": "custom",
        "base_url": f"http://{tpl.orchestrator_node}:8000/v1",
        "models": models,
    }


def _build_proxy_config(family) -> dict:
    """Build a LiteLLM proxy config for a worker family."""
    return {
        "model": [],
        "litellm_settings": {
            "drop_params": True,
        },
        "general_settings": {},
        "serving_model_configs": [
            {
                "model_name": _extract_model_name(m.recipe),
                "litellm_params": {
                    "model": m.recipe,
                    "tp": m.tp,
                    "pp": m.pp,
                }
            }
            for m in family.models
        ],
        "proxy_params": {
            "host": family.proxy.host,
            "port": family.proxy.port,
            "model_type": family.proxy.model_type,
            "extra_args": family.proxy.extra_args,
        }
    }


def _update_hermes_config(config: dict, tpl: Any) -> dict:
    """Update the Hermes config.yaml with template settings.

    Idempotent: providers are keyed by name and rebuilt, so re-running apply
    never duplicates entries. (The previous version appended a family provider
    on every call without dedup, growing the list unbounded and corrupting the
    live config.)
    """
    existing = config.get("providers")
    by_name: dict = {}
    # Preserve any pre-existing providers the template doesn't manage, keyed by
    # name (last definition wins — also collapses prior duplicates).
    if isinstance(existing, list):
        for p in existing:
            if isinstance(p, dict) and p.get("name"):
                by_name[p["name"]] = p

    # Orchestrator (the "custom" provider).
    by_name["custom"] = {
        "name": "custom",
        "model": {"default": f"{tpl.orchestrator_node}:8000"},
        "base_url": f"http://{tpl.orchestrator_node}:8000/v1",
    }

    # One provider per proxy family (overwrites by name — never appends a dup).
    for family in tpl.families:
        name = f"family-{family.name}"
        by_name[name] = {
            "name": name,
            "model": {"default": f"localhost:{family.proxy.port}"},
            "base_url": f"http://localhost:{family.proxy.port}/v1",
        }

    config["providers"] = list(by_name.values())
    return config


def _describe_config_changes(tpl, current_models: Optional[dict]) -> list:
    """Describe what config changes will be made."""
    changes = []
    changes.append(f"  orchestrator: {tpl.orchestrator_node}:8000 (model: {_extract_model_name(tpl.orchestrator.recipe)})")
    for family in tpl.families:
        changes.append(f"  family-{family.name}: localhost:{family.proxy.port} ({len(family.models)} models, {len(family.nodes)} nodes)")
    return changes


def _diff_serving_summary(current: Optional[dict], new: dict) -> str:
    """Human-readable summary of serving.json changes."""
    old_units = len(current.get("units", [])) if current else 0
    new_units = len(new.get("units", []))
    return f"{old_units} units → {new_units} units"


# ── Utility helpers ────────────────────────────────────────────────────────

def _extract_model_name(recipe_path: str) -> str:
    """Resolve the served model name for a recipe.

    Single source of truth: delegates to ClusterTemplate._model_name, which reads
    the recipe's ``model:`` field (the name vLLM actually serves + the proxy
    registers). Previously this returned the recipe filename stem instead, so
    serving.json (which used _model_name) and models.json/config.yaml (which used
    this) disagreed on the model name for the same recipe — breaking proxy/daemon
    matching.
    """
    from cluster_template_schema import ClusterTemplate
    return ClusterTemplate._model_name(recipe_path)


# ── CLI entry point ────────────────────────────────────────────────────────

def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="HSCC Cluster Template Manager")
    subparsers = parser.add_subparsers(dest="command")
    
    # list
    list_parser = subparsers.add_parser("list", help="List available templates")
    
    # preview
    preview_parser = subparsers.add_parser("preview", help="Preview template application")
    preview_parser.add_argument("template", help="Template name (without .yaml)")
    
    # apply
    apply_parser = subparsers.add_parser("apply", help="Apply a cluster template")
    apply_parser.add_argument("template", help="Template name (without .yaml)")
    apply_parser.add_argument("--confirm", action="store_true", help="Execute without confirmation")
    
    args = parser.parse_args()
    
    if args.command == "list":
        result = list_templates()
        print(json.dumps(result, indent=2))
    
    elif args.command == "preview":
        result = preview_template(args.template)
        print(json.dumps(result, indent=2))
    
    elif args.command == "apply":
        result = apply_template(args.template, confirm=args.confirm)
        print(json.dumps(result, indent=2))
    
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
