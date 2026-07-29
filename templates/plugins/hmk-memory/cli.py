"""CLI subcommand for hmk-memory.

Discovered by Hermes' ``discover_plugin_cli_commands()`` and exposed as
``hermes hmk-memory <subcommand>`` only when the provider is the active
``memory.provider`` (active-provider gating).
"""
from __future__ import annotations

import importlib
import importlib.util as iu
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


def _resolve_hermes_home():
    for k in ("HMK_HERMES_HOME", "HERMES_HOME"):
        v = os.environ.get(k)
        if v:
            return v
    return None


def _import_memoryctl():
    """Load memoryctl.py using the same priority as the provider."""
    hermes_home = _resolve_hermes_home()
    candidates = [os.environ.get("HMK_MEMORYCTL_PATH")]
    if hermes_home:
        candidates.append(str(Path(hermes_home).parent / "scripts" / "memoryctl.py"))
    for c in candidates:
        if c and Path(c).is_file():
            spec = iu.spec_from_file_location("hmk_memoryctl_cli", c)
            mod = iu.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(mod)
            return mod
    return importlib.import_module("memoryctl")


def _load_provider_class():
    """Load HMKMemoryProvider from the sibling __init__.py at runtime."""
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


def _get_memoryctl():
    """Return a memoryctl module reference, or exit with a clear error."""
    try:
        return _import_memoryctl()
    except Exception as e:
        print(f"ERROR: cannot load memoryctl module: {e}", file=sys.stderr)
        print(
            "Set HMK_MEMORYCTL_PATH to the absolute path of memoryctl.py "
            "or ensure HMK_AGENT_MEMORY_BASE / HERMES_HOME are set.",
            file=sys.stderr,
        )
        sys.exit(2)


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


def _parse_csv(value):
    if not value:
        return None
    if isinstance(value, (list, tuple)):
        return [str(x).strip() for x in value if str(x).strip()] or None
    return [s.strip() for s in str(value).split(",") if s.strip()] or None


def _cmd_query(args) -> int:
    mc = _get_memoryctl()
    if not args.query:
        print("ERROR: --query is required", file=sys.stderr)
        return 2
    result = mc.hybrid_pack(
        query=args.query,
        budget_tokens=args.budget_tokens,
        limit=args.limit,
        threshold=args.threshold,
        shelves=_parse_csv(args.shelves),
        exclude_shelves=_parse_csv(args.exclude_shelves),
    )
    import json

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


def _cmd_search(args) -> int:
    mc = _get_memoryctl()
    if not args.query:
        print("ERROR: --query is required", file=sys.stderr)
        return 2
    rows = mc.search(
        query=args.query,
        limit=args.limit,
        shelves=_parse_csv(args.shelves),
        exclude_shelves=_parse_csv(args.exclude_shelves),
    )
    import json

    print(json.dumps({"items": rows}, indent=2, ensure_ascii=False, default=str))
    return 0


def _cmd_add_text(args) -> int:
    mc = _get_memoryctl()
    if not args.shelf or not args.title or args.content is None:
        print("ERROR: --shelf, --title, and --content are required", file=sys.stderr)
        return 2
    chapter_id = mc.add_text(
        shelf_name=args.shelf,
        title=args.title,
        raw=args.content,
        tags=_parse_csv(args.tags),
        importance=args.importance,
    )
    print(f"Added chapter {chapter_id} to shelf '{args.shelf}'")
    return 0


def _cmd_add_file(args) -> int:
    mc = _get_memoryctl()
    if not args.shelf or not args.path:
        print("ERROR: --shelf and --path are required", file=sys.stderr)
        return 2
    p = Path(args.path).expanduser()
    if not p.is_file():
        print(f"ERROR: file not found: {p}", file=sys.stderr)
        return 2
    chapter_id = mc.add_file(
        path=str(p),
        shelf_name=args.shelf,
        title=args.title,
        tags=_parse_csv(args.tags),
        importance=args.importance,
    )
    print(f"Added chapter {chapter_id} from {p}")
    return 0


def _cmd_expand(args) -> int:
    mc = _get_memoryctl()
    if args.chapter_id is None:
        print("ERROR: --chapter-id is required", file=sys.stderr)
        return 2
    import json

    data = mc.expand(args.chapter_id)
    print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    return 0


def _cmd_update(args) -> int:
    mc = _get_memoryctl()
    if args.chapter_id is None:
        print("ERROR: --chapter-id is required", file=sys.stderr)
        return 2
    tags = _parse_csv(args.tags)
    if args.content is None and not args.title and tags is None and args.importance is None:
        print("ERROR: nothing to update: pass --content, --title, --tags, and/or --importance", file=sys.stderr)
        return 2
    import json

    result = mc.update_chapter(
        args.chapter_id,
        content=args.content,
        title=args.title or None,
        tags=tags,
        importance=args.importance,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


def _cmd_delete(args) -> int:
    mc = _get_memoryctl()
    if args.chapter_id is None:
        print("ERROR: --chapter-id is required", file=sys.stderr)
        return 2
    import json

    result = mc.delete_chapter(args.chapter_id, prune_book=not args.keep_book)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


def _cmd_stats(args) -> int:
    mc = _get_memoryctl()
    import json

    data = mc.stats()
    print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    return 0


def _cmd_link(args) -> int:
    mc = _get_memoryctl()
    if args.source_id is None or args.target_id is None:
        print("ERROR: --source-id and --target-id are required", file=sys.stderr)
        return 2
    mc.add_link(
        src_id=args.source_id,
        dst_id=args.target_id,
        link_type=args.link_type,
        weight=args.weight,
        note=args.note,
    )
    print(f"Linked {args.source_id} -> {args.target_id} ({args.link_type})")
    return 0


def hmk_memory_command(args) -> int:
    sub = getattr(args, "hmk_memory_command", None)
    handlers = {
        "status": _print_status,
        "query": _cmd_query,
        "search": _cmd_search,
        "add-text": _cmd_add_text,
        "add-file": _cmd_add_file,
        "expand": _cmd_expand,
        "update": _cmd_update,
        "delete": _cmd_delete,
        "stats": _cmd_stats,
        "link": _cmd_link,
    }
    handler = handlers.get(sub)
    if handler is None:
        print("Usage: hermes hmk-memory <status|query|search|add-text|add-file|expand|update|delete|stats|link>", file=sys.stderr)
        return 1
    return handler(args)


def register_cli(subparser) -> None:
    """Build the ``hermes hmk-memory`` argparse tree.

    Called by Hermes' ``discover_plugin_cli_commands()`` at argparse setup
    time, but only when ``memory.provider`` is set to ``hmk-memory``.
    """
    subs = subparser.add_subparsers(dest="hmk_memory_command")

    # status
    subs.add_parser("status", help="Show DB + ENGRAM + provider config snapshot")

    # query
    query_p = subs.add_parser("query", help="Hybrid lexical+semantic query")
    query_p.add_argument("--query", "-q", required=True, help="Query text")
    query_p.add_argument("--limit", "-l", type=int, default=8, help="Max results")
    query_p.add_argument("--threshold", "-t", type=float, default=0.4, help="Score threshold")
    query_p.add_argument("--shelves", "-s", help="Comma-separated shelf filter")
    query_p.add_argument("--exclude-shelves", help="Comma-separated shelves to exclude")
    query_p.add_argument("--budget-tokens", type=int, default=4000, help="Token budget")

    # search
    search_p = subs.add_parser("search", help="Pure lexical FTS search")
    search_p.add_argument("--query", "-q", required=True, help="Search text")
    search_p.add_argument("--limit", "-l", type=int, default=8, help="Max results")
    search_p.add_argument("--shelves", "-s", help="Comma-separated shelf filter")
    search_p.add_argument("--exclude-shelves", help="Comma-separated shelves to exclude")

    # add-text
    add_text_p = subs.add_parser("add-text", help="Add a text chapter to a shelf")
    add_text_p.add_argument("--shelf", required=True, help="Shelf name")
    add_text_p.add_argument("--title", required=True, help="Chapter title")
    add_text_p.add_argument("--content", "-c", required=True, help="Raw text content")
    add_text_p.add_argument("--tags", help="Comma-separated tags")
    add_text_p.add_argument("--importance", type=float, default=0.5, help="Importance 0.0-1.0")

    # add-file
    add_file_p = subs.add_parser("add-file", help="Ingest a file into a shelf")
    add_file_p.add_argument("--shelf", required=True, help="Shelf name")
    add_file_p.add_argument("--path", "-p", required=True, help="Path to file")
    add_file_p.add_argument("--title", help="Optional title (defaults to file stem)")
    add_file_p.add_argument("--tags", help="Comma-separated tags")
    add_file_p.add_argument("--importance", type=float, default=0.5, help="Importance 0.0-1.0")

    # expand
    expand_p = subs.add_parser("expand", help="Show full chapter record")
    expand_p.add_argument("--chapter-id", type=int, required=True, help="Chapter id")

    # update
    update_p = subs.add_parser("update", help="Update a chapter in place (content/title/tags/importance)")
    update_p.add_argument("--chapter-id", type=int, required=True, help="Chapter id")
    update_p.add_argument("--content", "-c", help="New content (recomputes spr; drops stored embeddings)")
    update_p.add_argument("--title", help="New title (keeps book title/slug in sync)")
    update_p.add_argument("--tags", help="Comma-separated tags; replaces the tag set when provided")
    update_p.add_argument("--importance", type=float, help="Importance 0.0-1.0")

    # delete
    delete_p = subs.add_parser("delete", help="Delete a chapter (cascades embeddings/links; prunes empty book)")
    delete_p.add_argument("--chapter-id", type=int, required=True, help="Chapter id")
    delete_p.add_argument("--keep-book", action="store_true", help="Keep the parent book even if left empty")

    # stats
    subs.add_parser("stats", help="Library statistics")

    # link
    link_p = subs.add_parser("link", help="Create a link between two chapters")
    link_p.add_argument("--source-id", type=int, required=True, help="Source chapter id")
    link_p.add_argument("--target-id", type=int, required=True, help="Destination chapter id")
    link_p.add_argument("--link-type", default="related", help="Link type")
    link_p.add_argument("--weight", type=float, default=1.0, help="Link weight")
    link_p.add_argument("--note", help="Optional note")

    subparser.set_defaults(func=hmk_memory_command)
