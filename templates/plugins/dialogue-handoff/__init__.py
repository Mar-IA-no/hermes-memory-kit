"""dialogue-handoff plugin v3.0 — conversational continuity for Hermes.

v2.1 → v3.0 (BREAKING):
  - ALL hardcoded fallbacks to /home/onairam/... REMOVED. The plugin now
    requires the agent's .env to be correctly sourced into the environment
    with at least HMK_AGENT_MEMORY_BASE (or HMK_DIALOGUE_HANDOFF_PATH /
    HMK_ALWAYS_CONTEXT_PATH directly) and HMK_HERMES_HOME (or HMK_SESSIONS_DIR
    directly).
  - If required paths cannot be resolved from env, the plugin logs an error
    once and its hooks become no-ops. This prevents cross-agent contamination
    in multi-agent deployments where a misconfigured .env would otherwise
    cause one agent to write its handoff into another's tree.
  - Legacy env vars (AGENT_MEMORY_BASE, HERMES_HOME) still work in the
    cascade — they're read from the Hermes upstream environment.

v2.0 → v2.1:
  - ALWAYS-CONTEXT layer on is_first_turn.

v1.0 → v2.0:
  - pre_llm_call hook with tiered-compressed continuity block.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# --- paths ------------------------------------------------------------
#
# v3.0: NO hardcoded fallbacks. Either the env resolves, or the plugin
# disables itself. Cascade order (most specific first):
#
#   agent-memory root:
#     HMK_AGENT_MEMORY_BASE > AGENT_MEMORY_BASE > HMK_BASE_DIR
#   hermes-home root:
#     HMK_HERMES_HOME > HERMES_HOME
#
# Individual file paths can be overridden directly:
#   HMK_DIALOGUE_HANDOFF_PATH, HMK_ALWAYS_CONTEXT_PATH, HMK_SESSIONS_DIR.


def _env_path(keys: list) -> Optional[Path]:
    for k in keys:
        v = os.environ.get(k)
        if v:
            return Path(v).expanduser()
    return None


_AGENT_MEMORY_BASE = _env_path(["HMK_AGENT_MEMORY_BASE", "AGENT_MEMORY_BASE", "HMK_BASE_DIR"])
_HERMES_HOME = _env_path(["HMK_HERMES_HOME", "HERMES_HOME"])

_HANDOFF_PATH: Optional[Path] = _env_path(["HMK_DIALOGUE_HANDOFF_PATH"]) or (
    (_AGENT_MEMORY_BASE / "state" / "DIALOGUE-HANDOFF.md") if _AGENT_MEMORY_BASE else None
)
_ALWAYS_CONTEXT_PATH: Optional[Path] = _env_path(["HMK_ALWAYS_CONTEXT_PATH"]) or (
    (_AGENT_MEMORY_BASE / "state" / "ALWAYS-CONTEXT.md") if _AGENT_MEMORY_BASE else None
)
_SESSIONS_DIR: Optional[Path] = _env_path(["HMK_SESSIONS_DIR"]) or (
    (_HERMES_HOME / "sessions") if _HERMES_HOME else None
)

_CONFIG_OK = bool(_HANDOFF_PATH and _ALWAYS_CONTEXT_PATH and _SESSIONS_DIR)

if not _CONFIG_OK:
    missing = []
    if not _HANDOFF_PATH:
        missing.append("HMK_DIALOGUE_HANDOFF_PATH or HMK_AGENT_MEMORY_BASE")
    if not _ALWAYS_CONTEXT_PATH:
        missing.append("HMK_ALWAYS_CONTEXT_PATH or HMK_AGENT_MEMORY_BASE")
    if not _SESSIONS_DIR:
        missing.append("HMK_SESSIONS_DIR or HMK_HERMES_HOME")
    logger.error(
        "dialogue-handoff v3.0: plugin DISABLED — missing env: %s. "
        "The plugin will not read or write any handoff/always-context file. "
        "Set the above vars in the agent's .env (see hermes-memory-kit v3.0 docs).",
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


def _first_line(s: str, cap: int = 300) -> str:
    if not s:
        return ""
    return s.strip().splitlines()[0][:cap]


def _on_post_llm_call(
    session_id: str = "",
    user_message: str = "",
    assistant_response: str = "",
    conversation_history: List[Dict[str, Any]] = None,
    model: str = "",
    platform: str = "",
    **_ignored: Any,
) -> None:
    if not _CONFIG_OK or _HANDOFF_PATH is None:
        return
    try:
        um = (user_message or "").strip()
        if not um or um.startswith("/") or len(um) < 3:
            return

        working_set = _extract_working_set(conversation_history or [])
        session_path = _resolve_session_path(session_id)
        hint = _resume_hint(assistant_response)
        now = datetime.datetime.now().isoformat(timespec="seconds")

        lines = [
            "# DIALOGUE-HANDOFF",
            "",
            "## Last Turn",
            f"- platform: {platform or 'cli'}",
            f"- session_id: {session_id}",
            f"- timestamp: {now}",
            f"- model: {model}",
            "",
            "## Session Path",
            f"- {session_path or 'none'}",
            "",
            "## Last User Message",
            f"- {_first_line(um)}",
            "",
            "## Last Assistant Response",
            f"- {_first_line(assistant_response)}",
            "",
            "## Last Working Set",
        ]
        if working_set:
            for p in working_set:
                lines.append(f"- {p}")
        else:
            lines.append("- none")
        lines += ["", "## Resume Hint", f"- {hint}", ""]

        _HANDOFF_PATH.parent.mkdir(parents=True, exist_ok=True)
        _HANDOFF_PATH.write_text("\n".join(lines), encoding="utf-8")
        try:
            os.chmod(_HANDOFF_PATH, 0o600)
        except Exception:
            pass
    except Exception as e:
        logger.warning("dialogue-handoff post_llm_call failed: %s", e)


# --- read side (pre_llm_call) ----------------------------------------

_STALE_HOURS = 24
_BUDGET_CHARS = 6000
_TIER1_CHARS = 300
_TIER2_CHARS = 150
_TIER3_CHARS = 80
_TIER3_STRIDE = 3

_ALWAYS_CONTEXT_BUDGET = 1500


def _load_always_context() -> str:
    if not _CONFIG_OK or _ALWAYS_CONTEXT_PATH is None:
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


def _parse_bullets(lines: List[str]) -> List[str]:
    out = []
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if s.startswith("- "):
            s = s[2:].strip()
        if s:
            out.append(s)
    return out


def _parse_handoff_md(text: str) -> Dict[str, Any]:
    sections: Dict[str, List[str]] = {}
    current = None
    buf: List[str] = []
    for ln in text.splitlines():
        if ln.startswith("## "):
            if current:
                sections[current] = buf
            current = ln[3:].strip()
            buf = []
        elif current is not None:
            buf.append(ln)
    if current:
        sections[current] = buf

    out: Dict[str, Any] = {}
    for bullet in _parse_bullets(sections.get("Last Turn", [])):
        if ":" in bullet:
            k, _, v = bullet.partition(":")
            out[k.strip().lower().replace(" ", "_")] = v.strip()

    sp = _parse_bullets(sections.get("Session Path", []))
    out["session_path"] = sp[0] if sp and sp[0] != "none" else ""

    lum = _parse_bullets(sections.get("Last User Message", []))
    out["last_user_message"] = lum[0] if lum else ""

    lar = _parse_bullets(sections.get("Last Assistant Response", []))
    out["last_assistant_response"] = lar[0] if lar else ""

    out["last_working_set"] = [
        p for p in _parse_bullets(sections.get("Last Working Set", []))
        if p and p != "none"
    ]

    rh = _parse_bullets(sections.get("Resume Hint", []))
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


def _trunc(s: str, n: int) -> str:
    if not s:
        return ""
    s = s.strip().splitlines()[0] if "\n" in s else s.strip()
    if len(s) <= n:
        return s
    return s[:n - 1].rstrip() + "…"


def _build_injection(handoff: Dict[str, Any], exchanges: List[Dict[str, str]], budget: int = _BUDGET_CHARS) -> str:
    lines: List[str] = []
    lines.append("<previous_session_context>")
    lines.append("<!-- auto-injected continuity from previous session; absorb naturally, do not quote metadata -->")
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

        always_block = _load_always_context()

        handoff_block = ""
        try:
            if _HANDOFF_PATH is not None and _HANDOFF_PATH.exists():
                text = _HANDOFF_PATH.read_text(encoding="utf-8", errors="replace")
                handoff = _parse_handoff_md(text)
                if (handoff.get("last_user_message")
                        and handoff.get("last_user_message") != "none"
                        and not _is_stale(handoff)):
                    session_path = handoff.get("session_path", "")
                    exchanges: List[Dict[str, str]] = []
                    if session_path:
                        messages = _load_session_messages(session_path)
                        exchanges = _group_exchanges(messages)
                    handoff_block = _build_injection(handoff, exchanges)
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
