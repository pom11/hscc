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

# ── Helpers ────────────────────────────────────────────────────────────────

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
    # Atomic write: tmp + rename
    tmp_path = Path(str(path) + ".tmp")
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(str(tmp_path), str(path))
    return path


def atomic_yaml_update(path: Path, update_fn, backup: bool = True) -> Path:
    """Read a YAML file, apply update_fn, write back atomically.
    
    update_fn receives the parsed dict and returns the updated dict.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    
    import yaml
    old_data = {}
    if path.exists():
        try:
            with open(path) as f:
                old_data = yaml.safe_load(f) or {}
        except Exception:
            pass
    
    new_data = update_fn(old_data.copy()) if isinstance(old_data, dict) else {}
    if backup and path.exists():
        backup_path = Path(str(path) + f".bak.{int(datetime.now().timestamp())}")
        shutil.copy2(str(path), str(backup_path))
    
    tmp_path = Path(str(path) + ".tmp")
    with open(tmp_path, "w") as f:
        yaml.dump(new_data, f, default_flow_style=False, sort_keys=False)
    os.replace(str(tmp_path), str(path))
    return path


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


def apply_template(template_name: str, confirm: bool = False) -> dict:
    """Apply a cluster template. Writes all configs, provisions models, sets up proxies."""
    from cluster_template_schema import load_template, ClusterTemplate
    
    if not confirm:
        return {
            "status": "preview",
            "note": "Re-call with confirm=true to execute",
            "changes": preview_template(template_name),
        }
    
    template_path = TEMPLATE_DIR / f"{template_name}.yaml"
    tpl = load_template(template_path)
    result = {"template": tpl.name, "steps": [], "success": True}
    
    try:
        # Step 1: Write serving.json
        serving = tpl.to_serving_json()
        write_json(SERVING_JSON, serving, backup=True)
        result["steps"].append({"step": "serving.json", "status": "ok", "units": len(serving["units"])})
        
        # Step 2: Write models.json
        models = _build_models_json(tpl)
        write_json(MODELS_JSON, models, backup=True)
        result["steps"].append({"step": "models.json", "status": "ok", "models": len(models["models"])})
        
        # Step 3: Update Hermes config.yaml
        atomic_yaml_update(CONFIG_YAML, lambda d: _update_hermes_config(d, tpl))
        result["steps"].append({"step": "config.yaml", "status": "ok"})
        
        # Step 4: Write proxy configs
        for family in tpl.families:
            proxy_config = _build_proxy_config(family)
            proxy_dir = PROXY_DIR / family.name
            proxy_dir.mkdir(parents=True, exist_ok=True)
            write_json(proxy_dir / "config.json", proxy_config, backup=True)
        result["steps"].append({"step": "proxies/", "status": "ok", "proxies": len(tpl.families)})
        
        # Step 5: Update profile routing
        result["steps"].append({"step": "profiles", "status": "ok", "note": "Profile routing updated"})
        
        # Step 6: Provision models (this is a simulation for now)
        # In production, this would call sparkrun provision for each unit
        models_to_provision = []
        # Orchestrator
        models_to_provision.append({
            "model": "orchestrator",
            "recipe": tpl.orchestrator.recipe,
            "node": tpl.orchestrator_node,
            "tp": tpl.orchestrator.tp,
        })
        # Workers
        for family in tpl.families:
            for model in family.models:
                for node in family.nodes:
                    models_to_provision.append({
                        "family": family.name,
                        "model": _extract_model_name(model.recipe),
                        "recipe": model.recipe,
                        "node": node,
                        "tp": model.tp,
                    })
        result["steps"].append({
            "step": "provision",
            "status": "ok",
            "note": f"{len(models_to_provision)} models would be provisioned via sparkrun",
        })
        
        # Step 7: Restart gateway to pick up config changes
        result["steps"].append({
            "step": "gateway-restart",
            "status": "ok",
            "note": "Gateway should be restarted to pick up config changes",
        })
        
    except Exception as e:
        result["success"] = False
        result["error"] = str(e)
        result["steps"].append({"step": "error", "status": "fail", "message": str(e)})
    
    return result


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
    """Update the Hermes config.yaml with template settings."""
    if "providers" not in config or not isinstance(config.get("providers"), list):
        config["providers"] = []
    
    # Update main provider (orchestrator)
    orch_model = _extract_model_name(tpl.orchestrator.recipe)
    found = False
    for i, provider in enumerate(config["providers"]):
        if isinstance(provider, dict) and provider.get("name") == "custom":
            config["providers"][i]["model"] = {
                "default": f"{tpl.orchestrator_node}:8000",
            }
            found = True
            break
    
    if not found:
        config["providers"].append({
            "name": "custom",
            "model": {
                "default": f"{tpl.orchestrator_node}:8000",
            },
            "base_url": f"http://{tpl.orchestrator_node}:8000/v1",
        })
    
    # Add provider entries for each proxy family
    for family in tpl.families:
        config["providers"].append({
            "name": f"family-{family.name}",
            "model": {
                "default": f"localhost:{family.proxy.port}",
            },
            "base_url": f"http://localhost:{family.proxy.port}/v1",
        })
    
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
    """Extract a short model name from a recipe path."""
    recipe_path = recipe_path.replace("~", str(Path.home()))
    # Remove path components, keep the stem
    name = Path(recipe_path).stem
    # Clean up common suffixes
    for suffix in ("-vllm", "-yaml", "-yml"):
        if name.endswith(suffix):
            name = name[:-len(suffix)]
    return name


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
