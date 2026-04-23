#!/usr/bin/env python3
"""Bootstrap or upgrade a Hermes Memory Kit workspace.

A workspace is self-contained: it gets its own copy of scripts/, plugins/,
templates-derived directories, and a canonical .env.example. Users run the
tooling through ./scripts/hmk <script-name.py> [args...] from the
workspace root.

Modes:
  --with-wiki-templates   Include starter wiki notes (first bootstrap).
  --upgrade               Refresh tooling (scripts/, plugins/) and templates
                          that the user hasn't edited. Preserves user data:
                          agent-memory/library.db, .env, files the user
                          modified (mtime vs. template original).
"""
import argparse
import os
import shutil
import filecmp
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = REPO_ROOT / "templates"
SCRIPTS_DIR = REPO_ROOT / "scripts"

# Files the user might edit; only overwrite on upgrade if mtime matches template
USER_EDITABLE = {
    "AGENTS.md",
    "skills",
    "wiki",
    "agent-memory",
}


def copy_if_missing(src: Path, dst: Path):
    if dst.exists():
        return
    if src.is_dir():
        shutil.copytree(src, dst)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def copy_tree_overwrite(src: Path, dst: Path, preserve_exec: bool = False):
    """Remove dst if exists, then copy src → dst."""
    if dst.exists():
        if dst.is_dir():
            shutil.rmtree(dst)
        else:
            dst.unlink()
    shutil.copytree(src, dst) if src.is_dir() else shutil.copy2(src, dst)
    if preserve_exec and dst.is_dir():
        # Keep executable bits for shell scripts inside
        for p in dst.rglob("*"):
            if p.suffix in (".sh",) or p.name in ("hmk",):
                os.chmod(p, 0o755)


def files_match(a: Path, b: Path) -> bool:
    try:
        return a.exists() and b.exists() and filecmp.cmp(a, b, shallow=False)
    except Exception:
        return False


def bootstrap(workspace: Path, with_wiki_templates: bool):
    workspace.mkdir(parents=True, exist_ok=True)
    # Base layout
    copy_if_missing(TEMPLATES / "AGENTS.md", workspace / "AGENTS.md")
    copy_if_missing(TEMPLATES / "memory", workspace / "agent-memory")
    copy_if_missing(REPO_ROOT / ".env.example", workspace / ".env.example")
    copy_if_missing(TEMPLATES / "skills", workspace / "skills")
    # Tooling: scripts and plugins live in the workspace for self-containment
    copy_if_missing(SCRIPTS_DIR, workspace / "scripts")
    # Ensure executable bits on wrapper + smoke-test
    for name in ("hmk", "smoke-test.sh"):
        p = workspace / "scripts" / name
        if p.exists():
            os.chmod(p, 0o755)
    # Plugins (optional components; may not exist yet if repo is pre-v2)
    plugins_src = TEMPLATES / "plugins"
    if plugins_src.exists():
        copy_if_missing(plugins_src, workspace / "plugins")
    # Wiki — copy templates first, only create empty dir if opted out
    if with_wiki_templates:
        copy_if_missing(TEMPLATES / "wiki", workspace / "wiki")
    else:
        (workspace / "wiki").mkdir(exist_ok=True)
    # DIALOGUE-HANDOFF.md placeholder (perms 600) if not already there
    handoff = workspace / "agent-memory" / "state" / "DIALOGUE-HANDOFF.md"
    if handoff.exists():
        try:
            os.chmod(handoff, 0o600)
        except Exception:
            pass


def upgrade(workspace: Path):
    """Refresh tooling + non-user-edited templates. Preserves user data."""
    if not workspace.exists():
        raise SystemExit(f"workspace does not exist: {workspace}")
    # Always refresh scripts/ and plugins/ (tooling)
    copy_tree_overwrite(SCRIPTS_DIR, workspace / "scripts", preserve_exec=True)
    for name in ("hmk", "smoke-test.sh"):
        p = workspace / "scripts" / name
        if p.exists():
            os.chmod(p, 0o755)
    plugins_src = TEMPLATES / "plugins"
    if plugins_src.exists():
        copy_tree_overwrite(plugins_src, workspace / "plugins")
    # Refresh .env.example
    shutil.copy2(REPO_ROOT / ".env.example", workspace / ".env.example")
    # Templates-derived files that the user might have edited: only overwrite
    # if the current content matches the OLD template (i.e. user didn't touch).
    # Since we don't ship the old template for comparison, policy: if the
    # workspace file matches the current template verbatim, leave alone; if it
    # differs, skip (assume user-edited) and warn.
    for rel, is_dir in (
        ("AGENTS.md", False),
        ("skills", True),
    ):
        src = TEMPLATES / rel
        dst = workspace / rel
        if not src.exists():
            continue
        if not dst.exists():
            if is_dir:
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
            continue
        # Heuristic: skip if user touched (mtime newer than ours is ambiguous;
        # safest = never auto-overwrite). Leave a hint.
        print(f"  SKIP (user may have edited): {dst} — diff manually against {src}")


def main():
    parser = argparse.ArgumentParser(description="Bootstrap / upgrade a Hermes Memory Kit workspace")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--with-wiki-templates", action="store_true", help="copy starter wiki notes")
    parser.add_argument("--upgrade", action="store_true", help="refresh tooling in an existing workspace, preserving user data")
    args = parser.parse_args()

    workspace = Path(args.workspace).expanduser().resolve()

    if args.upgrade:
        upgrade(workspace)
        print(f"upgraded: {workspace}")
    else:
        bootstrap(workspace, args.with_wiki_templates)
        print(f"bootstrapped: {workspace}")


if __name__ == "__main__":
    main()
