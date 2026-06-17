# Procedimiento de Ingreso Bibliotecario

Versión 1.1 | Research Library

## Objetivo

Garantizar que todo material que ingrese al corpus sea:
- **Trazable**: sabemos de dónde vino, cuándo y cómo se extrajo.
- **Citable**: cada capítulo tiene metadatos precisos (autor, año, capítulo, página).
- **Reproducible**: el PDF original queda en `raw/`; si la extracción falla, se rehace.
- **Descubrible**: queda registrado un puntero en la memoria (`library.db`).

## Flujo

```
[PDF recibido] → [Verificación de duplicado] → [Identificación de tópico]
    → [Extracción + limpieza] → [Segmentación en capítulos]
    → [meta.json] → [Actualización de índices]
    → [Puntero de catálogo en library.db] → [Cierre]
```

## Reglas de oro

1. **Nunca modificar el PDF original** en `raw/`. Es la fuente de verdad.
2. **Nunca subir capítulos sin limpieza** (soft-hyphens, headers, pies).
3. **Nunca omitir `meta.json`**. Sin metadatos, el material no es citable.
4. **Nunca borrar un libro sin archivar** (`topics/_archive/` + nota de razón).
5. **Siempre verificar `wc -w` real** contra `meta.json` antes de cerrar.
6. **Siempre registrar el puntero** en `library.db` al cerrar.

## Limpieza (la hace `library_extract.py corpus`, verificar resultado)

- [ ] Headers de obra/autor removidos.
- [ ] Pies: números de página solitarios removidos.
- [ ] Soft-hyphens unidos (`bioló\xad\ngico` → `biológico`).
- [ ] Párrafos preservados (líneas unidas dentro de párrafo, `\n\n` entre párrafos).
- [ ] Espacios múltiples normalizados.
- [ ] Bullets y listas preservados.

## Compartir entre agentes hermanos

El corpus es por-workspace. Un agente hermano que comparta el corpus debe conocer:
la raíz (`$LIBRARY_ROOT`), el `schema_version` actual, y este skill como procedimiento
estándar. Si el corpus se comparte físicamente, coordinar para evitar slugs duplicados.

## Historial

- v1.1 — generalizado a HMK: paths workspace-relative, extractor consolidado
  (`library_extract.py`), bridge obligatorio a `library.db`.
- v1.0 — estructura inicial (origen: Chiwa Research Library).
