# Always-context

## Memoria durable (usar ANTES de grep/find en filesystem)

- `./scripts/hmk memoryctl.py hybrid-pack --query "..." --limit 4 --threshold 0.4`
- `./scripts/hmk memoryctl.py search --query "..." --limit 5`
- `./scripts/hmk memoryctl.py expand --id N`

**Regla**: ante cualquier pregunta sobre conocimiento del workspace, probar la library PRIMERO. Si da `null_retrieval`, recién ahí buscar en disco.

## Skill de curación

`skill_view librarian` describe convenciones completas: cuándo hybrid-pack vs expand, cómo citar `[mem:N]`, cómo leer handoff vs meta_context.

## Re-hidratación táctica

`./scripts/hmk continuityctl.py rehydrate` devuelve identity + meta_context + dialogue_handoff + memorias exactas en un JSON. Útil tras restart/crash/model switch.

## Wiki

`wiki/` es proyección desde el canon. OK leerla para orientación; NO citarla como evidencia — siempre volver a `library.db`.
