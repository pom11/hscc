#!/usr/bin/env python3
"""model-check.py — SparkRun model availability checker and manager.

Usage:
    python3 model-check.py check          # Check model availability on hosts
    python3 model-check.py list           # List all recipes
    python3 model-check.py fix-recipes    # Add HF env vars to missing recipes
    python3 model-check.py sync <model>   # Sync model from NAS to all reachable hosts
    python3 model-check.py download <model> <host-ip>
    python3 model-check.py download-all   # Sync all NAS models to all hosts

Pitfalls:
    - macOS has bash 3.2 (no associative arrays) and old rsync (no remote-to-remote)
    - Always use ssh-from-NAS pattern: ssh spark@NAS_HOST "rsync src/ spark@target:/dst/"
    - Check for files (find -type f), not directories, when detecting cached models
    - Clean empty dirs before checking: rm -rf cache/repo/ then mkdir -p cache/repo/
"""
import os
import sys
import subprocess

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RECIPE_DIR = os.path.join(SCRIPT_DIR, "..", "recipes", "official")
SSH_KEY = os.path.expanduser("~/.ssh/id_ed25519")
NAS_HOST = "192.0.2.10"
ALL_IPS = ["192.0.2.10", "192.0.2.10", "192.0.2.11", "192.0.2.12"]

R = "\033[0;31m"
G = "\033[0;32m"
Y = "\033[0;33m"
B = "\033[0;34m"
NC = "\033[0m"


def hf_repo_name(model_id):
    """Qwen/Qwen3.6-35B-A3B-FP8 -> models--Qwen--Qwen3.6-35B-A3B-FP8"""
    slug = model_id.replace("/", "--")
    return f"models--{slug}"


def ssh(host, cmd, timeout=10):
    """Run SSH command, return (stdout, success)."""
    try:
        result = subprocess.run(
            [
                "ssh",
                "-i", SSH_KEY,
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", f"ConnectTimeout={timeout}",
                f"spark@{host}",
                cmd,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.stdout, result.returncode == 0
    except subprocess.TimeoutExpired:
        return "", False
    except Exception as e:
        return str(e), False


def recipe_model(path):
    """Extract model ID from recipe YAML."""
    with open(path) as f:
        for line in f:
            if line.startswith("model:"):
                return line.split(":", 1)[1].strip()
    return None


def recipe_has_hf_env(path):
    """Check if recipe has HF_HOME and HF_HUB_OFFLINE."""
    with open(path) as f:
        content = f.read()
    return "HF_HOME" in content and "HF_HUB_OFFLINE" in content


def model_on_host(host, model_id):
    """Check if model is cached locally on host.
    Returns: 'local', 'missing', or 'error'
    NOTE: Check for files (blobs), not directories. Empty dirs = not cached.
    """
    repo = hf_repo_name(model_id)
    _, ok = ssh(host, f"find /home/spark/.cache/huggingface/hub/{repo}/blobs -type f | wc -l | grep -v '^0$'")
    if ok:
        return "local"
    return "missing"


def model_on_nas(model_id):
    """Check if model exists on NAS."""
    repo = hf_repo_name(model_id)
    _, ok = ssh(NAS_HOST, f"test -d /mnt/nas/hub/{repo}/snapshots")
    return ok


def do_check():
    print(f"{B}{'=' * 56}{NC}")
    print(f"{B}║           SparkRun Model Availability Check           ║{NC}")
    print(f"{B}{'=' * 56}{NC}\n")

    # First pass: check NAS availability for all models
    recipes = sorted(
        f for f in os.listdir(RECIPE_DIR) if f.endswith(".yaml")
    )
    nas_cache = {}
    for r in recipes:
        model = recipe_model(os.path.join(RECIPE_DIR, r))
        if model and model not in nas_cache:
            nas_cache[model] = model_on_nas(model)

    for r in recipes:
        path = os.path.join(RECIPE_DIR, r)
        model = recipe_model(path)
        if not model:
            continue
        repo = hf_repo_name(model)
        has_hf = recipe_has_hf_env(path)
        has_nas = nas_cache.get(model, False)

        env_status = f"{G}✓{NC}" if has_hf else f"{R}⚠ missing{NC}"
        print(f"{Y}📄 {r}{NC}  —  {model}  {env_status}")

        for ip in ALL_IPS:
            status = model_on_host(ip, model)
            if status == "local":
                print(f"   {G}✓ {ip}{NC} — local cache")
            elif has_nas:
                print(f"   {Y}! {ip}{NC} — only on NAS (can sync)")
            else:
                print(f"   {R}✗ {ip}{NC} — not found (need HF download)")
        print()


def do_list():
    print(f"{B}Recipes:{NC}")
    print(f"{'RECIPE':<40} {'MODEL':<35} {'HF':<6}")
    print(f"{'─' * 65}")
    for r in sorted(f for f in os.listdir(RECIPE_DIR) if f.endswith(".yaml")):
        path = os.path.join(RECIPE_DIR, r)
        model = recipe_model(path)
        hf = "yes" if recipe_has_hf_env(path) else "no"
        print(f"{r:<40} {model:<35} {hf:<6}")


def do_fix_recipes():
    """Add HF env vars (HF_HOME, HF_HUB_OFFLINE, TRANSFORMERS_OFFLINE) to recipes missing them."""
    print(f"{B}Scanning recipes for missing HF env vars...{NC}")
    fixed = 0
    already = 0

    for r in sorted(f for f in os.listdir(RECIPE_DIR) if f.endswith(".yaml")):
        path = os.path.join(RECIPE_DIR, r)
        if recipe_has_hf_env(path):
            already += 1
            continue

        print(f"  Fixing: {r}")
        fixed += 1
        with open(path) as f:
            lines = f.readlines()

        # Find env: section and insert HF vars before next non-env line
        new_lines = []
        in_env = False
        added = False
        for line in lines:
            if line.rstrip() == "env:":
                in_env = True
                new_lines.append(line)
            elif in_env and not added:
                stripped = line.strip()
                # Leaving env section: non-empty, non-indented, not a comment
                if stripped and not line.startswith(" ") and not line.startswith("\t") and not line.startswith("#") and not line.startswith("env:"):
                    new_lines.append("  HF_HOME: /cache/huggingface\n")
                    new_lines.append('  HF_HUB_OFFLINE: "1"\n')
                    new_lines.append('  TRANSFORMERS_OFFLINE: "1"\n')
                    added = True
                    in_env = False
                new_lines.append(line)
            else:
                new_lines.append(line)

        if not added and in_env:
            new_lines.append("  HF_HOME: /cache/huggingface\n")
            new_lines.append('  HF_HUB_OFFLINE: "1"\n')
            new_lines.append('  TRANSFORMERS_OFFLINE: "1"\n')

        with open(path, "w") as f:
            f.writelines(new_lines)

    print(f"\n{G}Fixed: {fixed}{NC}  {B}Already OK: {already}{NC}")


def do_sync(model_id):
    """Sync model from NAS to all reachable hosts."""
    if not model_id:
        print("Usage: model-check.py sync <model-id>")
        print("  e.g. Qwen/Qwen3.6-35B-A3B-FP8")
        sys.exit(1)

    repo = hf_repo_name(model_id)

    # Check NAS first
    if not model_on_nas(model_id):
        print(f"{R}Model not on NAS. Cannot sync.{NC}")
        print(f"To download from HF Hub on target hosts:")
        print(f"  ssh spark@192.0.2.<ip> 'HF_HUB_OFFLINE=0 huggingface-cli download {model_id}'")
        sys.exit(1)

    print(f"{B}Syncing {model_id} from NAS to reachable hosts...{NC}\n")

    synced = 0
    for ip in ALL_IPS:
        status = model_on_host(ip, model_id)
        if status == "local":
            print(f"  {G}✓ {ip}{NC} — already cached")
            continue

        print(f"  {Y}→ Syncing to {ip}...{NC}")
        # Clean empty dirs and create fresh structure
        ssh(ip, f"rm -rf /home/spark/.cache/huggingface/hub/{repo}/; mkdir -p /home/spark/.cache/huggingface/hub/{repo}/{{blobs,snapshots,refs}}")

        # Sync via ssh FROM NAS (local file access) TO target
        # macOS rsync does NOT support remote-to-remote transfers
        for section in ["blobs", "refs", "snapshots"]:
            src = f"/mnt/nas/hub/{repo}/{section}/"
            dst = f"/home/spark/.cache/huggingface/hub/{repo}/{section}/"
            cmd = f'rsync -avz --progress -e "ssh -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=no" {src} spark@{ip}:{dst}'
            result = subprocess.run(
                [
                    "ssh", "-i", SSH_KEY, "-o", "StrictHostKeyChecking=no",
                    "-o", "UserKnownHostsFile=/dev/null",
                    f"spark@{NAS_HOST}",
                    cmd,
                ],
                capture_output=True,
                text=True,
                timeout=600,
            )
            if result.returncode != 0:
                print(f"  {R}rsync failed for {section}: {result.stderr[:300]}{NC}")
            else:
                lines = result.stdout.split('\r')
                file_count = sum(1 for l in lines if '/' in l and 'total size' not in l and 'sent' not in l and 'speedup' not in l)
                print(f"  {G}  {section}: synced ({file_count} items){NC}")

        print(f"  {G}✓ {ip} synced{NC}")
        synced += 1

    print(f"\n{G}Synced: {synced} hosts{NC}")


def do_download(model_id, host):
    """Download model to specific host from NAS (or print HF download instructions)."""
    if not model_id or not host:
        print("Usage: model-check.py download <model-id> <host-ip>")
        sys.exit(1)

    repo = hf_repo_name(model_id)

    if model_on_nas(model_id):
        print(f"{B}Downloading {model_id} to {host} (from NAS)...{NC}")
        ssh(host, f"rm -rf /home/spark/.cache/huggingface/hub/{repo}/; mkdir -p /home/spark/.cache/huggingface/hub/{repo}/{{blobs,snapshots,refs}}")
        for section in ["blobs", "refs", "snapshots"]:
            src = f"/mnt/nas/hub/{repo}/{section}/"
            dst = f"/home/spark/.cache/huggingface/hub/{repo}/{section}/"
            cmd = f'rsync -avz -e "ssh -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=no" {src} spark@{host}:{dst}'
            subprocess.run(
                [
                    "ssh", "-i", SSH_KEY, "-o", "StrictHostKeyChecking=no",
                    "-o", "UserKnownHostsFile=/dev/null",
                    f"spark@{NAS_HOST}",
                    cmd,
                ],
                capture_output=True,
                timeout=600,
            )
        print(f"{G}✓ Downloaded to {host}{NC}")
    else:
        print(f"{R}Model not on NAS. Must download from HuggingFace Hub:{NC}")
        print(f"  ssh spark@{host} 'HF_HUB_OFFLINE=0 huggingface-cli download {model_id}'")


def do_download_all():
    """Sync all NAS-cached models to all reachable hosts."""
    print(f"{B}Downloading all NAS-cached models to all hosts...{NC}\n")

    # Collect unique models
    models = {}
    for r in sorted(f for f in os.listdir(RECIPE_DIR) if f.endswith(".yaml")):
        path = os.path.join(RECIPE_DIR, r)
        model = recipe_model(path)
        if model:
            models[model] = True

    for model_id in sorted(models.keys()):
        repo = hf_repo_name(model_id)
        print(f"{Y}→ {model_id}{NC}")

        if not model_on_nas(model_id):
            print(f"  {R}Not on NAS — skipping (need HF download){NC}\n")
            continue

        for ip in ALL_IPS:
            status = model_on_host(ip, model_id)
            if status != "local":
                print(f"  Syncing to {ip}...")
                ssh(ip, f"rm -rf /home/spark/.cache/huggingface/hub/{repo}/; mkdir -p /home/spark/.cache/huggingface/hub/{repo}/{{blobs,snapshots,refs}}")
                for section in ["blobs", "refs", "snapshots"]:
                    src = f"/mnt/nas/hub/{repo}/{section}/"
                    dst = f"/home/spark/.cache/huggingface/hub/{repo}/{section}/"
                    cmd = f'rsync -aq -e "ssh -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=no" {src} spark@{ip}:{dst}'
                    subprocess.run(
                        [
                            "ssh", "-i", SSH_KEY, "-o", "StrictHostKeyChecking=no",
                            "-o", "UserKnownHostsFile=/dev/null",
                            f"spark@{NAS_HOST}",
                            cmd,
                        ],
                        capture_output=True,
                        timeout=600,
                    )
        print()

    print(f"{G}Done! Run model-check.py check to verify.{NC}")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    args = sys.argv[2:]

    if cmd == "check":
        do_check()
    elif cmd == "list":
        do_list()
    elif cmd == "fix-recipes":
        do_fix_recipes()
    elif cmd == "sync":
        do_sync(args[0] if args else None)
    elif cmd == "download":
        if len(args) >= 2:
            do_download(args[0], args[1])
        else:
            print("Usage: model-check.py download <model-id> <host-ip>")
    elif cmd == "download-all":
        do_download_all()
    else:
        print(f"Unknown command: {cmd}")
        print("Usage: model-check.py {check|list|fix-recipes|sync|download|download-all}")
        sys.exit(1)


if __name__ == "__main__":
    main()
