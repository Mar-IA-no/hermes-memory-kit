"""CLI subcommand for hmk-memory.

Discovered by Hermes' ``discover_plugin_cli_commands()`` and exposed as
``hermes hmk-memory <subcommand>`` only when the provider is the active
``memory.provider`` (active-provider gating).
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

# NOTE: the plugin directory is "hmk-memory" (with a hyphen), which is NOT a
# valid Python identifier. Hermes loads __init__.py via importlib at discovery
# time, but we cannot rely on a `from .` relative import in this CLI module
# when it's invoked outside that loader context. Inline the small env-var
# helpers and load the provider class via importlib for the status snapshot.

def _resolve_base_dir():
    for k in ("HMK_AGENT_MEMORY_BASE", "AGENT_MEMORY_BASE", "HMK_BASE_DIR"):
        v = os.environ.get(k)
        if v:
            return v
    return None


def _resolve_db_path():
    direct = os.environ.get("HMK_DB_PATH")
    if direct:
        return direct
    base = _resolve_base_dir()
    if base:
        return os.path.join(base, "library.db")
    return None


def _load_provider_class():
    """Load HMKMemoryProvider from the sibling __init__.py at runtime."""
    import importlib.util as iu
    here = Path(__file__).resolve().parent
    spec = iu.spec_from_file_location("hmk_memory_provider", here / "__init__.py")
    mod = iu.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod.HMKMemoryProvider


def _format_int(n: int) -> str:
    return f"{n:>6,}"


def _safe_open_ro(db_path: str):
    """Open the DB read-only. Returns None on any failure."""
    try:
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except Exception:
        return None


def _print_status() -> int:
    base = _resolve_base_dir()
    db_path = _resolve_db_path()

    print("hmk-memory status")
    print("-" * 60)
    print(f"BASE_DIR      : {base or '(unset — set HMK_AGENT_MEMORY_BASE)'}")
    print(f"DB_PATH       : {db_path or '(unresolvable)'}")

    if not db_path or not Path(db_path).is_file():
        print("DB            : MISSING — provider is_available()=False")
        return 2

    size = Path(db_path).stat().st_size
    print(f"DB size       : {size:,} bytes")

    con = _safe_open_ro(db_path)
    if con is None:
        print("DB            : COULD NOT OPEN — provider is_available()=False")
        return 2

    cols = {r[1] for r in con.execute("PRAGMA table_info(chapters)").fetchall()}
    has_engram = "engram_type" in cols
    print(f"ENGRAM applied: {'yes' if has_engram else 'no'}")
    print()

    if has_engram:
        print("Chapter counts by engram_type:")
        for row in con.execute(
            "SELECT engram_type, COUNT(*) FROM chapters "
            "GROUP BY engram_type ORDER BY 2 DESC"
        ).fetchall():
            print(f"  {row[0]:<12s} {_format_int(row[1])}")
        print()
        print("Engram-backfill books per shelf:")
        rows = con.execute(
            """
            SELECT s.name, COUNT(c.id)
            FROM chapters c
            JOIN books b ON b.id = c.book_id
            JOIN shelves s ON s.id = b.shelf_id
            WHERE b.slug = 'engram-backfill'
            GROUP BY s.name ORDER BY 2 DESC
            """
        ).fetchall()
        if rows:
            for r in rows:
                print(f"  {r[0]:<14s} {_format_int(r[1])}")
        else:
            print("  (none — run scripts/backfill-semantic.py to populate)")
    else:
        total = con.execute("SELECT COUNT(*) FROM chapters").fetchone()[0]
        print(f"Total chapters: {_format_int(total)}")
        print("Run scripts/migrate-engram.py to enable bucketed retrieval.")
    con.close()

    print()
    print("Provider config (effective env):")
    p = _load_provider_class()()
    # Initialize with a transient session so the env-derived attributes
    # populate. We pass hermes_home from env or fall back; this is read-only
    # for status purposes.
    hh = os.environ.get("HERMES_HOME") or os.environ.get("HMK_HERMES_HOME") or ""
    p.initialize(session_id="status-cli", hermes_home=hh)
    print(f"  retriever          : {p._retriever}")
    print(f"  limit              : {p._limit}")
    print(f"  threshold          : {p._threshold}")
    print(f"  budget_tokens      : {p._budget}")
    print(f"  quota_episodic     : {p._quotas['episodic']}")
    print(f"  quota_semantic     : {p._quotas['semantic']}")
    print(f"  quota_procedural   : {p._quotas['procedural']}")
    print(f"  shelves filter     : {p._shelves or '(all)'}")
    return 0


def hmk_memory_command(args) -> int:
    sub = getattr(args, "hmk_memory_command", None)
    if sub == "status":
        return _print_status()
    print("Usage: hermes hmk-memory <status>", file=sys.stderr)
    return 1


def register_cli(subparser) -> None:
    """Build the ``hermes hmk-memory`` argparse tree.

    Called by Hermes' ``discover_plugin_cli_commands()`` at argparse setup
    time, but only when ``memory.provider`` is set to ``hmk-memory``.
    """
    subs = subparser.add_subparsers(dest="hmk_memory_command")
    subs.add_parser("status", help="Show DB + ENGRAM + provider config snapshot")
    subparser.set_defaults(func=hmk_memory_command)
