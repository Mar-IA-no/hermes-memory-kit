#!/usr/bin/env python3
"""Deprecated shim: use `bootstrap_agent.py` instead.

This file exists only so that users / docs referencing the old entry
point continue to work through the v3.0 transition. Behavior delegates
to `bootstrap_agent.py` with the same args (translating old --workspace
to the new positional).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_DEPRECATION = (
    "WARNING: bootstrap_workspace.py is deprecated since hermes-memory-kit v3.0.\n"
    "         Use scripts/bootstrap_agent.py <agent_dir> [--name NAME] instead.\n"
    "         Delegating now. This shim may be removed in v4.\n"
)


def main():
    sys.stderr.write(_DEPRECATION)

    # Parse old-style args (--workspace, --with-wiki-templates, --upgrade)
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--workspace", required=False)
    ap.add_argument("--with-wiki-templates", action="store_true")
    ap.add_argument("--upgrade", action="store_true")
    ap.add_argument("remainder", nargs=argparse.REMAINDER)
    args, unknown = ap.parse_known_args()

    # Accept either --workspace path or a bare positional (new style)
    agent_dir = args.workspace
    if agent_dir is None:
        if args.remainder:
            agent_dir = args.remainder[0]

    if not agent_dir:
        sys.stderr.write("ERROR: must pass --workspace <path> or <agent_dir> positional\n")
        sys.exit(2)

    # Delegate to bootstrap_agent.py in the same directory
    here = Path(__file__).resolve().parent
    target = here / "bootstrap_agent.py"
    if not target.exists():
        sys.stderr.write(f"ERROR: bootstrap_agent.py not found at {target}\n")
        sys.exit(2)

    # Build new-style argv
    argv = [sys.executable, str(target), agent_dir]
    if args.with_wiki_templates:
        argv.append("--with-wiki-templates")
    if args.upgrade:
        argv.append("--upgrade")

    import os as _os
    _os.execvp(argv[0], argv)


if __name__ == "__main__":
    main()
