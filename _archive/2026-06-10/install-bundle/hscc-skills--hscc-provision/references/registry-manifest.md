# Local Sparkrun Recipe Registry Manifest

## Structure

```
~/.sparkrun-local/
├── .sparkrun/registry.yaml    ← registry manifest
├── recipes/
│   ├── official/              ← Spark-Arena official recipes
│   └── transitional/          ← Sparkrun transitional recipes
└── README.md                  ← (optional)
```

## Registry Manifest Format (`.sparkrun/registry.yaml`)

The manifest declares local recipe sources for sparkrun to discover.

```yaml
registries:
  - name: local-official
    description: Official Spark-Arena recipes
    recipes: recipes/official
    enabled: true
    visible: true

  - name: local-transitional
    description: Sparkrun transitional recipes
    recipes: recipes/transitional
    enabled: true
    visible: true
```

### Field descriptions

| Field | Required | Description |
|---|---|---|
| `name` | Yes | Unique registry name (e.g. `local-official`) |
| `description` | No | Human-readable description |
| `recipes` | Yes | Path to recipes directory (relative to registry root) |
| `enabled` | No | If `false`, sparkrun won't list recipes from this source |
| `visible` | No | If `false`, hidden from user-visible listings |

## Adding the Registry to Sparkrun

```bash
sparkrun registry add ~/.sparkrun-local --no-update
sparkrun update
```

## Recipe YAML Files

Recipe files are standard sparkrun recipes. Example:

```yaml
recipe_version: "2"
name: qwen3.6-35b-a3b-fp8-vllm
model: Qwen/Qwen3.6-35B-A3B-FP8
runtime: vllm-distributed
image: ghcr.io/spark-arena/dgx-vllm-eugr-nightly:latest
container: ...
```

## Syncing from Remote Registries

To sync recipes from remote registries:

1. Clone the remote registry:
   ```bash
   git clone --depth 1 https://github.com/spark-arena/recipe-registry.git /tmp/sparkrun-registry
   ```

2. Copy recipe files to local directories:
   ```bash
   cp -r /tmp/sparkrun-registry/official-recipes/qwen3.6/vllm/*.yaml ~/.sparkrun-local/recipes/official/
   ```

3. Verify models exist on NAS:
   ```bash
   python3 ~/.hermes/plugins/hscc-provision/hscc.py registry list
   ```

4. Commit changes:
   ```bash
   cd ~/.sparkrun-local && git add -A && git commit -m "Sync: +N recipes"
   ```

## Remote Registry Sources

| Prefix | Git URL | Description |
|---|---|---|
| `@official` | `https://github.com/spark-arena/recipe-registry.git` | Official Spark-Arena recipes |
| `@sparkrun-transitional` | `https://github.com/dbotwinick/sparkrun-recipe-registry.git` | Transitional recipes |
| `@eugr` | `https://github.com/eugr/spark-vllm-docker.git` | Eugene's VLLM Docker recipes |

## Troubleshooting

- **Missing recipe files after clone**: Run `git read-tree --reset -u HEAD`
- **Registry not found by sparkrun**: Run `sparkrun update` after adding
- **`sparkrun list` doesn't show local recipes**: Check `.sparkrun/registry.yaml` is valid and registry was added