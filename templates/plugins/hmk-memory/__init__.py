"""hmk-memory — long-term memory provider backed by hermes-memory-kit's library.db.

This plugin implements the Hermes Agent ``MemoryProvider`` ABC. On every API
call the active provider receives the user's message via ``prefetch(query)``
and may inject recalled context into the conversation. ``hmk-memory`` answers
that call by running ``memoryctl.engram_pack`` (RRF over episodic / semantic /
procedural buckets) or, as a fallback when the DB does not yet have the
ENGRAM schema applied, ``memoryctl.hybrid_pack`` — and returns the result as
a markdown bullet list.

In addition to per-turn ``prefetch`` recall, this provider now exposes the
``remember`` / ``recall`` tools (deliberate write/read of library.db) and an
``on_session_end`` hook that distills the closing conversation into durable
chapters via an auxiliary LLM (organic growth), tagging each by the session's
interlocutor for gateway-independent, per-contact partitioning.

Configuration is env-var-only. See ``README.md`` for the full table.
"""
from __future__ import annotations

import importlib
import importlib.util as iu
import logging
import os
import re
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

    @staticmethod
    def _slug_interlocutor(*candidates) -> str:
        # Derive a stable tag from the session's interlocutor (prefer a human
        # name; fall back to the id with platform suffix stripped). Makes
        # organic, gateway-independent per-contact partitioning of memory.
        for v in candidates:
            if not v or not isinstance(v, str):
                continue
            v = v.split("@", 1)[0].strip()
            if not v:
                continue
            slug = re.sub(r"[^a-z0-9]+", "-", v.lower()).strip("-")
            if slug:
                return slug[:40]
        return ""

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id
        self._interlocutor = self._slug_interlocutor(
            kwargs.get("chat_name"), kwargs.get("user_name"),
            kwargs.get("chat_id"), kwargs.get("user_id"),
        )
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

    _SHELVES = ["identity", "state", "plans", "episodes", "library", "evidence"]

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        # FLAT schema format (name/description/parameters at top level) — the
        # MemoryManager reads schema["name"] directly; an OpenAI-nested
        # {"type":"function","function":{...}} wrapper makes name="" and the
        # tool is silently dropped from the routing table.
        return [
            {
                "name": "remember",
                "description": (
                    "Save a DURABLE fact to long-term memory (HMK library.db). "
                    "Use whenever the user asks you to remember something, or when "
                    "you learn a lasting fact, decision, preference, or person worth "
                    "keeping across sessions. This is your reliable write path \u2014 it "
                    "always persists. Do NOT save transient/session-only details."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "The fact, as a self-contained sentence or two."},
                        "title": {"type": "string", "description": "Short handle/title for this memory."},
                        "shelf": {"type": "string", "enum": self._SHELVES, "description": "Bucket. Default 'library' (reusable knowledge, people, lessons). 'identity'=who I/the operator am; 'plans'=decisions/roadmap; 'evidence'=source docs; 'state'=current operating state; 'episodes'=chronological log."},
                        "tags": {"type": "string", "description": "Optional comma-separated tags."},
                        "importance": {"type": "number", "description": "0-10; default 5. Higher = surfaces more readily."},
                    },
                    "required": ["content", "title"],
                },
            },
            {
                "name": "recall",
                "description": (
                    "Explicitly search long-term memory (HMK library.db) for durable "
                    "facts. Passive recall already runs each turn; use this for a "
                    "deliberate lookup ('what do I know about X?')."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "What to look up."},
                        "limit": {"type": "integer", "description": "Max items (default 5)."},
                    },
                    "required": ["query"],
                },
            },
        ]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        try:
            mc = self._get_memoryctl()
        except Exception as e:  # pragma: no cover - defensive
            return "ERROR: memory backend unavailable: %s" % e

        if tool_name == "remember":
            content = (args.get("content") or "").strip()
            if not content:
                return "ERROR: 'content' is required."
            title = (args.get("title") or content[:60]).strip()
            shelf = args.get("shelf") or "library"
            if shelf not in self._SHELVES:
                shelf = "library"
            tags = [t.strip() for t in (args.get("tags") or "").split(",") if t.strip()]
            try:
                importance = float(args.get("importance", 5.0))
            except (TypeError, ValueError):
                importance = 5.0
            try:
                # replace=False: never destroy an existing same-title memory.
                cid = mc.add_text(shelf_name=shelf, title=title, raw=content,
                                  tags=tags, importance=importance, replace=False)
            except Exception as e:
                logger.warning("remember: add_text failed: %s", e)
                return "ERROR saving to memory: %s" % e
            embed_note = ""
            try:
                mc.backfill_embeddings(only_missing=True)
            except Exception as e:
                logger.warning("remember: embed backfill failed (saved, lexical-only): %s", e)
                embed_note = " (semantic index pending; lexically searchable now)"
            return "Saved to long-term memory [HMK shelf '%s', id %s] \"%s\".%s" % (
                shelf, cid, title, embed_note)

        if tool_name == "recall":
            query = (args.get("query") or "").strip()
            if not query:
                return "ERROR: 'query' is required."
            try:
                limit = int(args.get("limit", 5))
            except (TypeError, ValueError):
                limit = 5
            try:
                result = mc.hybrid_pack(query=query, budget_tokens=self._budget,
                                        limit=limit, threshold=self._threshold,
                                        shelves=self._shelves)
            except Exception as e:
                logger.warning("recall: hybrid_pack failed: %s", e)
                return "ERROR searching memory: %s" % e
            items = result.get("items", []) if isinstance(result, dict) else []
            if not items:
                return "No durable memories found for: %s" % query
            return self._render_items(items)

        return "ERROR: hmk-memory does not handle tool '%s'." % tool_name

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

    # ---- organic growth: end-of-session distillation -------------------
    # When a session expires/resets the gateway calls on_session_end(messages)
    # with the real transcript. We distill durable novelties into HMK via an
    # auxiliary-LLM call, OFF the hot path (daemon thread) so we never block
    # the gateway's session-expiry watcher. Best-effort: failures are logged
    # and dropped, never raised.

    _DISTILL_SYS = (
        "You extract DURABLE long-term memories from a conversation transcript "
        "for an AI agent's knowledge base. Return ONLY a JSON array (possibly "
        "empty). Each element: {\"shelf\": one of [identity,state,plans,episodes,"
        "library,evidence], \"title\": short handle, \"content\": 1-3 self-contained "
        "sentences, \"importance\": 0-10, \"tags\": comma-separated}. "
        "INCLUDE only lasting facts: decisions, stable user/operator preferences, "
        "people/relationships, project facts, durable lessons. "
        "EXCLUDE: transient/session-only details, greetings, the agent's own "
        "chit-chat, and ANYTHING about security config, secrets, credentials, "
        "allowlists, or tokens. If nothing durable, return []. shelf guidance: "
        "library=reusable knowledge/people/lessons; identity=who the agent/operator is; "
        "plans=decisions/roadmap; evidence=source docs; state=current operating state; "
        "episodes=notable chronological events."
    )

    def on_session_end(self, messages):
        try:
            if (os.environ.get("HMK_DISTILL_ENABLED", "1").strip().lower()
                    in ("0", "false", "no", "off")):
                return
            if not messages or not isinstance(messages, list):
                return
            import threading
            t = threading.Thread(
                target=self._run_distill, args=(list(messages),),
                name="hmk-distill", daemon=True,
            )
            t.start()
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("hmk-memory on_session_end spawn failed: %s", e)

    @staticmethod
    def _messages_to_transcript(messages, max_chars=12000):
        parts = []
        for m in messages:
            if not isinstance(m, dict):
                continue
            role = m.get("role", "")
            if role not in ("user", "assistant"):
                continue
            c = m.get("content", "")
            if isinstance(c, list):  # multimodal blocks
                segs = []
                for b in c:
                    if isinstance(b, dict) and b.get("type") in ("text", "input_text"):
                        segs.append(b.get("text", ""))
                c = " ".join(segs)
            if not isinstance(c, str):
                c = str(c)
            c = c.strip()
            if c:
                parts.append("%s: %s" % (role.upper(), c))
        text = "\n".join(parts)
        if len(text) > max_chars:  # keep the tail (most recent) within budget
            text = text[-max_chars:]
        return text

    def _run_distill(self, messages):
        try:
            min_turns = int(os.environ.get("HMK_DISTILL_MIN_TURNS", "2"))
            user_turns = sum(1 for m in messages if isinstance(m, dict) and m.get("role") == "user")
            if user_turns < min_turns:
                return
            transcript = self._messages_to_transcript(messages)
            if len(transcript) < 200:
                return
            cands = self._extract_candidates(transcript)
            if not cands:
                logger.info("hmk-distill: no durable novelties extracted")
                return
            n = self._persist_candidates(cands)
            logger.info("hmk-distill: extracted %d candidate(s), persisted %d new", len(cands), n)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("hmk-distill failed: %s", e)

    def _extract_candidates(self, transcript):
        provider = os.environ.get("HMK_DISTILL_PROVIDER", "kimi-coding")
        model = os.environ.get("HMK_DISTILL_MODEL", "kimi-k2.7")
        timeout = float(os.environ.get("HMK_DISTILL_TIMEOUT", "120"))
        max_facts = int(os.environ.get("HMK_DISTILL_MAX_FACTS", "5"))
        from agent.auxiliary_client import call_llm
        resp = call_llm(
            provider=provider, model=model,
            messages=[
                {"role": "system", "content": self._DISTILL_SYS},
                {"role": "user", "content": "Transcript:\n\n" + transcript},
            ],
            temperature=0.2, max_tokens=1400, timeout=timeout,
        )
        text = resp.choices[0].message.content if resp and resp.choices else ""
        if not text:
            return []
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
            if text.lower().startswith("json"):
                text = text[4:].strip()
        import json as _json
        try:
            data = _json.loads(text)
        except Exception:
            i, j = text.find("["), text.rfind("]")
            if i == -1 or j == -1 or j <= i:
                logger.warning("hmk-distill: could not parse extractor output")
                return []
            data = _json.loads(text[i:j + 1])
        if not isinstance(data, list):
            return []
        out = []
        for d in data[:max_facts]:
            if not isinstance(d, dict):
                continue
            content = (d.get("content") or "").strip()
            if not content:
                continue
            shelf = d.get("shelf") or "library"
            if shelf not in ("identity", "state", "plans", "episodes", "library", "evidence"):
                shelf = "library"
            out.append({
                "shelf": shelf,
                "title": (d.get("title") or content[:60]).strip(),
                "content": content,
                "importance": float(d.get("importance", 5.0)) if str(d.get("importance", "")).strip() not in ("",) else 5.0,
                "tags": d.get("tags") or "",
            })
        return out

    def _persist_candidates(self, cands):
        mc = self._get_memoryctl()
        dedup_thr = float(os.environ.get("HMK_DISTILL_DEDUP_THRESHOLD", "0.82"))
        added = 0
        for c in cands:
            try:
                dup = mc.hybrid_pack(query=c["content"], budget_tokens=300, limit=1, threshold=0.0)
                items = dup.get("items", []) if isinstance(dup, dict) else []
                if items and float(items[0].get("score", 0.0)) >= dedup_thr:
                    continue
            except Exception:
                pass  # dedup is best-effort; fall through to write
            try:
                tags = [t.strip() for t in str(c["tags"]).split(",") if t.strip()]
                tags.append("auto-distilled")
                _interloc = getattr(self, "_interlocutor", "")
                if _interloc and _interloc not in tags:
                    tags.append(_interloc)
                mc.add_text(shelf_name=c["shelf"], title=c["title"], raw=c["content"],
                            tags=tags, importance=c["importance"], replace=False)
                added += 1
            except Exception as e:
                logger.warning("hmk-distill: add_text failed: %s", e)
        if added:
            try:
                mc.backfill_embeddings(only_missing=True)
            except Exception as e:
                logger.warning("hmk-distill: embed backfill failed: %s", e)
        return added


def register(ctx) -> None:
    """Discovery entry point — Hermes calls this when scanning the plugin."""
    ctx.register_memory_provider(HMKMemoryProvider())
