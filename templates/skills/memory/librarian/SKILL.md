---
name: librarian
description: Query, expand, and curate the local Hermes memory library stored in this agent's `agent-memory/library.db`, with `wiki/` as a projected navigation layer. Use this when durable project memory, legacy context, architecture notes, or current memory-state orientation are needed.
version: 2.0.0
author: Local System
license: MIT
metadata:
  hermes:
    tags: [Memory, Librarian, Retrieval, SQLite, FTS5, Local]
    related_skills: [codebase-inspection]
prerequisites:
  commands: [python3]
---

# Librarian

Use the local memory controller instead of bloating the live prompt with raw notes.

## Invocation idiom (v3 — workspace-relative)

All commands run from the **agent workspace root** (`$HMK_WORKSPACE_ROOT`, typically `~/agents/<agent-name>/`) using the `./scripts/hmk` wrapper. The wrapper cd's to the workspace, loads the agent's `.env`, and absolutizes `HMK_*` paths, so scripts never rely on hardcoded user-specific paths. Never call the underlying `.py` files directly with absolute paths — use `./scripts/hmk` always.

Paths referenced below (e.g. `agent-memory/state/NOW.md`, `wiki/index.md`) are **relative to the workspace root**. They resolve correctly for any agent bootstrapped from `hermes-memory-kit`, not just `hermes-prime`.

## When to Use

- User asks what we were previously working on
- You need legacy project roadmap or architecture context
- You need current memory-system status or design rationale
- You want to register a durable fact or document
- You need to expand one stored item without loading everything

## Rules

- Prefer `hybrid-pack` before `expand`
- Accept `null_retrieval` when returned; do not pad with weak matches
- Never write to `$HERMES_HOME/SOUL.md` through memory maintenance flows
- Do not treat `MEMORY.md` as the hot write path
- Use `expand` only for items already selected as relevant
- Treat `wiki/` as a projected map layer, not as the canonical store
- For evidence or precise continuity, fall back to `library.db`

## Core Commands

### 1. Query a compact bundle

```bash
./scripts/hmk memoryctl.py hybrid-pack --query "USER_QUESTION" --budget 1800 --limit 4 --threshold 0.4
```

Use `--threshold 0.4` as the current pragmatic default. Raise it only when retrieval is noisy.
Fallback to plain `pack` only if the embeddings layer is unavailable or you explicitly want a lexical baseline.

### 2. Inspect raw ranked candidates

```bash
./scripts/hmk memoryctl.py search --query "USER_QUESTION" --limit 6
```

Use this when ranking looks suspicious and you need to inspect why.

### 3. Expand a stored item

```bash
./scripts/hmk memoryctl.py expand --id 17
```

This returns the full raw content plus linked neighbors.

### 4. Add a durable text memory

```bash
./scripts/hmk memoryctl.py add-text \
  --shelf episodes \
  --title "short-title" \
  --raw "durable fact or decision text" \
  --tags memory,decision \
  --importance 0.8
```

### 5. Add a file into the library

```bash
./scripts/hmk memoryctl.py add-file \
  --shelf plans \
  --path /path/to/file.md \
  --title "descriptive-title" \
  --tags plan,architecture \
  --importance 0.9
```

### 6. Check library stats

```bash
./scripts/hmk memoryctl.py stats
```

### 6b. Inspect embedding backend configuration

```bash
./scripts/hmk memoryctl.py embed-config
```

Use this when retrieval quality, provider selection, or portability of the memory stack is relevant.
Current architecture supports:
- `nvidia` as the active default
- `google` (`gemini-embedding-001`) when a Gemini/Google API key is present
- `local` as an optional backend for a compatible sentence-transformers stack

### 7. Ingest heterogeneous sources via the modular stack

```bash
./scripts/hmk ingest_any.py \
  --source /path/to/file.pdf \
  --shelf evidence \
  --title "descriptive-title" \
  --tags pdf,source \
  --importance 0.7
```

Use this for PDF, DOCX, HTML, URLs, or any source that should be normalized to markdown before entering `library.db`.

Current modular stack:
- `pdftotext` for PDF
- `trafilatura` for URLs and HTML
- `pandoc` for broad document conversion
- `mammoth` as DOCX fallback

### 8. Inspect the projected wiki layer

```bash
sed -n '1,200p' wiki/index.md
sed -n '1,200p' wiki/maps/project-memory-system.md
```

Use this when the user wants a high-level map, topic navigation, or conceptual grouping before diving into canonical memory.

### 9. Fast continuity rehydration

```bash
./scripts/hmk continuityctl.py rehydrate
```

Use this after:
- model changes
- service restart
- crash recovery
- obvious continuity loss

This path is intentionally cheap:
- reads identity anchors
- reads `agent-memory/state/NOW.md`
- reads `agent-memory/state/ACTIVE-CONTEXT.md`
- reads `agent-memory/state/DIALOGUE-HANDOFF.md` for conversational handoff (platform, last user msg, last assistant resp, working set, resume hint, and — with plugin v3.1+ — the `## Recent Exchanges` multi-line tail)
- reads legacy episodic handoff fields from `ACTIVE-CONTEXT.md` when present (deprecated)
- expands exact `[mem:N]` anchors from `ACTIVE-CONTEXT.md`
- runs one small `hybrid-pack` only as a supplement

Do not replace normal retrieval with this. Use it as tactical re-entry.
Use it once at the beginning of the first substantial turn after restart, crash recovery, or `/model` change when continuity may be degraded.

## Memory Topology (workspace-relative)

Paths relative to the agent workspace root:
- `agent-memory/README.md`
- `agent-memory/index/INDEX.md`
- `agent-memory/state/NOW.md`
- `agent-memory/state/ACTIVE-CONTEXT.md`
- `agent-memory/state/DIALOGUE-HANDOFF.md`
- `agent-memory/plans/MEMORY-ARCHITECTURE.md`
- `agent-memory/library.db`
- `wiki/index.md`
- `wiki/maps/project-memory-system.md`

## Retrieval Strategy

1. Start with `hybrid-pack`
2. If continuity was likely lost, run `continuityctl.py rehydrate` first
3. If the task is orientation, roadmap mapping, or conceptual grouping, inspect `wiki/` next
4. Cite chapter ids like `[mem:17]`
5. If needed, `expand` one or two ids
6. Synthesize only from retrieved items; do not hallucinate continuity

## Handoff sources — two separate files, two distinct semantics

`continuityctl.py rehydrate` returns two independent handoff sources:

| Key | File | Semantic | Written by |
|---|---|---|---|
| `meta_context` | `agent-memory/state/ACTIVE-CONTEXT.md` + `NOW.md` | Engineering / operator meta-state (system goals, blockers, memory architecture) | Operator manually via `continuityctl update` |
| `dialogue_handoff` | `agent-memory/state/DIALOGUE-HANDOFF.md` | Last real user↔Hermes conversation turn + (plugin v3.1+) a `## Recent Exchanges` multi-line tail of the last N substantive turns | `dialogue-handoff` plugin on `post_llm_call` |
| `episode_handoff` | legacy section inside `ACTIVE-CONTEXT.md` | historical/compat — deprecated | (kept only for back-compat) |

### Rule for consumption — source of truth order

Authority order for "what were we talking about" / re-entry queries (this matches the ALWAYS-CONTEXT rule):

1. **`<previous_session_context>` already injected at turn start** — treat as authoritative; cite concrete details before looking anywhere else.
2. **`dialogue_handoff` (DIALOGUE-HANDOFF.md)** — same content as injected; read directly if you need exchanges older than the rolling tail.
3. **Sessions JSON** (`$HMK_SESSIONS_DIR/*.json`) — fallback ONLY if (a) the user mentions a topic not in the handoff, or (b) you need history older than the rolling window.
4. **`meta_context` (ACTIVE-CONTEXT.md, NOW.md)** — never as conversation; describes the system, not the dialogue.

For "what are we working on / project status / roadmap" queries → `meta_context` is primary.

Never quote `meta_context` as if it were the user conversation.

### Recovering broader thread context (beyond last turn)

With plugin v3.1+, `DIALOGUE-HANDOFF.md` contains a `## Recent Exchanges` block with the last N (default 4) substantive turns verbatim — usually enough to recover the thread without reopening session JSON.

If you need older context, `dialogue_handoff.session_path` points to the full session JSON of the previous session. Scan the last ~20 messages for:
- referenced paths, files, or URLs
- the main topic of the exchange
- decisions or conclusions already reached

`last_working_set` already captures paths touched in shell / file tools of the last turn; cross-reference those with the session messages to reconstruct thread. Do not reread the entire session file — 20 trailing messages are enough.

### Response style on re-entry

Treat recovered context as YOUR OWN memory — not as something to report.

When the user asks "¿en qué estábamos?", "de qué veníamos hablando?", "recordás lo último?" or similar re-entry cues:

- DO absorb the handoff silently and pick up the thread naturally. Examples:
  - "Dale, seguimos con el PDF de malabarismos. Decías que…"
  - "Veníamos trabajando con X sobre Y. ¿Querés que continúe desde Z?"
- DO NOT list structured fields like `session_id`, timestamps, model, platform, working set paths — they are metadata for you, not for the user.
- DO NOT frame the answer as a "report" with headers/bullets unless the user explicitly asks for a recap or audit.
- If context is genuinely empty/stale, say so briefly in one sentence and invite the user to reorient you.

The handoff system is infrastructure; from the user's perspective, you just remember.

`DIALOGUE-HANDOFF.md` is updated automatically by the `dialogue-handoff` plugin on every non-trivial user turn. `ACTIVE-CONTEXT.md` remains the engineering meta-context file (goals, blockers, focus) and is updated manually by the operator.

## Ingestion Strategy

- use `./scripts/hmk memoryctl.py add-file` for clean markdown or text already in final form
- use `./scripts/hmk ingest_any.py` for heterogeneous formats
- normalize first, then store
- do not put binary formats directly into `library.db`
- do not rely on Python `MarkItDown` on pre-AVX CPUs; it crashes (SIGILL). The host stub at `~/.local/bin/markitdown-local` explains alternatives.

## Curation Workflow For New Documentation

When new documentation enters the system, do not jump straight to wiki projection.

Default flow:

1. normalize and ingest the source into canonical memory
2. retrieve related context with `hybrid-pack`
3. inspect the wiki only as a conceptual map layer
4. decide the curation outcome
5. write canon first, projection second

### Minimal curation loop

#### A. Normalize and ingest

```bash
./scripts/hmk ingest_any.py \
  --source /path/to/source \
  --shelf evidence \
  --title "descriptive-title" \
  --tags source,new \
  --importance 0.7
```

#### B. Retrieve related context

```bash
./scripts/hmk memoryctl.py hybrid-pack --query "topic of the new document" --budget 1800 --limit 4 --threshold 0.4
```

#### C. Inspect conceptual map if needed

```bash
sed -n '1,200p' wiki/index.md
sed -n '1,220p' wiki/maps/project-memory-system.md
```

#### D. Choose one or more outputs

- evidence only
- evidence + links
- evidence + distilled `library` note
- evidence + wiki update
- evidence + map note update
- evidence + pending-curation follow-up

### Hard rule

- `library.db` is canonical
- wiki is curation support, not canonical truth
- do not write wiki-first and hope to reconcile later

Detailed contract: `agent-memory/plans/CURATION-PIPELINE.md` (if present in this agent)

## Anti-Patterns

- Dumping entire raw docs into the answer when SPR is enough
- Using `expand` on many items at once
- Writing scratch noise into the library
- Treating absence of results as failure instead of valid null retrieval
- Referencing absolute paths under `/home/<user>/agent-memory/...` — those are pre-v3 legacy and may not exist. Use workspace-relative paths always.
