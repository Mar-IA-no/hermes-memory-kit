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

The workspace gets its own `.env`. Copy the example and edit:

```bash
cd /path/to/workspace
cp .env.example .env
$EDITOR .env
```

The default `.env.example` uses paths relative to the workspace root (e.g. `HMK_BASE_DIR=./agent-memory`). That works out of the box — you only need to edit if you want to point any path elsewhere.

Paths you can set:

- `HMK_BASE_DIR` — where memory state / DB live (default: `./agent-memory`)
- `HMK_DB_PATH` — the SQLite library (default: `./agent-memory/library.db`)
- `HMK_VAULT_DIR` — Obsidian-style wiki projection target (default: `./wiki`)
- `HMK_WORKSPACE_ROOT` — resolved automatically by the wrapper; override if you need to
- `HMK_HERMES_HOME` — your Hermes Agent home (only relevant if you use the optional plugin)
- `HMK_AGENT_MEMORY_BASE` — alias for `HMK_BASE_DIR`, used by the plugin
- `HMK_SESSIONS_DIR` — overrides `$HMK_HERMES_HOME/sessions`
- `HMK_DIALOGUE_HANDOFF_PATH` — direct path to the handoff file (rarely needed)

## Running the tooling

Always use the wrapper:

```bash
./scripts/hmk memoryctl.py init
./scripts/hmk memoryctl.py add-text --shelf library --title foo --raw "content" --tags t1
./scripts/hmk memoryctl.py hybrid-pack --query "something" --budget 1800
./scripts/hmk continuityctl.py rehydrate
./scripts/hmk export_obsidian.py --ids 1 2 3
```

The wrapper loads `.env`, absolutizes any relative `HMK_*` paths against the workspace root, and `cd`s to the workspace before executing the target Python script. That makes invocation from any cwd predictable.

## Dependencies

Install once from the repo:

```bash
cd /path/to/hermes-memory-kit
pip install -r requirements.txt
# for local embeddings (optional):
pip install -r requirements-local-embeddings.txt
```

## Optional: install the dialogue-handoff plugin into Hermes Agent

If you also run Hermes Agent, the plugin auto-injects conversational continuity on new sessions. Full instructions in [dialogue-handoff.md](dialogue-handoff.md). Short version:

```bash
cp -r /path/to/workspace/plugins/dialogue-handoff "$HERMES_HOME/plugins/"
# then add plugins.enabled: [dialogue-handoff] to $HERMES_HOME/config.yaml
# and set HMK_AGENT_MEMORY_BASE + HMK_HERMES_HOME in the Hermes systemd unit
systemctl --user daemon-reload && systemctl --user restart hermes-gateway
hermes plugins list | grep dialogue-handoff   # verify: enabled 2.0.0
```

The plugin is pinned against **Hermes Agent v0.10.0** (upstream commit `e710bb1f`). See the plugin doc for details.

## Smoke test

Before relying on a fresh install, run the smoke test once from the repo:

```bash
./scripts/smoke-test.sh
```

It exercises bootstrap → init → add-text → search → pack → export → upgrade, in a throwaway temp workspace.

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
