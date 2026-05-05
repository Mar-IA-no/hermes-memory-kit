# Plugin backups under `plugins/<name>.bak/` are loaded as active plugins and override the live plugin

## Summary

Hermes Agent loads **every directory** under `$HERMES_HOME/plugins/` that contains a valid `plugin.yaml`, regardless of name suffix. Keeping backups of a plugin in-place as sibling directories (e.g. `dialogue-handoff.v30.bak/`, `dialogue-handoff.v21.bak/`) causes them to be registered as **additional active plugins** — their hooks run alongside the live version's, and the last one to write wins.

This is a silent, hard-to-diagnose source of state corruption. In our case, the `dialogue-handoff.v30.bak` plugin was executing its `post_llm_call` after the v3.1 live plugin, overwriting the freshly written `DIALOGUE-HANDOFF.md` (with `## Recent Exchanges` block) back to the v3.0 legacy format (without it). From outside, it looked like the v3.1 code never ran.

## Environment

- `hermes-memory-kit` workflow recommends creating `.bak` sibling directories during plugin upgrades (see v2→v3 migration playbook).
- Hermes Agent upstream `v0.10.0` (`e710bb1f`).
- Plugin `dialogue-handoff` v3.1.0 (from `hermes-memory-kit`).

## Reproduction

1. Install a plugin that writes a state file (e.g. `dialogue-handoff` writing `DIALOGUE-HANDOFF.md`).
2. Upgrade it and keep the old version as `plugins/<name>.v<old>.bak/` with its `plugin.yaml` intact. Both directories are siblings under `plugins/`.
3. In `config.yaml`, enable only one:
   ```yaml
   plugins:
     enabled:
       - dialogue-handoff
   ```
4. Run any turn through the gateway or CLI.
5. Inspect the output file. It's written in the **old** plugin's format — the old plugin hooks ran AFTER the new one's.

## Evidence from this host

Before the fix:
- `$HERMES_HOME/plugins/dialogue-handoff/plugin.yaml` → `version: 3.1.0`
- `$HERMES_HOME/plugins/dialogue-handoff.v30.bak/plugin.yaml` → `version: 3.0.0`, **same `name: dialogue-handoff`**, **same `provides_hooks: [post_llm_call, pre_llm_call]`**.
- `$HERMES_HOME/plugins/dialogue-handoff.v21.bak/plugin.yaml` → `version: 2.1.0`, idem.
- Handoff written by pipeline: legacy v3.0 format (no `## Recent Exchanges`, no `(headline)` suffix, no `substantive: true` field).
- Unit test calling `_on_post_llm_call()` directly from Python on the v3.1 plugin: correct v3.1 format.

Diagnosis: three plugins with identical `name` and hooks were all loaded and executed in sequence. Load order was non-deterministic; the `.bak` with older code won the last-write race on most real turns.

After moving the two `.bak` directories to `$HERMES_HOME/plugin-backups/` (outside the scanned path) and restarting the gateway:
- Handoff written by pipeline: correct v3.1 format, with `(headline)` suffix, `substantive: true`, and `## Recent Exchanges` block populated verbatim with multi-line content.
- Matching unit-test output.

## Expected behavior (to prevent future foot-guns)

Either:
- **A)** Hermes loads only plugins listed in `config.yaml: plugins.enabled`, and ignores sibling directories even if they contain a valid `plugin.yaml`. This matches the user mental model of what "enabled" means.
- **B)** Hermes loads every valid plugin directory under `plugins/` BUT deduplicates by `name` (keeping only the first, or the enabled one), and warns when duplicates are found.
- **C)** Hermes loads every valid plugin directory without dedup (current behavior), BUT the kit documentation prescribes keeping backups outside `plugins/` (e.g. in `plugin-backups/`), and `bootstrap_agent.py` creates that directory up front so users have the convention at hand.

## Proposed fix for `hermes-memory-kit`

Regardless of upstream's choice, the kit should stop recommending `.bak` siblings under `plugins/`.

1. **Docs**: update `docs/migration-v3.md`, `migration-v3*.md`, and the in-plan step
   ```bash
   mv plugins/<name> plugins/<name>.v30.bak
   cp -r staging/<name> plugins/<name>
   ```
   to:
   ```bash
   mkdir -p plugin-backups
   mv plugins/<name> plugin-backups/<name>.v30.bak
   cp -r staging/<name> plugins/<name>
   ```

2. **`bootstrap_agent.py`**: create `hermes-home/plugin-backups/` in the scaffolded agent, with a `.gitkeep` + short `README.md` explaining the convention.

3. **Optional kit-side linter**: `memoryctl.py doctor` could scan `$HMK_HERMES_HOME/plugins/` and warn about name collisions or `.bak` suffixes.

## Priority

**High**. Silent plugin collision overrides fresh behavior with stale logic. Users upgrading any plugin via the kit-recommended `.bak` pattern hit this immediately, with no log signal that something's wrong — they just see the old format output and assume the upgrade didn't take.

## Labels

`bug`, `plugins`, `docs`, `migration`, `high`
