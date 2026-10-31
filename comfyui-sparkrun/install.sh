#!/usr/bin/env bash
# Install (or link) comfyui-sparkrun into a ComfyUI custom_nodes directory.
#
# This is an OPERATOR tool, not part of any automated deploy: it writes only
# inside the custom_nodes dir you name, and never starts, stops or restarts
# ComfyUI. Dry-run by default? No — dry-run is an explicit flag, because a
# script whose "do nothing" mode is the default is a script people learn to
# run without reading.
#
# Usage:
#   install.sh [--dry-run] [--link] [--force] [--name DIR] CUSTOM_NODES_DIR
#
#   --dry-run  print exactly what would happen and change nothing
#   --link     symlink the pack (keeps it live against this checkout)
#   --force    replace an existing install instead of refusing
#   --name DIR  install under a different folder name (default: comfyui-sparkrun)
set -u

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACK="$(basename "$SRC")"
DRY=0
LINK=0
FORCE=0
DEST_NAME="$PACK"
TARGET=""

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run|-n) DRY=1; shift ;;
    --link) LINK=1; shift ;;
    --force) FORCE=1; shift ;;
    --name) [ $# -ge 2 ] || { echo "--name needs a directory name" >&2; exit 2; }; DEST_NAME="$2"; shift 2 ;;
    -h|--help) sed -n '2,16p' "${BASH_SOURCE[0]}"; exit 0 ;;
    -*) echo "unknown option: $1" >&2; exit 2 ;;
    *) [ -z "$TARGET" ] || { echo "one target directory only" >&2; exit 2; }; TARGET="$1"; shift ;;
  esac
done

[ -n "$TARGET" ] || { echo "usage: install.sh [--dry-run] [--link] [--force] [--name DIR] CUSTOM_NODES_DIR" >&2; exit 2; }

if [ ! -d "$SRC/tests" ] || [ ! -f "$SRC/nodes.py" ]; then
  echo "refusing: $SRC does not look like the comfyui-sparkrun pack" >&2
  exit 1
fi

# A custom_nodes dir that does not exist is a typo, not a directory to create:
# creating it would drop a pack into a stray path ComfyUI will never read.
if [ ! -d "$TARGET" ]; then
  echo "refusing: '$TARGET' is not a directory (ComfyUI's custom_nodes dir must already exist)" >&2
  exit 1
fi
TARGET="$(cd "$TARGET" && pwd)"

DEST="$TARGET/$DEST_NAME"

run() {
  if [ "$DRY" -eq 1 ]; then
    echo "DRY-RUN: $*"
  else
    "$@"
  fi
}

if [ "$DEST" = "$SRC" ]; then
  echo "refusing: source and destination are the same directory" >&2
  exit 1
fi

if [ -e "$DEST" ] || [ -L "$DEST" ]; then
  if [ "$FORCE" -ne 1 ]; then
    echo "refusing: $DEST already exists (use --force to replace it)" >&2
    echo "  existing: $(ls -ld "$DEST" | awk '{print $1, $NF}')" >&2
    exit 1
  fi
  echo "replacing existing install at $DEST"
  run rm -rf "$DEST"
fi

if [ "$LINK" -eq 1 ]; then
  run ln -s "$SRC" "$DEST"
else
  # Copy WITHOUT the tests dir: the pack's pytest fixtures live in this repo's
  # layout (they import the parent repo's helpers by path) and would only
  # confuse ComfyUI's loader or a reader of custom_nodes. __pycache__ too, or a
  # stale .pyc from a torch-ful venv could shadow a fresh source edit.
  if [ "$DRY" -eq 1 ]; then
    echo "DRY-RUN: copy $SRC -> $DEST (excluding tests/ and __pycache__/)"
  else
    mkdir -p "$DEST"
    ( cd "$SRC" && tar cf - --exclude tests --exclude '__pycache__' . ) | ( cd "$DEST" && tar xf - )
    if [ $? -ne 0 ]; then
      echo "copy failed; leaving $DEST for inspection" >&2
      exit 1
    fi
  fi
fi

if [ "$DRY" -eq 1 ]; then
  echo
  echo "dry run only — nothing was written. Re-run without --dry-run to install."
  exit 0
fi

echo "installed $PACK -> $DEST ($([ "$LINK" -eq 1 ] && echo symlink || echo copy))"
echo "restart ComfyUI to load it; the nodes appear under category 'sparkrun'."
