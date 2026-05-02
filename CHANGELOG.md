# Changelog

All notable changes to hermes-memory-kit.

> **Note**: entries for v3.2.0 through v3.5.0 are absent from this file —
> see `git log` for shelf/tag filters (v3.5.0), CPU-only model2vec stack
> with binary quantization (v3.4.0), continuity-plugin split (v3.3.0),
> plugin-backups convention (v3.2.x). Reconstructing those entries here
> is a separate housekeeping task.

## v3.6.0 — 2026-05-01

### New (ENGRAM taxonomy)

- **`scripts/migrate-engram.py`** — idempotent schema migration that adds
  `engram_type` (`episodic`/`semantic`/`procedural` with CHECK constraint)
  plus `event_ts`, `actor`, `location_json` columns to `chapters`, with
  matching indexes. Backs up the DB to `<db>.bak.preengram.<unix_ts>`
  before any DDL. Applies a heuristic shelf→engram_type UPDATE pass
  (e.g. `mc-episodic`, `episodes` → `episodic`; `mc-skills`, `plans` →
  `procedural`; rest defaults to `semantic`).
- **`memoryctl.py engram-pack` subcommand** — Reciprocal Rank Fusion
  retrieval across the three buckets. Runs `hybrid_pack` per
  `engram_type`, then fuses the ranked lists via
  `Σ_b 1 / (k + rank_b(d))` with `k=60`. Quotas
  (`--quota-episodic`, `--quota-semantic`, `--quota-procedural`)
  guarantee a minimum number of items from each bucket before the
  remaining prompt budget is filled by RRF ranking. Each returned
  item carries its source `engram_type` so the caller can render
  bucket-aware prompt sections.
- **`engram_types` filter** propagated through `_filter_clauses_and_params`,
  `search`, `semantic_search`, and `hybrid_pack` for callers that want
  explicit single-bucket retrieval from the Python API. The CLI does not
  expose `--engram-type` directly — `engram-pack` is the typed entry point.
- **`scripts/backfill-semantic.py`** — walks every
  `engram_type='episodic'` chapter (scoped by `--shelf-pattern`, default
  `mc-%`), asks Hermes via `hermes chat -q` to extract durable facts in
  `TYPE | TEXT` format, and inserts the results as new
  `engram_type='semantic'` chapters under an auto-created
  `engram-backfill` book in the appropriate shelf. Each new chapter is
  tagged `engram-backfill`, `<fact_type>`, and `src-chapter-<source_id>`
  for traceability and easy rollback. Default prompt is generic; set
  `HMK_AGENT_NAME` and `HMK_DOMAIN_DESC` to specialize, or pass
  `--prompt-file` for a fully custom template. `--dry-run` prints
  extracted facts without writing.
- **`docs/engram.md`** — concept, schema, shelf mapping, RRF formula,
  CLI usage, backfill workflow, and the few-shot retrieval pattern used
  by the reference deployment.

### Notes for the reference deployment

- This release ports the live ENGRAM stack from the hermes-prime
  workspace (where it had been running since the Sprint 3 work on the
  Minecraft agent project) into the kit so the public repo reflects the
  shipped behavior. No data migration is required for installs that do
  not yet use ENGRAM — `engram_type` defaults to `semantic` and the new
  CLI subcommand is opt-in.

## v3.1.0 — 2026-04-23

### Fixed (critical continuity bug)

- **`_trunc()` preserves multi-line content.** Before, it did `s.splitlines()[0]` when the text had newlines, so any markdown response (tables, lists, code blocks) was reduced to its first sentence before injection. A 5,326-char response ended up as a 100-char headline. Fix: truncate by character budget, preserving newlines. Measured impact on a real session: injection grew from 458 chars to 2,464 chars (5.4×) with semantic structure intact.

### New (persistence of substantive tail)

- **`DIALOGUE-HANDOFF.md` now persists `## Recent Exchanges`** — a verbatim multi-line tail of the last N substantive turns (default N=4, cap 2000 chars per message). Written by `post_llm_call` and read by `pre_llm_call` directly, eliminating the need to reopen the session JSON on every new-session cold start.
- **Backwards-compatible**: if the handoff still lacks the Recent Exchanges block (e.g. a v3.0 handoff mid-upgrade), `pre_llm_call` falls back to the legacy tiered-JSON path. The next `post_llm_call` substantive turn upgrades the handoff to v3.1 format.

### New (anti-trivial gate)

- **`_is_substantive(user, assistant)`** threshold (default 300 chars combined) gates the Recent Exchanges update. Trivial turns like "en qué estábamos?" no longer overwrite the real conversation tail — metadata still updates (Last Turn timestamp, session_id), but the tail stays intact until a real turn arrives.

### Knobs (calibrable)

- `_SUBSTANTIVE_MIN_CHARS = 300` — below this, turn does not update tail.
- `_TAIL_EXCHANGES = 4` — how many substantive exchanges kept in handoff.
- `_TAIL_CHARS_PER_MSG = 2000` — per-message char cap in tail.

Legacy tiered knobs (`_BUDGET_CHARS`, `_TIER*_CHARS`, `_TIER3_STRIDE`) remain but are used only in the backwards-compatibility fallback path.

## v3.0.1 — 2026-04-23

### Fixed (critical)

- **`.env.template` uses absolute paths at bootstrap**. Before, rendered `.env` had `${HMK_WORKSPACE_ROOT}/hermes-home` chains that systemd `EnvironmentFile=` reads literally (systemd does NOT expand `${VAR}`). Consequence: gateway started with `HERMES_HOME=${HMK_HERMES_HOME}` string literal and failed before loading anything. Fix: template uses `{{WORKSPACE_ROOT}}` placeholder that `render_template()` replaces with the agent's absolute path — final `.env` has literal paths compatible with systemd.

### Fixed (layout)

- **`.env` canonical location moved from agent root to `hermes-home/.env`**. Hermes Agent upstream rewrites `HERMES_HOME/.env` via `os.replace()` (atomic rename) in `config.py:3292/3395/3451`. A symlink at that path would be replaced by a regular file on the first save. The new layout puts the real file in `hermes-home/.env` and a relative symlink at `agent_root/.env → hermes-home/.env`, so:
  - Hermes upstream writes to the real file; the symlink stays intact.
  - `hmk` wrapper reads from agent root (workspace) via the symlink.
  - systemd template `EnvironmentFile=%h/agents/%i/.env` resolves via the symlink.

### Fixed (UX)

- **`bootstrap_agent.py --next-steps` prints an absolute path** to the kit's systemd template (`<kit>/templates/systemd/hermes-gateway@.service`), not a relative path through the agent's `scripts/..` that does not exist (templates are not copied into agents).

### Docs

- `docs/multi-agent.md`, `docs/migration-v3.md`, and `README.md` all updated to reflect the canonical `hermes-home/.env` + inverted symlink model, with explicit references to `config.py:3292/3395/3451` as the reason for the inversion.

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
