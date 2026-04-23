#!/usr/bin/env python3
import argparse
import json
import re
from pathlib import Path

import memoryctl


import os as _os

def _resolve(keys, fallback):
    for k in keys:
        v = _os.environ.get(k)
        if v:
            return Path(v).expanduser()
    return Path(fallback).expanduser()

AGENT_MEMORY_BASE = _resolve(["HMK_AGENT_MEMORY_BASE", "HMK_BASE_DIR", "AGENT_MEMORY_BASE"], "/home/onairam/agent-memory")
BASE_DIR = AGENT_MEMORY_BASE
STATE_DIR = BASE_DIR / "state"
ACTIVE_CONTEXT_PATH = STATE_DIR / "ACTIVE-CONTEXT.md"
NOW_PATH = STATE_DIR / "NOW.md"
DIALOGUE_HANDOFF_PATH = _resolve(["HMK_DIALOGUE_HANDOFF_PATH"], str(STATE_DIR / "DIALOGUE-HANDOFF.md"))
HERMES_HOME = _resolve(["HMK_HERMES_HOME", "HERMES_HOME"], "/home/onairam/agents/hermes-prime/hermes-home")
SOUL_PATH = HERMES_HOME / "SOUL.md"
USER_PATH = HERMES_HOME / "memories" / "USER.md"
MEMORY_PATH = HERMES_HOME / "memories" / "MEMORY.md"


def read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace").strip()


def normalize_bullet(value: str) -> str:
    value = value.strip()
    value = re.sub(r"^\-\s*", "", value)
    return value.strip()


def split_bullets(block: str):
    out = []
    for raw in block.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        if raw.startswith("- "):
            out.append(normalize_bullet(raw))
        else:
            out.append(raw)
    return out


def compact_phrase(text: str, max_words: int = 10) -> str:
    words = text.strip().split()
    if not words:
        return ""
    return " ".join(words[:max_words])


def extract_mem_ids(block: str):
    ids = []
    for item in split_bullets(block):
        for match in re.findall(r"\[mem:(\d+)\]", item):
            try:
                ids.append(int(match))
            except ValueError:
                continue
    out = []
    seen = set()
    for item in ids:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def parse_sections(text: str):
    sections = {}
    current = None
    buf = []
    for line in text.splitlines():
        if line.startswith("## "):
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            current = line[3:].strip()
            buf = []
            continue
        buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip()
    return sections


def load_active_context():
    text = read_text(ACTIVE_CONTEXT_PATH)
    if not text:
        return {}
    return parse_sections(text)


def render_bullets(items):
    if not items:
        return "- none"
    return "\n".join(f"- {item}" for item in items)


def update_active_context(args):
    sections = load_active_context()

    def set_section(name, values):
        if values is None:
            return
        sections[name] = render_bullets(values)

    set_section("Active Goal", args.goal)
    set_section("Current Focus", args.focus)
    set_section("Open Tasks", args.tasks)
    set_section("Blockers", args.blockers)
    set_section("Next Steps", args.next_steps)
    set_section("Last Topic", args.last_topic)
    set_section("Last User Intent", args.last_user_intent)
    set_section("Last Working Set", args.last_working_set)
    set_section("Resume Hint", args.resume_hint)
    set_section("Relevant Memory", args.memories)
    set_section("Notes", args.notes)

    status_lines = []
    if args.state:
        status_lines.append(f"- state: {args.state}")
    if args.confidence:
        status_lines.append(f"- confidence: {args.confidence}")
    if args.last_updated:
        status_lines.append(f"- last_updated: {args.last_updated}")
    if status_lines:
        sections["Status"] = "\n".join(status_lines)

    ordered = [
        "Status",
        "Active Goal",
        "Current Focus",
        "Open Tasks",
        "Blockers",
        "Next Steps",
        "Last Topic",
        "Last User Intent",
        "Last Working Set",
        "Resume Hint",
        "Relevant Memory",
        "Notes",
    ]
    lines = ["# ACTIVE-CONTEXT", ""]
    for name in ordered:
        value = sections.get(name)
        if value is None:
            continue
        lines.append(f"## {name}")
        lines.append("")
        lines.append(value.strip() if value.strip() else "- none")
        lines.append("")
    ACTIVE_CONTEXT_PATH.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return {"ok": True, "path": str(ACTIVE_CONTEXT_PATH)}


def summarize_markdown(text: str, max_lines: int):
    bullets = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            bullets.append(f"- heading: {line.lstrip('#').strip()}")
        elif line.startswith("- "):
            bullets.append(line)
        else:
            bullets.append(f"- {line[:140]}")
        if len(bullets) >= max_lines:
            break
    return bullets or ["- none"]


def summarize_memory_row(row, max_lines: int):
    text = (row.get("spr") or row.get("raw") or "").strip()
    bullets = []
    if row.get("title"):
        bullets.append(f"- title: {row['title']}")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            bullets.append(f"- heading: {line.lstrip('#').strip()}")
        elif line.startswith("- "):
            bullets.append(line)
        else:
            bullets.append(f"- {line[:140]}")
        if len(bullets) >= max_lines:
            break
    return bullets or ["- none"]


def load_relevant_memories(max_items: int, max_lines: int):
    sections = load_active_context()
    mem_ids = extract_mem_ids(sections.get("Relevant Memory", ""))
    items = []
    for chapter_id in mem_ids[:max_items]:
        try:
            row = memoryctl.expand(chapter_id)
        except (Exception, SystemExit) as exc:
            items.append(
                {
                    "id": chapter_id,
                    "citation": f"[mem:{chapter_id}]",
                    "error": str(exc),
                }
            )
            continue
        items.append(
            {
                "id": chapter_id,
                "citation": f"[mem:{chapter_id}]",
                "title": row.get("title", ""),
                "shelf": row.get("shelf", ""),
                "book_title": row.get("book_title", ""),
                "source_path": row.get("source_path", ""),
                "summary": summarize_memory_row(row, max_lines),
            }
        )
    return items


def build_query():
    sections = load_active_context()
    query_parts = ["continuidad", "estado actual", "foco activo"]
    for key in ["Active Goal", "Current Focus", "Next Steps", "Last Topic", "Last User Intent"]:
        if key in sections:
            compacted = [compact_phrase(item) for item in split_bullets(sections[key])[:2]]
            query_parts.extend([item for item in compacted if item])
    cleaned = []
    seen = set()
    for part in query_parts:
        part = part.strip()
        if not part:
            continue
        lowered = part.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        cleaned.append(part)
    return " ".join(cleaned)


def load_episode_handoff(max_lines: int):
    sections = load_active_context()
    out = {}
    mapping = [
        ("last_topic", "Last Topic"),
        ("last_user_intent", "Last User Intent"),
        ("last_working_set", "Last Working Set"),
        ("resume_hint", "Resume Hint"),
    ]
    for out_key, section_name in mapping:
        raw = sections.get(section_name, "")
        items = split_bullets(raw)[:max_lines]
        out[out_key] = items or []
    return out


def load_dialogue_handoff(max_lines: int):
    if not DIALOGUE_HANDOFF_PATH.exists():
        return {}
    text = read_text(DIALOGUE_HANDOFF_PATH)
    sections = parse_sections(text)
    mapping = [
        ("last_turn", "Last Turn"),
        ("session_path", "Session Path"),
        ("last_user_message", "Last User Message"),
        ("last_assistant_response", "Last Assistant Response"),
        ("last_working_set", "Last Working Set"),
        ("resume_hint", "Resume Hint"),
    ]
    out = {}
    for key, section in mapping:
        raw = sections.get(section, "")
        out[key] = split_bullets(raw)[:max_lines]
    return out


def rehydrate(args):
    identity = {
        "soul": summarize_markdown(read_text(SOUL_PATH), args.max_identity_lines),
        "user": summarize_markdown(read_text(USER_PATH), args.max_identity_lines),
        "memory": summarize_markdown(read_text(MEMORY_PATH), args.max_identity_lines),
    }
    state = {
        "now": summarize_markdown(read_text(NOW_PATH), args.max_state_lines),
        "active_context": summarize_markdown(read_text(ACTIVE_CONTEXT_PATH), args.max_state_lines),
    }
    episode_handoff = load_episode_handoff(args.max_episode_lines)
    dialogue_handoff = load_dialogue_handoff(args.max_dialogue_lines)
    exact_memories = load_relevant_memories(args.relevant_limit, args.max_memory_lines)
    query = build_query()
    retrieval = None
    valid_exact = [item for item in exact_memories if not item.get("error")]
    should_retrieve = (not args.skip_retrieval) and (args.always_retrieve or not valid_exact)
    if should_retrieve:
        retrieval = memoryctl.hybrid_pack(
            query,
            budget_tokens=args.budget,
            limit=args.limit,
            threshold=args.threshold,
        )
    result = {
        "mode": "minimal-rehydration",
        "query": query,
        "identity": identity,
        "meta_context": state,
        "dialogue_handoff": dialogue_handoff,
        "state": state,
        "episode_handoff": episode_handoff,
        "exact_memories": exact_memories,
        "retrieval": retrieval,
    }
    return result


def main():
    parser = argparse.ArgumentParser(description="Control tactico de continuidad para Hermes")
    sub = parser.add_subparsers(dest="command", required=True)

    show = sub.add_parser("show")
    show.add_argument("--json", action="store_true")

    upd = sub.add_parser("update")
    upd.add_argument("--state")
    upd.add_argument("--confidence")
    upd.add_argument("--last-updated")
    upd.add_argument("--goal", nargs="*")
    upd.add_argument("--focus", nargs="*")
    upd.add_argument("--tasks", nargs="*")
    upd.add_argument("--blockers", nargs="*")
    upd.add_argument("--next-steps", nargs="*")
    upd.add_argument("--last-topic", nargs="*")
    upd.add_argument("--last-user-intent", nargs="*")
    upd.add_argument("--last-working-set", nargs="*")
    upd.add_argument("--resume-hint", nargs="*")
    upd.add_argument("--memories", nargs="*")
    upd.add_argument("--notes", nargs="*")

    reh = sub.add_parser("rehydrate")
    reh.add_argument("--budget", type=int, default=700)
    reh.add_argument("--limit", type=int, default=2)
    reh.add_argument("--threshold", type=float, default=0.52)
    reh.add_argument("--max-identity-lines", type=int, default=4)
    reh.add_argument("--max-state-lines", type=int, default=6)
    reh.add_argument("--max-episode-lines", type=int, default=4)
    reh.add_argument("--max-dialogue-lines", type=int, default=6)
    reh.add_argument("--max-memory-lines", type=int, default=5)
    reh.add_argument("--relevant-limit", type=int, default=2)
    reh.add_argument("--skip-retrieval", action="store_true")
    reh.add_argument("--always-retrieve", action="store_true")

    args = parser.parse_args()

    if args.command == "show":
        if args.json:
            print(json.dumps(load_active_context(), indent=2, ensure_ascii=False))
        else:
            print(read_text(ACTIVE_CONTEXT_PATH))
        return

    if args.command == "update":
        print(json.dumps(update_active_context(args), indent=2, ensure_ascii=False))
        return

    if args.command == "rehydrate":
        print(json.dumps(rehydrate(args), indent=2, ensure_ascii=False))
        return


if __name__ == "__main__":
    main()
