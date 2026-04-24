#!/bin/bash
# hermes-env.sh — activate this agent's environment in an interactive shell.
#
# Usage: add to ~/.bashrc / ~/.profile:
#   if [ -f "$HOME/agents/<agent-name>/scripts/hermes-env.sh" ]; then
#     . "$HOME/agents/<agent-name>/scripts/hermes-env.sh"
#   fi
#
# Sourcing exports every var in the canonical hermes-home/.env, prepends the
# agent's venv/bin to PATH, and sets $PRIME to the workspace root.
#
# Relocatable: derives paths from the script's own location — copies into any
# agent workspace under ~/agents/<name>/scripts/ and just works.

_hermes_env_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRIME="$(dirname "$_hermes_env_dir")"
unset _hermes_env_dir

if [ -f "$PRIME/hermes-home/.env" ]; then
  set -a
  . "$PRIME/hermes-home/.env"
  set +a
fi

if [ -d "$PRIME/venv/bin" ]; then
  case ":$PATH:" in
    *":$PRIME/venv/bin:"*) ;;
    *) export PATH="$PRIME/venv/bin:$PATH" ;;
  esac
fi

export PRIME
