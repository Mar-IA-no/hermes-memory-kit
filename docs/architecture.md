# Architecture

Hermes Memory Kit separates:

1. canon (`library.db`)
2. retrieval (`memoryctl.py`)
3. ingestion (`ingest_any.py`)
4. projection (`export_obsidian.py`)
5. skill (`librarian`)

Principle:

- the wiki helps organize
- the DB decides factual truth
