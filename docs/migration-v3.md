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

The v3 layout has `.env` canonically in `hermes-home/.env` (real file, where Hermes
Agent upstream rewrites it via `os.replace()` in `config.py:3292/3395/3451`), with a
relative symlink from the agent root (`agent_root/.env → hermes-home/.env`) so the
`hmk` wrapper and the systemd template `EnvironmentFile=%h/agents/%i/.env` both
resolve to the same file. A symlink at `hermes-home/.env` would be destroyed by
the first atomic-rename write from Hermes.

Merge whatever `~/agents/hermes-prime/hermes-home/.env` contains with the HMK_* keys
from the `.env.template` (which uses `{{WORKSPACE_ROOT}}` placeholders resolved to
absolute paths at bootstrap — required for systemd EnvironmentFile= compatibility):

```bash
cd ~/agents/hermes-prime
# Render HMK_* block with absolute paths for this agent:
python3 /path/to/hermes-memory-kit/scripts/bootstrap_agent.py \
    /tmp/render-hermes-prime --name hermes-prime
# Merge: operational vars from old .env + regenerated HMK_* block
# (exclude from old: HMK_*, HERMES_HOME, AGENT_MEMORY_BASE, HMK_BASE_DIR only)
# Install the merged .env as real file in hermes-home/:
cp <merged-env> ~/agents/hermes-prime/hermes-home/.env
chmod 600 ~/agents/hermes-prime/hermes-home/.env
# Create relative symlink from agent root:
ln -sf hermes-home/.env ~/agents/hermes-prime/.env
```

All HMK_* paths in the merged `.env` must be absolute (e.g.
`HMK_HERMES_HOME=/home/onairam/agents/hermes-prime/hermes-home`), never
`${HMK_WORKSPACE_ROOT}/hermes-home` — systemd `EnvironmentFile=` reads values
literally and does not expand `${VAR}` references.

The legacy aliases `HERMES_HOME` and `AGENT_MEMORY_BASE` must appear in the `.env`
(Hermes Agent upstream reads `HERMES_HOME` directly); bootstrap writes them
alongside the HMK_* block.

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

## Upgrading a plugin (critical — avoid silent collisions)

Hermes Agent loads **every directory** under `hermes-home/plugins/` that
contains a valid `plugin.yaml`. This is independent of `config.yaml:
plugins.enabled` — the enabled list affects registration of some behaviors,
but hook-registering plugins run regardless if their directory is present.

Consequence: if you upgrade a plugin by renaming the old directory as a
sibling (e.g. `plugins/dialogue-handoff.v30.bak/`), the backup **still runs**
its hooks alongside the live plugin, and the last writer wins. In practice
this means the new plugin's output gets silently overwritten by the old
backup's, with no warning in logs. You'll see stale format in whatever file
the plugin writes and assume the upgrade didn't take.

### Convention: use `hermes-home/plugin-backups/`

`bootstrap_agent.py` creates `hermes-home/plugin-backups/` as part of the
standard layout. `bootstrap_agent.py --upgrade` rotates the previous plugin
directory there with a timestamp suffix before copying the new one in.
Hermes does not scan `plugin-backups/`, so old versions are safe there.

Manual plugin swap (outside `--upgrade`):

```bash
# Wrong — Hermes loads BOTH:
mv hermes-home/plugins/my-plugin hermes-home/plugins/my-plugin.v1.bak
cp -r new-version hermes-home/plugins/my-plugin

# Right — backup is out of the scanned path:
mkdir -p hermes-home/plugin-backups
mv hermes-home/plugins/my-plugin hermes-home/plugin-backups/my-plugin.$(date +%Y%m%d-%H%M%S).bak
cp -r new-version hermes-home/plugins/my-plugin
```

### Verifying plugin layout health

`memoryctl.py doctor` audits `$HMK_HERMES_HOME/plugins/` for the two most
common foot-guns:

- **Name collisions**: two or more plugin directories whose `plugin.yaml`
  declares the same `name`. Indicates a sibling backup got registered as a
  second active plugin.
- **Suspect suffixes**: directory names matching `.bak`, `.old`, `.prev`,
  `.vNN`. Even without a collision, these shouldn't live under `plugins/`.

```bash
./scripts/hmk memoryctl.py doctor
# exits 0 if OK, 1 if any issue. Output is JSON for scripting / CI.
```

Run this after any plugin change and before assuming a new plugin version is
actually active in production.
