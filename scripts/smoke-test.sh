#!/bin/bash
# Smoke test for Hermes Memory Kit base.
# NOT validated here: plugin+Hermes integration — that requires Hermes installed
# and is tested manually. See docs/dialogue-handoff.md for manual steps.
set -e
TMP=$(mktemp -d)
trap "rm -rf $TMP" EXIT

REPO="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"

echo "[1] bootstrap workspace with wiki + scripts + plugins"
python3 "$REPO/scripts/bootstrap_workspace.py" --workspace "$TMP/ws" --with-wiki-templates

echo "[2] hmk wrapper is executable (regression 1C)"
test -x "$TMP/ws/scripts/hmk" || { echo "FAIL: hmk not executable"; exit 1; }

echo "[3] init via wrapper"
cd "$TMP/ws" && ./scripts/hmk memoryctl.py init >/dev/null

echo "[4] add-text + search + pack"
./scripts/hmk memoryctl.py add-text --shelf library --title "smoke" --raw "test content for smoke check" --tags smoke >/dev/null
./scripts/hmk memoryctl.py search --query "smoke" --limit 3 | grep -q "smoke" || { echo "FAIL: search did not return smoke entry"; exit 1; }
./scripts/hmk memoryctl.py pack --query "smoke" --limit 3 >/dev/null

echo "[5] export_obsidian (regression 1B — maps dir)"
./scripts/hmk export_obsidian.py --ids 1 >/dev/null
test -f "$TMP/ws/wiki/maps/project-memory-system.md" || { echo "FAIL: wiki/maps/project-memory-system.md not created"; exit 1; }

echo "[6] wiki templates copied (regression 1A)"
test -f "$TMP/ws/wiki/index.md" || { echo "FAIL: wiki templates missing"; exit 1; }

echo "[7] plugins + DIALOGUE-HANDOFF template + continuityctl present"
test -f "$TMP/ws/plugins/dialogue-handoff/__init__.py" || { echo "FAIL: plugin __init__.py missing"; exit 1; }
test -f "$TMP/ws/plugins/dialogue-handoff/plugin.yaml" || { echo "FAIL: plugin.yaml missing"; exit 1; }
test -f "$TMP/ws/agent-memory/state/DIALOGUE-HANDOFF.md" || { echo "FAIL: handoff template missing"; exit 1; }
test -f "$TMP/ws/agent-memory/state/ALWAYS-CONTEXT.md" || { echo "FAIL: always-context template missing (v2.1)"; exit 1; }
test -f "$TMP/ws/scripts/continuityctl.py" || { echo "FAIL: continuityctl missing"; exit 1; }
grep -q "version: 2.1.0" "$TMP/ws/plugins/dialogue-handoff/plugin.yaml" || { echo "FAIL: plugin not v2.1.0"; exit 1; }

echo "[8] continuityctl syntax OK + rehydrate runs (even with placeholder state)"
python3 -m py_compile "$TMP/ws/scripts/continuityctl.py"
./scripts/hmk continuityctl.py rehydrate --skip-retrieval >/dev/null

echo "[9] --upgrade preserves user data (DB) while refreshing tooling"
./scripts/hmk memoryctl.py add-text --shelf library --title "user-data" --raw "important user content" --tags u >/dev/null
python3 "$REPO/scripts/bootstrap_workspace.py" --workspace "$TMP/ws" --upgrade
./scripts/hmk memoryctl.py search --query "important" --limit 3 | grep -q "user-data" || { echo "FAIL: user data lost on upgrade"; exit 1; }

echo ""
echo "SMOKE TEST PASSED (kit base)"
echo "NOTE: plugin+Hermes integration is NOT validated here. See docs/dialogue-handoff.md for manual verification steps."
