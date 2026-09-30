#!/usr/bin/env bash
# Bring up an isolated hermes serve on scratch port 9211 with a scratch home,
# for the session-continuity e2e probe (live operator gateway 9119 untouched).
set -e
SCRATCH=/tmp/hscc_e2e_home
rm -rf "$SCRATCH"
mkdir -p "$SCRATCH"
# Seed a minimal config.yaml pointing at the litellm proxy (:4000), the same
# isolated-probe model config the earlier probes used (worker-model).
cat > "$SCRATCH/config.yaml" <<'YAML'
model:
  default:
    provider: openai
    model: worker-model
    base_url: http://localhost:4000/v1
YAML
echo "scratch home ready at $SCRATCH"
ls -la "$SCRATCH"
