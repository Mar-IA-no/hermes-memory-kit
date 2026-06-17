# Research Library — schema

Corpus documental de fuentes primarias citables. Vive en `$LIBRARY_ROOT`
(default `$HMK_WORKSPACE_ROOT/library`). Distinto de `agent-memory/library.db`
(memoria) — ver el skill `librarian`.

## Estructura

```
$LIBRARY_ROOT/
├── index.json                      # catálogo maestro de tópicos
└── topics/
    └── <topic_id>/
        ├── index.json              # libros del tópico
        └── books/
            └── <book_slug>/
                ├── meta.json       # metadatos bibliográficos + capítulos
                ├── chapters/       # capítulos extraídos como .txt
                │   └── 00_completo.txt
                ├── raw/            # PDF original — inmutable
                └── annotations/    # resúmenes, citas, notas
```

## `meta.json`

```json
{
  "book":   { "title": "...", "author": "...", "year": "...", "isbn": "...", "language": "..." },
  "source": { "original_filename": "...", "raw_path": "raw/<file>.pdf" },
  "extraction": { "method": "pymupdf_page_text", "date": "YYYY-MM-DD",
                  "coverage_pages": "X-Y", "cleaning": ["..."] },
  "chapters": [ { "num": "00", "title": "...", "pages_pdf": "X-Y", "words": N, "file": "chapters/00_...txt" } ],
  "annotations": {},
  "total_chapters": N,
  "total_words": N
}
```

## Convenciones

- **`topic_id`**: snake_case, ≤30 chars.
- **`book_slug`**: snake_case, sin acentos, ≤60 chars.
- **Capítulos**: `##_titulo_normalizado.txt` (dos dígitos). Texto limpio: sin
  headers/pies, sin soft-hyphens, párrafos preservados.
- **Cita**: *(Autor, Año, cap. X, p. Y)*.

## Índices

- `index.json` (maestro) — lista de tópicos.
- `topics/<topic_id>/index.json` — libros del tópico.
- `books/<slug>/meta.json` — metadatos + capítulos del libro.

## Bridge a memoria

Cada libro adquirido registra un puntero en `library.db` (shelf `evidence`)
apuntando a su `meta.json`, para que sea descubrible vía `librarian`/`hybrid-pack`.

Schema version: 1.0
