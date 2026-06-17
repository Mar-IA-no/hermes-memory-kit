#!/usr/bin/env python3
"""library_extract.py — PDF extraction for the Research Library corpus.

Single PyMuPDF-based extractor with two modes (replaces the earlier pair of
per-agent scripts ``extract_pdf.py`` + ``pdf_extract.py``):

  corpus  Structured ingestion into the Research Library: copies the source
          PDF to ``raw/``, extracts+cleans the full text, writes a chapter
          file, a citable ``meta.json``, and updates the topic + master
          indexes. This is the source of truth for primary text.

  dump    Flat full-text extraction (every page) to stdout or a file, with a
          ``pages=N chars=M`` line on stderr so the caller can confirm the
          WHOLE document was read, not a fragment. Use this for one-off
          translation / TTS narration of a PDF that is not (yet) in the corpus.

Why a dedicated script: ``read_file`` does not parse PDFs, and the agent's
``execute_code`` sandbox strips the venv so ``import fitz`` fails there. Run
this through the ``terminal`` tool with the interpreter that HAS PyMuPDF.

Paths are workspace-relative. The corpus root resolves from, in order:
  1. ``--library-root``
  2. ``$LIBRARY_ROOT``
  3. ``$HMK_WORKSPACE_ROOT/library``
  4. ``<cwd>/library``

Examples:
  ./scripts/hmk library_extract.py corpus book.pdf medicina_germanica \\
      la_gran_confusion --title "La Gran Confusión" --author "Gastón Vargas" \\
      --year 2022 --language es
  ./scripts/hmk library_extract.py dump book.pdf --out /tmp/chapter.txt

Requires: pymupdf (``pip install pymupdf``).
"""
import argparse
import json
import os
import re
import shutil
import sys
from datetime import date

try:
    import fitz  # PyMuPDF
except ImportError:
    sys.exit(
        "PyMuPDF not available for this interpreter — run via the venv python "
        "that has PyMuPDF (e.g. the agent's `.venv/bin/python3`)."
    )


def resolve_library_root(explicit=None):
    if explicit:
        return os.path.abspath(explicit)
    env = os.environ.get("LIBRARY_ROOT")
    if env:
        return os.path.abspath(env)
    ws = os.environ.get("HMK_WORKSPACE_ROOT")
    if ws:
        return os.path.join(os.path.abspath(ws), "library")
    return os.path.join(os.getcwd(), "library")


def slugify(s):
    return re.sub(r"[^a-z0-9_]", "", s.lower().replace(" ", "_").replace("-", "_"))[:60]


def clean_page_text(text, page_number, title="", author=""):
    """Drop running headers/footers and lone page numbers, join soft hyphens,
    collapse runs of spaces. Paragraph structure (blank lines) is preserved."""
    title_u, author_u = title.upper().strip(), author.upper().strip()
    cleaned = []
    for line in text.splitlines():
        stripped = line.strip()
        if title_u and stripped.upper() == title_u:
            continue
        if author_u and stripped.upper() == author_u:
            continue
        # lone page number matching this page's 1-based index
        if re.match(r"^\d{1,4}$", stripped) and int(stripped) == page_number:
            continue
        cleaned.append(line)
    out = "\n".join(cleaned)
    out = out.replace("\xad\n", "").replace("\xad", "")  # soft hyphens
    out = re.sub(r" +", " ", out)
    return out


def extract_all_text(pdf_path, title="", author="", clean=True):
    doc = fitz.open(pdf_path)
    pages = doc.page_count
    parts = []
    for p in range(pages):
        raw = doc.load_page(p).get_text("text")
        text = clean_page_text(raw, p + 1, title, author) if clean else raw
        if text.strip():
            parts.append(text)
    doc.close()
    return "\n\n".join(parts), pages


# --------------------------------------------------------------------------
# mode: dump
# --------------------------------------------------------------------------
def cmd_dump(args):
    text, pages = extract_all_text(
        args.pdf, clean=not args.raw_text
    )
    sys.stderr.write("pages=%d chars=%d\n" % (pages, len(text)))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        sys.stderr.write("written: %s\n" % args.out)
    else:
        sys.stdout.write(text)
    return 0


# --------------------------------------------------------------------------
# mode: corpus
# --------------------------------------------------------------------------
def _load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def _write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def cmd_corpus(args):
    library = resolve_library_root(args.library_root)
    topics_dir = os.path.join(library, "topics")
    topic_dir = os.path.join(topics_dir, args.topic_id)
    book_dir = os.path.join(topic_dir, "books", args.book_slug)
    for sub in ("chapters", "raw", "annotations"):
        os.makedirs(os.path.join(book_dir, sub), exist_ok=True)

    # duplicate guard
    topic_index_path = os.path.join(topic_dir, "index.json")
    topic_index = _load_json(
        topic_index_path,
        {"topic": {"id": args.topic_id, "name": args.topic_id, "description": "", "tags": []}, "books": []},
    )
    if any(b.get("slug") == args.book_slug for b in topic_index["books"]):
        sys.exit(f"book '{args.book_slug}' already exists in topic '{args.topic_id}' — remove or rename first")

    # copy original PDF to raw/ (never mutated thereafter)
    raw_rel = os.path.join("raw", os.path.basename(args.pdf))
    raw_path = os.path.join(book_dir, raw_rel)
    if not os.path.exists(raw_path):
        shutil.copy2(args.pdf, raw_path)

    full_text, pages = extract_all_text(args.pdf, args.title, args.author)
    chapter_rel = os.path.join("chapters", "00_completo.txt")
    with open(os.path.join(book_dir, chapter_rel), "w", encoding="utf-8") as f:
        f.write(full_text)
    words = len(full_text.split())

    meta = {
        "book": {
            "title": args.title,
            "author": args.author,
            "year": args.year,
            "isbn": args.isbn,
            "language": args.language,
        },
        "source": {
            "original_filename": os.path.basename(args.pdf),
            "raw_path": raw_rel,
        },
        "extraction": {
            "method": "pymupdf_page_text",
            "date": date.today().isoformat(),
            "coverage_pages": f"1-{pages}",
            "cleaning": ["removed_headers_footers", "joined_soft_hyphens", "collapsed_spaces"],
        },
        "chapters": [
            {"num": "00", "title": "Completo", "pages_pdf": f"1-{pages}", "words": words, "file": chapter_rel}
        ],
        "annotations": {},
        "total_chapters": 1,
        "total_words": words,
    }
    _write_json(os.path.join(book_dir, "meta.json"), meta)

    # topic index
    topic_index["books"].append({
        "slug": args.book_slug,
        "title": args.title,
        "author": args.author,
        "year": args.year,
        "path": f"books/{args.book_slug}",
        "meta": f"books/{args.book_slug}/meta.json",
    })
    _write_json(topic_index_path, topic_index)

    # master index
    master_path = os.path.join(library, "index.json")
    master = _load_json(master_path, {
        "library": {
            "name": "Research Library",
            "path": library,
            "created": date.today().isoformat(),
            "schema_version": "1.0",
            "description": "Document library for research with precise bibliographic citations.",
        },
        "topics": [],
    })
    if not any(t.get("id") == args.topic_id for t in master["topics"]):
        master["topics"].append({"id": args.topic_id, "path": f"topics/{args.topic_id}", "index": f"topics/{args.topic_id}/index.json"})
        _write_json(master_path, master)

    print(json.dumps({
        "ok": True,
        "book_dir": book_dir,
        "pages": pages,
        "words": words,
        "chapter": chapter_rel,
        "meta": os.path.join(book_dir, "meta.json"),
    }, ensure_ascii=False))
    sys.stderr.write(
        "\nNOTE: chapters/00_completo.txt holds the WHOLE book. If it has a clear\n"
        "table of contents, split it into ##_titulo.txt chapters and update meta.json.\n"
        "Then register a catalog entry in memory (see the library-acquisition skill,\n"
        "step 'Cierre de ingreso') so the book is discoverable via librarian/hybrid-pack.\n"
    )
    return 0


def build_parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="mode", required=True)

    pc = sub.add_parser("corpus", help="structured ingestion into the Research Library")
    pc.add_argument("pdf")
    pc.add_argument("topic_id")
    pc.add_argument("book_slug")
    pc.add_argument("--title", required=True)
    pc.add_argument("--author", required=True)
    pc.add_argument("--year", default="")
    pc.add_argument("--isbn", default="")
    pc.add_argument("--language", default="es")
    pc.add_argument("--library-root", default=None, help="override corpus root (else $LIBRARY_ROOT / $HMK_WORKSPACE_ROOT/library)")
    pc.set_defaults(func=cmd_corpus)

    pd = sub.add_parser("dump", help="flat full-text extraction to stdout/file")
    pd.add_argument("pdf")
    pd.add_argument("--out", default=None, help="write to this file instead of stdout")
    pd.add_argument("--raw-text", action="store_true", help="skip header/footer/soft-hyphen cleaning")
    pd.set_defaults(func=cmd_dump)
    return p


if __name__ == "__main__":
    args = build_parser().parse_args()
    sys.exit(args.func(args))
