# Embedding Providers

## Soportados

- `nvidia`
- `google`
- `local`

## Variables

### NVIDIA

- `NVIDIA_API_KEY`
- `HERMES_EMBED_NVIDIA_MODEL`

### Google / Gemini

- `GEMINI_API_KEY` o `GOOGLE_API_KEY`
- `HERMES_EMBED_GOOGLE_MODEL`
- `HERMES_EMBED_GOOGLE_OUTPUT_DIMS`

### Local

- `HERMES_EMBED_LOCAL_MODEL`

## Default

El toolkit arranca con:

- provider default `nvidia`

## Regla importante

No mezclar scores de embeddings de distintos modelos como si fueran comparables.
La DB soporta múltiples sets por `(chapter_id, provider, model)`, pero cada retrieval debe consultar un provider/model consistente.
