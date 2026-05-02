# Memory Provider plugin (`hmk-memory`)

Since v3.7.0 the kit ships a memory provider that implements the formal
[`MemoryProvider`](https://github.com/NousResearch/hermes-agent/blob/main/app/agent/memory_provider.py)
ABC of Hermes Agent. This is a different extension point from the
`dialogue-handoff` plugin — they live in different planes and they coexist.

## Two planes, two plugins

| Plane | Plugin | Question it answers |
|---|---|---|
| **Working memory** (per-session, anti-amnesia across sessions / model swaps / context compression) | `dialogue-handoff` (vendored from `hermes-continuity-plugin`) — generic plugin via `pre_llm_call` / `post_llm_call` hooks | "What were we doing last time?" |
| **Long-term memory** (per-turn, fact recall during the live session) | `hmk-memory` — formal `MemoryProvider` via `prefetch(query)` | "What does our knowledge base say about this query?" |

Both ship in the kit and the bootstrap copies both into a new agent. They
don't share state and they don't conflict.

## Architecture

```
┌────────────────┐      prefetch(query)        ┌──────────────────┐
│ Hermes runtime │ ──────────────────────────▶ │   hmk-memory     │
│  (per-turn)    │                              │  MemoryProvider  │
└────────────────┘ ◀──── markdown bullets ──── └──────────────────┘
                                                         │
                                                         │ engram_pack(...)  /  hybrid_pack(...)
                                                         ▼
                                                ┌──────────────────┐
                                                │   memoryctl.py   │
                                                │  (lexical+semantic
                                                │  +rerank +RRF)    │
                                                └──────────────────┘
                                                         │
                                                         ▼
                                                ┌──────────────────┐
                                                │   library.db     │
                                                │  (FTS5 + chapter │
                                                │   embeddings +   │
                                                │   ENGRAM cols)   │
                                                └──────────────────┘
```

When ENGRAM columns are present (see `docs/engram.md`), the provider uses
`engram_pack` to fuse three per-bucket retrievals via Reciprocal Rank Fusion
(`Σ_b 1 / (k + rank_b)` with `k=60`) and per-bucket quotas. When ENGRAM is
not applied, the provider degrades to a single `hybrid_pack` call — same
output shape, no bucket distinction.

## Activation

In the agent's `config.yaml`:

```yaml
memory:
  provider: hmk-memory
```

Restart the gateway. Hermes' "single provider rule" means only one external
memory provider is active at a time. If another provider was active, change
this entry and restart — the old one stops, the new one initializes.

After activation, `hermes hmk-memory status` becomes available; before that,
the subcommand is hidden by Hermes' active-provider gating.

## Configuration (env-var-only)

The provider does NOT participate in `hermes memory setup` (no setup
wizard). All configuration is via env vars in the agent's `.env`. The full
table lives in the plugin's `README.md`. The mandatory variable is
`HMK_AGENT_MEMORY_BASE` — `memoryctl.connect()` requires `BASE_DIR` to
resolve, and that variable is the canonical source.

## Discovery

Hermes finds memory providers by scanning each plugin's `__init__.py` for
the strings `MemoryProvider` or `register_memory_provider`. `plugin.yaml` is
descriptive metadata, not active discovery. This means:

- A typo in `plugin.yaml` does **not** prevent activation.
- A typo in `__init__.py` (or moving `register_memory_provider` out of the
  text scan range) **does** prevent activation.
- Only the file containing the `register()` call needs to be changed when
  upgrading.

## What this provider does NOT do (yet)

The v3.7.0 MVP only implements `prefetch` + `system_prompt_block`. Other
hooks of the `MemoryProvider` ABC are no-op:

- `sync_turn` — turns are not auto-persisted as new chapters. Use the
  existing CLI ingestion path (`memoryctl add-text`, `ingest_any.py`) for
  durable writes.
- `on_pre_compress` — Hermes' context compression runs without per-call
  insight extraction. The `dialogue-handoff` plugin still preserves the
  recent-exchanges tail.
- `get_tool_schemas` — the LLM cannot invoke memory queries on demand.
  Recall is purely pre-emptive via `prefetch`.

These are reserved for future releases. The MVP scope was chosen so the
provider could ship behind a single configuration switch without changing
how chapters are written or compressed.

## Performance characteristics

On a CPU-only deployment with `model2vec` embeddings (kit default since
v3.4):

- First call after restart: includes embedding-model load (~5-10s of
  one-off cost).
- Steady-state `engram_pack`: ~3-10s on modern CPUs, ~10-30s on older
  hardware. The fan-out (3 hybrid passes + RRF) is the dominant cost.
- Steady-state `hybrid_pack` fallback: ~1-3s.

If latency budget is tight, set `HMK_PROVIDER_RETRIEVER=hybrid_pack` and
accept the loss of bucket balance. Future work (v3.8+) may add async /
queued prefetch for next-turn pre-warming via `queue_prefetch`.

## Roadmap

| Version | Scope |
|---|---|
| v3.7.0 (this release) | `prefetch` + `system_prompt_block` over engram_pack/hybrid_pack. Env-driven config. CLI status. |
| v3.8.x (future) | `sync_turn` (auto-persist substantive turns to `chapters` as episodic), `queue_prefetch` (async pre-warm). |
| v3.9.x (future) | `on_pre_compress` (extract durable facts from to-be-compressed messages, write as semantic chapters). |
| v3.10.x (future) | `get_tool_schemas` exposing `search_memory`/`recall(query)` tools to the LLM for on-demand recall. |

These are tentative — concrete scope and timing get committed when the
prior release has stabilized in production for at least a couple of weeks.
