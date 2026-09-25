#!/usr/bin/env python3
"""Hermetic demo for t_e287cfb6: a second generate_profile run does NOT revert
a hand-set memory value. Runs entirely under a temp HERMES_HOME — never
touches live ~/.hermes profiles.

Usage:
    TMPHOME=$(mktemp -d); HERMES_HOME=$TMPHOME python3 demo_memory_regen.py $TMPHOME/profiles
"""
import os
import sys
import tempfile
import yaml

# Point rolelib at a throwaway profile root (fallback path; HERMES_HOME env
# covers the native-API path too). pdir is passed as argv[1]. We also set
# HERMES_HOME so the native create_profile/get_profile_dir resolve there.
import rolelib
import generator

root = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp()
profiles_dir = os.path.join(root, "profiles")
os.makedirs(profiles_dir, exist_ok=True)
rolelib.PROFILES_DIR = profiles_dir
os.environ["HERMES_HOME"] = root

spec = {"name": "demo-orch", "identity": "You demo.\\n",
        "preload_skills": [], "model_tier": "strong"}

# Run 1: fresh profile -> default memory block.
changed1 = generator.generate_profile(spec, base_identity="BASE")
pdir = os.path.join(profiles_dir, "demo-orch")
cfg_path = os.path.join(pdir, "config.yaml")

def read_memory():
    with open(cfg_path) as f:
        return yaml.safe_load(f)["memory"]

mem1 = read_memory()
print("run1 changed      :", changed1)
print("run1 memory       :", mem1)

# Operator hand-sets memory_char_limit to 6000 and provides its own provider.
cfg = yaml.safe_load(open(cfg_path))
cfg["memory"]["memory_char_limit"] = 6000
cfg["memory"]["provider"] = "operator-picked-provider"
cfg["memory"]["user_char_limit"] = 2000
with open(cfg_path, "w") as f:
    yaml.safe_dump(cfg, f, sort_keys=False)
print("after hand-edit  :", read_memory())

# Run 2: regenerate -> hand-set values must SURVIVE.
changed2 = generator.generate_profile(spec, base_identity="BASE")
mem2 = read_memory()
print("run2 changed      :", changed2)
print("run2 memory       :", mem2)
print("run2 is no-op     :", changed2 is False)

assert mem2["memory_char_limit"] == 6000, "hand-set char limit was reverted!"
assert mem2["provider"] == "operator-picked-provider", "operator provider was reverted!"
assert mem2["user_char_limit"] == 2000, "user_char_limit was dropped!"
assert mem2["memory_enabled"] is True

print("DEMO PASS: second generate_profile run did NOT revert hand-set memory values.")
