# Always-context

## Re-entry (crítico — cuando user pregunta "en qué estábamos")

**Orden de autoridad, de más a menos confiable:**

1. **`<previous_session_context>` inyectado al primer turn**: ESTE ES EL HILO CONVERSACIONAL AUTORITATIVO. Si menciona el tema que pregunta el user, citá sus detalles (nombres de variables, números, decisiones, opciones propuestas) **antes** de buscar en ningún otro lado. No digas "no tengo contexto" si el bloque existe y toca el tema — aunque sea breve, es la verdad más reciente.
2. **`dialogue_handoff` (DIALOGUE-HANDOFF.md)**: mismo contenido que el injected, leeslo si no lo tenés inyectado o si necesitás ver exchanges más viejos que el tail.
3. **Sessions JSON (`$HMK_SESSIONS_DIR/*.json`)**: fallback **sólo** si (a) el user menciona un tema que NO aparece en el handoff/injected, o (b) necesitás detalles más viejos que el rolling window. No grepear sessions como primer reflejo.
4. **`meta_context` (ACTIVE-CONTEXT.md, NOW.md)**: meta-ingeniería del sistema de memoria — **NO lo cites como si fuera la conversación**.

**Regla práctica**: si el `<previous_session_context>` mencionado al inicio del turn cita el tema → usalo y citá detalles concretos. Sólo si no aparece o está vacío, pasá al fallback (sessions → library).

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
