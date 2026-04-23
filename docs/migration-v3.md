# Migration playbook: v2.x → v3.0

v3.0 re-positions the kit from "memory layer for an agent" to "scaffold for a full self-contained agent". An agent's workspace now *includes* `hermes-home/` (the HERMES_HOME the Hermes Agent upstream expects) and a consolidated `.env`. The memory subsystem is still there, just one layer inside a larger layout.

## What changed between v2.x and v3.0

- New directory `hermes-home/` inside the workspace — holds `config.yaml`, `SOUL.md`, `memories/`, `sessions/`, `plugins/`, `skills/`.
- `.env.template` replaces `.env.example`. `.env.example` is kept as a symlink for one release.
- Canonical memory-root env var is now **`HMK_AGENT_MEMORY_BASE`**. `HMK_BASE_DIR` and `AGENT_MEMORY_BASE` still work as legacy fallbacks in the cascade.
- `bootstrap_workspace.py` → `bootstrap_agent.py`. The old name is a shim that prints a deprecation warning and delegates. The shim may be removed in v4.
- `dialogue-handoff` plugin bumped to v3.0 — no more hardcoded fallbacks to `/home/onairam/*`. If the agent's `.env` is wrong, the plugin logs an error and disables itself, instead of writing handoff to someone else's tree.
- `memoryctl.py` and `continuityctl.py` refactored — hard-fail instead of silently defaulting to the repo's own `agent-memory/` or a user-home guess.
- New `templates/systemd/hermes-gateway@.service` — user-service template instance, one unit file per agent via systemd `%i`.
- A workspace that follows the v3 layout is fully portable: you can `rsync` `~/agents/<name>/` to another host and only the API keys inside `.env` need changing.

## Who has to migrate

- If your workspace was created with `bootstrap_workspace.py` from v2.x and you want to adopt the `hermes-home/` + systemd template flow → yes.
- If you just use the kit as a memory store via `memoryctl.py` and don't care about the rest → the scripts still work as long as your env provides `HMK_AGENT_MEMORY_BASE` (or one of the legacy aliases). You will see deprecation warnings when running `bootstrap_workspace.py`.

## Not covered by automatic tools

v3 does **not** provide an automatic v2 → v3 upgrade command. The kit's `bootstrap_agent.py --upgrade` only refreshes tooling in an already-v3 workspace; it will detect a v2 layout and refuse to run. Migration is designed as a staged manual process because it crosses several live concerns at once: memory, gateway config, systemd unit, Hermes Agent `HERMES_HOME`.

## Migration playbook

The following assumes you have a working v2 workspace plus a running Hermes Agent using that workspace (this is the hermes-prime configuration on the reference host). Adapt paths as needed.

### 0. Snapshot everything

Before touching anything, create a full tar:

```bash
tar -cf ~/hermes-v2-snapshot-$(date +%Y%m%d-%H%M%S).tar.gz \
    ~/agent-memory ~/wiki ~/agents/hermes-prime/hermes-home ~/.config/systemd/user
```

Keep that tarball until the migrated agent has run cleanly for a couple of days.

### 1. Stop the gateway

```bash
systemctl --user stop hermes-gateway.service
```

### 2. Move state into the agent's workspace

For the reference deployment, everything consolidates under `~/agents/hermes-prime/`:

```bash
# Move memory into the workspace
mv ~/agent-memory ~/agents/hermes-prime/agent-memory
# Move wiki
mv ~/wiki ~/agents/hermes-prime/wiki
```

Consider excluding ad-hoc venvs that may be inside the old `agent-memory/` (e.g. `.venv-markitdown`, `.venv-ingest`) — they are not needed in the new layout.

### 3. Consolidate `.env`

The v3 layout expects a single `.env` at the agent root, not one inside `hermes-home/`. Merge whatever `~/agents/hermes-prime/hermes-home/.env` contains with the HMK_* keys from the `.env.template`:

```bash
cd ~/agents/hermes-prime
# start from the template rendered for this agent
python3 /path/to/hermes-memory-kit/scripts/bootstrap_agent.py \
    . --name hermes-prime --upgrade  # NOTE: upgrade on a v3 workspace
# but we are migrating from v2, so bootstrap --upgrade will refuse.
# Instead, render .env manually:
cp /path/to/hermes-memory-kit/.env.template .env
# then edit: fill HMK_WORKSPACE_ROOT, API keys from the old hermes-home/.env
```

Make sure the new `.env` includes the legacy aliases `HERMES_HOME=${HMK_HERMES_HOME}` and `AGENT_MEMORY_BASE=${HMK_AGENT_MEMORY_BASE}` — Hermes Agent upstream reads the former.

### 4. Drop in the v3 plugin + skills

```bash
cp -r /path/to/hermes-memory-kit/templates/plugins/dialogue-handoff \
      ~/agents/hermes-prime/hermes-home/plugins/
cp -r /path/to/hermes-memory-kit/templates/skills/* \
      ~/agents/hermes-prime/hermes-home/skills/
```

The plugin v3.0 reads `HMK_DIALOGUE_HANDOFF_PATH` / `HMK_ALWAYS_CONTEXT_PATH` / `HMK_SESSIONS_DIR` from env; if any is unset, it disables itself rather than writing somewhere it shouldn't.

### 5. Install the systemd unit template

```bash
cp /path/to/hermes-memory-kit/templates/systemd/hermes-gateway@.service \
   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user disable --now hermes-gateway.service    # old
systemctl --user enable --now hermes-gateway@hermes-prime.service   # new
```

### 6. Optional safety net — transitional symlinks

If some script (yours, or something else on the host) still looks for `~/agent-memory` or `~/wiki`, keep them pointing to the new location for a few days:

```bash
ln -s ~/agents/hermes-prime/agent-memory ~/agent-memory
ln -s ~/agents/hermes-prime/wiki ~/wiki
```

Remove the symlinks once nothing references the old paths.

### 7. Verify

```bash
# Memory still reads
~/agents/hermes-prime/scripts/hmk memoryctl.py stats
# Handoff plugin active and sees the right paths (no WARN in gateway logs)
systemctl --user status hermes-gateway@hermes-prime.service
journalctl --user -u hermes-gateway@hermes-prime.service -n 50

# Interact with the agent, send a message; confirm DIALOGUE-HANDOFF.md updates
ls -la ~/agents/hermes-prime/agent-memory/state/DIALOGUE-HANDOFF.md
```

### 8. Rollback if needed

```bash
systemctl --user stop hermes-gateway@hermes-prime.service
systemctl --user disable hermes-gateway@hermes-prime.service
# restore the tarball from step 0
tar -xzf ~/hermes-v2-snapshot-*.tar.gz -C /
systemctl --user enable --now hermes-gateway.service
```

## What is not migrated automatically

- `config.yaml`: the v3 template is a minimal starter. The live one on hermes-prime has host-specific providers, platform toolsets, reasoning knobs. Port those by hand.
- Session history in `hermes-home/sessions/`: moved as part of step 2.
- The live plugin: replaced outright in step 4. If you had local edits to the v2.1 plugin, port them after dropping in v3.0.
- Any external cron or systemd timers that referenced the old paths. Grep for `/home/onairam/agent-memory` and `/home/onairam/wiki` before finalizing.
