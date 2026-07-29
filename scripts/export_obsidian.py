#!/usr/bin/env python3
"""Proyecta nodos de library.db a un vault de Obsidian con staging atomico.

v3.9.0 — atomic publish + idempotent manifest:
- Build into <vault>/.staging/<run_id>/ (temporary, invisible to Obsidian)
- Write manifest.json with per-note content sha256 hashes
- Idempotency: skip rewriting notes whose hash matches the manifest;
  report written/unchanged/removed counts
- Atomic swap: os.rename the staging dir into <vault>/live/ after build
- Prune notes for chapters that no longer exist
- Takes the maintenance flock before building
- Optional --check mode: exit non-zero if the published projection differs
  from a fresh build (drift detector for CI/cron)
"""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent

WORKSPACE_ROOT = Path(os.environ.get("HMK_WORKSPACE_ROOT", str(Path.cwd())))
BASE_DIR = Path(os.environ.get(
    "HMK_BASE_DIR", "HMK_AGENT_MEMORY_BASE" in os.environ
    and os.environ["HMK_AGENT_MEMORY_BASE"]
    or str(REPO_ROOT / "agent-memory")
)).expanduser()
DB_PATH = Path(os.environ.get("HMK_DB_PATH", str(BASE_DIR / "library.db"))).expanduser()
VAULT_DIR = Path(os.environ.get("HMK_VAULT_DIR", str(REPO_ROOT / "wiki"))).expanduser()

MANIFEST_FILENAME = "projection-manifest.json"
LIVE_DIR_NAME = "live"
STAGING_DIR_NAME = ".staging"
LINK_TYPES = ["summarizes", "depends_on", "related_to", "evidence_for",
              "anchors", "references"]

DEFAULT_IDS = [int(item) for item in
               os.environ.get("HMK_EXPORT_IDS", "").split(",")
               if item.strip()]


# ---------------------------------------------------------------------------
# flock (reuse the same pattern as memoryctl)
# ---------------------------------------------------------------------------

def _lock_maintenance():
    import fcntl
    lock_path = BASE_DIR / ".maintenance.lock"
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    except OSError as exc:
        raise SystemExit(f"ERROR: cannot open maintenance lock at {lock_path}: {exc}")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise SystemExit(
            "ERROR: another maintenance process holds the lock.\n"
            f"  Lock file: {lock_path}"
        )
    return fd


def _unlock_maintenance(fd):
    import fcntl
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except Exception:
        pass
    os.close(fd)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def slugify(text):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "item"


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def fetch_chapter(con, chapter_id):
    row = con.execute(
        """
        SELECT
          c.id, c.book_id, c.title, c.spr, c.raw, c.tokens,
          c.importance, c.tags_json, c.updated_at,
          b.title AS book_title, b.source_path, b.slug AS book_slug,
          s.name AS shelf
        FROM chapters c
        JOIN books b ON b.id = c.book_id
        JOIN shelves s ON s.id = b.shelf_id
        WHERE c.id=?
        """,
        (chapter_id,),
    ).fetchone()
    if not row:
        raise SystemExit(f"chapter not found: {chapter_id}")
    data = dict(row)
    data["tags"] = json.loads(data["tags_json"] or "[]")
    data["links_out"] = [
        dict(link)
        for link in con.execute(
            """
            SELECT l.link_type, l.dst_chapter_id AS other_id,
                   c.title AS other_title
            FROM chapter_links l
            JOIN chapters c ON c.id = l.dst_chapter_id
            WHERE l.src_chapter_id=?
            ORDER BY l.link_type, l.weight DESC, c.id ASC
            """,
            (chapter_id,),
        ).fetchall()
    ]
    data["links_in"] = [
        dict(link)
        for link in con.execute(
            """
            SELECT l.link_type, l.src_chapter_id AS other_id,
                   c.title AS other_title
            FROM chapter_links l
            JOIN chapters c ON c.id = l.src_chapter_id
            WHERE l.dst_chapter_id=?
            ORDER BY l.link_type, l.weight DESC, c.id ASC
            """,
            (chapter_id,),
        ).fetchall()
    ]
    return data


# ---------------------------------------------------------------------------
# classification and rendering
# ---------------------------------------------------------------------------

def classify_folder(chapter):
    tags = set(chapter["tags"])
    title = (chapter["title"] or "").lower()
    shelf = chapter["shelf"]
    if shelf == "state":
        return "projects"
    if shelf == "plans":
        if "roadmap" in title or "architecture" in title or "plan" in title:
            return "projects"
        return "maps"
    if shelf == "library":
        if "summary" in tags or "resumen" in title:
            return "synthesis"
        if "install-guide" in tags or "guide" in title:
            return "sources"
        if "providers" in tags or "nvidia" in tags:
            return "projects"
        return "concepts"
    if shelf == "evidence":
        return "sources"
    if shelf == "episodes":
        return "log"
    return "concepts"


def pretty_title(title):
    text = title.replace("-", " ").strip()
    parts = [chunk.capitalize() if chunk.islower() else chunk
             for chunk in text.split()]
    return " ".join(parts) or title


def build_projection_map(chapters):
    mapping = {}
    used = set()
    for chapter in chapters:
        folder = classify_folder(chapter)
        base = slugify(chapter["title"])
        slug = base
        if slug in used:
            slug = f"{base}--mem-{chapter['id']}"
        used.add(slug)
        mapping[chapter["id"]] = {
            "folder": folder,
            "slug": slug,
            "path": f"{folder}/{slug}.md",
            "title": pretty_title(chapter["title"]),
        }
    return mapping


def wikilink_for(other_id, mapping, fallback_title):
    if other_id in mapping:
        return f"[[{mapping[other_id]['slug']}]]"
    return f"`mem:{other_id}` {fallback_title}"


def group_links(chapter, mapping):
    groups = defaultdict(list)
    for link in chapter["links_out"]:
        groups[link["link_type"]].append(
            wikilink_for(link["other_id"], mapping, link["other_title"]))
    return groups


def yaml_list(items, indent=0):
    pad = " " * indent
    if not items:
        return f"{pad}[]"
    lines = []
    for item in items:
        escaped = str(item).replace('"', '\\"')
        lines.append(f'{pad}- "{escaped}"')
    return "\n".join(lines)


def render_frontmatter(chapter, projection, mapping):
    link_map = group_links(chapter, mapping)
    source_paths = [chapter["source_path"]] if chapter["source_path"] else []
    safe_title = projection["title"].replace('"', '\\"')
    lines = [
        "---",
        f'title: "{safe_title}"',
        f"memory_book_id: {chapter['book_id']}",
        f"memory_chapter_id: {chapter['id']}",
        f'memory_shelf: "{chapter["shelf"]}"',
        f'memory_kind: "{projection["folder"][:-1] if projection["folder"].endswith("s") else projection["folder"]}"',
        "memory_tags:",
        yaml_list(chapter["tags"], indent=2),
        "memory_links:",
    ]
    for key in LINK_TYPES:
        lines.append(f"  {key}:")
        lines.append(yaml_list(link_map.get(key, []), indent=4))
    lines.extend([
        "source_paths:",
        yaml_list(source_paths, indent=2),
        f'last_projected_at: "{now_iso()}"',
        'projection_status: "active"',
        "---",
    ])
    return "\n".join(lines)


def extract_key_points(spr):
    points = []
    for line in spr.splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        content = line[2:].strip()
        if content and content not in points:
            points.append(content)
    return points[:6]


def render_body(chapter, projection, mapping):
    links = group_links(chapter, mapping)
    key_points = extract_key_points(chapter["spr"])
    lines = [
        f"# {projection['title']}",
        "",
        "## Summary",
        chapter["spr"],
        "",
        "## Key Points",
    ]
    for point in key_points or ["pending curation"]:
        lines.append(f"- {point}")
    if links.get("depends_on"):
        lines.extend(["", "## Depends On"])
        lines.extend([f"- {item}" for item in links["depends_on"]])
    related = []
    for key in ["related_to", "summarizes", "references", "anchors"]:
        related.extend(links.get(key, []))
    if related:
        lines.extend(["", "## Related"])
        lines.extend([f"- {item}" for item in related])
    if links.get("evidence_for") or chapter["source_path"]:
        lines.extend(["", "## Evidence"])
        if chapter["source_path"]:
            lines.append(f"- source_path: `{chapter['source_path']}`")
        lines.extend([f"- {item}" for item in links.get("evidence_for", [])])
    lines.extend([
        "",
        "## Canonical Memory",
        f"- chapter_id: `{chapter['id']}`",
        f"- shelf: `{chapter['shelf']}`",
        f"- book_title: `{chapter['book_title']}`",
        "- raw_ref: `library.db`",
    ])
    return "\n".join(lines).strip() + "\n"


# ---------------------------------------------------------------------------
# note writing with idempotency
# ---------------------------------------------------------------------------

def write_note(chapter, projection, mapping, staging_root: Path,
               manifest: Dict[int, str]):
    """Write a note to the staging dir.  Skip if content hash matches manifest."""
    content = (render_frontmatter(chapter, projection, mapping)
               + "\n\n"
               + render_body(chapter, projection, mapping))
    note_hash = content_hash(content)

    if manifest.get(chapter["id"]) == note_hash:
        return "unchanged", note_hash

    path = staging_root / projection["path"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return "written", note_hash


# ---------------------------------------------------------------------------
# index and map
# ---------------------------------------------------------------------------

def write_index(mapping, staging_root: Path):
    groups = defaultdict(list)
    for chapter_id, meta in mapping.items():
        groups[meta["folder"]].append((chapter_id, meta))
    lines = [
        "# Wiki Index",
        "",
        "Vault proyectado desde `library.db`.",
        "",
        "## Sections",
    ]
    for folder in ["maps", "projects", "concepts", "entities",
                   "sources", "synthesis", "log"]:
        lines.append(f"- {folder}/")
        for chapter_id, meta in sorted(groups.get(folder, []),
                                        key=lambda item: item[1]["title"].lower()):
            lines.append(f"  - [[{meta['slug']}]] (`mem:{chapter_id}`)")
    (staging_root / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_map_note(mapping, staging_root: Path):
    by_folder = defaultdict(list)
    for chapter_id, meta in mapping.items():
        by_folder[meta["folder"]].append((chapter_id, meta))

    def lines_for(folder, limit=4):
        rows = sorted(by_folder.get(folder, []),
                      key=lambda item: item[1]["title"].lower())
        return [f"- [[{meta['slug']}]] (`mem:{chapter_id}`)"
                for chapter_id, meta in rows[:limit]]

    lines = [
        "# Project Memory System",
        "",
        "## What This Map Is",
        "- entrada humana al sistema de memoria proyectado en Obsidian;",
        "- no reemplaza `library.db`;",
        "- cada nota vuelve a su `memory_chapter_id` canonico.",
        "",
        "## Core",
    ]
    core_lines = lines_for("projects") + lines_for("concepts")
    lines.extend(core_lines[:6] or ["- pendiente de proyeccion"])
    lines.extend(["", "## Integrations"])
    lines.extend(lines_for("sources", limit=3) or ["- pendiente de proyeccion"])
    lines.extend([
        "",
        "## Notes",
        "- este mapa es sintetico;",
        "- para evidencia o detalle, volver a la biblioteca canonica.",
    ])
    path = staging_root / "maps" / "project-memory-system.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------

def load_existing_manifest(live_dir: Path) -> dict:
    """Return {chapter_id: content_hash} from the live projection's manifest."""
    manifest_path = live_dir / MANIFEST_FILENAME
    if not manifest_path.exists():
        return {}
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, IOError):
        return {}
    entries = data.get("entries", []) if isinstance(data, dict) else []
    return {int(e["chapter_id"]): e["content_hash"]
            for e in entries
            if "chapter_id" in e and "content_hash" in e}


def write_manifest(chapters, mapping, note_hashes: Dict[int, str],
                   staging_root: Path):
    """Write the projection manifest to the staging dir."""
    manifest = {
        "generated_at": now_iso(),
        "run_id": staging_root.name,
        "vault_dir": str(VAULT_DIR),
        "total_notes": len(chapters),
        "entries": [
            {
                "chapter_id": chapter["id"],
                "title": chapter["title"],
                "shelf": chapter["shelf"],
                "path": mapping[chapter["id"]]["path"],
                "source_path": chapter["source_path"],
                "content_hash": note_hashes[chapter["id"]],
            }
            for chapter in chapters
        ],
    }
    (staging_root / MANIFEST_FILENAME).write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")


# ---------------------------------------------------------------------------
# prune orphan notes
# ---------------------------------------------------------------------------

def prune_orphans(live_dir: Path, expected_paths: set):
    """Remove notes in live_dir that are not in expected_paths."""
    removed = 0
    if not live_dir.exists():
        return removed
    for note_path in sorted(live_dir.rglob("*.md")):
        rel = str(note_path.relative_to(live_dir))
        # Skip manifest and special files
        if rel.endswith(MANIFEST_FILENAME):
            continue
        if rel not in expected_paths:
            note_path.unlink()
            removed += 1
    # Clean up empty directories
    for dirpath in sorted(live_dir.rglob("*"), reverse=True):
        if dirpath.is_dir() and dirpath != live_dir:
            try:
                dirpath.rmdir()
            except OSError:
                pass
    return removed


# ---------------------------------------------------------------------------
# atomic swap
# ---------------------------------------------------------------------------

def atomic_publish(staging_root: Path, live_dir: Path):
    """Atomically swap staging dir into live/ via os.rename."""
    # Remove old live dir if it exists
    if live_dir.exists():
        tmp = live_dir.parent / f".live-old-{staging_root.name}"
        os.rename(str(live_dir), str(tmp))
        import shutil
        shutil.rmtree(str(tmp), ignore_errors=True)
    os.rename(str(staging_root), str(live_dir))


# ---------------------------------------------------------------------------
# check mode — drift detector
# ---------------------------------------------------------------------------

def check_mode(ids, live_dir: Path):
    """Exit non-zero if the live projection differs from a fresh build."""
    if not live_dir.exists():
        raise SystemExit("ERROR: live projection does not exist — "
                         "run without --check first")

    # Write temp staging
    staging = VAULT_DIR / STAGING_DIR_NAME / f"check-{int(time.time())}"
    staging.mkdir(parents=True, exist_ok=True)
    try:
        con = connect()
        chapters = [fetch_chapter(con, cid) for cid in ids]
        mapping = build_projection_map(chapters)
        manifest_prev = load_existing_manifest(live_dir)
        note_hashes: Dict[int, str] = {}
        written = 0
        unchanged = 0

        for chapter in chapters:
            result, h = write_note(chapter, mapping[chapter["id"]], mapping,
                                   staging, manifest_prev)
            note_hashes[chapter["id"]] = h
            if result == "written":
                written += 1
            else:
                unchanged += 1

        expected = {mapping[c["id"]]["path"] for c in chapters}
        orphans = prune_orphans(staging, expected)

        if written > 0 or orphans > 0:
            raise SystemExit(
                f"DRIFT DETECTED: {written} changed, {orphans} orphan notes.\n"
                f"  Run without --check to publish the latest projection."
            )
        print(f"CHECK OK: projection is up to date ({unchanged} notes unchanged)")
    finally:
        import shutil
        shutil.rmtree(str(staging), ignore_errors=True)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Proyecta nodos de library.db a un vault de Obsidian "
                    "(staging atomico + idempotente)")
    parser.add_argument("--ids", nargs="*", type=int, default=DEFAULT_IDS)
    parser.add_argument("--check", action="store_true",
                        help="exit non-zero if published projection differs "
                             "from a fresh build (drift detector)")
    args = parser.parse_args()

    VAULT_DIR.mkdir(parents=True, exist_ok=True)

    fd = _lock_maintenance()
    try:
        con = connect()
        chapters = [fetch_chapter(con, cid) for cid in args.ids]
        mapping = build_projection_map(chapters)

        live_dir = VAULT_DIR / LIVE_DIR_NAME
        manifest_prev = load_existing_manifest(live_dir)

        if args.check:
            check_mode(args.ids, live_dir)
            return

        # Build into staging
        run_id = f"build-{int(time.time())}"
        staging_root = VAULT_DIR / STAGING_DIR_NAME / run_id
        staging_root.mkdir(parents=True, exist_ok=True)

        note_hashes: Dict[int, str] = {}
        written = 0
        unchanged = 0
        for chapter in chapters:
            result, h = write_note(chapter, mapping[chapter["id"]], mapping,
                                    staging_root, manifest_prev)
            note_hashes[chapter["id"]] = h
            if result == "written":
                written += 1
            else:
                unchanged += 1

        write_index(mapping, staging_root)
        write_map_note(mapping, staging_root)
        write_manifest(chapters, mapping, note_hashes, staging_root)

        # Prune orphans
        expected_paths = {mapping[c["id"]]["path"] for c in chapters}
        expected_paths.add("index.md")
        expected_paths.add("maps/project-memory-system.md")
        expected_paths.add(MANIFEST_FILENAME)
        removed = prune_orphans(staging_root, expected_paths)

        # Atomic publish
        atomic_publish(staging_root, live_dir)

        print(json.dumps({
            "ok": True,
            "vault_dir": str(VAULT_DIR),
            "live_dir": str(live_dir),
            "exported": len(chapters),
            "written": written,
            "unchanged": unchanged,
            "removed_orphans": removed,
            "map_note": str(live_dir / "maps" / "project-memory-system.md"),
        }, indent=2, ensure_ascii=False))
    finally:
        _unlock_maintenance(fd)


if __name__ == "__main__":
    main()
