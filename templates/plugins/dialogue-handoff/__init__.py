"""hermes-continuity-plugin v1.0.0 — conversational continuity for Hermes.

Working memory plugin: persists last-N substantive turns and injects them at
the start of new sessions. Anti-amnesia layer between sessions/crashes/model
switches/context compaction.

Equivalent in code to dialogue-handoff v3.1.0 bundled inside hermes-memory-kit
(commit b3b449e + 0e499d8). Re-numbered to v1.0.0 because this is the first
release as a standalone repo.

Compatibility: hermes-memory-kit ≥ v3.1.0 (when used vendored).
                Standalone (any Hermes Agent ≥ v0.10) when used drop-in.

Inherited from ex-kit-v3.1.0:
  - _trunc() multi-line preservation
  - ## Recent Exchanges rolling tail (N=4 substantive turns, 2000 chars/msg)
  - _SUBSTANTIVE_MIN_CHARS=300 gate (trivial turns don't overwrite tail)
  - Backwards-compat with v3.0 handoffs (legacy tiered-JSON fallback)

Changed vs ex-kit-v3.1.0:
  - Env var cascade now accepts HERMES_* canonical names with HMK_*/legacy
    fallback. logger.warning() once per legacy var matched. NO DeprecationWarning
    (Python filters those by default in runtime).
  - Bases (HERMES_HOME, HERMES_AGENT_MEMORY_BASE) included in cascade so users
    can configure with 2 env vars instead of 6.

Env vars supported (canonical first, legacy after, base-derived last):

    Direct paths:
        HERMES_HANDOFF_PATH        || HMK_DIALOGUE_HANDOFF_PATH
        HERMES_ALWAYS_CONTEXT_PATH || HMK_ALWAYS_CONTEXT_PATH
        HERMES_SESSIONS_DIR        || HMK_SESSIONS_DIR

    Bases (used to derive direct paths if those are not set):
        HERMES_AGENT_MEMORY_BASE   || AGENT_MEMORY_BASE / HMK_AGENT_MEMORY_BASE / HMK_BASE_DIR
        HERMES_HOME                || HMK_HERMES_HOME

    Derivation rules:
        HANDOFF_PATH         := <agent_memory_base>/state/DIALOGUE-HANDOFF.md
        ALWAYS_CONTEXT_PATH  := <agent_memory_base>/state/ALWAYS-CONTEXT.md
        SESSIONS_DIR         := <hermes_home>/sessions

If neither direct path nor base is set, the plugin disables itself (no-op
hooks) and logs an error explaining what to set.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# --- paths -----------------------------------------------------------
#
# Cascade: HERMES_* canonical || legacy fallback || base-derived.
# logger.warning() once per legacy var matched (visible in gateway logs;
# Python's DeprecationWarning is filtered by default in runtime).

_legacy_warned: set = set()


def _log_legacy_once(legacy_var: str, canonical_var: str) -> None:
    if legacy_var in _legacy_warned:
        return
    _legacy_warned.add(legacy_var)
    logger.warning(
        "continuity-plugin: using legacy env var %s (canonical: %s). "
        "Legacy fallback removal planned for plugin v2.0.",
        legacy_var,
        canonical_var,
    )


def _resolve_path(keys: List[str], canonical_idx: int = 0) -> Optional[Path]:
    """Resolve the first env var set in `keys`. If the matched key is at
    index > canonical_idx (i.e. legacy), warn once via logger."""
    for i, k in enumerate(keys):
        v = os.environ.get(k)
        if v:
            if i > canonical_idx:
                _log_legacy_once(k, keys[canonical_idx])
            return Path(v).expanduser()
    return None


# Bases (canonical first, legacy after)
_AGENT_MEMORY_BASE = _resolve_path([
    "HERMES_AGENT_MEMORY_BASE",   # canonical
    "AGENT_MEMORY_BASE",           # legacy generic (no HMK_ prefix)
    "HMK_AGENT_MEMORY_BASE",       # legacy kit
    "HMK_BASE_DIR",                # legacy kit alternate
])
_HERMES_HOME = _resolve_path([
    "HERMES_HOME",                 # canonical (Hermes Agent reads this directly)
    "HMK_HERMES_HOME",             # legacy kit
])

# Direct paths (canonical, legacy, then derive from base)
_HANDOFF_PATH = (
    _resolve_path(["HERMES_HANDOFF_PATH", "HMK_DIALOGUE_HANDOFF_PATH"])
    or (_AGENT_MEMORY_BASE / "state" / "DIALOGUE-HANDOFF.md" if _AGENT_MEMORY_BASE else None)
)
_ALWAYS_CONTEXT_PATH = (
    _resolve_path(["HERMES_ALWAYS_CONTEXT_PATH", "HMK_ALWAYS_CONTEXT_PATH"])
    or (_AGENT_MEMORY_BASE / "state" / "ALWAYS-CONTEXT.md" if _AGENT_MEMORY_BASE else None)
)
_SESSIONS_DIR = (
    _resolve_path(["HERMES_SESSIONS_DIR", "HMK_SESSIONS_DIR"])
    or (_HERMES_HOME / "sessions" if _HERMES_HOME else None)
)

_CONFIG_OK = all([_HANDOFF_PATH, _ALWAYS_CONTEXT_PATH, _SESSIONS_DIR])
if not _CONFIG_OK:
    missing = []
    if not _HANDOFF_PATH:
        missing.append("HERMES_HANDOFF_PATH (or HERMES_AGENT_MEMORY_BASE)")
    if not _ALWAYS_CONTEXT_PATH:
        missing.append("HERMES_ALWAYS_CONTEXT_PATH (or HERMES_AGENT_MEMORY_BASE)")
    if not _SESSIONS_DIR:
        missing.append("HERMES_SESSIONS_DIR (or HERMES_HOME)")
    logger.error(
        "continuity-plugin v1.0.0: plugin DISABLED — missing env: %s. "
        "The plugin will not read or write any handoff/always-context file. "
        "Legacy HMK_* names are accepted as fallback. See README of "
        "hermes-continuity-plugin for the full env var cascade.",
        ", ".join(missing),
    )


# --- write side (post_llm_call) --------------------------------------

_FILE_TOOLS_WITH_PATH = {"read_file", "write_file", "search_files", "patch"}
_SHELL_TOOLS = {"terminal", "execute_code", "shell", "bash"}

_QUOTED_PATH_RE = re.compile(r'''["'](/[^"'\n]{2,400})["']''')
_UNQUOTED_PATH_RE = re.compile(r'(?<![\w/])(/(?:home|mnt|media|opt|srv)/[\w./\-]+)')

_ALLOWED_PATH_ROOTS = ("/home/", "/mnt/", "/media/", "/opt/", "/srv/")


def _extract_paths_from_shell(text: str) -> List[str]:
    if not text:
        return []
    quoted = list(_QUOTED_PATH_RE.findall(text))
    unquoted = list(_UNQUOTED_PATH_RE.findall(text))
    unquoted = [u for u in unquoted if not any(u in q for q in quoted)]
    found = quoted + unquoted
    seen = set()
    out = []
    for p in found:
        if not any(p.startswith(r) for r in _ALLOWED_PATH_ROOTS):
            continue
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out


def _extract_working_set(history: List[Dict[str, Any]]) -> List[str]:
    paths: List[str] = []
    seen = set()
    for msg in reversed(history or []):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "user":
            break
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
            name = fn.get("name")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:
                args = {}
            if name in _FILE_TOOLS_WITH_PATH:
                for k in ("path", "file_path", "target_path"):
                    v = args.get(k)
                    if isinstance(v, str) and v and v not in seen:
                        paths.append(v)
                        seen.add(v)
            elif name in _SHELL_TOOLS:
                blob = args.get("command") or args.get("code") or args.get("cmd") or ""
                if isinstance(blob, str):
                    for p in _extract_paths_from_shell(blob):
                        if p not in seen:
                            paths.append(p)
                            seen.add(p)
    return paths[:8]


def _resolve_session_path(session_id: str) -> str:
    if not session_id or not _SESSIONS_DIR:
        return ""
    candidates = [
        _SESSIONS_DIR / f"session_{session_id}.json",
        _SESSIONS_DIR / f"{session_id}.json",
        _SESSIONS_DIR / f"{session_id}.jsonl",
    ]
    for c in candidates:
        try:
            if c.exists():
                return str(c)
        except Exception:
            pass
    return ""


def _resume_hint(response: str) -> str:
    if not response:
        return ""
    sent = re.split(r"[.!?]\s", response.strip(), 1)[0]
    return sent[:120].strip()


# v3.1: _trunc preserves multi-line.
def _trunc(s: str, n: int) -> str:
    if not s:
        return ""
    s = s.strip()
    if len(s) <= n:
        return s
    return s[: n - 1].rstrip() + "…"


# v3.1: substantive gate.
_SUBSTANTIVE_MIN_CHARS = 300


def _is_substantive(user_msg: str, assistant_resp: str) -> bool:
    """A turn is substantive if user+assistant combined content meets the threshold."""
    return len((user_msg or "").strip()) + len((assistant_resp or "").strip()) >= _SUBSTANTIVE_MIN_CHARS


# v3.1: recent-exchanges persistence.
_TAIL_EXCHANGES = 4  # how many recent substantive exchanges to persist in handoff
_TAIL_CHARS_PER_MSG = 2000  # per-message cap inside tail (user or assistant)

# Legacy tier constants (used ONLY if reading a v3.0 handoff without Recent Exchanges block).
_BUDGET_CHARS = 6000
_TIER1_CHARS = 300
_TIER2_CHARS = 150
_TIER3_CHARS = 80
_TIER3_STRIDE = 3
_STALE_HOURS = 24

# ALWAYS-CONTEXT layer (v2.1).
_ALWAYS_CONTEXT_BUDGET = 1500


def _load_always_context() -> str:
    if not _ALWAYS_CONTEXT_PATH:
        return ""
    try:
        if not _ALWAYS_CONTEXT_PATH.exists():
            return ""
        text = _ALWAYS_CONTEXT_PATH.read_text(encoding="utf-8", errors="replace").strip()
        if not text:
            return ""
        if len(text) > _ALWAYS_CONTEXT_BUDGET:
            text = text[:_ALWAYS_CONTEXT_BUDGET].rstrip() + "\n[truncated]"
        return text
    except Exception as exc:
        logger.warning("dialogue-handoff: could not read ALWAYS-CONTEXT: %s", exc)
        return ""


def _first_line(s: str, cap: int = 300) -> str:
    if not s:
        return ""
    return s.strip().splitlines()[0][:cap]


# --- parsers -----------------------------------------------------------


def _parse_recent_exchanges(text: str) -> List[Dict[str, str]]:
    """Parse `## Recent Exchanges` block. Returns newest-first list of
    {header, user, assistant}.
    """
    lines = text.splitlines()
    in_section = False
    current: Optional[Dict[str, str]] = None
    exchanges: List[Dict[str, str]] = []
    role: Optional[str] = None
    for ln in lines:
        if ln.startswith("## Recent Exchanges"):
            in_section = True
            continue
        if not in_section:
            continue
        # Next top-level section ends our block
        if ln.startswith("## ") and not ln.startswith("## Recent Exchanges"):
            break
        if ln.startswith("### "):
            if current is not None:
                exchanges.append(current)
            current = {"header": ln[4:].strip(), "user": "", "assistant": ""}
            role = None
            continue
        if current is None:
            continue
        if ln.startswith("USER:"):
            role = "user"
            current["user"] = ln[5:].lstrip()
            continue
        if ln.startswith("HERMES:") or ln.startswith("ASSISTANT:"):
            role = "assistant"
            _, _, rest = ln.partition(":")
            current["assistant"] = rest.lstrip()
            continue
        if role == "user":
            current["user"] += "\n" + ln
        elif role == "assistant":
            current["assistant"] += "\n" + ln
    if current is not None:
        exchanges.append(current)
    for ex in exchanges:
        ex["user"] = ex["user"].strip()
        ex["assistant"] = ex["assistant"].strip()
    return exchanges


def _format_recent_exchanges_block(exchanges: List[Dict[str, str]]) -> str:
    """Serialize tail list into the markdown block.

    exchanges: newest-first. Output numbering: newest = N, oldest = 1.
    """
    lines = [
        "## Recent Exchanges",
        "<!-- Verbatim multi-line tail of the last substantive turns. "
        "Read directly by pre_llm_call; do not require reopening session JSON. -->",
        "",
    ]
    n = len(exchanges)
    for i, ex in enumerate(exchanges):
        idx = n - i  # exchanges[0] is newest → N
        header = ex.get("header") or f"Exchange {idx}"
        u = _trunc(ex.get("user", ""), _TAIL_CHARS_PER_MSG)
        a = _trunc(ex.get("assistant", ""), _TAIL_CHARS_PER_MSG)
        lines.append(f"### {header}")
        if u:
            lines.append(f"USER: {u}")
        if a:
            lines.append(f"HERMES: {a}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _read_existing_tail() -> List[Dict[str, str]]:
    if not _HANDOFF_PATH or not _HANDOFF_PATH.exists():
        return []
    try:
        text = _HANDOFF_PATH.read_text(encoding="utf-8", errors="replace")
        return _parse_recent_exchanges(text)
    except Exception as exc:
        logger.warning("dialogue-handoff: could not parse existing tail: %s", exc)
        return []


def _on_post_llm_call(
    session_id: str = "",
    user_message: str = "",
    assistant_response: str = "",
    conversation_history: List[Dict[str, Any]] = None,
    model: str = "",
    platform: str = "",
    **_ignored: Any,
) -> None:
    if not _CONFIG_OK:
        return
    try:
        um = (user_message or "").strip()
        ar = (assistant_response or "").strip()
        # Basic sanity: skip command-only turns (/reset, /model, etc.) and very empty
        if not um or um.startswith("/") or len(um) < 3:
            return

        working_set = _extract_working_set(conversation_history or [])
        session_path = _resolve_session_path(session_id)
        hint = _resume_hint(ar)
        now_iso = datetime.datetime.now().isoformat(timespec="seconds")

        # v3.1: decide if this turn is substantive.
        sustantivo = _is_substantive(um, ar)

        # Build/update the Recent Exchanges tail.
        tail = _read_existing_tail()
        if sustantivo:
            new_entry = {
                "header": f"Exchange @ {now_iso} ({platform or 'cli'}, session {session_id or '?'})",
                "user": um,
                "assistant": ar,
            }
            tail = [new_entry] + tail
            tail = tail[:_TAIL_EXCHANGES]
        # else: keep tail unchanged (trivial echoes don't overwrite a good tail)

        # Compose the full handoff doc.
        lines = [
            "# DIALOGUE-HANDOFF",
            "",
            "## Last Turn",
            f"- platform: {platform or 'cli'}",
            f"- session_id: {session_id}",
            f"- timestamp: {now_iso}",
            f"- model: {model}",
            f"- substantive: {str(sustantivo).lower()}",
            "",
            "## Session Path",
            f"- {session_path or 'none'}",
            "",
            "## Last User Message (headline)",
            f"- {_first_line(um)}",
            "",
            "## Last Assistant Response (headline)",
            f"- {_first_line(ar)}",
            "",
            "## Last Working Set",
        ]
        if working_set:
            for p in working_set:
                lines.append(f"- {p}")
        else:
            lines.append("- none")
        lines += ["", "## Resume Hint", f"- {hint}", ""]

        # Append the Recent Exchanges tail block (or explicit empty marker).
        if tail:
            lines.append(_format_recent_exchanges_block(tail))
        else:
            lines += [
                "## Recent Exchanges",
                "<!-- empty: no substantive turn recorded yet -->",
                "",
            ]

        _HANDOFF_PATH.parent.mkdir(parents=True, exist_ok=True)
        _HANDOFF_PATH.write_text("\n".join(lines), encoding="utf-8")
        try:
            os.chmod(_HANDOFF_PATH, 0o600)
        except Exception:
            pass
    except Exception as e:
        logger.warning("dialogue-handoff post_llm_call failed: %s", e)


# --- read side (pre_llm_call) ----------------------------------------


def _parse_handoff_md(text: str) -> Dict[str, Any]:
    sections: Dict[str, List[str]] = {}
    current = None
    buf: List[str] = []
    for ln in text.splitlines():
        if ln.startswith("## ") and not ln.startswith("## Recent Exchanges"):
            if current:
                sections[current] = buf
            current = ln[3:].strip()
            buf = []
        elif current is not None and not ln.startswith("## Recent Exchanges"):
            buf.append(ln)
        elif ln.startswith("## Recent Exchanges"):
            # stop parsing "header" sections at the tail block
            if current:
                sections[current] = buf
            current = None
    if current:
        sections[current] = buf

    def parse_bullets(block):
        out = []
        for raw in block:
            s = raw.strip()
            if s.startswith("- "):
                out.append(s[2:].strip())
        return out

    out: Dict[str, Any] = {}
    for bullet in parse_bullets(sections.get("Last Turn", [])):
        if ":" in bullet:
            k, _, v = bullet.partition(":")
            out[k.strip().lower().replace(" ", "_")] = v.strip()

    sp = parse_bullets(sections.get("Session Path", []))
    out["session_path"] = sp[0] if sp and sp[0] != "none" else ""

    lum = parse_bullets(sections.get("Last User Message (headline)", []) or sections.get("Last User Message", []))
    out["last_user_message"] = lum[0] if lum else ""

    lar = parse_bullets(sections.get("Last Assistant Response (headline)", []) or sections.get("Last Assistant Response", []))
    out["last_assistant_response"] = lar[0] if lar else ""

    out["last_working_set"] = [p for p in parse_bullets(sections.get("Last Working Set", [])) if p and p != "none"]
    rh = parse_bullets(sections.get("Resume Hint", []))
    out["resume_hint"] = rh[0] if rh else ""
    return out


def _is_stale(handoff: Dict[str, Any], hours: int = _STALE_HOURS) -> bool:
    ts = handoff.get("timestamp")
    if not ts:
        return True
    try:
        dt = datetime.datetime.fromisoformat(ts)
        return (datetime.datetime.now() - dt) > datetime.timedelta(hours=hours)
    except Exception:
        return True


def _load_session_messages(session_path: str) -> List[Dict[str, Any]]:
    if not session_path:
        return []
    p = Path(session_path)
    if not p.exists():
        return []
    try:
        raw = p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    try:
        decoder = json.JSONDecoder()
        data, _end = decoder.raw_decode(raw.lstrip())
        if isinstance(data, dict):
            return data.get("messages", []) or []
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass
    out = []
    for ln in raw.splitlines():
        ln = ln.strip()
        if not ln or not ln.startswith("{"):
            continue
        try:
            obj = json.loads(ln)
            if isinstance(obj, dict):
                out.append(obj)
        except Exception:
            continue
    return out


def _msg_text(msg: Dict[str, Any]) -> str:
    c = msg.get("content", "")
    if isinstance(c, str):
        return c.strip()
    if isinstance(c, list):
        parts = []
        for it in c:
            if isinstance(it, dict):
                t = it.get("text") or it.get("content") or ""
                if t:
                    parts.append(str(t))
            elif isinstance(it, str):
                parts.append(it)
        return " ".join(parts).strip()
    return ""


def _group_exchanges(messages: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    exchanges = []
    current_user = None
    assistant_parts: List[str] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "user":
            if current_user is not None:
                exchanges.append({
                    "user": current_user,
                    "assistant": "\n".join(assistant_parts).strip(),
                })
            current_user = _msg_text(msg)
            assistant_parts = []
        elif role == "assistant":
            t = _msg_text(msg)
            if t:
                assistant_parts.append(t)
    if current_user is not None:
        exchanges.append({
            "user": current_user,
            "assistant": "\n".join(assistant_parts).strip(),
        })
    exchanges.reverse()
    return exchanges


def _build_injection_from_tail(handoff: Dict[str, Any], tail: List[Dict[str, str]]) -> str:
    """v3.1 injection: take the pre-packed `## Recent Exchanges` tail directly.
    Multi-line preserved up to _TAIL_CHARS_PER_MSG per message.
    """
    lines: List[str] = []
    lines.append("<previous_session_context>")
    lines.append("<!-- auto-injected continuity from previous session; absorb naturally, do not quote metadata -->")
    lines.append("")
    ts = handoff.get("timestamp", "?")
    platform = handoff.get("platform", "?")
    lines.append(f"Previous session: {ts} ({platform})")
    ws = handoff.get("last_working_set") or []
    if ws:
        lines.append(f"Files touched: {', '.join(ws[:5])}")
    if handoff.get("resume_hint"):
        lines.append(f"Resume hint: {handoff['resume_hint']}")
    lines.append("")
    lines.append("### Recent exchanges (newest at the bottom, multi-line preserved):")
    lines.append("")
    # Emit oldest→newest (reverse of newest-first tail) so newest sits at the end
    for ex in reversed(tail):
        header = ex.get("header", "")
        u = _trunc(ex.get("user", ""), _TAIL_CHARS_PER_MSG)
        a = _trunc(ex.get("assistant", ""), _TAIL_CHARS_PER_MSG)
        if header:
            lines.append(f"#### {header}")
        if u:
            lines.append(f"USER: {u}")
        if a:
            lines.append(f"HERMES: {a}")
        lines.append("")
    lines.append("</previous_session_context>")
    return "\n".join(lines)


def _build_injection_legacy_tiered(handoff: Dict[str, Any], exchanges: List[Dict[str, str]], budget: int = _BUDGET_CHARS) -> str:
    """Legacy v3.0 injection — reads session JSON and emits 3-tier compressed block.

    Used only if the handoff has no Recent Exchanges block yet (v3.0 → v3.1 transition)
    OR as a fallback if the tail is empty.
    """
    lines: List[str] = []
    lines.append("<previous_session_context>")
    lines.append("<!-- auto-injected continuity (legacy tiered mode — handoff lacks Recent Exchanges) -->")
    lines.append("")
    ts = handoff.get("timestamp", "?")
    platform = handoff.get("platform", "?")
    lines.append(f"Previous session: {ts} ({platform})")
    ws = handoff.get("last_working_set", [])
    if ws:
        lines.append(f"Files touched: {', '.join(ws[:5])}")
    if handoff.get("resume_hint"):
        lines.append(f"Resume hint: {handoff['resume_hint']}")
    lines.append("")

    t1 = exchanges[:2]
    t2 = exchanges[2:6]
    t3 = exchanges[6:20]
    t3_strided = [ex for i, ex in enumerate(t3) if i % _TIER3_STRIDE == 0]

    if t3_strided:
        lines.append("### Earlier arc (sparse 1-of-3 sampling, ~80 chars):")
        for ex in reversed(t3_strided):
            u = _trunc(ex.get("user", ""), _TIER3_CHARS)
            if u:
                lines.append(f"- U: {u}")
            a = _trunc(ex.get("assistant", ""), _TIER3_CHARS)
            if a:
                lines.append(f"  H: {a}")
        lines.append("")
    if t2:
        lines.append("### Middle exchanges (headlines):")
        for ex in reversed(t2):
            u = _trunc(ex.get("user", ""), _TIER2_CHARS)
            if u:
                lines.append(f"- U: {u}")
            a = _trunc(ex.get("assistant", ""), _TIER2_CHARS)
            if a:
                lines.append(f"  H: {a}")
        lines.append("")
    if t1:
        lines.append("### Most recent exchanges:")
        for ex in reversed(t1):
            u = _trunc(ex.get("user", ""), _TIER1_CHARS)
            a = _trunc(ex.get("assistant", ""), _TIER1_CHARS)
            if u:
                lines.append(f"USER: {u}")
            if a:
                lines.append(f"HERMES: {a}")
            lines.append("")

    lines.append("</previous_session_context>")
    out = "\n".join(lines)
    if len(out) > budget:
        cutoff = budget - len("\n[truncated]\n</previous_session_context>")
        out = out[:cutoff].rstrip() + "\n[truncated]\n</previous_session_context>"
    return out


def _on_pre_llm_call(
    session_id: str = "",
    user_message: str = "",
    conversation_history: List[Dict[str, Any]] = None,
    is_first_turn: bool = False,
    **_ignored: Any,
) -> Any:
    if not _CONFIG_OK:
        return None
    try:
        if not is_first_turn:
            return None
        um = (user_message or "").strip()
        if um.startswith("/"):
            return None

        # Layer 1 (v2.1): always-context
        always_block = _load_always_context()

        # Layer 2: dialogue handoff (v3.1 tail-first, v3.0 JSON fallback)
        handoff_block = ""
        try:
            if _HANDOFF_PATH and _HANDOFF_PATH.exists():
                text = _HANDOFF_PATH.read_text(encoding="utf-8", errors="replace")
                handoff = _parse_handoff_md(text)
                if handoff.get("last_user_message") and handoff["last_user_message"] != "none" and not _is_stale(handoff):
                    # Try v3.1 path: parsed tail from handoff itself (no JSON)
                    tail = _parse_recent_exchanges(text)
                    if tail:
                        handoff_block = _build_injection_from_tail(handoff, tail)
                    else:
                        # Legacy fallback: reopen session JSON + tiered build
                        session_path = handoff.get("session_path", "")
                        if session_path:
                            messages = _load_session_messages(session_path)
                            exchanges = _group_exchanges(messages)
                            if exchanges:
                                handoff_block = _build_injection_legacy_tiered(handoff, exchanges)
        except Exception as exc:
            logger.warning("dialogue-handoff: handoff build failed: %s", exc)

        if not always_block and not handoff_block:
            return None

        parts: List[str] = []
        if always_block:
            parts.append(
                "<always_context>\n"
                "<!-- stable capabilities + rules; absorb as working knowledge -->\n"
                + always_block
                + "\n</always_context>"
            )
        if handoff_block:
            parts.append(handoff_block)

        return {"context": "\n\n".join(parts)}
    except Exception as exc:
        logger.warning("dialogue-handoff pre_llm_call failed: %s", exc)
        return None


def register(ctx):
    ctx.register_hook("post_llm_call", _on_post_llm_call)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
