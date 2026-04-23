#!/usr/bin/env python3
"""Remove a specific (provider, model) embedding set from chapter_embeddings.

Safe rollback / cleanup helper. Prints a dry-run count unless --confirm is
passed. The wrapper (hmk) resolves paths so HMK_DB_PATH is reliably absolute.

Usage:
    ./scripts/hmk embed_clear.py --provider google --model gemini-embedding-001
    ./scripts/hmk embed_clear.py --provider google --model gemini-embedding-001 --confirm
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sqlite3
import sys


def main():
    ap = argparse.ArgumentParser(description="Clear an embedding set from chapter_embeddings")
    ap.add_argument("--provider", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--confirm", action="store_true", help="actually delete (without this, dry-run)")
    args = ap.parse_args()

    # Resolve DB path (hmk wrapper already exported HMK_DB_PATH; may be relative
    # still if user's .env had a relative value that wasn't absolutized —
    # defensive fallback chain).
    db = (
        os.environ.get("HMK_DB_PATH")
        or os.environ.get("HERMES_DB_PATH")
        or os.path.join(os.environ.get("HMK_BASE_DIR", "agent-memory"), "library.db")
    )
    db_path = pathlib.Path(db).expanduser()
    if not db_path.is_absolute():
        db_path = pathlib.Path(os.getcwd()) / db_path
    if not db_path.exists():
        sys.exit(f"ERROR: DB not found at {db_path}")

    con = sqlite3.connect(str(db_path))
    try:
        count = con.execute(
            "SELECT COUNT(*) FROM chapter_embeddings WHERE provider=? AND model=?",
            (args.provider, args.model),
        ).fetchone()[0]
        print(f"embedding set found: provider={args.provider}, model={args.model}, rows={count}")
        if count == 0:
            print("nothing to clear.")
            return
        if not args.confirm:
            print("DRY RUN — pass --confirm to actually delete.")
            return
        n = con.execute(
            "DELETE FROM chapter_embeddings WHERE provider=? AND model=?",
            (args.provider, args.model),
        ).rowcount
        con.commit()
        print(f"deleted {n} rows from chapter_embeddings")
    finally:
        con.close()


if __name__ == "__main__":
    main()
