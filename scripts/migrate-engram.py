#!/usr/bin/env python3
"""ENGRAM schema migration for library.db.

Adds episodic/semantic/procedural taxonomy + episode metadata columns to
the chapters table. Idempotent: re-running on an already-migrated DB is a
no-op (after backup + version check).

Schema added:
    chapters.engram_type   TEXT NOT NULL DEFAULT 'semantic'
                                  CHECK in ('episodic','semantic','procedural')
    chapters.event_ts      INTEGER NULL  -- unix ts of the event (not the INSERT)
    chapters.actor         TEXT NULL     -- who did it
    chapters.location_json TEXT NULL     -- coords/place

Indexes: idx_chapters_engram, idx_chapters_event_ts, idx_chapters_actor.

Shelf to engram_type mapping (heuristic taxonomy applied as a UPDATE pass):
    episodic    : mc-episodic, episodes
    procedural  : mc-skills, plans
    semantic    : mc-social, mc-places, library, identity, evidence, state
                  (anything else stays default 'semantic')

Backup written to <db>.bak.preengram.<unix_ts> before any DDL.

Env vars (cascade, first match wins):
    HMK_DB_PATH                 # explicit path to library.db
    HERMES_DB_PATH              # legacy
    HMK_AGENT_MEMORY_BASE       # if set, uses <base>/library.db
    HERMES_AGENT_MEMORY_BASE    # legacy alias
    AGENT_MEMORY_BASE           # legacy alias
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import time


def resolve_db_path() -> str:
    direct = os.environ.get("HMK_DB_PATH") or os.environ.get("HERMES_DB_PATH")
    if direct:
        return direct
    base = (
        os.environ.get("HMK_AGENT_MEMORY_BASE")
        or os.environ.get("HERMES_AGENT_MEMORY_BASE")
        or os.environ.get("AGENT_MEMORY_BASE")
    )
    if base:
        return os.path.join(base, "library.db")
    print(
        "ERROR: cannot resolve library.db path. Set HMK_DB_PATH or "
        "HMK_AGENT_MEMORY_BASE.",
        file=sys.stderr,
    )
    sys.exit(2)


SHELF_TO_ENGRAM = {
    "mc-episodic": "episodic",
    "episodes": "episodic",
    "mc-skills": "procedural",
    "plans": "procedural",
    "mc-social": "semantic",
    "mc-places": "semantic",
    "library": "semantic",
    "identity": "semantic",
    "evidence": "semantic",
    "state": "semantic",
}


def main() -> int:
    db_path = resolve_db_path()
    if not os.path.exists(db_path):
        print(f"ERROR: db not found at {db_path}", file=sys.stderr)
        return 2

    backup = f"{db_path}.bak.preengram.{int(time.time())}"
    shutil.copy2(db_path, backup)
    print(f"backup: {backup} ({os.path.getsize(backup)} bytes)")

    db = sqlite3.connect(db_path)
    db.execute("PRAGMA foreign_keys=ON")

    cols = [r[1] for r in db.execute("PRAGMA table_info(chapters)").fetchall()]
    if "engram_type" in cols:
        print(f"already migrated, columns: {cols}")
        return 0

    db.executescript(
        """
        ALTER TABLE chapters ADD COLUMN engram_type TEXT NOT NULL DEFAULT 'semantic'
            CHECK (engram_type IN ('episodic', 'semantic', 'procedural'));
        ALTER TABLE chapters ADD COLUMN event_ts INTEGER NULL;
        ALTER TABLE chapters ADD COLUMN actor TEXT NULL;
        ALTER TABLE chapters ADD COLUMN location_json TEXT NULL;

        CREATE INDEX IF NOT EXISTS idx_chapters_engram   ON chapters(engram_type);
        CREATE INDEX IF NOT EXISTS idx_chapters_event_ts ON chapters(event_ts);
        CREATE INDEX IF NOT EXISTS idx_chapters_actor    ON chapters(actor);
        """
    )
    print("schema migrated: engram_type, event_ts, actor, location_json columns added")

    for shelf_name, etype in SHELF_TO_ENGRAM.items():
        n = db.execute(
            """UPDATE chapters SET engram_type=? WHERE book_id IN (
                   SELECT b.id FROM books b
                   JOIN shelves s ON b.shelf_id=s.id
                   WHERE s.name=?)""",
            (etype, shelf_name),
        ).rowcount
        print(f"  {shelf_name} → {etype}: {n} chapters updated")

    db.execute(
        "UPDATE chapters SET event_ts=created_at "
        "WHERE engram_type='episodic' AND event_ts IS NULL"
    )

    db.commit()

    print()
    print("=== ENGRAM distribution ===")
    for r in db.execute(
        "SELECT engram_type, COUNT(*) FROM chapters "
        "GROUP BY engram_type ORDER BY 2 DESC"
    ).fetchall():
        print(f"  {r[0]}: {r[1]}")

    n_event_ts = db.execute(
        "SELECT COUNT(*) FROM chapters WHERE event_ts IS NOT NULL"
    ).fetchone()[0]
    print(f"\n=== chapters with event_ts populated: {n_event_ts} ===")

    db.close()
    print("\nMIGRATION OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
