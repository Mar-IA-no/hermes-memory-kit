# Embedding Benchmarks

Harness for comparing embedding providers against a live Hermes Memory Kit corpus. Designed to produce evidence before flipping the default provider in `.env.example`.

## What it measures

For each query in an input file, the harness runs:

1. **`memoryctl.py semantic-search`** — isolates embedding quality (no FTS5 lexical mix).
2. **`memoryctl.py hybrid-pack`** — end-to-end retrieval impact (lexical + semantic + priors).

Per query, it records:

- Top-k chapter IDs returned
- Latency (wall clock)
- Null-retrieval flag
- If queries are annotated with `expected: id1,id2,id3` → Precision@3, Recall@3, Hit@1, Hit@3

Globally:

- Latency p50 / p95
- Null-retrieval rate (should be 100% on negative controls)
- Storage per `(provider, model, dims)` — via direct SQL (`stats()` doesn't expose dims)

## Files in this dir

| File | What |
|---|---|
| `queries.example.txt` | Template showing the format. **Not your real queries.** |
| `README.md` | This doc. |

## Privacy — where your benchmark outputs live

Reports include chapter IDs, scores, and partial corpus content. **They are NOT meant to be committed to any public repo.**

**Default output directory**: `$HOME/hmk-benchmarks/<timestamp>-<provider>/report.{json,md}` — outside any workspace or repo. Safe by design.

**Your real queries file** should live there too (e.g. `$HOME/hmk-benchmarks/queries.txt`) so it's not tracked anywhere. Copy-edit from `queries.example.txt` in this dir.

## Migration note for existing workspaces

If your workspace was bootstrapped before these scripts existed, the workspace `scripts/` dir won't have them. Refresh it:

```bash
python3 /path/to/hermes-memory-kit/scripts/bootstrap_workspace.py \
    --workspace /my/workspace --upgrade
```

That copies the new scripts (`embed_benchmark.py`, `embed_benchmark_compare.py`, `embed_clear.py`, `embed_verify.py`) into your workspace. Your `library.db` and `.env` are preserved.

## Prerequisites

Your workspace `.env` must have:

```
# Always needed
HMK_BASE_DIR=./agent-memory
HMK_DB_PATH=./agent-memory/library.db

# For the provider(s) you're benchmarking:
NVIDIA_API_KEY=...
GEMINI_API_KEY=...     # or GOOGLE_API_KEY=...

# For Google specifically (MRL dimensionality)
HERMES_EMBED_GOOGLE_OUTPUT_DIMS=768
```

The harness's preflight is provider-aware: it only requires keys for the provider you pass via `--provider`.

## Running the A/B

### Step 1 — Set up a queries file

```bash
mkdir -p $HOME/hmk-benchmarks
cp /path/to/hermes-memory-kit/docs/benchmarks/queries.example.txt $HOME/hmk-benchmarks/queries.txt
# Edit with your real queries. Ideally annotate some with expected IDs.
```

### Step 2 — Baseline NVIDIA

```bash
cd /path/to/your-workspace
./scripts/hmk embed_benchmark.py \
    --queries $HOME/hmk-benchmarks/queries.txt \
    --provider nvidia \
    --model "nvidia/llama-3.2-nemoretriever-300m-embed-v1"
# Outputs: $HOME/hmk-benchmarks/<ts>-nvidia/report.{json,md}
```

### Step 3 — Re-embed with Google @ 768

Before embedding, **clear any prior Google set** (guards against stale dimensions from earlier runs — the PK doesn't include dims):

```bash
./scripts/hmk embed_clear.py --provider google --model gemini-embedding-001 --confirm
```

Then backfill, forcing full recompute via `--all`:

```bash
./scripts/hmk memoryctl.py embed-backfill \
    --provider google \
    --model gemini-embedding-001 \
    --batch-size 8 \
    --all
```

Verify the actual stored dimensionality:

```bash
./scripts/hmk embed_verify.py --provider google
# expected row: google | gemini-embedding-001 | 768 | 211 (or your chapter count)
```

If `dims != 768`, your `.env` doesn't have `HERMES_EMBED_GOOGLE_OUTPUT_DIMS=768` loaded properly. Fix that and repeat step 3.

### Step 4 — Run Google benchmark

```bash
./scripts/hmk embed_benchmark.py \
    --queries $HOME/hmk-benchmarks/queries.txt \
    --provider google \
    --model gemini-embedding-001
# Outputs: $HOME/hmk-benchmarks/<ts>-google/report.{json,md}
```

### Step 5 — Compare

```bash
./scripts/hmk embed_benchmark_compare.py \
    --a $HOME/hmk-benchmarks/<ts>-nvidia \
    --b $HOME/hmk-benchmarks/<ts>-google
# Outputs: $HOME/hmk-benchmarks/comparison-<ts>.md
```

Open the comparison markdown. It has three sections:

1. **Auto metrics** (weak signal) — latency, null-rate. Score distributions shown with a disclaimer ("scores are not comparable across models").
2. **Expected-based** — Precision@3, Recall@3, Hit@k — only for queries you annotated with `expected: ...`. This is the strongest objective signal.
3. **Manual review checklist** — side-by-side top-5 per query with `- [ ] A wins  - [ ] B wins  - [ ] Tie`. Go through and mark.

### Step 6 — Decide

**Switch to Google** only if:
- Expected-based recall@3 on semantic-search favors Google by ≥10%, OR
- Manual review ≥60% "B wins" on semantic-search, OR
- Both agree.

If the verdict is tie or unclear, keep NVIDIA.

## Rollback

If you switch and later want NVIDIA back, just set `HERMES_EMBED_PROVIDER=nvidia` in your workspace `.env`. The NVIDIA set stays in the DB — the switch is zero-risk.

If you want to fully remove the Google set from your DB:

```bash
./scripts/hmk embed_clear.py --provider google --model gemini-embedding-001 --confirm
```

## Cost reference

For a ~200-chapter corpus (~300K tokens total):

- **Google Gemini**: ~$0.05 for full re-embed (input pricing ~$0.15/M tokens).
- **NVIDIA NIM**: usage depends on your plan.
- **Queries**: negligible (tens of tokens each).
