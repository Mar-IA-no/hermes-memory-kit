---
name: librarian
description: Use the local Hermes Memory Kit library as canonical durable memory and the wiki layer as projected navigation.
version: 0.2.0
author: Hermes Memory Kit
license: MIT
---

# Librarian

## Rules

- prefer `hybrid-pack` before `expand`
- use the wiki as map, not as canonical evidence
- canon first, projection after
- accept `null_retrieval`

## Core Retrieval

```bash
./scripts/hmk memoryctl.py hybrid-pack --query "USER_QUESTION" --budget 1800 --limit 4 --threshold 0.4
```

## Expand

```bash
./scripts/hmk memoryctl.py expand --id 17
```

## Ingest

```bash
./scripts/hmk ingest_any.py \
  --source-root /path/to/folder \
  --shelf library \
  --tags bulk-import,md
```

## Embedding config

```bash
./scripts/hmk memoryctl.py embed-config
```

## Fast continuity rehydration

```bash
./scripts/hmk continuityctl.py rehydrate
```

Use this after: model changes, service restart, crash recovery, obvious continuity loss. Reads identity anchors, `NOW.md`, `ACTIVE-CONTEXT.md` (engineering state), `DIALOGUE-HANDOFF.md` (conversational state), exact `[mem:N]` anchors, and runs a single small `hybrid-pack` as supplement. Tactical re-entry only — don't replace normal retrieval.

## Handoff sources — two separate files, two distinct semantics

`continuityctl.py rehydrate` returns two independent handoff sources:

| Key | File | Semantic | Written by |
|---|---|---|---|
| `meta_context` | `agent-memory/state/ACTIVE-CONTEXT.md` + `NOW.md` | Engineering / operator meta-state (system goals, blockers, memory architecture) | Operator manually via `continuityctl.py update` |
| `dialogue_handoff` | `agent-memory/state/DIALOGUE-HANDOFF.md` | Last real user↔agent conversation turn (platform, session_id, last user msg, last assistant resp, working set, resume hint) | `dialogue-handoff` plugin on `post_llm_call` |
| `episode_handoff` | legacy section inside `ACTIVE-CONTEXT.md` | historical / compat — deprecated | (kept for back-compat only) |

### Rule for consumption

- "what were we talking about / ¿en qué estábamos? / recién" → `dialogue_handoff` FIRST. If empty or stale (>6h old based on `last_turn.timestamp`), fallback to `meta_context`.
- "what are we working on / project status / estado del sistema / roadmap" → `meta_context`.
- Never quote `meta_context` as if it were the user conversation. It describes the system, not the dialogue.

### Recovering broader thread context (beyond last turn)

`dialogue_handoff.session_path` points to the full session JSON (messages array) of the previous session. When the user asks about the **thread arc** — not just the last message, but the topic they were working on, the document being discussed, earlier reasoning — the last turn in `DIALOGUE-HANDOFF.md` is NOT enough.

In that case, ALSO read `session_path` and scan the last ~20 messages for:

- referenced paths, files, or URLs
- the main topic of the exchange
- decisions or conclusions already reached

`last_working_set` already captures paths touched in shell / file tools of the last turn; cross-reference those with the session messages to reconstruct what the user was thinking about. Do not reread the entire session file — 20 trailing messages are enough to recover the thread without blowing up the context window.

### Response style on re-entry

Treat recovered context as YOUR OWN memory — not as something to report.

When the user asks "¿en qué estábamos?", "de qué veníamos hablando?", "continua", "recordás lo último?" or similar re-entry cues:

- DO absorb the handoff silently and pick up the thread naturally. Examples:
  - "Dale, seguimos con el PDF de X. Decías que…"
  - "Veníamos trabajando con X sobre Y. ¿Querés que continúe desde Z?"
- DO NOT list structured fields like `session_id`, timestamps, model, platform, working set paths — that is metadata for the agent, not for the user.
- DO NOT frame the answer as a "report" with headers/bullets unless the user explicitly asks for a recap or audit.
- If context is genuinely empty/stale, say so briefly in one sentence and invite the user to reorient.

The handoff system is infrastructure; from the user's perspective, the agent just remembers.

## Memory Topology (reference)

- `agent-memory/library.db` — FTS5 + sqlite-vec canonical store
- `agent-memory/state/NOW.md`, `ACTIVE-CONTEXT.md`, `DIALOGUE-HANDOFF.md` — state files
- `wiki/` — projected navigation layer (not canonical)
- `plugins/dialogue-handoff/` — auto-injection plugin for Hermes Agent (optional)
