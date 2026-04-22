# Hermes Memory Kit

Toolkit reusable para montar una memorioteca local para agentes tipo Hermes.

## Qué trae

- `scripts/memoryctl.py`
  - almacenamiento canónico en SQLite
  - FTS5
  - retrieval lexical e híbrido
  - soporte de embeddings por provider
- `scripts/ingest_any.py`
  - normalización de fuentes heterogéneas a markdown
- `scripts/export_obsidian.py`
  - proyección del canon a un vault tipo Obsidian / LLM Wiki
- `templates/`
  - `AGENTS.md`
  - skill `librarian`
  - estructura base de memoria
- `docs/`
  - arquitectura
  - instalación
  - curación
  - providers de embeddings

## Principios

- canon primero, proyección después
- `library.db` como verdad canónica
- `wiki/` como capa curada de navegación
- embeddings desacoplados del provider
- nada de loops autónomos por defecto

## Estructura esperada

```text
your-workspace/
├── AGENTS.md
├── agent-memory/
│   ├── identity/
│   ├── state/
│   ├── plans/
│   ├── episodes/
│   ├── library/
│   ├── evidence/
│   ├── index/
│   └── library.db
├── wiki/
└── .env
```

## Variables principales

Copiá `.env.example` a `.env` y ajustá según tu host.

Variables más importantes:

- `HMK_BASE_DIR`
- `HMK_DB_PATH`
- `HMK_VAULT_DIR`
- `HMK_ENV_FILE`
- `HMK_HERMES_HOME`
- `HMK_WORKSPACE_ROOT`
- `HERMES_EMBED_PROVIDER`
- `HERMES_EMBED_MODEL`
- `NVIDIA_API_KEY`
- `GEMINI_API_KEY`

## Providers de embeddings

Backends soportados estructuralmente:

- `nvidia`
- `google`
- `local`

Default actual del toolkit:

- `nvidia`

## Bootstrapping

1. cloná el repo
2. copiá `.env.example` a `.env`
3. creá la estructura base:

```bash
python3 scripts/bootstrap_workspace.py --workspace /path/to/workspace --with-wiki-templates
```

4. inicializá la DB:

```bash
python3 scripts/memoryctl.py init
```

5. inspeccioná config de embeddings:

```bash
python3 scripts/memoryctl.py embed-config
```

## Ingesta

```bash
python3 scripts/ingest_any.py \
  --source /path/to/file.pdf \
  --shelf evidence \
  --title "paper-x" \
  --tags pdf,source \
  --importance 0.7
```

## Retrieval

```bash
python3 scripts/memoryctl.py hybrid-pack --query "..." --budget 1800 --limit 4 --threshold 0.4
```

## Obsidian / LLM Wiki

```bash
python3 scripts/export_obsidian.py --ids 1 2 3
```

## Estado del repo

Este repo es una extracción portable del sistema construido en una notebook real de experimentación.  
Ya está desacoplado de rutas fijas gruesas, pero sigue en fase de hardening.  
Lo correcto es usarlo como base reusable y seguir puliéndolo con pruebas en otros hosts.

## Docs rápidas

- [Install](./docs/install.md)
- [Architecture](./docs/architecture.md)
- [Curation Pipeline](./docs/curation-pipeline.md)
- [Providers](./docs/providers.md)
