#!/usr/bin/env python3
"""
Hermes Spark Cluster Control (HSCC) — Dynamic Model Provisioning

Manage the lifecycle of ML model containers on the DGX Spark cluster.
Auto-discover available sparkrun recipes, spin up containers, wire agents.

Usage: hscc-provision <command> [args]

Commands:
  recipes              List all available sparkrun recipes
  list                 Show all running containers with status
  run <recipe> [host]
                       Spin up a sparkrun container.
                       recipe: sparkrun recipe name (e.g. @official/qwen3.6-35b-a3b-fp8-vllm)
                       host: choose a specific host (default: first idle)
  stop <container_id>  Stop a running container
  assign <agent_id> <recipe> [host]
                       Spin up recipe (if needed) and wire agent to it
  unassign <agent_id>  Clear an agent's model assignment
  health [host_ip]     Check vLLM health endpoint
  cleanup              Stop containers whose recipes have no active agents
  status               Combined fleet + model status summary
"""

import sys
import json
import os
import subprocess
from datetime import datetime, timezone
from urllib.parse import urlparse

# ── Constants ──────────────────────────────────────────────────────────────

SPARKRUN = "sparkrun"
HSCC_DIR = os.path.expanduser("~/.hscc")
AGENTS_JSON = os.path.expanduser("~/.hscc/agents.json")
PROVISION_JSON = os.path.join(HSCC_DIR, "provision.json")
HERMES_CONFIG = os.path.expanduser("~/.hermes/config.yaml")
NAS_HOST = "192.0.2.10"
SSH_USER = "spark"


def get_hermes_inference_host():
    """Extract the host IP that hermes uses for its own inference backend."""
    try:
        in_model = False
        with open(HERMES_CONFIG) as f:
            for line in f:
                stripped = line.strip()
                if line[0:1] not in (" ", "\t") and stripped.endswith(":"):
                    in_model = stripped == "model:"
                if in_model and stripped.startswith("base_url:"):
                    url = stripped.split(":", 1)[1].strip()
                    if url:
                        return urlparse(url).hostname
        return None
    except (IOError, OSError):
        return None


# ── Helpers ────────────────────────────────────────────────────────────────

def run_cmd(args, timeout=30, as_json=False):
    """Run a command and return structured output."""
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        output = result.stdout.strip()
        error = result.stderr.strip()
        rc = result.returncode
        resp = {"success": rc == 0, "returncode": rc, "output": output}
        if error:
            resp["error"] = error
        if as_json and output:
            try:
                resp["json"] = json.loads(output)
            except json.JSONDecodeError:
                pass
        return resp
    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"command timed out after {timeout}s", "command": args}
    except FileNotFoundError:
        return {"success": False, "error": f"command not found: {args[0]}", "command": args}


def ssh_cmd(host, command):
    """Run a command via SSH on a cluster host."""
    return run_cmd(["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10",
                     f"{SSH_USER}@{host}", command], timeout=20)


def load_provision_state():
    """Load provisioning state (container → agent mappings)."""
    os.makedirs(HSCC_DIR, exist_ok=True)
    if not os.path.exists(PROVISION_JSON):
        return {"mappings": {}, "history": []}
    try:
        with open(PROVISION_JSON) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {"mappings": {}, "history": []}


def save_provision_state(state):
    """Save provisioning state."""
    os.makedirs(HSCC_DIR, exist_ok=True)
    with open(PROVISION_JSON, "w") as f:
        json.dump(state, f, indent=4)


def log_event(action, details):
    """Log a provisioning event."""
    state = load_provision_state()
    event = {"timestamp": datetime.now(timezone.utc).isoformat(), "action": action, **details}
    state["history"] = state.get("history", [])[-100:] + [event]
    save_provision_state(state)


def get_running_containers():
    """Get all running sparkrun containers from sparkrun status output."""
    result = run_cmd([SPARKRUN, "status"], timeout=15)
    containers = []
    job_name = None
    for line in result.get("output", "").split("\n"):
        line = line.strip()
        if not line or line.startswith("Idle") or line.startswith("Total:"):
            continue
        if line.startswith("Job:"):
            # Parse: "Job: @official/qwen3.6-35b-a3b-fp8-mtp-vllm  (tp=1, pp=1)  [1b6e77192e59]  (1 container(s))"
            parts = line.split()
            name = parts[1] if len(parts) > 1 else "?"
            container_id = tp = pp = "?"
            for p in parts:
                if p.startswith("[") and p.endswith("]"):
                    container_id = p.strip("[]")
                if "tp=" in p:
                    tp = p.strip("(),").split("=")[1]
                if "pp=" in p:
                    pp = p.strip("(),").split("=")[1]
            job_name = {"name": name, "container_id": container_id, "tp": tp, "pp": pp, "host": None, "uptime": None}
            containers.append(job_name)
        elif "solo" in line and "Up " in line:
            # Host line: "  solo       192.0.2.10  Up 37 minutes  sparkrun-eugr-vllm"
            parts = line.split()
            for i, p in enumerate(parts):
                if p == "solo" and i+1 < len(parts):
                    if containers:
                        containers[-1]["host"] = parts[i+1]
                    # Extract uptime
                    for j, q in enumerate(parts):
                        if q == "Up" and j+1 < len(parts) and j+2 < len(parts):
                            if containers:
                                containers[-1]["uptime"] = f"Up {parts[j+1]} {parts[j+2]}"
                            break
    return containers


def get_idle_hosts():
    """Get hosts with no running containers."""
    result = run_cmd([SPARKRUN, "status"], timeout=15)
    idle = []
    in_idle_section = False
    for line in result.get("output", "").split("\n"):
        line = line.strip()
        if "Idle hosts" in line:
            in_idle_section = True
            continue
        if in_idle_section and line and line.count(".") >= 3:
            idle.append(line)
    return idle


def recipe_to_model_path(recipe_name):
    """Convert a sparkrun recipe name to its target model string.
    
    e.g. @official/qwen3.6-35b-a3b-fp8-vllm → Qwen/Qwen3.6-35B-A3B-FP8
         /path/to/recipe.yaml → Qwen/Qwen3.6-35B-A3B-FP8
    """
    # If it's a local file path, use sparkrun show with the path
    if os.path.exists(recipe_name):
        result = run_cmd([SPARKRUN, "show", recipe_name], timeout=15)
    else:
        result = run_cmd([SPARKRUN, "show", recipe_name], timeout=15)
    
    if not result.get("success"):
        return None
    
    for line in result.get("output", "").split("\n"):
        if line.startswith("Model:"):
            return line.split(":", 1)[1].strip()
    return None


def model_to_nas_dir(model_name):
    """Convert a model name to its HF hub directory on NAS.
    
    e.g. Qwen/Qwen3.6-35B-A3B-FP8 → models--Qwen--Qwen3.6-35B-A3B-FP8
    """
    return f"models--{model_name.replace('/', '--', 1)}"


def check_model_on_nas(host, model_name):
    """Check if a model's HF cache exists on a given host's NAS mount.
    
    Returns True if the model directory exists with snapshots.
    """
    nas_dir = model_to_nas_dir(model_name)
    snapshot_path = f"/mnt/nas/hub/{nas_dir}/snapshots"
    
    result = ssh_cmd(host, f"ls '{snapshot_path}' 2>/dev/null")
    if result.get("success") and result.get("output"):
        # Check if snapshots dir has actual content (hash directories)
        hashes = [h.strip() for h in result["output"].split("\n") if h.strip()]
        return len(hashes) > 0
    
    # Also check with glob in case path differs
    result = ssh_cmd(host, f"find /mnt/nas/hub -maxdepth 2 -name '{model_name.replace('/', '_')}' -type d 2>/dev/null | head -1")
    if result.get("success") and result.get("output"):
        return True
    
    return False


def verify_recipe_on_nas(recipe_name, target_host):
    """Verify a recipe's model is already downloaded on the target host's NAS.
    
    Returns dict with verification result.
    """
    model_name = recipe_to_model_path(recipe_name)
    if not model_name:
        return {"verified": False, "error": f"Could not resolve model for recipe: {recipe_name}"}
    
    if check_model_on_nas(target_host, model_name):
        return {"verified": True, "model": model_name, "nas_path": f"/mnt/nas/hub/{model_to_nas_dir(model_name)}"}
    
    return {"verified": False, "model": model_name, "error": f"Model {model_name} not found on NAS at {target_host}"}


# ── Commands ───────────────────────────────────────────────────────────────

def cmd_recipes():
    """List all available sparkrun recipes with their target models.
    
    Also checks if the model is pre-downloaded on NAS (has_nas=true).
    """
    result = run_cmd([SPARKRUN, "list"], timeout=30)
    
    # Pick a known-good host for NAS verification
    test_host = "192.0.2.10"
    
    recipes = []
    lines = result.get("output", "").split("\n")
    for line in lines:
        if not line.startswith("@") or line.startswith("Name"):
            continue
        parts = line.split()
        if len(parts) >= 6:
            name = parts[0]
            runtime = parts[1]
            tp = parts[2]
            nodes = parts[3]
            gpu_mem = parts[4]
            # Model is everything after gpu_mem, but strip trailing registry name
            raw_model = " ".join(parts[5:])
            # Remove trailing registry name (official, eugr, sparkrun-transitional, etc.)
            known_registries = {"official", "eugr", "sparkrun-transitional", "community", "experimental", "testing", "atlas"}
            tokens = raw_model.split()
            model = " ".join(tokens[:-1]) if tokens[-1] in known_registries else raw_model
            
            # Check if model is on NAS
            has_nas = check_model_on_nas(test_host, model)
            
            recipes.append({
                "name": name,
                "runtime": runtime,
                "tp": tp,
                "nodes": nodes,
                "gpu_mem": gpu_mem,
                "model": model,
                "has_nas": has_nas,
            })
    
    print(json.dumps({
        "recipes": recipes, 
        "total": len(recipes),
        "on_nas": sum(1 for r in recipes if r.get("has_nas")),
    }, indent=2))


def cmd_list():
    """Show all running containers."""
    containers = get_running_containers()
    print(json.dumps({"containers": containers, "total": len(containers)}, indent=2))


def cmd_run(recipe_name, host=None, tp=1, pp=1):
    """Spin up a sparkrun container for the specified recipe.
    
    Prefers local recipes from ~/.sparkrun-local/recipes/
    Injects model_path from recipe if set (overrides {model} in command).
    """
    import tempfile
    import re
    
    # Resolve to local recipe path if available
    local_recipe = resolve_local_recipe(recipe_name)
    if local_recipe:
        recipe_name = local_recipe
        print(f"⠋ Using local recipe: {local_recipe}")
    
    # Check if this is a local recipe with model_path
    model_path_override = None
    if os.path.exists(recipe_name) and recipe_name.endswith(".yaml"):
        # Read recipe file and look for model_path
        with open(recipe_name) as f:
            content = f.read()
            # Look for model_path: field
            match = re.search(r'model_path:\s*(.+)', content)
            if match:
                model_path_override = match.group(1).strip()
                print(f"⠋ Found model_path in recipe: {model_path_override}")
    
    if not host:
        idle = get_idle_hosts()
        if not idle:
            print(json.dumps({"error": "No idle hosts available. Stop some containers first.", "idle_hosts": []}))
            return
        host = idle[0]
    
    # Prepare recipe file for sparkrun
    recipe_to_run = recipe_name
    tmp_recipe = None
    
    if model_path_override:
        # Read the recipe, modify command to use model_path instead of {model}
        print(f"⠋ Injecting local model path: {model_path_override}")
        
        with open(recipe_name) as f:
            content = f.read()
        
        # Replace {model} with model_path in command section
        # Find the command section (after "command:" key)
        cmd_start = content.find("command:")
        if cmd_start >= 0:
            # Replace {model} with the local path in the command
            cmd_section = content[cmd_start:]
            modified_cmd = re.sub(r'\{model\}', model_path_override, cmd_section)
            content = content[:cmd_start] + modified_cmd
        
        # Also replace the model field
        content = re.sub(r'^model:\s*.+$', f'model: {model_path_override}', content, flags=re.MULTILINE)
        
        # Write to temp file
        tmp_recipe = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, dir="/tmp")
        tmp_recipe.write(content)
        tmp_recipe.close()
        recipe_to_run = tmp_recipe.name
        print(f"  Modified recipe → {recipe_to_run}")
    
    # 1. Resolve model name from recipe
    model_name = recipe_to_model_path(recipe_name) if not model_path_override else model_path_override
    if not model_name:
        print(json.dumps({"error": f"Could not resolve model for recipe: {recipe_name}"}))
        return
    
    if model_path_override:
        # When using model_path_override, skip NAS verification
        # The model_path is already a direct path to the snapshot
        print(f"⠋ Skipping NAS verification (using local model path)")
        verification = True
    else:
        verification = check_model_on_nas(host, model_name)
    
    if not verification:
        print(json.dumps({
            "error": f"Model {model_name} NOT FOUND on NAS at {host}",
            "detail": "This recipe requires a model download — use only recipes with pre-downloaded models",
            "model": model_name,
            "host": host,
        }))
        return
    
    print(f"⠋ Spinning up '{recipe_name}' on {host}...")
    print(f"  Model: {model_name}")
    print(f"  TP: {tp}, PP: {pp}")
    
    # 3. Run with HF_HUB_OFFLINE=1 to prevent any downloads
    import os as _os
    env = _os.environ.copy()
    env["HF_HUB_OFFLINE"] = "1"
    env["TRANSFORMERS_OFFLINE"] = "1"
    env["HF_HUB_DISABLE_TELEMETRY"] = "1"
    
    # --cluster makes the recipe inherit the cluster cache_dir (/mnt/nas),
    # bind-mounting the NAS HF cache so the offline-downloaded model is served
    # instead of a local copy. --hosts still pins the specific worker host.
    cmd = [SPARKRUN, "run", recipe_to_run,
           f"--tp={tp}", f"--pp={pp}",
           "--cluster=hscc",
           f"--hosts={host}",
           "--no-follow"]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, env=env)
        output = result.stdout.strip()
        rc = result.returncode
        error = result.stderr.strip()
        
        resp = {"success": rc == 0, "returncode": rc, "output": output}
        if error:
            resp["error"] = error
        if rc != 0:
            print(json.dumps(resp))
            return
            
        log_event("model_spun_up", {"recipe": recipe_name, "host": host, "output": output[:200]})
        print(json.dumps({"success": True, "output": output, "host": host}))
    except subprocess.TimeoutExpired:
        print(json.dumps({"error": "Command timed out after 300s"}))
    except Exception as e:
        print(json.dumps({"error": str(e)}))


def resolve_local_recipe(recipe_name):
    """Resolve a recipe name to a local file path if available.
    
    Checks ~/.sparkrun-local/recipes/ for matching recipes.
    Returns the local path if found, None otherwise.
    """
    # Convert recipe name to potential local file name
    # @official/qwen3.6-35b-a3b-fp8-vllm -> qwen3.6-35b-a3b-fp8-vllm.yaml
    # @sparkrun-transitional/qwen3.5-35b-a3b-fp8-sglang -> qwen3.5-35b-a3b-fp8-sglang.yaml
    base_name = recipe_name.replace("@", "").replace("/", "-") + ".yaml"
    # Strip registry prefix if present
    for prefix in ["official-", "eugr-", "sparkrun-transitional-", "sparkrun-testing-", "community-", "experimental-", "atlas-"]:
        if base_name.startswith(prefix):
            base_name = base_name[len(prefix):]
            break
    base_name = base_name + ".yaml" if not base_name.endswith(".yaml") else base_name
    
    # Prefer local-fixed (carries chat-template/mods fixes) over the stock
    # official/transitional variants, so a plain recipe name resolves to the
    # canonical fixed recipe everywhere provisioning runs.
    for sub_dir in ["local-fixed", "official", "transitional"]:
        local_path = f"{LOCAL_REGISTRY_DIR}/recipes/{sub_dir}/{base_name}"
        if os.path.exists(local_path):
            return local_path
    
    return None


# ── Registry Management ────────────────────────────────────────────────────

LOCAL_REGISTRY_DIR = os.path.expanduser("~/.sparkrun-local")
LOCAL_REGISTRIES = {
    "official": {"dir": "recipes/official", "source": "@official", "description": "Official Spark-Arena recipes"},
    "transitional": {"dir": "recipes/transitional", "source": "@sparkrun-transitional", "description": "Sparkrun transitional recipes"},
}


def cmd_registry(cmd, *args):
    """Manage local hscc recipe registry."""
    if cmd == "list":
        cmd_registry_list()
    elif cmd == "sync":
        cmd_registry_sync()
    elif cmd == "add":
        cmd_registry_add(args[0])
    elif cmd == "remove":
        cmd_registry_remove(args[0])
    else:
        print(json.dumps({"error": f"Unknown registry command: {cmd}. Use: list, sync, add, remove"}))


def cmd_registry_list():
    """List all local recipes with their details."""
    result = {"registries": {}, "total": 0, "ready": 0}
    test_host = "192.0.2.10"
    
    for reg_name, reg_info in LOCAL_REGISTRIES.items():
        recipes = []
        reg_path = os.path.join(LOCAL_REGISTRY_DIR, reg_info["dir"])
        
        if not os.path.exists(reg_path):
            result["registries"][reg_name] = {"path": reg_path, "recipes": []}
            continue
            
        for yaml_file in os.listdir(reg_path):
            if not yaml_file.endswith(".yaml"):
                continue
            
            file_path = os.path.join(reg_path, yaml_file)
            meta = get_recipe_metadata(yaml_file, file_path)
            meta["has_nas"] = check_model_on_nas(test_host, meta.get("model", ""))
            recipes.append(meta)
            
        result["registries"][reg_name] = {
            "path": reg_path,
            "description": reg_info["description"],
            "recipes": recipes,
            "ready": sum(1 for r in recipes if r.get("has_nas")),
        }
        result["total"] += len(recipes)
        result["ready"] += sum(1 for r in recipes if r.get("has_nas"))
    
    print(json.dumps(result, indent=2))


def get_recipe_metadata(yaml_file, file_path):
    """Extract metadata from a local recipe yaml file."""
    try:
        # Use sparkrun show to get the recipe details
        result = run_cmd([SPARKRUN, "show", file_path], timeout=15)
        if not result.get("success"):
            return {"name": yaml_file.replace(".yaml", ""), "model": "unknown", "runtime": "unknown", "description": "", "container": ""}
        
        meta = {
            "name": yaml_file.replace(".yaml", ""),
            "model": "",
            "runtime": "",
            "description": "",
            "container": "",
        }
        
        for line in result.get("output", "").split("\n"):
            if line.startswith("Model:"):
                meta["model"] = line.split(":", 1)[1].strip()
            elif line.startswith("Runtime:"):
                meta["runtime"] = line.split(":", 1)[1].strip()
            elif line.startswith("Description:"):
                meta["description"] = line.split(":", 1)[1].strip()
            elif line.startswith("Container:"):
                meta["container"] = line.split(":", 1)[1].strip()
        
        return meta
    except Exception as e:
        return {"name": yaml_file.replace(".yaml", ""), "model": "unknown", "runtime": "unknown", "description": str(e)}


def cmd_registry_sync():
    """Sync local recipes from remote sparkrun registries."""
    print("Syncing local recipes from remote registries...")
    
    synced = {"official": 0, "transitional": 0}
    test_host = "192.0.2.10"
    
    # Get remote recipes
    result = run_cmd([SPARKRUN, "list"], timeout=30)
    lines = result.get("output", "").split("\n")
    
    for line in lines:
        if not line.startswith("@") or line.startswith("Name"):
            continue
        parts = line.split()
        if len(parts) < 6:
            continue
            
        name = parts[0]
        model = " ".join(parts[5:].split()[:-1])
        
        # Determine if this recipe has a model on NAS
        has_nas = check_model_on_nas(test_host, model)
        
        # Skip recipes without models on NAS
        if not has_nas:
            continue
            
        # Determine which registry this belongs to
        if name.startswith("@official"):
            local_dir = "official"
        elif name.startswith("@sparkrun-transitional"):
            local_dir = "transitional"
        else:
            continue
            
        # Skip if already local
        existing = resolve_local_recipe(name)
        if existing:
            print(f"  ✓ {name} (already local)")
            continue
            
        # Download from remote registry
        print(f"  ⠋ Downloading {name}...")
        remote_result = run_cmd([SPARKRUN, "show", name], timeout=15)
        if not remote_result.get("success"):
            print(f"  ✗ Failed to show {name}")
            continue
            
        # Use sparkrun to get the recipe file
        # sparkrun doesn't export recipes directly, so we need to clone the remote registry
        try:
            import shutil
            from pathlib import Path
            
            reg_url = get_remote_registry_url(name)
            if not reg_url:
                continue
                
            temp_dir = Path(tempfile.gettempdir()) / f"sparkrun-registry-{name}"
            if not temp_dir.exists():
                subprocess.run(["git", "clone", "--depth", "1", reg_url, str(temp_dir)], 
                             capture_output=True, timeout=60)
                
            # Find the recipe file
            yaml_file = find_recipe_in_remote(str(temp_dir), name)
            if yaml_file and os.path.exists(yaml_file):
                dest = os.path.join(LOCAL_REGISTRY_DIR, LOCAL_REGISTRIES[local_dir]["dir"], 
                                  yaml_file.replace(".yaml", "").replace("/", "-") + ".yaml")
                shutil.copy2(yaml_file, dest)
                synced[local_dir] += 1
                print(f"  ✓ Synced {name} → {dest}")
            else:
                print(f"  ✗ Recipe file not found for {name}")
                
        except Exception as e:
            print(f"  ✗ Error syncing {name}: {e}")
            
    print(f"\nSync complete: {synced['official']} official, {synced['transitional']} transitional")
    
    # Commit the changes
    subprocess.run(["git", "-C", LOCAL_REGISTRY_DIR, "add", "-A"], capture_output=True)
    subprocess.run(["git", "-C", LOCAL_REGISTRY_DIR, "commit", "-m", f"Sync: +{sum(synced.values())} recipes"], 
                 capture_output=True)


def get_remote_registry_url(recipe_name):
    """Get the git URL for a recipe's remote registry."""
    urls = {
        "@official": "https://github.com/spark-arena/recipe-registry.git",
        "@sparkrun-transitional": "https://github.com/dbotwinick/sparkrun-recipe-registry.git",
        "@eugr": "https://github.com/eugr/spark-vllm-docker.git",
    }
    
    for prefix, url in urls.items():
        if recipe_name.startswith(prefix):
            return url
    return None


def find_recipe_in_remote(temp_dir, recipe_name):
    """Find a recipe yaml file in a cloned remote registry."""
    # Convert recipe name to potential file path
    name_parts = recipe_name.replace("@", "").split("/")
    if len(name_parts) >= 2:
        model_name = name_parts[1].replace("-", "/")
        # Search for matching yaml files
        for root, dirs, files in os.walk(temp_dir):
            for f in files:
                if f.endswith(".yaml") and recipe_name.replace("@", "").replace("/", "-") in f:
                    return os.path.join(root, f)
    return None


def cmd_registry_add(source):
    """Add a new recipe source to the local registry."""
    print(f"Adding source: {source}")
    # TODO: Implement adding new remote registries


def cmd_registry_remove(source):
    """Remove a recipe source from the local registry."""
    print(f"Removing source: {source}")
    # TODO: Implement removing recipes from a source


def cmd_stop(container_id, force=False):
    """Stop a running container. Refuses to stop hermes' own backend unless --force."""
    if not force:
        self_host = get_hermes_inference_host()
        if self_host:
            containers = get_running_containers()
            for c in containers:
                if c["container_id"] == container_id and c.get("host") == self_host:
                    print(json.dumps({"error": f"REFUSED: container {container_id} is on {self_host} which serves hermes' own inference. Use 'stop {container_id} --force' to override."}))
                    return
    result = run_cmd([SPARKRUN, "stop", container_id], timeout=30)
    log_event("model_stopped", {"container_id": container_id, "result": result.get("output", "")})
    print(json.dumps({"success": result["returncode"] == 0, "output": result.get("output", "")}))


# ── Main Dispatch ──────────────────────────────────────────────────────────



def cmd_assign(agent_id, recipe_name, host=None):
    """Wire an agent to a recipe — spin up container if needed."""
    agents_data = {}
    if os.path.exists(AGENTS_JSON):
        with open(AGENTS_JSON) as f:
            agents_data = json.load(f)

    agent = None
    agent_idx = None
    for i, a in enumerate(agents_data.get("agents", [])):
        if a["id"] == agent_id:
            agent = a
            agent_idx = i
            break

    if agent is None:
        print(json.dumps({"error": f"Agent not found: {agent_id}"}))
        return

    # Check if recipe is already running
    containers = get_running_containers()
    running = [c for c in containers if c["name"] == recipe_name]

    if not running:
        idle = get_idle_hosts()
        target_host = host or (idle[0] if idle else None)
        if not target_host:
            print(json.dumps({"error": "No idle hosts. Use 'hscc-provision run' first."}))
            return
        cmd_run(recipe_name, target_host)
        # Re-check containers
        containers = get_running_containers()
        running = [c for c in containers if c["name"] == recipe_name]

    if not running:
        print(json.dumps({"error": "Failed to start container"}))
        return

    container = running[0]
    container_host = container["host"]

    # Wire agent to recipe endpoint
    agents_data["agents"][agent_idx]["model"] = recipe_name
    agents_data["agents"][agent_idx]["status"] = "idle"

    with open(AGENTS_JSON, "w") as f:
        json.dump(agents_data, f, indent=4)

    # Update provision state
    state = load_provision_state()
    state["mappings"][agent_id] = {
        "recipe": recipe_name,
        "container_id": container["container_id"],
        "host": container_host,
        "wired_at": datetime.now(timezone.utc).isoformat(),
    }
    save_provision_state(state)

    log_event("agent_assigned", {
        "agent_id": agent_id,
        "recipe": recipe_name,
        "container_host": container_host,
        "container_id": container["container_id"],
    })

    print(json.dumps({
        "success": True,
        "agent_id": agent_id,
        "recipe": recipe_name,
        "container": container["container_id"],
        "host": container_host,
    }, indent=2))


def cmd_unassign(agent_id):
    """Clear an agent's model assignment."""
    agents_data = {}
    if os.path.exists(AGENTS_JSON):
        with open(AGENTS_JSON) as f:
            agents_data = json.load(f)

    for i, a in enumerate(agents_data.get("agents", [])):
        if a["id"] == agent_id:
            a["model"] = "auto"
            a["endpoint"] = ""
            a["status"] = "idle"
            with open(AGENTS_JSON, "w") as f:
                json.dump(agents_data, f, indent=4)

            state = load_provision_state()
            state["mappings"].pop(agent_id, None)
            save_provision_state(state)

            log_event("agent_unassigned", {"agent_id": agent_id})
            print(json.dumps({"success": True, "agent_id": agent_id, "model": "auto"}))
            return

    print(json.dumps({"error": f"Agent not found: {agent_id}"}))


def check_health(host_ip):
    """Check vLLM health on a specific host."""
    endpoints = [
        ("http://localhost:8000/health", 8000),
        ("http://localhost:8001/health", 8001),
        ("http://localhost:8080/health", 8080),
        ("http://localhost:8000/v1/models", 8000),
        ("http://localhost:8081/v1/models", 8081),
    ]
    for url, port in endpoints:
        result = ssh_cmd(host_ip, f"curl -s --max-time 5 '{url}' 2>/dev/null")
        if result.get("success") and result.get("output"):
            return {"host": host_ip, "port": port, "healthy": True, "response": result["output"][:200]}
    return {"host": host_ip, "healthy": False, "message": "No vLLM responding on any port"}


def cmd_health(host_ip):
    """Check vLLM health endpoint."""
    if not host_ip:
        containers = get_running_containers()
        if not containers:
            print(json.dumps({"message": "No running containers"}))
            return
        results = {}
        for c in containers:
            if c["host"]:
                results[c["host"]] = check_health(c["host"])
        print(json.dumps(results, indent=2))
        return
    print(json.dumps(check_health(host_ip), indent=2))


def cmd_cleanup():
    """Stop containers whose recipes have no active agent assignments."""
    state = load_provision_state()
    mappings = state.get("mappings", {})
    active_recipes = set(m["recipe"] for m in mappings.values())
    self_host = get_hermes_inference_host()

    containers = get_running_containers()
    stopped = 0
    skipped_self = 0
    for c in containers:
        if c["name"] not in active_recipes:
            if self_host and c.get("host") == self_host:
                print(f"  SKIPPED (hermes inference backend): {c['container_id']} ({c['name']}) on {self_host}")
                skipped_self += 1
                continue
            print(f"  Stopping orphaned container: {c['container_id']} ({c['name']})")
            run_cmd([SPARKRUN, "stop", c["container_id"]], timeout=30)
            stopped += 1

    print(json.dumps({"stopped": stopped, "skipped_self": skipped_self, "reason": "no active agent assignments"}))
    log_event("cleanup", {"stopped": stopped, "skipped_self": skipped_self})


def cmd_status():
    """Combined fleet + model + provisioning status."""
    agents_data = {}
    if os.path.exists(AGENTS_JSON):
        with open(AGENTS_JSON) as f:
            agents_data = json.load(f)

    agents = agents_data.get("agents", [])
    total = len(agents)
    assigned = sum(1 for a in agents if a.get("model") != "auto")
    auto = total - assigned
    idle = sum(1 for a in agents if a.get("status") == "idle")

    containers = get_running_containers()
    idle_hosts = get_idle_hosts()
    state = load_provision_state()
    active_assignments = len(state.get("mappings", {}))

    print(f"╔══════════════════════════════════════════════╗")
    print(f"║       HSCC Provisioning Status               ║")
    print(f"╠══════════════════════════════════════════════╣")
    print(f"║ Agents: {total} total | {assigned} assigned | {auto} auto     ║")
    print(f"║ Idle: {idle} | Assigned: {assigned}                        ║")
    print(f"╠══════════════════════════════════════════════╣")
    recipe_result = run_cmd([SPARKRUN, "list"], timeout=15)
    total_recipes = 0
    if recipe_result.get("success") and recipe_result.get("output"):
        total_recipes = sum(1 for l in recipe_result["output"].split("\n") if l.startswith("@"))
    print(f"║ Recipes: {total_recipes} available in sparkrun        ║")
    print(f"║ Running: {len(containers)} container(s)                   ║")
    for c in containers:
        print(f"║   • {c['name']:<30s} host={c['host']:<14s} id={c['container_id']:<10s}      ║")
    print(f"╠══════════════════════════════════════════════╣")
    print(f"║ Idle hosts: {len(idle_hosts)} ({', '.join(idle_hosts[:3])})             ║")
    print(f"╚══════════════════════════════════════════════╝")

    if active_assignments:
        print(f"\nActive agent-recipe assignments ({active_assignments}):")
        for aid, m in state["mappings"].items():
            print(f"  {aid} → {m['recipe']} @ {m['host']}")


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    cmd = sys.argv[1].lower()
    commands = {
        "recipes": cmd_recipes,
        "list": cmd_list,
        "run": lambda: cmd_run(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None) if len(sys.argv) > 2 else print(json.dumps({"error": "Usage: hscc-provision run <recipe_name> [host]"})),
        "stop": lambda: cmd_stop(sys.argv[2], "--force" in sys.argv) if len(sys.argv) > 2 else print(json.dumps({"error": "Usage: hscc-provision stop <container_id> [--force]"})),
        "assign": lambda: cmd_assign(sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else None) if len(sys.argv) > 3 else print(json.dumps({"error": "Usage: hscc-provision assign <agent_id> <recipe> [host]"})),
        "unassign": lambda: cmd_unassign(sys.argv[2]) if len(sys.argv) > 2 else print(json.dumps({"error": "Usage: hscc-provision unassign <agent_id>"})),
        "health": lambda: cmd_health(sys.argv[2]) if len(sys.argv) > 2 else cmd_health(None),
        "cleanup": cmd_cleanup,
        "status": cmd_status,
        "registry": lambda: cmd_registry(sys.argv[2], *sys.argv[3:]) if len(sys.argv) > 2 else print(json.dumps({"error": "Usage: hscc-provision registry <list|sync|add|remove> [args]"})),
    }

    if cmd not in commands:
        print(json.dumps({"error": f"Unknown command: {cmd}. Available: {list(commands.keys())}"}))
        sys.exit(1)

    commands[cmd]()


if __name__ == "__main__":
    main()
