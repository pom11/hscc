#!/usr/bin/env python3
"""Guarded cleanup of the scratch e2e home (/tmp/hscc_e2e_home)."""
import shutil
from pathlib import Path

target = Path("/tmp/hscc_e2e_home")
print("clearing scratch home:", target)
if target.exists():
    for child in target.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            try:
                child.unlink()
            except FileNotFoundError:
                pass
    # recreate config.yaml (the model block — flat format matching the real
    # profiles: provider: custom + base_url to the local litellm proxy; the
    # key is never a real credential, localhost:4000 answers without auth)
    target.mkdir(parents=True, exist_ok=True)
    (target / "config.yaml").write_text(
        "model:\n  default: worker-model\n  provider: custom\n"
        "  base_url: http://localhost:4000/v1\n"
        "  api_key: sk-local-isolated-probe-noauth\n")
    print("recreated empty scratch home + config.yaml")
else:
    print("scratch home did not exist")
