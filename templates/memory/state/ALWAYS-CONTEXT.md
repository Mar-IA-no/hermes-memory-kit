# Always-context

## Re-entry (crítico — cuando user pregunta "en qué estábamos")

- `dialogue_handoff` es el hilo conversacional real. `meta_context` (ACTIVE-CONTEXT.md, NOW.md) es meta-ingeniería del sistema de memoria — **NO lo cites como si fuera la conversación**.
- Si el user menciona un tema que NO aparece en tu handoff actual, es una sesión previa. Antes de decir "no encuentro", probá:
  - `ls ~/agents/hermes-prime/hermes-home/sessions/ | tail -20`
  - `grep -l "TEMA" ~/agents/hermes-prime/hermes-home/sessions/*.json`

## Memoria durable (ANTES de grep en filesystem)

- `./scripts/hmk memoryctl.py hybrid-pack --query "..." --limit 4 --threshold 0.4`
- `./scripts/hmk memoryctl.py search --query "..." --limit 5`
- `./scripts/hmk memoryctl.py expand --id N`

Regla: library PRIMERO. Si `null_retrieval`, recién ahí grep/find.

## Rehydration

`./scripts/hmk continuityctl.py rehydrate` → identity + meta_context + dialogue_handoff + memorias en JSON.

## Wiki

`wiki/` = proyección del canon. OK leerla, NO citarla como evidencia.

## Skill de curación

`skill_view librarian` = convenciones completas.
