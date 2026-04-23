# Always-context

## Durable Memory (use BEFORE grep/find on the filesystem)

- `./scripts/hmk memoryctl.py hybrid-pack --query "..." --limit 4 --threshold 0.4`
- `./scripts/hmk memoryctl.py search --query "..." --limit 5`
- `./scripts/hmk memoryctl.py expand --id N`

**Rule**: for any question about workspace knowledge, try the library FIRST. If it returns `null_retrieval`, only then search on disk.

## Curation Skill

`skill_view librarian` describes the full conventions: when to use hybrid-pack vs expand, how to cite `[mem:N]`, and how to read handoff vs meta_context.

## Tactical Rehydration

`./scripts/hmk continuityctl.py rehydrate` returns identity + meta_context + dialogue_handoff + exact memories in a single JSON. Useful after restart/crash/model switch.

## Wiki

`wiki/` is a projection from the canon. It is OK to read for orientation; do NOT cite it as evidence — always return to `library.db`.
