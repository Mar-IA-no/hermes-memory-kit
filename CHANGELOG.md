# Changelog

All notable changes to hermes-memory-kit.

## v3.0.0 — 2026-04-23 (unreleased)

### Breaking

- **Repositioning**: the kit is now a scaffold for a self-contained Hermes agent, not just a memory workspace. A bootstrapped workspace includes `hermes-home/` (config, SOUL, memories, sessions, plugins, skills) alongside the existing `agent-memory/` and `wiki/`.
- **`bootstrap_workspace.py` renamed to `bootstrap_agent.py`**. The old name is kept as a deprecation shim that delegates to the new entry point. Shim may be removed in v4.
- **Canonical memory-root env var is `HMK_AGENT_MEMORY_BASE`**. `HMK_BASE_DIR` and `AGENT_MEMORY_BASE` still work as legacy fallbacks in the cascade.
- **No more hardcoded path fallbacks**. `memoryctl.py`, `continuityctl.py`, and the `dialogue-handoff` plugin now hard-fail (exit 2 for scripts, disable-plugin for the hook) when required env vars are unset, rather than silently defaulting to `/home/onairam/agent-memory` or the repo's own `agent-memory/`. This prevents one misconfigured agent from writing over another's state.
- **`.env.template` replaces `.env.example`**. `.env.example` is kept as a symlink for one release.
- **`dialogue-handoff` plugin bumped to v3.0**. Requires `HMK_AGENT_MEMORY_BASE` (or direct `HMK_DIALOGUE_HANDOFF_PATH` / `HMK_ALWAYS_CONTEXT_PATH`) and `HMK_HERMES_HOME` (or `HMK_SESSIONS_DIR`). If unresolved, it logs an error and becomes a no-op.
- **Agent name regex**: `bootstrap_agent.py` validates names against `^[a-z0-9][a-z0-9-]*$`. Lowercase alphanumeric plus dash, first char not a dash. Mismatched names fail the bootstrap.
- **Auto-upgrade v2 → v3 is explicitly NOT supported**. `bootstrap_agent.py` detects a v2 layout (has `agent-memory/` but no `hermes-home/`) and refuses. See `docs/migration-v3.md` for the migration playbook.

### New

- `templates/hermes-home/` — config.yaml, SOUL.md, memories/MEMORY.md, memories/USER.md as templates with `{{AGENT_NAME}}` and `{{USER_PROFILE}}` placeholders.
- `templates/systemd/hermes-gateway@.service` — systemd user-service template, enable one instance per agent with `systemctl --user enable --now hermes-gateway@<name>.service`.
- `docs/multi-agent.md` — guide for running several agents on one host.
- `docs/migration-v3.md` — migration playbook for v2.x workspaces.

### Removed

- Default fallback paths in memoryctl, continuityctl, and dialogue-handoff. Any code path that previously fell back to `/home/onairam/agent-memory` or `$HOME/.hermes` now errors out explicitly.

### Notes for the reference deployment

- The live hermes-prime workspace on onairam-agent is still on v2.x layout as of this release. Migration scheduled for a dedicated session.

## v2.1.x — 2026-04-23

- Added ALWAYS-CONTEXT injection layer in `dialogue-handoff` plugin (1500 char budget, prepended to tiered handoff).
- Embedding benchmark harness (`embed_benchmark.py`, `embed_clear.py`, `embed_verify.py`) with provider-aware preflight.
- README reframed as "operational memory stack".

## v2.0.x — 2026-04

- `dialogue-handoff` plugin: `pre_llm_call` hook injects tiered-compressed continuity context on first turn of each new session. Never touches the system prompt.

## v1.0.0 — initial release

- `memoryctl.py` FTS5 + embeddings hybrid retrieval.
- `continuityctl.py` rehydration.
- `bootstrap_workspace.py` self-contained workspaces.
- `hmk` wrapper.
