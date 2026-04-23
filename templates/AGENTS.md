# AGENTS.md

## Scope

This workspace uses Hermes Memory Kit as its durable memory layer.

## Memory Discipline

- canonical memory lives in `agent-memory/library.db`
- use `./scripts/hmk memoryctl.py hybrid-pack` for durable retrieval
- use `./scripts/hmk ingest_any.py` to normalize heterogeneous sources before storage
- use `wiki/` as projected navigation, not canonical truth
- canon first, projection after
- accept `null_retrieval` instead of padding weak context

## Curation Discipline

- new documentation enters canonical memory first
- then retrieve related context
- then inspect wiki maps if conceptual orientation is needed
- then decide whether to keep as evidence, link, distill, or project

## Token Discipline

- no periodic loops by default
- no background polling unless explicitly requested
- prefer bounded retrieval bundles over raw dumps

## Re-entry priorities

On fresh session start (new CLI, restart, `/model` change), the priority is:

- **Priority 1 — `agent-memory/state/DIALOGUE-HANDOFF.md`** — the last real user↔agent turn, auto-populated by the `dialogue-handoff` plugin (if installed).
- **Priority 2 — `./scripts/hmk continuityctl.py rehydrate`** — returns identity + meta_context + dialogue_handoff + exact memories in a single JSON.
- **Never** infer current dialogue from engineering-state files like `agent-memory/state/ACTIVE-CONTEXT.md`. That file describes the system's meta-state, not the conversation.

When recovering context on a new session, **absorb the handoff silently** and continue the conversation naturally. Do NOT present `session_id`, timestamps, file paths, model name, or working-set entries as a report to the user — that is metadata for the agent, not for the user. The user experience should be the agent remembers, not the agent just queried a database.
