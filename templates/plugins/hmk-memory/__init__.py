"""hmk-memory — long-term memory provider backed by hermes-memory-kit's library.db.

This plugin implements the Hermes Agent ``MemoryProvider`` ABC. On every API
call the active provider receives the user's message via ``prefetch(query)``
and may inject recalled context into the conversation. ``hmk-memory`` answers
that call by running ``memoryctl.engram_pack`` (RRF over episodic / semantic /
procedural buckets) or, as a fallback when the DB does not yet have the
ENGRAM schema applied, ``memoryctl.hybrid_pack`` — and returns the result as
a markdown bullet list.

Configuration is env-var-only. See ``README.md`` for the full table.

v3.8.0 — exposes the ``librarian`` tool so agents can read and write durable
knowledge in ``library.db`` without leaving the conversation.

v3.8.1 / plugin 1.1.0 — the ``librarian`` tool gains ``update`` and ``delete``
actions (backed by ``memoryctl.update_chapter`` / ``delete_chapter``).
"""
from __future__ import annotations

import importlib
import importlib.util as iu
import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Memoryctl import — profile-scoped via hermes_home (kwarg from initialize()).
# Two layouts are recognised:
#   - workspace layout: hermes_home is a child of the workspace dir, with
#     scripts/ as a sibling. Example: ~/agents/steve/hermes-home/  →
#     ~/agents/steve/scripts/memoryctl.py
#   - root layout: hermes_home IS the workspace, scripts/ is a child of it.
#     Example: ~/.hermes/  →  ~/.hermes/scripts/memoryctl.py
# Lookup priority:
#   1. HMK_MEMORYCTL_PATH    — explicit override for non-standard deploys
#   2. <hermes_home>/../scripts/memoryctl.py  (workspace layout)
#   3. <hermes_home>/scripts/memoryctl.py     (root layout)
#   4. importlib.import_module("memoryctl") — PYTHONPATH lookup (rare)
# Deliberately no fallback to ~/hermes-memory-kit or /home/<user>/... — those
# would be host-specific and break profile isolation.
# ---------------------------------------------------------------------------
def _import_memoryctl(hermes_home: Optional[str] = None):
    candidates: List[Optional[str]] = [os.environ.get("HMK_MEMORYCTL_PATH")]
    if hermes_home:
        hh = Path(hermes_home)
        candidates.append(str(hh.parent / "scripts" / "memoryctl.py"))
        candidates.append(str(hh / "scripts" / "memoryctl.py"))
    for c in candidates:
        if c and Path(c).is_file():
            spec = iu.spec_from_file_location("hmk_memoryctl", c)
            mod = iu.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(mod)
            return mod
    return importlib.import_module("memoryctl")


# ---------------------------------------------------------------------------
# Env-var resolution mirroring memoryctl.py exactly.
# memoryctl.connect() requires BOTH BASE_DIR and DB_PATH non-None and
# sys.exit(2) otherwise. BASE_DIR comes from HMK_AGENT_MEMORY_BASE /
# AGENT_MEMORY_BASE / HMK_BASE_DIR — HMK_DB_PATH does NOT contribute to it.
# ---------------------------------------------------------------------------
def _resolve_base_dir() -> Optional[str]:
    for k in ("HMK_AGENT_MEMORY_BASE", "AGENT_MEMORY_BASE", "HMK_BASE_DIR"):
        v = os.environ.get(k)
        if v:
            return v
    return None


def _resolve_db_path() -> Optional[str]:
    direct = os.environ.get("HMK_DB_PATH")
    if direct:
        return direct
    base = _resolve_base_dir()
    if base:
        return os.path.join(base, "library.db")
    return None


# Try to import the ABC. Outside of a real Hermes runtime (e.g. unit tests
# in the kit's CI) the import fails; fall back to a tiny stub so the file
# parses and the class can still be exercised.
try:
    from agent.memory_provider import MemoryProvider  # type: ignore
except Exception:  # pragma: no cover - exercised only outside Hermes
    class MemoryProvider:  # type: ignore
        pass


def _parse_csv(value: Any) -> Optional[List[str]]:
    """Normalize a comma-separated string or list into a list of trimmed values."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str):
        return [s.strip() for s in value.split(",") if s.strip()] or None
    return None


# ---------------------------------------------------------------------------
# Tool schema for the librarian tool
# ---------------------------------------------------------------------------
LIBRARIAN_SCHEMA = {
    "name": "librarian",
    "description": (
        "Read, write, and inspect durable knowledge in the local HMK library "
        "(~/.hermes/agent-memory/library.db). This is the canonical long-term "
        "memory store: anything saved here survives across sessions and can be "
        "retrieved later by query or exact chapter id.\n\n"
        "Actions:\\n"
        "- query: hybrid (lexical + semantic) retrieval. Returns ranked items.\\n"
        "- search: pure lexical FTS search.\\n"
        "- add_text: store a new text chapter under a shelf.\\n"
        "- add_file: ingest a file from disk into a shelf.\\n"
        "- expand: return the full record for a chapter id, including neighbors.\\n"
        "- update: edit a chapter in place (content/title/tags/importance).\\n"
        "- delete: remove a chapter (cascades embeddings/links).\\n"
        "- add_link: create a directed link between two chapters.\\n"
        "- suggest_links: list link suggestions from vector similarity (read-only).\\n"
        "- stats: return library counts and embedding metadata.\\n\\n"
        "Use this tool instead of SQL scripts or manual memoryctl calls."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["query", "search", "add_text", "add_file", "expand", "update", "delete", "stats", "add_link", "suggest_links"],
                "description": "The librarian action to perform.",
            },
            "query": {"type": "string", "description": "Search/query text (required for query/search)."},
            "shelf": {"type": "string", "description": "Target shelf name (required for add_text/add_file)."},
            "title": {"type": "string", "description": "Chapter/book title (required for add_text; optional for add_file, defaults to file stem; optional for update)."},
            "content": {"type": "string", "description": "Raw text content (required for add_text; optional for update)."},
            "file_path": {"type": "string", "description": "Absolute or relative path to a file (required for add_file)."},
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional list of tags to attach to the new chapter.",
            },
            "importance": {"type": "number", "description": "Optional importance score (0.0-1.0, default 0.5)."},
            "limit": {"type": "integer", "description": "Max results for query/search (default 8)."},
            "threshold": {"type": "number", "description": "Minimum score threshold for query (default 0.4)."},
            "shelves": {"type": "string", "description": "Comma-separated shelf filter for query/search."},
            "exclude_shelves": {"type": "string", "description": "Comma-separated shelves to exclude from query/search."},
            "chapter_id": {"type": "integer", "description": "Chapter id (required for expand/update/delete)."},
            "source_id": {"type": "integer", "description": "Source chapter id (required for add_link)."},
            "target_id": {"type": "integer", "description": "Destination chapter id (required for add_link)."},
            "link_type": {"type": "string", "description": "Link type (default 'related')."},
            "note": {"type": "string", "description": "Optional note for add_link."},
        },
        "required": ["action"],
    },
}


class HMKMemoryProvider(MemoryProvider):
    """Long-term memory provider for Hermes Agent backed by library.db."""

    DEFAULT_QUOTAS = {"episodic": 2, "semantic": 4, "procedural": 2}
    DEFAULT_LIMIT = 8
    DEFAULT_THRESHOLD = 0.30
    DEFAULT_BUDGET_TOKENS = 1500
    DEFAULT_RETRIEVER = "engram_pack"

    # ---- core lifecycle -----------------------------------------------

    @property
    def name(self) -> str:
        return "hmk-memory"

    def is_available(self) -> bool:
        # Both BASE_DIR and DB_PATH must resolve. memoryctl.connect() does
        # _require_config() and sys.exit(2) if either is missing — setting
        # only HMK_DB_PATH would leave BASE_DIR=None and break the gateway
        # at the first prefetch.
        if _resolve_base_dir() is None:
            return False
        db_path = _resolve_db_path()
        if not db_path or not Path(db_path).is_file():
            return False
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            cols = {r[1] for r in con.execute("PRAGMA table_info(chapters)").fetchall()}
            con.close()
        except Exception:
            return False
        # ENGRAM is OPTIONAL — without those columns the provider stays
        # available and falls back to hybrid_pack at initialize time.
        return "id" in cols

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id
        self._hermes_home = (
            kwargs.get("hermes_home")
            or os.environ.get("HERMES_HOME")
            or os.environ.get("HMK_HERMES_HOME")
        )
        self._memoryctl = None  # lazy; first prefetch loads it

        self._engram_available = self._check_engram_columns()
        self._retriever = os.environ.get("HMK_PROVIDER_RETRIEVER", self.DEFAULT_RETRIEVER)
        if self._retriever == "engram_pack" and not self._engram_available:
            logger.info(
                "hmk-memory: ENGRAM columns not present, falling back to hybrid_pack"
            )
            self._retriever = "hybrid_pack"

        self._quotas = {
            "episodic": int(os.environ.get(
                "HMK_PROVIDER_QUOTA_EPISODIC", self.DEFAULT_QUOTAS["episodic"])),
            "semantic": int(os.environ.get(
                "HMK_PROVIDER_QUOTA_SEMANTIC", self.DEFAULT_QUOTAS["semantic"])),
            "procedural": int(os.environ.get(
                "HMK_PROVIDER_QUOTA_PROCEDURAL", self.DEFAULT_QUOTAS["procedural"])),
        }
        self._limit = int(os.environ.get("HMK_PROVIDER_LIMIT", self.DEFAULT_LIMIT))
        self._threshold = float(
            os.environ.get("HMK_PROVIDER_THRESHOLD", self.DEFAULT_THRESHOLD)
        )
        self._budget = int(
            os.environ.get("HMK_PROVIDER_BUDGET_TOKENS", self.DEFAULT_BUDGET_TOKENS)
        )
        shelves = os.environ.get("HMK_PROVIDER_SHELVES", "").strip()
        self._shelves = (
            [s.strip() for s in shelves.split(",") if s.strip()] or None
        )

        logger.info(
            "hmk-memory initialized: retriever=%s engram=%s limit=%d threshold=%.2f budget=%d shelves=%s",
            self._retriever,
            self._engram_available,
            self._limit,
            self._threshold,
            self._budget,
            self._shelves,
        )

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [LIBRARIAN_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if tool_name != "librarian":
            raise NotImplementedError(f"hmk-memory: tool '{tool_name}' not implemented")

        try:
            mc = self._get_memoryctl()
            action = (args.get("action") or "").strip()

            def _get_int(key: str, default: int) -> int:
                v = args.get(key)
                return int(v) if v is not None else default

            def _get_float(key: str, default: float) -> float:
                v = args.get(key)
                return float(v) if v is not None else default

            if action == "query":
                query = args.get("query") or ""
                if not query:
                    return json.dumps({"success": False, "error": "query is required"}, ensure_ascii=False)
                limit = _get_int("limit", self._limit)
                threshold = _get_float("threshold", self._threshold)
                shelves = _parse_csv(args.get("shelves")) or self._shelves
                exclude_shelves = _parse_csv(args.get("exclude_shelves"))
                if self._retriever == "engram_pack":
                    result = mc.engram_pack(
                        query=query,
                        budget_tokens=self._budget,
                        limit=limit,
                        threshold=threshold,
                        shelves=shelves,
                        exclude_shelves=exclude_shelves,
                        quotas=self._quotas,
                    )
                else:
                    result = mc.hybrid_pack(
                        query=query,
                        budget_tokens=self._budget,
                        limit=limit,
                        threshold=threshold,
                        shelves=shelves,
                        exclude_shelves=exclude_shelves,
                    )
                return json.dumps(result, ensure_ascii=False, default=str)

            if action == "search":
                query = args.get("query") or ""
                if not query:
                    return json.dumps({"success": False, "error": "query is required"}, ensure_ascii=False)
                limit = _get_int("limit", self._limit)
                shelves = _parse_csv(args.get("shelves")) or self._shelves
                exclude_shelves = _parse_csv(args.get("exclude_shelves"))
                rows = mc.search(
                    query=query,
                    limit=limit,
                    shelves=shelves,
                    exclude_shelves=exclude_shelves,
                )
                return json.dumps({"success": True, "items": rows}, ensure_ascii=False, default=str)

            if action == "add_text":
                shelf = args.get("shelf") or ""
                title = args.get("title") or ""
                content = args.get("content") or ""
                if not shelf or not title or not content:
                    return json.dumps({"success": False, "error": "shelf, title, and content are required"}, ensure_ascii=False)
                tags = _parse_csv(args.get("tags"))
                importance = _get_float("importance", 0.5)
                chapter_id = mc.add_text(
                    shelf_name=shelf,
                    title=title,
                    raw=content,
                    tags=tags,
                    importance=importance,
                )
                return json.dumps({"success": True, "chapter_id": chapter_id, "shelf": shelf, "title": title}, ensure_ascii=False)

            if action == "add_file":
                shelf = args.get("shelf") or ""
                file_path = args.get("file_path") or ""
                if not shelf or not file_path:
                    return json.dumps({"success": False, "error": "shelf and file_path are required"}, ensure_ascii=False)
                p = Path(file_path).expanduser()
                if not p.is_file():
                    return json.dumps({"success": False, "error": f"file not found: {p}"}, ensure_ascii=False)
                title = args.get("title") or None
                tags = _parse_csv(args.get("tags"))
                importance = _get_float("importance", 0.5)
                chapter_id = mc.add_file(
                    path=str(p),
                    shelf_name=shelf,
                    title=title,
                    tags=tags,
                    importance=importance,
                )
                return json.dumps({"success": True, "chapter_id": chapter_id, "shelf": shelf, "file": str(p)}, ensure_ascii=False)

            if action == "expand":
                chapter_id = args.get("chapter_id")
                if chapter_id is None:
                    return json.dumps({"success": False, "error": "chapter_id is required"}, ensure_ascii=False)
                data = mc.expand(int(chapter_id))
                return json.dumps({"success": True, "chapter": data}, ensure_ascii=False, default=str)

            if action == "update":
                chapter_id = args.get("chapter_id")
                if chapter_id is None:
                    return json.dumps({"success": False, "error": "chapter_id is required"}, ensure_ascii=False)
                content = args.get("content")
                title = args.get("title") or None
                tags = _parse_csv(args.get("tags"))
                importance_raw = args.get("importance")
                importance = float(importance_raw) if importance_raw is not None else None
                if content is None and title is None and tags is None and importance is None:
                    return json.dumps(
                        {"success": False, "error": "nothing to update: pass content, title, tags, and/or importance"},
                        ensure_ascii=False,
                    )
                result = mc.update_chapter(
                    int(chapter_id),
                    content=content,
                    title=title,
                    tags=tags,
                    importance=importance,
                )
                return json.dumps({"success": True, **result}, ensure_ascii=False, default=str)

            if action == "delete":
                chapter_id = args.get("chapter_id")
                if chapter_id is None:
                    return json.dumps({"success": False, "error": "chapter_id is required"}, ensure_ascii=False)
                result = mc.delete_chapter(int(chapter_id))
                return json.dumps({"success": True, **result}, ensure_ascii=False, default=str)

            if action == "stats":
                data = mc.stats()
                return json.dumps({"success": True, "stats": data}, ensure_ascii=False, default=str)

            if action == "add_link":
                source_id = args.get("source_id")
                target_id = args.get("target_id")
                link_type = args.get("link_type") or "related"
                if source_id is None or target_id is None:
                    return json.dumps({"success": False, "error": "source_id and target_id are required"}, ensure_ascii=False)
                mc.add_link(
                    src_id=int(source_id),
                    dst_id=int(target_id),
                    link_type=str(link_type),
                    weight=_get_float("weight", 1.0),
                    note=args.get("note"),
                )
                return json.dumps({"success": True, "source_id": source_id, "target_id": target_id, "link_type": link_type}, ensure_ascii=False)

            if action == "suggest_links":
                # Read-only: list candidates only — accept/reject is CLI-side
                chapter_id = None
                if args.get("chapter_id") is not None:
                    chapter_id = int(args["chapter_id"])
                status = args.get("status") or "candidate"
                limit = _get_int("limit", 20)
                rows = mc.list_link_suggestions(status=status, limit=limit)
                return json.dumps({"success": True, "items": rows}, ensure_ascii=False, default=str)

            return json.dumps({"success": False, "error": f"unknown action: {action}"}, ensure_ascii=False)
        except Exception as e:
            logger.error("librarian tool failed: %s", e, exc_info=True)
            return json.dumps({"success": False, "error": f"librarian tool failed: {e}"}, ensure_ascii=False)

    # ---- config (env-var-only, no setup wizard) -----------------------

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return []

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        return None

    # ---- optional hooks -----------------------------------------------

    def system_prompt_block(self) -> str:
        if getattr(self, "_retriever", self.DEFAULT_RETRIEVER) == "engram_pack":
            mode = "balanced retrieval over episodic/semantic/procedural buckets"
        else:
            mode = "lexical+semantic hybrid retrieval"
        return (
            "Long-term memory is available via hmk-memory: each turn you receive "
            f"a 'Memoria relevante' block under the user message, derived from {mode}. "
            "Cite items as [mem:N] when you use them. "
            "Use the `librarian` tool to query, add, update, delete, expand, or inspect the durable library."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not query or not query.strip():
            return ""
        try:
            mc = self._get_memoryctl()
            if self._retriever == "engram_pack":
                result = mc.engram_pack(
                    query=query,
                    budget_tokens=self._budget,
                    limit=self._limit,
                    threshold=self._threshold,
                    shelves=self._shelves,
                    quotas=self._quotas,
                )
            else:
                result = mc.hybrid_pack(
                    query=query,
                    budget_tokens=self._budget,
                    limit=self._limit,
                    threshold=self._threshold,
                    shelves=self._shelves,
                )
            items = result.get("items", []) if isinstance(result, dict) else []
            if not items:
                return ""
            return self._render_items(items)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("hmk-memory prefetch failed: %s", e)
            return ""

    def _render_items(self, items: List[Dict[str, Any]]) -> str:
        lines = ["## 🧠 Memoria relevante"]
        for it in items:
            etype = it.get("engram_type")  # only set when engram_pack ran
            shelf = it.get("shelf", "?")
            spr = (it.get("spr") or "")[:140].replace("\n", " ")
            mem_id = it.get("id") or it.get("chapter_id")
            tag = f"{etype}|{shelf}" if etype else shelf
            lines.append(f"- [{tag}] {spr}... [mem:{mem_id}]")
        return "\n".join(lines)

    def shutdown(self) -> None:
        return None

    # ---- private helpers ----------------------------------------------

    def _check_engram_columns(self) -> bool:
        db_path = _resolve_db_path()
        if not db_path or not Path(db_path).is_file():
            return False
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            cols = {r[1] for r in con.execute("PRAGMA table_info(chapters)").fetchall()}
            con.close()
        except Exception:
            return False
        return "engram_type" in cols

    def _get_memoryctl(self):
        if self._memoryctl is None:
            self._memoryctl = _import_memoryctl(hermes_home=self._hermes_home)
        return self._memoryctl


def register(ctx) -> None:
    """Discovery entry point — Hermes calls this when scanning the plugin."""
    ctx.register_memory_provider(HMKMemoryProvider())
