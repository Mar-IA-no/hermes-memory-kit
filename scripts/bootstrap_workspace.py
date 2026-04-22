#!/usr/bin/env python3
import argparse
import shutil
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = REPO_ROOT / "templates"


def copy_if_missing(src: Path, dst: Path):
    if dst.exists():
        return
    if src.is_dir():
        shutil.copytree(src, dst)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def main():
    parser = argparse.ArgumentParser(description="Bootstrap a workspace with Hermes Memory Kit templates")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--with-wiki-templates", action="store_true", help="copy starter wiki notes")
    args = parser.parse_args()

    workspace = Path(args.workspace).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    copy_if_missing(TEMPLATES / "AGENTS.md", workspace / "AGENTS.md")
    copy_if_missing(TEMPLATES / "memory", workspace / "agent-memory")
    copy_if_missing(REPO_ROOT / ".env.example", workspace / ".env.example")
    copy_if_missing(TEMPLATES / "skills", workspace / "skills")
    (workspace / "wiki").mkdir(parents=True, exist_ok=True)
    if args.with_wiki_templates:
        copy_if_missing(TEMPLATES / "wiki", workspace / "wiki")

    print(f"bootstrapped: {workspace}")


if __name__ == "__main__":
    main()
