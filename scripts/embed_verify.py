#!/usr/bin/env python3
"""Inspect chapter_embeddings per (provider, model, dims).

Useful right after a re-embed to confirm the actual dimensionality
stored, since stats() doesn't expose dims and the PK doesn't include it.

Usage:
    ./scripts/hmk embed_verify.py
    ./scripts/hmk embed_verify.py --provider google
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sqlite3
import sys


def main():
    ap = argparse.ArgumentParser(description="Inspect stored embedding sets")
    ap.add_argument("--provider", default=None, help="filter by provider")
    ap.add_argument("--model", default=None, help="filter by model")
    ap.add_argument("--json", action="store_true", help="output JSON instead of table")
    args = ap.parse_args()

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
    q = """
        SELECT provider, model, dims, COUNT(*) AS n,
               SUM(LENGTH(embedding_json)) AS total_json_bytes,
               AVG(LENGTH(embedding_json)) AS avg_row_bytes
          FROM chapter_embeddings
    """
    params: list = []
    where = []
    if args.provider:
        where.append("provider = ?")
        params.append(args.provider)
    if args.model:
        where.append("model = ?")
        params.append(args.model)
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " GROUP BY provider, model, dims ORDER BY provider, model, dims"
    rows = con.execute(q, params).fetchall()
    con.close()

    if not rows:
        if args.provider or args.model:
            sys.exit("no embedding sets matching filter")
        sys.exit("no embedding sets in DB")

    out = [
        {
            "provider": r[0],
            "model": r[1],
            "dims": r[2],
            "count": r[3],
            "total_json_bytes": r[4],
            "avg_row_bytes": round(r[5] or 0, 1),
        }
        for r in rows
    ]

    if args.json:
        print(json.dumps(out, indent=2))
        return

    print(f"DB: {db_path}")
    print(f"{'provider':<10} {'model':<48} {'dims':>5} {'count':>6} {'total_json':>12} {'avg_row':>8}")
    print("-" * 95)
    for s in out:
        print(
            f"{s['provider']:<10} {s['model']:<48} {s['dims']:>5} {s['count']:>6} "
            f"{s['total_json_bytes'] or 0:>12} {s['avg_row_bytes']:>8}"
        )


if __name__ == "__main__":
    main()
