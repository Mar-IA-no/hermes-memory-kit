# ENGRAM taxonomy

The kit's library.db extends `chapters` with an explicit memory-type taxonomy
inspired by classical episodic / semantic / procedural memory partitioning.
This lets retrieval be balanced across kinds of knowledge instead of collapsing
into a single semantic-similarity ranking.

## Schema

`chapters` gains four columns (added by `scripts/migrate-engram.py`):

| Column | Type | Notes |
|---|---|---|
| `engram_type` | `TEXT NOT NULL DEFAULT 'semantic'` | `CHECK in ('episodic','semantic','procedural')` |
| `event_ts` | `INTEGER NULL` | Unix ts of the event itself (not the row INSERT). |
| `actor` | `TEXT NULL` | Who did it (free-form). |
| `location_json` | `TEXT NULL` | Coords / place context as JSON. |

Indexes added: `idx_chapters_engram`, `idx_chapters_event_ts`, `idx_chapters_actor`.

The migration is idempotent. Re-running on an already-migrated DB is a no-op
(after taking the backup).

## Buckets

| Bucket | Purpose | Typical content |
|---|---|---|
| `episodic` | What happened, when. | Session logs, daily summaries, interaction transcripts. |
| `semantic` | Durable facts. | User preferences, place names, behavioral patterns, world descriptions. |
| `procedural` | How to do things. | Skills, plans, runbooks, "if X then Y" recipes. |

## Default shelf → engram_type mapping

`migrate-engram.py` applies this heuristic on the first run:

| Shelf name | engram_type |
|---|---|
| `mc-episodic`, `episodes` | `episodic` |
| `mc-skills`, `plans` | `procedural` |
| `mc-social`, `mc-places`, `library`, `identity`, `evidence`, `state` | `semantic` |
| (anything else) | `semantic` (column default) |

If your shelves don't match these names, edit `SHELF_TO_ENGRAM` in the
migration script before running, or `UPDATE chapters SET engram_type=...`
manually after the fact.

## Running the migration

```bash
HMK_DB_PATH=/path/to/library.db \
  python3 scripts/migrate-engram.py
```

Backs up to `<db>.bak.preengram.<unix_ts>` first, then applies DDL +
shelf-based UPDATE pass + `event_ts := created_at` for all episodic rows.

Output ends with the post-migration distribution per bucket.

## engram-pack — RRF retrieval across buckets

Once chapters are typed, use `memoryctl.py engram-pack` to retrieve a
balanced mix instead of running `hybrid-pack` per bucket and concatenating.

```bash
./scripts/hmk memoryctl.py engram-pack \
  --query "build a nighttime shelter" \
  --limit 6 \
  --threshold 0.3 \
  --shelf mc-episodic,mc-skills,mc-places,library \
  --quota-episodic 2 --quota-semantic 4 --quota-procedural 2 \
  --rrf-k 60
```

It runs `hybrid_pack` separately for each `engram_type`, then fuses the three
ranked lists using **Reciprocal Rank Fusion** (RRF):

```
rrf_score(d) = Σ_b  1 / (k + rank_b(d))
```

with `k = 60` (a robust default from the IR literature). Quotas
(`--quota-episodic`, `--quota-semantic`, `--quota-procedural`) guarantee a
minimum number of items from each bucket before the rest of the prompt
budget is filled by RRF ranking.

The output is a JSON object with `items[]` ordered by `rrf_score`, each
carrying its source `engram_type` so the caller can render bucket-aware
sections in the prompt.

## Filtering by engram_type in other commands

`memoryctl.py search`, `semantic-search`, `pack`, and `hybrid-pack` accept
filtering to a subset of buckets via the underlying `engram_types` kwarg.
The CLI does not expose this directly — `engram-pack` is the typed entry
point. If you need explicit single-bucket retrieval in a script, call the
Python API:

```python
from memoryctl import hybrid_pack
items = hybrid_pack(
    query="how to build a furnace",
    shelves=["mc-skills", "plans"],
    engram_types=["procedural"],
    limit=8,
)
```

## Backfill: extracting semantic facts from episodic chapters

`scripts/backfill-semantic.py` walks every `engram_type='episodic'` chapter
in a configurable shelf range, asks Hermes to extract durable facts via a
short prompt, and inserts them as new `engram_type='semantic'` chapters
under an `engram-backfill` book in the appropriate shelf:

| Fact type | Target shelf |
|---|---|
| `social` | `mc-social` |
| `place` | `mc-places` |
| `skill_pattern` | `mc-skills` |
| `preference`, `discovery` | `library` |

Each new chapter is tagged `engram-backfill`, `<fact_type>`, and
`src-chapter-<source_id>` so you can audit or undo a run with a single
SQL `DELETE WHERE tags_json LIKE '%engram-backfill%'`.

The default prompt is generic; set `HMK_AGENT_NAME` and `HMK_DOMAIN_DESC`
to specialize it, or pass `--prompt-file` for a fully custom template.

Recommended first run:

```bash
HMK_DB_PATH=/path/to/library.db \
HMK_HERMES_HOME=/path/to/hermes-home \
HMK_AGENT_NAME=onaiclaw \
HMK_DOMAIN_DESC="plays Minecraft alongside human commanders" \
  python3 scripts/backfill-semantic.py --limit 5 --dry-run
```

That extracts facts from the first 5 episodic chapters and prints them
without writing. Inspect the output, then re-run without `--dry-run` and
without `--limit` to backfill the full corpus.

## Few-shot retrieval pattern

The reference deployment (hermes-prime) uses `engram-pack` as a few-shot
retriever inside the agent's per-turn prompt builder:

1. Build a query from the latest user message + current task + survival state.
2. Call `engram-pack` with quotas calibrated to the use case.
3. Render `items[]` as bullet points grouped by `engram_type`.
4. Inject as a "Memoria relevante" section before the user-turn block.

This keeps episodic recall, factual grounding, and skill recipes visible
to the model in every turn, in proportions controlled by the quotas
(rather than whatever raw similarity ranking happens to surface).
