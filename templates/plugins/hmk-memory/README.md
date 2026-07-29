# hmk-memory

Long-term memory provider for [Hermes Agent](https://github.com/NousResearch/hermes-agent),
backed by [hermes-memory-kit](https://github.com/Mar-IA-no/hermes-memory-kit)'s
`library.db`.

This plugin implements the formal `MemoryProvider` ABC. On every API call,
Hermes invokes `prefetch(query)` and the returned text is injected into the
turn so the LLM can see relevant long-term memory without being asked to
search for it.

## What you get

When the DB has the ENGRAM schema applied (see `scripts/migrate-engram.py`),
each turn receives a "Memoria relevante" block balanced across **episodic**,
**semantic**, and **procedural** chapters via Reciprocal Rank Fusion
(`memoryctl.engram_pack`).

When ENGRAM is not applied, the provider degrades automatically to
`memoryctl.hybrid_pack` — a single lexical+semantic+rerank pass with no
bucket partitioning. The plugin still works; the prompt block just doesn't
distinguish kinds of knowledge.

This plugin is orthogonal to `dialogue-handoff` (the working-memory plugin
also shipped by the kit). Both can run at the same time.

## Setup

### Required env

`HMK_AGENT_MEMORY_BASE` is the only **mandatory** variable. It points at the
agent's memory directory; `library.db` is expected at `<base>/library.db`.

```bash
HMK_AGENT_MEMORY_BASE=/path/to/your-agent/agent-memory
```

`memoryctl.connect()` enforces this contract: BASE_DIR must resolve OR the
process exits with code 2 on the first call. Setting `HMK_DB_PATH` alone is
**not** enough — BASE_DIR is read from `HMK_AGENT_MEMORY_BASE` /
`AGENT_MEMORY_BASE` / `HMK_BASE_DIR`, none of which `HMK_DB_PATH` populates.

### Optional env (provider knobs)

| Variable | Default | Purpose |
|---|---|---|
| `HMK_PROVIDER_RETRIEVER` | `engram_pack` | `engram_pack` or `hybrid_pack`. Auto-falls-back to `hybrid_pack` if ENGRAM columns are absent. |
| `HMK_PROVIDER_LIMIT` | `8` | Max items returned per prefetch. |
| `HMK_PROVIDER_THRESHOLD` | `0.30` | Score threshold below which items are skipped. |
| `HMK_PROVIDER_BUDGET_TOKENS` | `1500` | Max tokens of context returned. Smaller than the batch default (4000) because prefetch runs per turn. |
| `HMK_PROVIDER_QUOTA_EPISODIC` | `2` | Min items per bucket guaranteed before RRF fills the rest (engram_pack only). |
| `HMK_PROVIDER_QUOTA_SEMANTIC` | `4` | idem |
| `HMK_PROVIDER_QUOTA_PROCEDURAL` | `2` | idem |
| `HMK_PROVIDER_SHELVES` | empty | CSV of shelves to filter to (e.g. `library,evidence,plans`). Empty = all. |
| `HMK_DB_PATH` | derived | Override DB path when it does NOT live at `<base>/library.db`. Does **not** replace `HMK_AGENT_MEMORY_BASE`. |
| `HMK_MEMORYCTL_PATH` | auto-detected | Override memoryctl.py path. Only needed for non-standard deploys (see Layout note below). |
| `HMK_HERMES_HOME` / `HERMES_HOME` | from kwargs | Profile dir; usually provided by Hermes via `initialize()` kwargs. |

### Memoryctl layout autodetection

The provider auto-detects where `memoryctl.py` lives by probing two known
layouts (in this order) relative to the `hermes_home` Hermes hands it:

| Layout | hermes_home example | Where memoryctl.py is |
|---|---|---|
| Workspace (default for `bootstrap_agent.py`) | `~/agents/steve/hermes-home/` | `~/agents/steve/scripts/memoryctl.py` |
| Root (when hermes_home IS the workspace) | `~/.hermes/` | `~/.hermes/scripts/memoryctl.py` |

If neither matches (e.g. you keep `memoryctl.py` outside the agent tree),
set `HMK_MEMORYCTL_PATH` to the absolute path. The override is checked
first and bypasses both probes.

## Activate it

In your agent's `config.yaml`:

```yaml
memory:
  provider: hmk-memory
```

Restart the gateway. Hermes' single-provider rule means only one external
memory provider is active at a time — if you have another (`mem0`,
`hindsight`, `openviking`, etc.) you'll need to choose.

## The `librarian` tool

When this provider is active, agents also get a `librarian` model tool for
read/write access to the library from inside the conversation. Actions:

| Action | Purpose |
|---|---|
| `query` | Hybrid (lexical + semantic) retrieval, ranked items |
| `search` | Pure lexical FTS search |
| `add_text` | Store a new text chapter under a shelf |
| `add_file` | Ingest a file from disk into a shelf |
| `expand` | Full record for a chapter id, including neighbors |
| `update` | Edit a chapter in place (content/title/tags/importance); drops stale embeddings when content or title change |
| `delete` | Remove a chapter; cascades embeddings/links, prunes the empty parent book |
| `stats` | Library counts and embedding metadata |
| `add_link` | Create a directed link between two chapters |

The same lifecycle is available to operators as
`hermes hmk-memory <query|search|add-text|add-file|expand|update|delete|stats|link>`
and as `memoryctl.py <update|delete>` for script use.

`update` only changes the fields you pass. When content or title change, the
chapter's stored embeddings are dropped so the next `embed-backfill`
recomputes them from the new text — stale vectors would keep ranking the old
content. `delete` returns the deleted chapter's `raw_sha256` so callers can
verify an archive taken beforehand.

## Verify

After activation, `hermes hmk-memory status` prints DB path, ENGRAM presence,
chapter counts per bucket, backfill counts, and the provider's effective
config. The subcommand only registers when this provider is the active one.

```
$ hermes hmk-memory status
hmk-memory status
------------------------------------------------------------
BASE_DIR      : /home/onairam/agents/hermes-prime/agent-memory
DB_PATH       : /home/onairam/agents/hermes-prime/agent-memory/library.db
DB size       : 36,712,448 bytes
ENGRAM applied: yes

Chapter counts by engram_type:
  semantic        237
  episodic          9
  procedural        8

Engram-backfill books per shelf:
  mc-places           6
  mc-skills           3
  library             3

Provider config (effective env):
  retriever          : engram_pack
  limit              : 8
  threshold          : 0.3
  budget_tokens      : 1500
  ...
```

## Performance notes

`engram_pack` runs `hybrid_pack` three times (one per bucket), each of which
does a binary-quantized prefilter, a float32 rescore, and a TinyBERT rerank.
On a CPU-only host with `model2vec` embeddings (the kit's v3.4+ default),
expect:

- `engram_pack`: ~3-10s per turn on a fast laptop, ~10-30s on older CPUs.
- `hybrid_pack` (single-pass fallback): ~1-3s per turn.

If your hardware is the bottleneck and you need lower latency, set
`HMK_PROVIDER_RETRIEVER=hybrid_pack` to skip the per-bucket fan-out at the
cost of bucket balance.

## Behavior with empty DB

If the DB has no chapters that score above `HMK_PROVIDER_THRESHOLD`, prefetch
returns an empty string silently. The agent gets nothing instead of an error
or a "no results" placeholder.

## Troubleshooting

- **`hermes: invalid choice: hmk-memory`** after configuring it: the provider
  did not register. Check journalctl for `hmk-memory` log lines. Common
  causes: `is_available()` returned False (BASE_DIR unset, DB missing, DB
  corrupt), or another memory provider grabbed the slot first.
- **Prefetch returns nothing** but you expect hits: check the threshold,
  check that the embeddings table is populated for your active provider
  (`memoryctl stats`), check shelves filter.
- **First-turn latency spike**: the embedding model loads lazily on the
  first `prefetch()`. Subsequent turns are faster.

## Discovery

Hermes finds this plugin by scanning `__init__.py` for the strings
`MemoryProvider` and `register_memory_provider`. `plugin.yaml` is metadata
only — not used by the runtime for activation. The activation source of
truth is `config.yaml: memory.provider`.

## Limitations (v3.7.0 MVP)

- No tools exposed to the LLM (`get_tool_schemas` returns `[]`). The model
  cannot ask for memory; it's served pre-emptively via prefetch.
- `sync_turn` is a no-op — turns are not auto-persisted as new chapters.
- `on_pre_compress` is a no-op — pre-compression insight extraction is
  reserved for v3.8+.

These are deliberate scope choices. Subsequent releases extend the surface.
