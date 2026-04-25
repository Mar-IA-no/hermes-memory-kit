#!/bin/bash
# sync-continuity-plugin.sh
#
# Syncs the vendored copy at templates/plugins/dialogue-handoff/ with the
# standalone hermes-continuity-plugin repo at the version pinned in
# .continuity-plugin-version.
#
# Modes:
#   --sync (default): clone the pinned tag, copy plugin/* into the vendored
#                     directory byte-identical, regenerate .synced-from sidecar.
#   --check:          clone the pinned tag, diff -r against the vendored copy
#                     (excluding .synced-from). Exit 1 if any drift.
#                     Useful for CI / pre-commit hooks.

set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION_FILE="$REPO_ROOT/.continuity-plugin-version"
VENDORED_DIR="$REPO_ROOT/templates/plugins/dialogue-handoff"
SIDECAR="$VENDORED_DIR/.synced-from"

if [ ! -f "$VERSION_FILE" ]; then
  echo "ERROR: $VERSION_FILE missing. Cannot determine pinned plugin version." >&2
  exit 2
fi

# Read tag (first whitespace-separated token of first non-empty line)
TAG="$(awk 'NF { print $1; exit }' "$VERSION_FILE")"
if [ -z "$TAG" ]; then
  echo "ERROR: could not parse tag from $VERSION_FILE" >&2
  exit 2
fi

PLUGIN_REPO="${HMK_CONTINUITY_PLUGIN_REPO:-https://github.com/Mar-IA-no/hermes-continuity-plugin.git}"
TMPDIR="$(mktemp -d -t hermes-continuity-plugin-clone-XXXXXX)"
trap 'rm -rf "$TMPDIR"' EXIT

echo "Cloning $PLUGIN_REPO @ $TAG → $TMPDIR ..."
git clone --depth 1 --branch "$TAG" "$PLUGIN_REPO" "$TMPDIR" 2>&1 | tail -3
SRC_PLUGIN="$TMPDIR/plugin"

if [ ! -d "$SRC_PLUGIN" ]; then
  echo "ERROR: plugin/ not found in cloned repo at $SRC_PLUGIN" >&2
  exit 3
fi

MODE="${1:---sync}"

case "$MODE" in
  --check)
    echo "Checking vendored copy against pinned tag $TAG (sidecar excluded) ..."
    # Compare files in plugin/ tree, excluding the sidecar metadata
    # __pycache__ is excluded too because it's auto-generated and irrelevant
    if diff -r --exclude=.synced-from --exclude=__pycache__ "$VENDORED_DIR" "$SRC_PLUGIN" >/dev/null; then
      echo "✓ vendored copy is byte-identical to $TAG."
      exit 0
    else
      echo "✗ DRIFT detected: vendored copy differs from $TAG. Files:" >&2
      diff -r --exclude=.synced-from --exclude=__pycache__ "$VENDORED_DIR" "$SRC_PLUGIN" >&2 || true
      echo
      echo "To fix:" >&2
      echo "  - run '$0 --sync' to overwrite vendored with the pinned version, OR" >&2
      echo "  - bump .continuity-plugin-version to a tag that matches the vendored state, OR" >&2
      echo "  - revert local edits to $VENDORED_DIR/" >&2
      exit 1
    fi
    ;;

  --sync)
    echo "Syncing vendored copy to $TAG (byte-identical, then regenerate sidecar) ..."
    mkdir -p "$VENDORED_DIR"
    # Wipe vendored except the sidecar (we regenerate it)
    find "$VENDORED_DIR" -mindepth 1 -not -name '.synced-from' -exec rm -rf {} + 2>/dev/null || true
    # Copy plugin contents byte-identical
    cp -r "$SRC_PLUGIN/." "$VENDORED_DIR/"
    # Regenerate sidecar
    cat > "$SIDECAR" <<EOF
source: $PLUGIN_REPO
tag: $TAG
synced_at: $(date -u +%Y-%m-%dT%H:%M:%SZ)
generated_by: scripts/sync-continuity-plugin.sh
# GENERATED FILE — DO NOT EDIT manually. Edits to plugin code should go to
# the standalone repo (source of truth) and be re-synced.
EOF
    echo "✓ synced. Run 'git status' to see staged changes."
    ;;

  *)
    echo "usage: $0 [--sync|--check]" >&2
    exit 64
    ;;
esac
