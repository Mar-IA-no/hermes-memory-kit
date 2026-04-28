#!/usr/bin/env bash
# full_backup.sh — snapshot consistente del deploy de Hermes (multi-agent).
#
# Shipped with hermes-memory-kit. Install this repo anywhere and symlink
# into the host's agents tree, e.g.:
#
#   ln -s /path/to/hermes-memory-kit/scripts/full_backup.sh \
#         /home/onairam/agents/full-backup.sh
#
# Supports both v2.x (agent-memory/ + wiki/ in $HOME) and v3.0
# (everything inside agents/<name>/) layouts without edits.
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Uso:
  sudo <path-to>/full_backup.sh [--sha256] [--only AGENT] [DESTINO]

Ejemplos:
  sudo /home/onairam/agents/full-backup.sh
  sudo /home/onairam/agents/full-backup.sh /media/backup
  sudo /home/onairam/agents/full-backup.sh --sha256 /media/backup
  sudo /home/onairam/agents/full-backup.sh --only hermes-prime /media/backup

Comportamiento:
  - crea un backup timestamped en DESTINO/hermes-agent-backups/YYYYmmdd-HHMMSS
  - captura TODOS los agentes en /home/onairam/agents/* (excepto lo que pida --only)
  - incluye configuraciones de systemd user, .bashrc/.profile, wrapper hermes CLI,
    hermes-memory-kit repo, Documentos, Obsidian installer
  - incluye /root/.hermes y unit legacy /etc/systemd/system/hermes-gateway.service
    como rollback-por-las-dudas (hasta que se decida limpiarlos)
  - hace snapshot consistente de TODAS las SQLite relevantes (auto-descubre
    library.db en agent-memory/ de cada agente y state.db en hermes-home/)
  - excluye archivos .bak.* redundantes de /root/.hermes para no inflar el tar
  - deja manifiesto, notas de restore, lista de instances activas de systemd
  - opcionalmente genera checksums SHA256
EOF
}

require_root() {
  if [[ "$(id -u)" -ne 0 ]]; then
    echo "Este script necesita root para leer /root/.hermes, /etc/systemd/system, y linger state." >&2
    exit 1
  fi
}

log() {
  printf '[backup] %s\n' "$*"
}

backup_sqlite() {
  local src="$1"
  local dst="$2"
  if [[ ! -f "$src" ]]; then
    return 0
  fi
  mkdir -p "$(dirname "$dst")"
  python3 - "$src" "$dst" <<'PY'
import sqlite3
import sys

src, dst = sys.argv[1], sys.argv[2]
src_con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
dst_con = sqlite3.connect(dst)
src_con.backup(dst_con)
dst_con.close()
src_con.close()
PY
}

WITH_SHA256=0
DEST_ROOT="/media/backup"
ONLY_AGENT=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --sha256)
      WITH_SHA256=1
      shift
      ;;
    --only)
      shift
      ONLY_AGENT="${1:?--only needs an agent name}"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      DEST_ROOT="$1"
      shift
      ;;
  esac
done

require_root

DEST_ROOT="${DEST_ROOT%/}"
BACKUP_ROOT="$DEST_ROOT/hermes-agent-backups"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
DEST="$BACKUP_ROOT/$TIMESTAMP"
SNAPSHOT="$DEST/snapshot"
META="$DEST/meta"
ONAIRAM_UID="$(id -u onairam 2>/dev/null || true)"
AGENTS_ROOT="/home/onairam/agents"

mkdir -p "$SNAPSHOT" "$META"

# -------- agents to back up --------------------------------------------

declare -a AGENT_DIRS=()
if [[ -n "$ONLY_AGENT" ]]; then
  if [[ -d "$AGENTS_ROOT/$ONLY_AGENT" ]]; then
    AGENT_DIRS+=("$AGENTS_ROOT/$ONLY_AGENT")
  else
    echo "ERROR: --only $ONLY_AGENT but $AGENTS_ROOT/$ONLY_AGENT does not exist" >&2
    exit 2
  fi
else
  # Enumerate every immediate child of agents/ that is a dir
  while IFS= read -r -d '' d; do
    AGENT_DIRS+=("$d")
  done < <(find "$AGENTS_ROOT" -mindepth 1 -maxdepth 1 -type d -print0 2>/dev/null)
fi

# -------- include paths -------------------------------------------------

INCLUDE_PATHS=(
  # Systemd user units — both legacy and v3.0 template + Minecraft services
  /home/onairam/.config/systemd/user/hermes-gateway.service
  /home/onairam/.config/systemd/user/hermes-gateway@.service
  /home/onairam/.config/systemd/user/mc-runtime.service
  /home/onairam/.config/systemd/user/mineflayer-onaiclaw.service
  /home/onairam/.config/systemd/user/papermc.service
  # User shell config + hermes CLI wrappers + mc wrapper
  /home/onairam/.bashrc
  /home/onairam/.profile
  /home/onairam/.local/bin/hermes
  /home/onairam/.local/bin/hermes-acp
  /home/onairam/.local/bin/hermes-agent
  /home/onairam/.local/bin/mc
  # OnaiClaw identity documents (outside agents/)
  /home/onairam/.hermes
  /home/onairam/EMBODIMENT.md
  # SSH keys (if they exist)
  /home/onairam/.ssh
  # Legacy root install (while it still exists; kept for rollback)
  /root/.hermes
  /root/.ssh/authorized_keys
  /etc/systemd/system/hermes-gateway.service
  # User-level persistent state
  /var/lib/systemd/linger/onairam
  # v2.x legacy paths (skipped if migrated)
  /home/onairam/agent-memory
  /home/onairam/wiki
  # The kit repo itself — source of truth for tooling
  /home/onairam/hermes-memory-kit
  # Reports and user data
  /home/onairam/Escritorio/onaiclaw-reports
  /home/onairam/Documentos
  # Minecraft — OnaiClaw's body in the game world
  /home/onairam/minecraft
  # Obsidian wrappers + installer
  /home/onairam/.local/bin/obsidian
  /home/onairam/.local/bin/obsidian-wiki
  /home/onairam/.local/share/applications/obsidian-wiki.desktop
  /home/onairam/Applications/Obsidian-1.12.7.AppImage
)

# Add each agent directory. This captures hermes-home/, agent-memory/
# (post-v3), wiki/, scripts/, app/, venv/, etc. — everything the agent
# owns — no matter whether the deployment is v2 or v3.
for ad in "${AGENT_DIRS[@]}"; do
  INCLUDE_PATHS+=("$ad")
done

# -------- SQLite databases to snapshot consistently --------------------

declare -a DB_PATHS=()

# Legacy paths (v2.x)
DB_PATHS+=(
  /home/onairam/agent-memory/library.db
  /root/.hermes/state.db
)

# Per-agent discovery (v3.x + v2.x hermes-home state.db)
for ad in "${AGENT_DIRS[@]}"; do
  DB_PATHS+=(
    "$ad/agent-memory/library.db"        # v3 location
    "$ad/hermes-home/state.db"           # both v2 and v3
  )
done

# -------- rsync excludes -----------------------------------------------

RSYNC_EXCLUDES=(
  # Old .env.bak.* and config.yaml.bak.* cluttering /root/.hermes
  --exclude='.env.bak.*'
  --exclude='config.yaml.bak.*'
  # Python bytecode
  --exclude='__pycache__'
  --exclude='*.pyc'
  # Node / Ruby caches if they land in agents/
  --exclude='node_modules'
  # SQLite WAL/SHM — we overwrite main DB with consistent snapshot below
  --exclude='*.db-wal'
  --exclude='*.db-shm'
)

# -------- metadata ------------------------------------------------------

log "Destino: $DEST"
log "Agentes: ${#AGENT_DIRS[@]} (${AGENT_DIRS[*]##*/})"
log "Creando metadatos"

{
  echo "timestamp=$TIMESTAMP"
  echo "hostname=$(hostname)"
  echo "user=$(id -un)"
  echo "uid=$(id -u)"
  echo "cwd=$(pwd)"
  echo "script=$(readlink -f "${BASH_SOURCE[0]}")"
  echo "only_agent=$ONLY_AGENT"
  echo "with_sha256=$WITH_SHA256"
} > "$META/context.env"

printf '%s\n' "${INCLUDE_PATHS[@]}" > "$META/included-paths.txt"
printf '%s\n' "${DB_PATHS[@]}" > "$META/sqlite-snapshots.txt"
printf '%s\n' "${AGENT_DIRS[@]}" > "$META/agents.txt"

uname -a > "$META/uname.txt" 2>/dev/null || true
lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL > "$META/lsblk.txt" 2>/dev/null || true
df -h > "$META/df.txt" 2>/dev/null || true

# Systemd: legacy system-scope unit + all user-scope instances
systemctl status hermes-gateway.service --no-pager > "$META/hermes-gateway.status.txt" 2>/dev/null || true
systemctl cat hermes-gateway.service > "$META/hermes-gateway.unit.txt" 2>/dev/null || true

if [[ -n "$ONAIRAM_UID" ]]; then
  # Non-templated user unit (v2 layout)
  sudo -u onairam XDG_RUNTIME_DIR="/run/user/$ONAIRAM_UID" \
    systemctl --user status hermes-gateway.service --no-pager \
    > "$META/hermes-gateway.user.status.txt" 2>/dev/null || true
  sudo -u onairam XDG_RUNTIME_DIR="/run/user/$ONAIRAM_UID" \
    systemctl --user cat hermes-gateway.service \
    > "$META/hermes-gateway.user.unit.txt" 2>/dev/null || true
  # Template instances (v3 layout)
  sudo -u onairam XDG_RUNTIME_DIR="/run/user/$ONAIRAM_UID" \
    systemctl --user list-units 'hermes-gateway@*' --all --no-pager \
    > "$META/hermes-gateway.user.instances.txt" 2>/dev/null || true
  sudo -u onairam XDG_RUNTIME_DIR="/run/user/$ONAIRAM_UID" \
    systemctl --user cat 'hermes-gateway@*' \
    > "$META/hermes-gateway.user.template.unit.txt" 2>/dev/null || true
  # Minecraft services
  for svc in papermc mineflayer-onaiclaw mc-runtime; do
    sudo -u onairam XDG_RUNTIME_DIR="/run/user/$ONAIRAM_UID" \
      systemctl --user status "$svc.service" --no-pager \
      > "$META/$svc.user.status.txt" 2>/dev/null || true
    sudo -u onairam XDG_RUNTIME_DIR="/run/user/$ONAIRAM_UID" \
      systemctl --user cat "$svc.service" \
      > "$META/$svc.user.unit.txt" 2>/dev/null || true
  done
fi

python3 --version > "$META/python-version.txt" 2>/dev/null || true
node --version > "$META/node-version.txt" 2>/dev/null || true
ffmpeg -version > "$META/ffmpeg-version.txt" 2>/dev/null || true

# Repo revs
git -C /home/onairam/hermes-memory-kit rev-parse HEAD > "$META/hermes-memory-kit.rev.txt" 2>/dev/null || true
for ad in "${AGENT_DIRS[@]}"; do
  name="${ad##*/}"
  if [[ -d "$ad/app/.git" ]]; then
    git -C "$ad/app" rev-parse HEAD > "$META/agent-$name-app.rev.txt" 2>/dev/null || true
  fi
  if [[ -x "$ad/venv/bin/python" ]]; then
    "$ad/venv/bin/python" --version > "$META/agent-$name-python-version.txt" 2>/dev/null || true
  fi
done

# -------- restore notes -------------------------------------------------

cat > "$META/RESTORE.txt" <<EOF
Restore rápido:

1. Verificá que estás restaurando sobre el host correcto o uno equivalente.

2. Copiá el snapshot preservando permisos:

   rsync -aHAX "$SNAPSHOT"/ /

3. Recargá systemd:

   systemctl daemon-reload
   sudo -iu onairam XDG_RUNTIME_DIR=/run/user/\$(id -u onairam) systemctl --user daemon-reload

4. Verificá rutas de cada agente:

   ls /home/onairam/agents/
   # Para cada agente restaurado:
   #   <agent>/hermes-home/ (config.yaml, SOUL.md, memories/)
   #   <agent>/agent-memory/ (v3) o /home/onairam/agent-memory (v2)
   #   <agent>/scripts/hmk (v3) o /home/onairam/agent-memory/memoryctl.py (v2)

5. Rehabilitá linger si hace falta:

   loginctl enable-linger onairam

6. Reiniciá el gateway del agente (v2 o v3 según corresponda):

   # v2 (non-templated):
   sudo -iu onairam XDG_RUNTIME_DIR=/run/user/\$(id -u onairam) \\
     systemctl --user enable --now hermes-gateway.service

   # v3 (templated, una instancia por agente):
   sudo -iu onairam XDG_RUNTIME_DIR=/run/user/\$(id -u onairam) \\
     systemctl --user enable --now hermes-gateway@hermes-prime.service

7. Reiniciá los servicios de Minecraft:

   sudo -iu onairam XDG_RUNTIME_DIR=/run/user/\$(id -u onairam) \\
     systemctl --user enable --now papermc mineflayer-onaiclaw mc-runtime

8. Verificá:

   sudo -iu onairam XDG_RUNTIME_DIR=/run/user/\$(id -u onairam) \\
     systemctl --user status hermes-gateway@hermes-prime.service --no-pager

   # Memoria (v3):
   /home/onairam/agents/hermes-prime/scripts/hmk memoryctl.py stats
   # Memoria (v2 legacy):
   python3 /home/onairam/agent-memory/memoryctl.py stats

   # Minecraft:
   sudo -iu onairam XDG_RUNTIME_DIR=/run/user/\$(id -u onairam) \\
     systemctl --user status papermc mineflayer-onaiclaw mc-runtime --no-pager

Notas:
- Este backup incluye secretos y credenciales locales.
- Tratá este directorio como material sensible.
- Las SQLite principales fueron copiadas con snapshot consistente.
- /root/.hermes y /etc/systemd/system/hermes-gateway.service quedan como
  rollback legacy hasta que se limpien del host.
- Si el host ya migró a v3 (agents/<name>/agent-memory/ presente), las rutas
  v2 (/home/onairam/agent-memory, /home/onairam/wiki) no se van a encontrar;
  es esperado.
- Minecraft world y bot data se restauran en /home/onairam/minecraft/
- ~/.hermes contiene SOUL.md y logs de identidad
EOF

# -------- rsync main tree ----------------------------------------------

log "Copiando árbol principal"
for path in "${INCLUDE_PATHS[@]}"; do
  if [[ -e "$path" ]]; then
    rsync -aHAX --numeric-ids --relative "${RSYNC_EXCLUDES[@]}" "$path" "$SNAPSHOT/"
  fi
done

# -------- SQLite consistent snapshots ----------------------------------

log "Sobrescribiendo con snapshots consistentes de SQLite"
# Deduplicate DB_PATHS first (two agents could resolve to the same DB in weird setups)
declare -A SEEN_DBS=()
for db in "${DB_PATHS[@]}"; do
  if [[ -z "${SEEN_DBS[$db]:-}" ]]; then
    SEEN_DBS[$db]=1
    if [[ -f "$db" ]]; then
      backup_sqlite "$db" "$SNAPSHOT/${db#/}"
      rm -f "$SNAPSHOT/${db#/}-wal" "$SNAPSHOT/${db#/}-shm"
    fi
  fi
done

# -------- checksums (optional) -----------------------------------------

if [[ "$WITH_SHA256" -eq 1 ]]; then
  log "Generando checksums SHA256"
  (
    cd "$SNAPSHOT"
    find . -type f -print0 | sort -z | xargs -0 sha256sum
  ) > "$META/SHA256SUMS.txt"
fi

# -------- latest symlink ------------------------------------------------

ln -sfn "$DEST" "$BACKUP_ROOT/latest"

log "Backup completo"
log "Snapshot: $SNAPSHOT"
log "Meta: $META"
log "Tamaño total: $(du -sh "$DEST" | cut -f1)"
