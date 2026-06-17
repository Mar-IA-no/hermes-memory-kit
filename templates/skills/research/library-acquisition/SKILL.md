---
name: library-acquisition
description: |
  Procedimiento estricto de ingreso bibliotecario para la Research Library — el
  corpus de fuentes primarias citables del agente (distinto de `library.db`, que
  es la memoria). Todo material nuevo (PDF, libro, documento) pasa por este flujo
  antes de ser citable: extracción + limpieza, estructura de directorios,
  metadatos, índices, y registro de un puntero en la memoria para que sea
  descubrible. EN: strict acquisition procedure for the citable primary-source
  corpus (the Research Library), separate from the memory store.
version: 1.0.0
author: Local System
license: MIT
metadata:
  hermes:
    tags: [Library, Corpus, Acquisition, PDF, Citations, Research]
    related_skills: [librarian, pdf-to-audio]
prerequisites:
  commands: [python3]
  python: [pymupdf]
---

# Procedimiento de Ingreso Bibliotecario

La **Research Library** es el corpus de **fuentes primarias** del agente: documentos
completos, fielmente extraídos, citables a nivel capítulo/página. Es **distinta de la
memoria** (`agent-memory/library.db`, ver skill [`librarian`](../../memory/librarian/SKILL.md)):

| | Research Library (corpus) | `library.db` (memoria) |
|---|---|---|
| Qué guarda | Texto primario completo (libros, papers) | Notas destiladas, decisiones, punteros |
| Cita | *(Autor, Año, cap. X, p. Y)* | `[mem:N]` |
| Acceso | filesystem (`$LIBRARY_ROOT`) | `./scripts/hmk memoryctl.py` |

**Regla de oro del stack:** el corpus es la verdad del texto primario; la memoria
indexa/apunta a él. Por eso el cierre de cada ingreso **registra un puntero en la
memoria** (paso 6) — así el libro se descubre con `librarian`/`hybrid-pack` y `expand`
salta al `.txt` del corpus.

## 0. Paths (workspace-relative)

El corpus vive en `$LIBRARY_ROOT` (default `$HMK_WORKSPACE_ROOT/library`). Todos los
comandos se corren desde el workspace root usando el wrapper `./scripts/hmk`, que
absolutiza los paths `HMK_*`/`LIBRARY_ROOT` y carga el `.env` del agente. Nunca
hardcodear rutas absolutas de un agente específico.

## 1. Prerequisitos

- `pymupdf` instalado en el intérprete activo (el `execute_code` sandbox NO lo tiene;
  correr `library_extract.py` por el tool `terminal` con el python del venv).
- Acceso de escritura a `$LIBRARY_ROOT`.
- El PDF disponible en una ruta local (los uploads suelen vivir en `cache/documents/`).

## 2. Recepción

1. **Verificar duplicado**: buscar en `topics/**/books/*/meta.json` si el libro ya
   existe (por ISBN, título, o autor + año).
2. **Identificar tópico**: `topic_id` en snake_case (≤30 chars). Si es nuevo, se crea solo.
3. **Asignar slug**: `slug = slugify(título)` (snake_case, sin acentos, ≤60 chars).

## 3. Extracción (un solo extractor)

```bash
./scripts/hmk library_extract.py corpus <pdf_path> <topic_id> <book_slug> \
  --title "Título" --author "Autor" [--year 2022] [--isbn ...] [--language es]
```

Esto, en un paso: copia el PDF a `raw/`, extrae **todas** las páginas con PyMuPDF,
limpia (headers/pies, soft-hyphens, espacios), escribe `chapters/00_completo.txt`,
crea `meta.json`, y actualiza los índices de tópico y maestro. Crea la estructura:

```
topics/<topic_id>/books/<book_slug>/
├── meta.json
├── chapters/   # 00_completo.txt (y capítulos segmentados luego)
├── raw/        # PDF original — NUNCA se modifica
└── annotations/
```

> Para extracción plana one-off (traducir/narrar un PDF que no va al corpus) usar el
> modo `dump` del mismo script — ver skill [`pdf-to-audio`].

## 4. Segmentación en capítulos

Si el libro tiene tabla de contenidos clara, dividir `00_completo.txt` en
`##_titulo_normalizado.txt` (dos dígitos, snake_case, sin acentos) y actualizar el
array `chapters` de `meta.json` con `num`, `title`, `pages_pdf`, `words`, `file`.

## 5. Validación

- `total_words` en `meta.json` debe coincidir con `wc -w` real de los capítulos.
- Confirmar que el char-count reportado corresponde a un libro completo, no un fragmento.

## 6. Cierre de ingreso — bridge a memoria (OBLIGATORIO)

Registrar un **puntero de catálogo** en `library.db` apuntando al corpus, para que el
libro sea descubrible vía retrieval normal:

```bash
./scripts/hmk memoryctl.py add-file \
  --shelf evidence \
  --path topics/<topic_id>/books/<book_slug>/meta.json \
  --title "<Título> — catálogo de biblioteca" \
  --tags corpus,book,<topic_id> \
  --importance 0.7
```

Checklist:
- [ ] PDF en `raw/`, capítulos limpios en `chapters/`
- [ ] `meta.json` creado y validado (`wc -w`)
- [ ] Índices de tópico y maestro actualizados
- [ ] **Puntero de catálogo registrado en `library.db`**
- [ ] Skill revisado si apareció un edge case nuevo

## 7. Citas

Citar material del corpus como *(Autor, Año, cap. X, p. Y)*. Si no hay página exacta
(PDF reestructurado), usar *(Autor, Año, cap. X)*.

## 8. Retiro

Nunca borrar un libro sin archivar: mover a `topics/_archive/` con una nota de razón,
y marcar/retirar el puntero en `library.db`.

---

Detalle del schema y checklist de limpieza: `references/library-schema.md` y
`references/procedimiento.md`.
