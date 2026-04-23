# Multi-agent deployment

Hermes Memory Kit v3.0+ treats **one directory = one fully isolated agent**. This page explains what that means in practice and how to deploy several agents side-by-side without cross-contamination.

## The rule

For each agent, everything it needs lives inside its own workspace:

```
~/agents/<name>/
├── AGENTS.md            operator-facing notes
├── .env                 symlink → hermes-home/.env (canonical)
├── hermes-home/         HERMES_HOME — config, SOUL, memories, sessions, plugins
│   └── .env             ← canonical real file (Hermes upstream writes here with os.replace)
├── agent-memory/        durable memory (library.db, state, episodes, plans, index)
├── wiki/                optional projection
├── scripts/             tooling (hmk, memoryctl, continuityctl, ...)
├── app/                 hermes-agent upstream clone (user installs)
└── venv/                Python venv for app/ (user creates)

> The `.env` is canonically located at `hermes-home/.env` because Hermes Agent
> upstream rewrites it via `os.replace()` (atomic rename) in `config.py:3292/3395/3451`
> — a symlink at that path would be replaced by a regular file on the first save.
> The symlink in the agent root is relative (`hermes-home/.env`), so both the
> `hmk` wrapper (reads from workspace root) and the systemd template
> (`EnvironmentFile=%h/agents/%i/.env`) follow it to the real file.
```

Agents **never share** any of: config, SOUL, memory, sessions, plugins, skills, library.db, wiki, or scripts. If you run the library on host Y and configure agent X wrong, X gets an explicit error — it cannot silently fall back into another agent's files.

Only thing that may be shared: an external read-only corpus you want every agent to see (e.g. a research library). That is not the kit's concern — keep it outside `~/agents/`.

## Bootstrapping a new agent

```bash
python3 /path/to/hermes-memory-kit/scripts/bootstrap_agent.py \
    ~/agents/hermes-alfa --name hermes-alfa --with-wiki-templates
```

Notes:
- `--name` defaults to the basename of the directory. It must match `^[a-z0-9][a-z0-9-]*$` (lowercase alphanumeric + dash, first char not a dash). The name is used as a systemd instance identifier and as the workspace basename.
- If the target already looks like a v2.x workspace (`agent-memory/` present, `hermes-home/` absent), bootstrap refuses to run and points to `docs/migration-v3.md`.
- Bootstrap does **not** clone `hermes-agent` nor create the venv. That's documented as a manual next step in the bootstrap output.

## Enabling the systemd unit

Install the template once per host, then enable one instance per agent:

```bash
mkdir -p ~/.config/systemd/user
cp /path/to/hermes-memory-kit/templates/systemd/hermes-gateway@.service \
   ~/.config/systemd/user/
systemctl --user daemon-reload

systemctl --user enable --now hermes-gateway@hermes-alfa.service
systemctl --user enable --now hermes-gateway@hermes-beta.service
```

The template assumes agents live at `%h/agents/%i/`. If an agent lives somewhere else (different disk, shared mount), generate a non-template unit with absolute paths — `bootstrap_agent.py` prints a hint when the location is non-standard.

## What gets shared — nothing

Every script in the kit checks the cascade and hard-fails if the agent's `.env` is missing or misconfigured:

| Resource | Canonical env | Legacy fallbacks | If unset |
| --- | --- | --- | --- |
| Agent memory root | `HMK_AGENT_MEMORY_BASE` | `AGENT_MEMORY_BASE`, `HMK_BASE_DIR` | hard fail |
| Library DB | `HMK_DB_PATH` (optional override) | derived from root | hard fail |
| Hermes home | `HMK_HERMES_HOME` | `HERMES_HOME` | hard fail for handoff plugin |
| Sessions dir | `HMK_SESSIONS_DIR` | derived from `HMK_HERMES_HOME` | hard fail |
| Handoff file | `HMK_DIALOGUE_HANDOFF_PATH` | derived from `HMK_AGENT_MEMORY_BASE` | hard fail for plugin |
| Always-context | `HMK_ALWAYS_CONTEXT_PATH` | derived from `HMK_AGENT_MEMORY_BASE` | hard fail for plugin |
| Vault (wiki) | `HMK_VAULT_DIR` | — | derived from workspace root |

The `hmk` wrapper loads each agent's `.env` and absolutizes relative paths against `HMK_WORKSPACE_ROOT`. You never run kit commands without the wrapper unless you source the `.env` yourself.

## Running two agents on one host

```bash
# Agent 1
python3 scripts/bootstrap_agent.py ~/agents/hermes-alfa --name hermes-alfa
# fill in ~/agents/hermes-alfa/.env (API keys, SOUL, platform tokens)
cd ~/agents/hermes-alfa
git clone https://github.com/NousResearch/hermes-agent.git app
python3 -m venv venv && ./venv/bin/pip install -e ./app
systemctl --user enable --now hermes-gateway@hermes-alfa.service

# Agent 2 — identical process, different persona and tokens
python3 scripts/bootstrap_agent.py ~/agents/hermes-beta --name hermes-beta
# ...
systemctl --user enable --now hermes-gateway@hermes-beta.service
```

Both agents run in parallel as systemd user services. Their sessions, memory, and config are fully disjoint.

## What if I misconfigure `.env`?

You see an explicit error, never a silent write to another agent's tree:

```
$ python3 scripts/memoryctl.py stats
ERROR: memoryctl needs HMK_AGENT_MEMORY_BASE (canonical) or
       AGENT_MEMORY_BASE / HMK_BASE_DIR / HMK_DB_PATH in the
       environment for initializing the library DB.
```

The `dialogue-handoff` plugin logs an error and disables itself rather than falling back to a default path.

## Upgrading the kit in an existing agent

```bash
python3 /path/to/hermes-memory-kit/scripts/bootstrap_agent.py \
    ~/agents/hermes-alfa --upgrade
```

Upgrade refreshes `scripts/`, `hermes-home/plugins/`, `hermes-home/skills/`. It preserves `config.yaml`, `SOUL.md`, `memories/*`, `.env`, `library.db`, and anything the user added.

It does **not** migrate a v2.x workspace to v3 layout. See `docs/migration-v3.md` for the migration playbook.
