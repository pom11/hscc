#!/usr/bin/env python3
"""onboard.py — bring a new model online across the DGX Spark cluster.

End-to-end new-model onboarding: validate/scaffold a sparkrun recipe, populate
the offline NAS->node-local HF cache, wire every HSCC config layer, launch vLLM
on each serving node, and verify health + a coherent completion.

Topology is READ from ~/.hscc/config.json (never hardcoded) so it tracks the
real cluster (gateway + workers + NAS + ssh_user + port).

Phases (each is a subcommand; `all` runs them in order):
    plan    <model> <recipe>          # dry-run: show topology, cache, config diff
    cache   <model>                   # ensure NAS + offline NAS->node-local cache
    wire    <model> <recipe> --yes    # backup + write all HSCC config layers
    launch  <recipe>          --yes   # sparkrun run on each serving node
    verify  <model>                   # health + completion on all serving nodes
    all     <model> <recipe> --yes    # plan -> cache -> wire -> launch -> verify

Safety:
    - Mutating phases (wire/launch/all) require --yes.
    - wire backs up ~/.hscc + hermes config + worker profiles first.
    - Cache sync is additive (rsync -a, no --delete); NEVER rm -rf.
    - hermes config.yaml is edited line-targeted (model.default only) so the
      file's secrets are never rewritten/printed.

Pitfalls honored (see references/nas-cache.md):
    - macOS rsync has no remote->remote: run rsync FROM the NAS-mount host (gateway).
    - System python3 has no `yaml` module: YAML edits are line-based.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

HSCC_HOME = os.path.expanduser(os.environ.get("HSCC_HOME", "~/.hscc"))
HERMES_HOME = os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes"))
HERMES_CONFIG = os.path.join(HERMES_HOME, "config.yaml")
PROFILES_DIR = os.path.join(HERMES_HOME, "profiles")
SPARKRUN_DIR = os.path.expanduser("~/sparkrun")
SPARKRUN_BIN = os.path.join(SPARKRUN_DIR, ".venv", "bin", "sparkrun")
SSH_KEY = os.path.expanduser("~/.ssh/id_ed25519")

R = "\033[0;31m"; G = "\033[0;32m"; Y = "\033[0;33m"; B = "\033[0;34m"; NC = "\033[0m"


def c(color, msg):
    return f"{color}{msg}{NC}"


def die(msg):
    print(c(R, f"ERROR: {msg}"))
    sys.exit(1)


# ── Topology ────────────────────────────────────────────────────────────────

def load_topology():
    """Read cluster topology from ~/.hscc/config.json (single source of truth)."""
    path = os.path.join(HSCC_HOME, "config.json")
    if not os.path.exists(path):
        die(f"missing {path} — is HSCC initialized?")
    cfg = json.load(open(path))
    gw = cfg["gateway_ip"]
    return {
        "gateway": gw,
        "workers": list(cfg.get("workers", [])),
        "nas_ip": cfg.get("nas_ip"),
        "ssh_user": cfg.get("ssh_user", "spark"),
        "port": int(cfg.get("vllm_port", 8000)),
        # The NAS is mounted at /mnt/nas on the gateway; rsync source host = gateway.
        "nas_mount_host": gw,
        # serving nodes = gateway (orchestrator) + workers
        "serving_nodes": [gw] + list(cfg.get("workers", [])),
    }


def ssh(user, host, cmd, timeout=30):
    """Run an SSH command; return (stdout, ok)."""
    try:
        r = subprocess.run(
            ["ssh", "-i", SSH_KEY, "-o", "StrictHostKeyChecking=no",
             "-o", "UserKnownHostsFile=/dev/null", "-o", f"ConnectTimeout={min(timeout,15)}",
             f"{user}@{host}", cmd],
            capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.returncode == 0
    except subprocess.TimeoutExpired:
        return "", False
    except Exception as e:
        return str(e), False


# ── Model / cache helpers ─────────────────────────────────────────────────────

def hf_repo(model_id):
    """nvidia/Qwen3.6-35B-A3B-NVFP4 -> models--nvidia--Qwen3.6-35B-A3B-NVFP4"""
    return "models--" + model_id.replace("/", "--")


def nas_ref(topo, model_id):
    """refs/main hash for the model on NAS, or '' if absent."""
    repo = hf_repo(model_id)
    out, ok = ssh(topo["ssh_user"], topo["nas_mount_host"],
                  f"cat /mnt/nas/hub/{repo}/refs/main 2>/dev/null")
    return out if ok else ""


def node_ref(topo, host, model_id):
    """refs/main hash for the model in a node's local cache, or '' if absent."""
    repo = hf_repo(model_id)
    out, ok = ssh(topo["ssh_user"], host,
                  f"cat /home/spark/.cache/huggingface/hub/{repo}/refs/main 2>/dev/null")
    return out if ok else ""


def nas_snapshot_files(topo, model_id, ref):
    """Count of files in the NAS snapshot dir for completeness comparison."""
    repo = hf_repo(model_id)
    out, ok = ssh(topo["ssh_user"], topo["nas_mount_host"],
                  f"find /mnt/nas/hub/{repo}/snapshots/{ref}/ -type f 2>/dev/null | wc -l")
    return int(out) if ok and out.isdigit() else 0


def node_snapshot_files(topo, host, model_id, ref):
    repo = hf_repo(model_id)
    out, ok = ssh(topo["ssh_user"], host,
                  f"find /home/spark/.cache/huggingface/hub/{repo}/snapshots/{ref}/ -type f 2>/dev/null | wc -l")
    return int(out) if ok and out.isdigit() else 0


def rsync_nas_to_node(topo, host, model_id):
    """Additive rsync of the whole repo dir from NAS-mount host to a node.

    Runs FROM the gateway (which has /mnt/nas) because macOS rsync cannot do
    remote->remote. Uses -a (preserves layout) WITHOUT --delete (never removes
    files on the node). Creates the parent dir first.
    """
    repo = hf_repo(model_id)
    user = topo["ssh_user"]
    src = f"/mnt/nas/hub/{repo}/"
    dst = f"/home/spark/.cache/huggingface/hub/{repo}/"
    ssh(user, host, f"mkdir -p {dst}")
    inner = (f'rsync -a -e "ssh -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=no '
             f'-o UserKnownHostsFile=/dev/null" {src} {user}@{host}:{dst}')
    try:
        r = subprocess.run(
            ["ssh", "-i", SSH_KEY, "-o", "StrictHostKeyChecking=no",
             "-o", "UserKnownHostsFile=/dev/null", f"{user}@{topo['nas_mount_host']}", inner],
            capture_output=True, text=True, timeout=1800)
        return r.returncode == 0, r.stderr.strip()[:300]
    except subprocess.TimeoutExpired:
        return False, "rsync timed out (30m)"


def node_cache_ready(topo, host, model_id, nref, nfiles):
    """True if a node's local cache matches NAS (same ref hash, same snapshot file count)."""
    if node_ref(topo, host, model_id) != nref or not nref:
        return False
    return node_snapshot_files(topo, host, model_id, nref) >= nfiles > 0


# ── Recipe helpers ────────────────────────────────────────────────────────────

def recipe_model(path):
    if not os.path.exists(path):
        return None
    for line in open(path):
        if line.startswith("model:"):
            return line.split(":", 1)[1].strip()
    return None


CANONICAL_RECIPE = os.path.expanduser(
    "~/.sparkrun-local/recipes/local-fixed/qwen3.6-35b-a3b-nvfp4-vllm.yaml")


def scaffold_recipe(path, model_id):
    """Create a starter recipe from the canonical NVFP4 recipe with the model
    swapped and a TODO banner. Does NOT auto-launch — human must review."""
    if not os.path.exists(CANONICAL_RECIPE):
        die(f"no canonical recipe to scaffold from at {CANONICAL_RECIPE}")
    text = open(CANONICAL_RECIPE).read()
    text = re.sub(r"^model:.*$", f"model: {model_id}", text, count=1, flags=re.M)
    banner = (f"# SCAFFOLDED by onboard.py for {model_id} on "
              f"{datetime.now().isoformat(timespec='seconds')}\n"
              f"# REVIEW before use: quant, mods (exp-w4a16 for NVFP4), "
              f"max_model_len, container.\n")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").write(banner + text)
    print(c(Y, f"  scaffolded {path} — REVIEW then re-run"))


# ── Config wiring (with backup) ───────────────────────────────────────────────

def backup_configs():
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(HSCC_HOME, f"_onboard-backup-{stamp}")
    os.makedirs(dest, exist_ok=True)
    for name in ("serving.json", "models.json", "provision.json", "agents.json"):
        src = os.path.join(HSCC_HOME, name)
        if os.path.exists(src):
            shutil.copy2(os.path.realpath(src), os.path.join(dest, name))
    if os.path.exists(HERMES_CONFIG):
        shutil.copy2(HERMES_CONFIG, os.path.join(dest, "hermes-config.yaml"))
    for n in ("worker-246", "worker-247", "worker-248"):
        p = os.path.join(PROFILES_DIR, n, "config.yaml")
        if os.path.exists(p):
            shutil.copy2(p, os.path.join(dest, f"{n}-config.yaml"))
    print(c(B, f"  backup -> {dest}"))
    return dest


def _load(name):
    return json.load(open(os.path.realpath(os.path.join(HSCC_HOME, name))))


def _save(name, data):
    path = os.path.realpath(os.path.join(HSCC_HOME, name))
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def wire_serving(model_id, recipe):
    d = _load("serving.json")
    for u in d.get("units", []):
        u["model"] = model_id
        u["recipe"] = recipe
    _save("serving.json", d)
    print(c(G, f"  serving.json: {len(d.get('units', []))} units -> {model_id}"))


def wire_models(model_id):
    d = _load("models.json")
    d["primary_model"] = model_id
    models = d.get("models", [])
    if models:
        models[0]["name"] = model_id
        models[0]["status"] = "cached"
    else:
        d["models"] = [{"name": model_id, "type": "llm", "status": "cached",
                        "location": "vLLM", "tp": 1, "pp": 1}]
    _save("models.json", d)
    print(c(G, f"  models.json: primary_model -> {model_id}"))


def wire_provision(recipe):
    d = _load("provision.json")
    n = 0
    for _, mp in d.get("mappings", {}).items():
        mp["recipe"] = recipe
        n += 1
    _save("provision.json", d)  # history left untouched (audit log)
    print(c(G, f"  provision.json: {n} active mappings -> recipe (history kept)"))


def wire_agents(model_id):
    """Swap only the model part of each agent's `vllm-<ip>/<model>` string,
    preserving the per-agent routing IP."""
    path = os.path.realpath(os.path.join(HSCC_HOME, "agents.json"))
    d = json.load(open(path))
    n = 0
    for a in d.get("agents", []):
        m = a.get("model", "")
        new = re.sub(r"^(vllm-\d+\.\d+\.\d+\.\d+/).*", r"\1" + model_id, m)
        if new != m:
            a["model"] = new
            n += 1
    d["lastUpdated"] = datetime.now(timezone.utc).isoformat()
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    print(c(G, f"  agents.json: {n} agents re-pointed (routing IPs preserved)"))


def set_yaml_model_default(path, model_id):
    """Line-targeted edit of model.default in a YAML file. Only the value on the
    `default:` line inside the top-level `model:` block is changed; the rest of
    the file (including secrets) is preserved byte-for-byte."""
    if not os.path.exists(path):
        return False
    lines = open(path).readlines()
    in_model = False
    changed = False
    for i, line in enumerate(lines):
        if re.match(r"^model:\s*$", line):
            in_model = True
            continue
        if in_model:
            # left the block: a new top-level key (col 0, non-space)
            if line and not line[0].isspace() and not line.startswith("#"):
                in_model = False
                continue
            m = re.match(r"^(\s+default:\s*).*$", line)
            if m:
                lines[i] = f"{m.group(1)}{model_id}\n"
                changed = True
                in_model = False
    if changed:
        tmp = path + ".tmp"
        open(tmp, "w").writelines(lines)
        os.replace(tmp, path)
    return changed


def wire_hermes_and_profiles(model_id):
    if set_yaml_model_default(HERMES_CONFIG, model_id):
        print(c(G, "  hermes config.yaml: model.default updated"))
    else:
        print(c(Y, "  hermes config.yaml: model.default NOT found (skipped)"))
    for n in sorted(os.listdir(PROFILES_DIR)) if os.path.isdir(PROFILES_DIR) else []:
        p = os.path.join(PROFILES_DIR, n, "config.yaml")
        if os.path.exists(p) and set_yaml_model_default(p, model_id):
            print(c(G, f"  profile {n}: model.default updated"))


# ── Phases ────────────────────────────────────────────────────────────────────

def phase_plan(topo, model_id, recipe):
    print(c(B, "── PLAN ──"))
    print(f"  gateway:  {topo['gateway']}  workers: {topo['workers']}  nas: {topo['nas_ip']}")
    print(f"  serving:  {topo['serving_nodes']}  port: {topo['port']}")
    rm = recipe_model(recipe)
    if rm is None:
        print(c(Y, f"  recipe:   {recipe} (MISSING — will scaffold on cache/all)"))
    elif rm != model_id:
        print(c(R, f"  recipe:   {recipe} model={rm} != {model_id} (MISMATCH)"))
    else:
        print(c(G, f"  recipe:   {recipe} model={rm} OK"))
    nref = nas_ref(topo, model_id)
    nfiles = nas_snapshot_files(topo, model_id, nref) if nref else 0
    if nref:
        print(c(G, f"  NAS:      cached ref={nref[:12]} ({nfiles} snapshot files)"))
    else:
        print(c(R, "  NAS:      MISSING (cache phase will need a download)"))
    for h in topo["serving_nodes"]:
        if nref and node_cache_ready(topo, h, model_id, nref, nfiles):
            print(c(G, f"    {h}: cache READY"))
        else:
            print(c(Y, f"    {h}: cache needs sync"))
    print(c(B, "  config changes wire would make:"))
    print("    serving.json units, models.json primary_model, provision.json mappings,")
    print("    agents.json (model part only), hermes config.yaml + worker profiles model.default")
    return nref, nfiles


def phase_cache(topo, model_id, recipe):
    print(c(B, "── CACHE ──"))
    if recipe and recipe_model(recipe) is None:
        scaffold_recipe(recipe, model_id)
        die("recipe was missing; scaffolded a starter. Review it, then re-run.")
    nref = nas_ref(topo, model_id)
    if not nref:
        die(f"model not on NAS. Download first, e.g.:\n"
            f"  ssh {topo['ssh_user']}@{topo['nas_mount_host']} "
            f"'HF_HUB_OFFLINE=0 hf download {model_id} --cache-dir /mnt/nas/hub'")
    nfiles = nas_snapshot_files(topo, model_id, nref)
    print(c(G, f"  NAS ref={nref[:12]} ({nfiles} snapshot files)"))
    for h in topo["serving_nodes"]:
        if node_cache_ready(topo, h, model_id, nref, nfiles):
            print(c(G, f"  {h}: already complete — skip"))
            continue
        print(c(Y, f"  {h}: rsync from NAS (additive)…"))
        ok, err = rsync_nas_to_node(topo, h, model_id)
        if not ok:
            print(c(R, f"  {h}: rsync FAILED: {err}"))
            continue
        if node_cache_ready(topo, h, model_id, nref, nfiles):
            print(c(G, f"  {h}: cache READY"))
        else:
            print(c(R, f"  {h}: still incomplete after rsync"))


def phase_wire(topo, model_id, recipe, yes):
    print(c(B, "── WIRE ──"))
    if recipe_model(recipe) != model_id:
        die(f"recipe {recipe} model != {model_id}; fix recipe before wiring")
    if not yes:
        die("wire mutates live HSCC config. Re-run with --yes to proceed.")
    backup_configs()
    wire_serving(model_id, recipe)
    wire_models(model_id)
    wire_provision(recipe)
    wire_agents(model_id)
    wire_hermes_and_profiles(model_id)
    print(c(G, "  wired. NOTE: gateway/daemon still hold old config until restarted."))


def phase_launch(topo, recipe, yes):
    print(c(B, "── LAUNCH ──"))
    if not os.path.exists(SPARKRUN_BIN):
        die(f"sparkrun bin not found at {SPARKRUN_BIN}")
    if not yes:
        die("launch starts vLLM containers on the cluster. Re-run with --yes.")
    for h in topo["serving_nodes"]:
        print(c(Y, f"  {h}: sparkrun run…"))
        try:
            r = subprocess.run(
                [SPARKRUN_BIN, "run", recipe, "--hosts", h],
                cwd=SPARKRUN_DIR, capture_output=True, text=True, timeout=180)
            tail = (r.stdout + r.stderr).strip().splitlines()[-3:]
            status = G + "launched" if r.returncode == 0 else R + "FAILED"
            print(f"    {status}{NC}: " + " | ".join(tail))
        except subprocess.TimeoutExpired:
            print(c(R, "    launch timed out (container may still be starting)"))


def _health(host, port, timeout=5):
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _completion(host, port, model_id, timeout=40):
    body = json.dumps({
        "model": model_id,
        "messages": [{"role": "user", "content": "Reply with exactly: online"}],
        "max_tokens": 16, "temperature": 0,
    }).encode()
    try:
        req = urllib.request.Request(
            f"http://{host}:{port}/v1/chat/completions", data=body,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
            return d["choices"][0]["message"]["content"].strip()[:60]
    except Exception as e:
        return f"ERR {e}"


def phase_verify(topo, model_id, wait_secs=0):
    print(c(B, "── VERIFY ──"))
    port = topo["port"]
    deadline = time.time() + wait_secs
    for h in topo["serving_nodes"]:
        ok = _health(h, port)
        while not ok and time.time() < deadline:
            time.sleep(15)
            ok = _health(h, port)
        if not ok:
            print(c(R, f"  {h}: DOWN"))
            continue
        comp = _completion(h, port, model_id)
        bad = comp.startswith("ERR")
        print(f"  {h}: {c(G,'health 200')} | completion: {c(R if bad else G, comp)}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def usage():
    print(__doc__)
    sys.exit(1)


def main():
    if len(sys.argv) < 2:
        usage()
    cmd = sys.argv[1]
    args = [a for a in sys.argv[2:] if not a.startswith("--")]
    yes = "--yes" in sys.argv
    topo = load_topology()

    if cmd == "plan":
        if len(args) < 2:
            die("plan <model> <recipe>")
        phase_plan(topo, args[0], os.path.expanduser(args[1]))
    elif cmd == "cache":
        if not args:
            die("cache <model> [recipe]")
        recipe = os.path.expanduser(args[1]) if len(args) > 1 else None
        phase_cache(topo, args[0], recipe)
    elif cmd == "wire":
        if len(args) < 2:
            die("wire <model> <recipe> --yes")
        phase_wire(topo, args[0], os.path.expanduser(args[1]), yes)
    elif cmd == "launch":
        if not args:
            die("launch <recipe> --yes")
        phase_launch(topo, os.path.expanduser(args[0]), yes)
    elif cmd == "verify":
        if not args:
            die("verify <model>")
        wait = 300 if "--wait" in sys.argv else 0
        phase_verify(topo, args[0], wait)
    elif cmd == "all":
        if len(args) < 2:
            die("all <model> <recipe> --yes")
        model, recipe = args[0], os.path.expanduser(args[1])
        phase_plan(topo, model, recipe)
        phase_cache(topo, model, recipe)
        phase_wire(topo, model, recipe, yes)
        phase_launch(topo, recipe, yes)
        phase_verify(topo, model, wait_secs=420)
    else:
        usage()


if __name__ == "__main__":
    main()
