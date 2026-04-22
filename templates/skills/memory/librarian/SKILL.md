---
name: librarian
description: Use the local Hermes Memory Kit library as canonical durable memory and the wiki layer as projected navigation.
version: 0.1.0
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
python3 scripts/memoryctl.py hybrid-pack --query "USER_QUESTION" --budget 1800 --limit 4 --threshold 0.4
```

## Expand

```bash
python3 scripts/memoryctl.py expand --id 17
```

## Ingest

```bash
python3 scripts/ingest_any.py \
  --source /path/to/source \
  --shelf evidence \
  --title "descriptive-title" \
  --tags source,new \
  --importance 0.7
```

## Embeddings

```bash
python3 scripts/memoryctl.py embed-config
```

## Curation

Detailed workflow:

- `docs/curation-pipeline.md`
