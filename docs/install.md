# Install

## Requisitos mínimos

- Python 3.11+
- SQLite con FTS5
- `pdftotext` para PDFs
- `pandoc` recomendado para formatos heterogéneos

## Setup base

```bash
git clone <repo-url>
cd hermes-memory-kit
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## Workspace bootstrap

```bash
python3 scripts/bootstrap_workspace.py --workspace /path/to/workspace --with-wiki-templates
```

## Variables

Copiá:

```bash
cp .env.example /path/to/workspace/.env
```

Ajustá según tu host:

- `HMK_BASE_DIR`
- `HMK_DB_PATH`
- `HMK_VAULT_DIR`
- `HMK_WORKSPACE_ROOT`
- `HMK_HERMES_HOME`

## Inicialización

Desde el workspace o exportando las variables necesarias:

```bash
python3 scripts/memoryctl.py init
python3 scripts/memoryctl.py stats
python3 scripts/memoryctl.py embed-config
```

## Embeddings locales opcionales

```bash
pip install -r requirements-local-embeddings.txt
```

No asumir compatibilidad de CPU sin probarla.
