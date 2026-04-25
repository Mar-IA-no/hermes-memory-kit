# Dialogue Handoff Plugin

The dialogue-handoff plugin provides **working memory** for Hermes Agent: it persists the last N substantive turns and injects them into the start of every new session, so the agent picks up the thread without grepping session JSONs.

As of v3.2.x of this kit, the plugin **lives in its own repo**: [hermes-continuity-plugin](https://github.com/Mar-IA-no/hermes-continuity-plugin).

This kit ships a **vendored copy** of the plugin at `templates/plugins/dialogue-handoff/`, pinned to the version recorded in `.continuity-plugin-version` at the repo root. The vendored copy is generated/read-only — edits to the plugin go to the standalone repo and are re-synced via `scripts/sync-continuity-plugin.sh`.

## Why the split

- The plugin is **working memory** (per-session, last few turns, anti-amnesia between sessions/crashes/model switches).
- This kit is **long-term memory** (durable facts, embeddings, hybrid retrieval, library curation).
- Both layers are useful independently. Splitting them lets each evolve at its own pace and lets users adopt only the part they need.

The continuity plugin is bundled by default because most kit users want both layers, but you can disable it (remove from `config.yaml: plugins.enabled`) if you have your own continuity mechanism.

## How it's used in this kit

`bootstrap_agent.py` copies the vendored plugin into `<workspace>/hermes-home/plugins/dialogue-handoff/`. The `.env.template` emits both canonical (`HERMES_HANDOFF_PATH`, etc) and legacy (`HMK_DIALOGUE_HANDOFF_PATH`, etc) env vars resolving to the workspace's `agent-memory/state/` paths.

The plugin reads via cascade:

```
HERMES_HANDOFF_PATH         (canonical) || HMK_DIALOGUE_HANDOFF_PATH (legacy)
HERMES_ALWAYS_CONTEXT_PATH  (canonical) || HMK_ALWAYS_CONTEXT_PATH   (legacy)
HERMES_SESSIONS_DIR         (canonical) || HMK_SESSIONS_DIR          (legacy)
```

Or it derives from bases:

```
HERMES_AGENT_MEMORY_BASE  || AGENT_MEMORY_BASE / HMK_AGENT_MEMORY_BASE / HMK_BASE_DIR
HERMES_HOME               || HMK_HERMES_HOME
```

When matched on a legacy var, `logger.warning()` once per var (visible in gateway logs).

## Sync workflow

When the standalone plugin has a new release:

1. Update `.continuity-plugin-version` at the kit root with the new tag.
2. Run `./scripts/sync-continuity-plugin.sh` (default mode is `--sync`).
3. The script clones the pinned tag, overwrites `templates/plugins/dialogue-handoff/` byte-identical, and regenerates `.synced-from` sidecar metadata.
4. Commit the diff.

To verify the vendored copy is in sync without modifying anything:

```bash
./scripts/sync-continuity-plugin.sh --check
```

Exits 0 if byte-identical to the pinned tag (excluding `.synced-from` metadata), exits 1 if drift detected. Useful for CI / pre-commit hooks.

## Plugin specifics

For full plugin documentation (hooks, tunables, state file format, install modes for non-kit deployments), see the standalone repo:

- README: https://github.com/Mar-IA-no/hermes-continuity-plugin/blob/main/README.md
- Architecture: https://github.com/Mar-IA-no/hermes-continuity-plugin/blob/main/docs/architecture.md
- Plugin doc: https://github.com/Mar-IA-no/hermes-continuity-plugin/blob/main/docs/dialogue-handoff.md

## Verify in your workspace

```bash
ls -la <workspace>/hermes-home/plugins/dialogue-handoff/
cat <workspace>/hermes-home/plugins/dialogue-handoff/.synced-from  # if vendored
./scripts/hmk memoryctl.py doctor   # checks plugin layout for sibling collisions
```

After a substantive turn (≥300 chars combined user+assistant):

```bash
cat <workspace>/agent-memory/state/DIALOGUE-HANDOFF.md
# Should show ## Recent Exchanges block with verbatim multi-line tail
```
