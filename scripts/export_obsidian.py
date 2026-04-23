#!/usr/bin/env python3
import argparse
import json
import os
import re
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
BASE_DIR = Path(os.environ.get("HMK_BASE_DIR", str(REPO_ROOT / "agent-memory"))).expanduser()
DB_PATH = Path(os.environ.get("HMK_DB_PATH", str(BASE_DIR / "library.db"))).expanduser()
VAULT_DIR = Path(os.environ.get("HMK_VAULT_DIR", str(REPO_ROOT / "wiki"))).expanduser()
MANIFEST_PATH = VAULT_DIR / ".projection-manifest.json"

DEFAULT_IDS = [int(item) for item in os.environ.get("HMK_EXPORT_IDS", "").split(",") if item.strip()]
LINK_TYPES = ["summarizes", "depends_on", "related_to", "evidence_for", "anchors", "references"]


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def slugify(text):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "item"


def connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def fetch_chapter(con, chapter_id):
    row = con.execute(
        """
        SELECT
          c.id,
          c.book_id,
          c.title,
          c.spr,
          c.raw,
          c.tokens,
          c.importance,
          c.tags_json,
          c.updated_at,
          b.title AS book_title,
          b.source_path,
          b.slug AS book_slug,
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
            SELECT l.link_type, l.dst_chapter_id AS other_id, c.title AS other_title
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
            SELECT l.link_type, l.src_chapter_id AS other_id, c.title AS other_title
            FROM chapter_links l
            JOIN chapters c ON c.id = l.src_chapter_id
            WHERE l.dst_chapter_id=?
            ORDER BY l.link_type, l.weight DESC, c.id ASC
            """,
            (chapter_id,),
        ).fetchall()
    ]
    return data


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
        if "summary" in tags or "resumen" in title or "sintesis" in title or "integracion" in title:
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
    parts = [chunk.capitalize() if chunk.islower() else chunk for chunk in text.split()]
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
        groups[link["link_type"]].append(wikilink_for(link["other_id"], mapping, link["other_title"]))
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
    lines.extend(
        [
            "source_paths:",
            yaml_list(source_paths, indent=2),
            f'last_projected_at: "{now_iso()}"',
            'projection_status: "active"',
            "---",
        ]
    )
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
    lines.extend(
        [
            "",
            "## Canonical Memory",
            f"- chapter_id: `{chapter['id']}`",
            f"- shelf: `{chapter['shelf']}`",
            f"- book_title: `{chapter['book_title']}`",
            "- raw_ref: `library.db`",
        ]
    )
    return "\n".join(lines).strip() + "\n"


def write_note(chapter, projection, mapping):
    path = VAULT_DIR / projection["path"]
    path.parent.mkdir(parents=True, exist_ok=True)
    content = render_frontmatter(chapter, projection, mapping) + "\n\n" + render_body(chapter, projection, mapping)
    path.write_text(content, encoding="utf-8")


def write_index(mapping):
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
    for folder in ["maps", "projects", "concepts", "entities", "sources", "synthesis", "log"]:
        lines.append(f"- {folder}/")
        for chapter_id, meta in sorted(groups.get(folder, []), key=lambda item: item[1]["title"].lower()):
            lines.append(f"  - [[{meta['slug']}]] (`mem:{chapter_id}`)")
    (VAULT_DIR / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_map_note(mapping):
    by_folder = defaultdict(list)
    for chapter_id, meta in mapping.items():
        by_folder[meta["folder"]].append((chapter_id, meta))

    def lines_for(folder, limit=4):
        rows = sorted(by_folder.get(folder, []), key=lambda item: item[1]["title"].lower())
        return [f"- [[{meta['slug']}]] (`mem:{chapter_id}`)" for chapter_id, meta in rows[:limit]]

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
    lines.extend(
        [
            "",
            "## Integrations",
        ]
    )
    lines.extend(lines_for("sources", limit=3) or ["- pendiente de proyeccion"])
    lines.extend(
        [
            "",
            "## Notes",
            "- este mapa es sintetico;",
            "- para evidencia o detalle, volver a la biblioteca canonica.",
        ]
    )
    path = VAULT_DIR / "maps" / "project-memory-system.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_manifest(chapters, mapping):
    manifest = {
        "generated_at": now_iso(),
        "vault_dir": str(VAULT_DIR),
        "items": [
            {
                "chapter_id": chapter["id"],
                "title": chapter["title"],
                "shelf": chapter["shelf"],
                "path": mapping[chapter["id"]]["path"],
                "source_path": chapter["source_path"],
            }
            for chapter in chapters
        ],
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Proyecta nodos de library.db a un vault de Obsidian")
    parser.add_argument("--ids", nargs="*", type=int, default=DEFAULT_IDS)
    args = parser.parse_args()

    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    con = connect()
    chapters = [fetch_chapter(con, cid) for cid in args.ids]
    mapping = build_projection_map(chapters)
    for chapter in chapters:
        write_note(chapter, mapping[chapter["id"]], mapping)
    write_index(mapping)
    write_map_note(mapping)
    write_manifest(chapters, mapping)
    print(
        json.dumps(
            {
                "ok": True,
                "vault_dir": str(VAULT_DIR),
                "exported": len(chapters),
                "map_note": str(VAULT_DIR / "maps" / "project-memory-system.md"),
            },
            indent=2,
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
