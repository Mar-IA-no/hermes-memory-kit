"""hmk-memory — long-term memory provider backed by hermes-memory-kit's library.db.

This plugin implements the Hermes Agent ``MemoryProvider`` ABC. On every API
call the active provider receives the user's message via ``prefetch(query)``
and may inject recalled context into the conversation. ``hmk-memory`` answers
that call by running ``memoryctl.engram_pack`` (RRF over episodic / semantic /
procedural buckets) or, as a fallback when the DB does not yet have the
ENGRAM schema applied, ``memoryctl.hybrid_pack`` — and returns the result as
a markdown bullet list.

Configuration is env-var-only. See ``README.md`` for the full table.
"""
from __future__ import annotations

import importlib
import importlib.util as iu
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Memoryctl import — profile-scoped via hermes_home (kwarg from initialize()).
# Lookup priority:
#   1. HMK_MEMORYCTL_PATH    — explicit override for non-standard deploys
#   2. <workspace>/scripts/memoryctl.py — standard bootstrapped layout (the
#      workspace is `Path(hermes_home).parent`)
#   3. importlib.import_module("memoryctl") — PYTHONPATH lookup (rare)
# Deliberately no fallback to ~/hermes-memory-kit or /home/<user>/... — those
# would be host-specific and break profile isolation.
# ---------------------------------------------------------------------------
def _import_memoryctl(hermes_home: Optional[str] = None):
    candidates: List[Optional[str]] = [os.environ.get("HMK_MEMORYCTL_PATH")]
    if hermes_home:
        ws = Path(hermes_home).parent
        candidates.append(str(ws / "scripts" / "memoryctl.py"))
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
        return []

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        raise NotImplementedError("hmk-memory: tools are not exposed in v3.7.0 MVP")

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
            "Cite items as [mem:N] when you use them."
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
