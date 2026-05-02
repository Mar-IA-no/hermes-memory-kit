#!/usr/bin/env python3
"""Backfill semantic facts from existing episodic chapters.

Walks `chapters` rows where `engram_type='episodic'`, sends each one to
the Hermes gateway with an extraction prompt, and inserts the resulting
durable facts as new `chapters` rows under an auto-created
`engram-backfill` book in the appropriate shelf:
    social         -> mc-social   (or first social-tagged shelf)
    place          -> mc-places
    skill_pattern  -> mc-skills
    preference     -> library
    discovery      -> library

Each new chapter is tagged `engram-backfill`, `<fact_type>`, and
`src-chapter-<source_id>` for traceability.

Idempotency note: this script does NOT dedupe against previous runs.
Run with `--shelf-pattern` and `--limit` to scope. Re-running the same
range will produce duplicate facts; use the source-chapter tag to clean
up if needed.

Env vars (cascade, first match wins):
    HMK_DB_PATH                 # path to library.db
    HERMES_DB_PATH              # legacy
    HMK_AGENT_MEMORY_BASE       # falls back to <base>/library.db
    HERMES_AGENT_MEMORY_BASE    # legacy
    AGENT_MEMORY_BASE           # legacy

    HMK_HERMES_HOME             # directory with Hermes config (cwd for `hermes chat`)
    HERMES_HOME                 # legacy

    HMK_HERMES_BIN              # explicit path to the hermes executable;
                                #   fallback: PATH lookup, then ~/.local/bin/hermes etc.

    HMK_AGENT_NAME              # rendered into the default prompt
    HMK_DOMAIN_DESC             # rendered into the default prompt (one sentence)

Optional: pass `--prompt-file PATH` for a fully custom prompt template
(must contain `{ep_text}` placeholder).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time


FACT_TYPES = ("social", "place", "skill_pattern", "preference", "discovery")

DEFAULT_PROMPT = """You are a semantic-fact extractor for an AI agent named "{agent_name}".
Domain context: {domain_desc}

DEFINITION: a SEMANTIC fact is durable information worth recalling repeatedly
(user preferences, recurring locations, agent behavior patterns, world discoveries).

NOT SEMANTIC: one-off events without durability ("it became night", "I cancelled a task"),
one-shot metrics, random errors.

TASK: from the following episode, extract 0-3 durable semantic facts in the format:
TYPE | TEXT
where TYPE is one of: social | place | skill_pattern | preference | discovery

If no durable facts are present, reply: NONE

EPISODE:
{ep_text}

OUTPUT (only lines TYPE|TEXT, or NONE):"""

FACT_TO_SHELF = {
    "social": "mc-social",
    "place": "mc-places",
    "skill_pattern": "mc-skills",
    "preference": "library",
    "discovery": "library",
}


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
        "ERROR: cannot resolve library.db. Set HMK_DB_PATH or "
        "HMK_AGENT_MEMORY_BASE.",
        file=sys.stderr,
    )
    sys.exit(2)


def resolve_hermes_home() -> str:
    home = os.environ.get("HMK_HERMES_HOME") or os.environ.get("HERMES_HOME")
    if not home:
        print(
            "ERROR: HMK_HERMES_HOME (or HERMES_HOME) not set. "
            "The script needs the Hermes config dir to invoke `hermes chat`.",
            file=sys.stderr,
        )
        sys.exit(2)
    return home


def resolve_hermes_bin() -> str:
    explicit = os.environ.get("HMK_HERMES_BIN")
    if explicit and os.path.isfile(explicit) and os.access(explicit, os.X_OK):
        return explicit
    found = shutil.which("hermes")
    if found:
        return found
    candidates = [
        os.path.expanduser("~/.local/bin/hermes"),
        "/usr/local/bin/hermes",
        "/usr/bin/hermes",
    ]
    for c in candidates:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    print(
        "ERROR: cannot locate the `hermes` binary. Set HMK_HERMES_BIN or "
        "ensure `hermes` is on PATH.",
        file=sys.stderr,
    )
    sys.exit(2)


def call_hermes(hermes_bin: str, hermes_home: str, prompt: str, timeout: int = 60) -> str:
    p = subprocess.run(
        [hermes_bin, "chat", "-q", prompt, "-Q"],
        cwd=hermes_home,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={
            **os.environ,
            "HERMES_PLATFORM": "cli",
            "HERMES_HOME": hermes_home,
        },
    )
    return p.stdout.strip()


def parse_facts(resp_tail: str) -> list:
    facts = []
    for line in resp_tail.split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or line.upper().startswith("NONE"):
            continue
        if "|" not in line:
            continue
        parts = line.split("|", 1)
        if len(parts) != 2:
            continue
        ftype = parts[0].strip().lower()
        ftext = parts[1].strip()
        if ftype in FACT_TYPES and len(ftext) > 10:
            facts.append((ftype, ftext))
    return facts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--shelf-pattern",
        default="mc-%",
        help="SQL LIKE pattern to scope source episodic chapters by shelf name (default: mc-%%).",
    )
    ap.add_argument("--limit", type=int, default=0, help="Process at most N chapters (0 = all).")
    ap.add_argument("--start-from-id", type=int, default=0, help="Skip chapters with id < N.")
    ap.add_argument("--dry-run", action="store_true", help="Extract + print facts but do not INSERT.")
    ap.add_argument("--timeout", type=int, default=45, help="LLM call timeout per chapter, seconds.")
    ap.add_argument("--sleep", type=float, default=1.0, help="Seconds between calls (rate-limit cushion).")
    ap.add_argument(
        "--prompt-file",
        help="Optional path to a custom prompt template. Must contain {ep_text}.",
    )
    args = ap.parse_args()

    db_path = resolve_db_path()
    hermes_home = resolve_hermes_home()
    hermes_bin = resolve_hermes_bin()

    agent_name = os.environ.get("HMK_AGENT_NAME") or "the agent"
    domain_desc = os.environ.get("HMK_DOMAIN_DESC") or "general-purpose assistant"

    if args.prompt_file:
        with open(args.prompt_file, "r", encoding="utf-8") as f:
            prompt_template = f.read()
        if "{ep_text}" not in prompt_template:
            print("ERROR: prompt template must contain {ep_text}", file=sys.stderr)
            return 2
    else:
        prompt_template = DEFAULT_PROMPT.replace(
            "{agent_name}", agent_name
        ).replace("{domain_desc}", domain_desc)

    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row

    rows = db.execute(
        """
        SELECT c.id, c.title, c.spr, c.raw, c.event_ts, c.actor, c.location_json,
               s.name AS shelf, b.title AS book_title
        FROM chapters c
        JOIN books b ON c.book_id = b.id
        JOIN shelves s ON b.shelf_id = s.id
        WHERE c.engram_type = 'episodic'
          AND s.name LIKE ?
          AND c.id >= ?
        ORDER BY c.id
        """,
        (args.shelf_pattern, args.start_from_id),
    ).fetchall()

    if args.limit > 0:
        rows = rows[: args.limit]

    print(f"=== {len(rows)} episodic chapters to process (shelf LIKE '{args.shelf_pattern}') ===")
    if args.dry_run:
        print("=== DRY RUN: facts will NOT be inserted ===")

    shelf_lookup = {
        r["name"]: r["id"]
        for r in db.execute("SELECT id, name FROM shelves").fetchall()
    }

    now = int(time.time())
    inserted = 0
    skipped_shelf = 0

    for r in rows:
        ep_text = f"[{r['shelf']}] {r['title']}\n{r['raw'] or r['spr']}"
        print(f"\n--- chapter {r['id']}: {r['title'][:60]} ---")
        try:
            resp = call_hermes(hermes_bin, hermes_home, prompt_template.format(ep_text=ep_text), timeout=args.timeout)
            resp_tail = resp[-800:]
            print(f"  resp: {resp_tail[:200]}...")

            facts = parse_facts(resp_tail)
            print(f"  -> extracted {len(facts)} facts")

            if args.dry_run:
                for ftype, ftext in facts:
                    print(f"     [{ftype}] {ftext[:80]}")
                continue

            for ftype, ftext in facts:
                target_shelf = FACT_TO_SHELF.get(ftype, "library")
                shelf_id = shelf_lookup.get(target_shelf)
                if not shelf_id:
                    skipped_shelf += 1
                    print(f"     SKIP: shelf '{target_shelf}' not present in this DB")
                    continue
                book_row = db.execute(
                    "SELECT id FROM books WHERE shelf_id=? AND slug=?",
                    (shelf_id, "engram-backfill"),
                ).fetchone()
                if book_row:
                    book_id = book_row[0]
                else:
                    cur = db.execute(
                        "INSERT INTO books (shelf_id, slug, title, source_kind, "
                        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            shelf_id,
                            "engram-backfill",
                            "Engram Backfill (auto-extracted facts)",
                            "auto",
                            now,
                            now,
                        ),
                    )
                    book_id = cur.lastrowid
                ord_row = db.execute(
                    "SELECT COALESCE(MAX(ordinal),0)+1 FROM chapters WHERE book_id=?",
                    (book_id,),
                ).fetchone()
                ord_n = ord_row[0]
                title = f"{ftype}: {ftext[:50]}"
                db.execute(
                    """
                    INSERT INTO chapters (book_id, ordinal, title, spr, raw, tokens,
                        importance, created_at, updated_at, engram_type, tags_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'semantic', ?)
                    """,
                    (
                        book_id,
                        ord_n,
                        title,
                        ftext,
                        ftext,
                        len(ftext) // 4,
                        0.6,
                        now,
                        now,
                        json.dumps(["engram-backfill", ftype, f"src-chapter-{r['id']}"]),
                    ),
                )
                inserted += 1
            if not args.dry_run:
                db.commit()
        except subprocess.TimeoutExpired:
            print("  TIMEOUT")
        except Exception as e:
            print(f"  ERROR: {e}")
        time.sleep(args.sleep)

    print(f"\n=== TOTAL inserted: {inserted} facts ===")
    if skipped_shelf:
        print(f"=== Skipped (missing target shelf): {skipped_shelf} ===")
    print("=== current semantic count ===")
    print(
        db.execute(
            "SELECT COUNT(*) FROM chapters WHERE engram_type='semantic'"
        ).fetchone()[0]
    )
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
