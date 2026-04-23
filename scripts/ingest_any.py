#!/usr/bin/env python3
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import mammoth
import trafilatura
from bs4 import BeautifulSoup
from markdownify import markdownify as html_to_markdown

sys.path.insert(0, str(Path(__file__).resolve().parent))
import memoryctl


TEXT_SUFFIXES = {
    ".md",
    ".markdown",
    ".txt",
}
PDF_SUFFIXES = {".pdf"}
HTML_SUFFIXES = {".html", ".htm"}
DOCX_SUFFIXES = {".docx"}
PANDOC_SUFFIXES = {".epub", ".odt", ".rst", ".org", ".rtf", ".tex"}


def is_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def read_plain_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def run_capture(cmd):
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise SystemExit(proc.stderr.strip() or f"command failed: {' '.join(cmd)}")
    return proc.stdout


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def text_looks_useful(text: str) -> bool:
    stripped = text.strip()
    if len(stripped) < 80:
        return False
    alpha = sum(1 for ch in stripped if ch.isalpha())
    return alpha >= 40


def convert_pdf_native(path: Path) -> str:
    return run_capture(["pdftotext", "-layout", "-nopgbrk", str(path), "-"])


def convert_pdf_via_ocr(path: Path) -> str:
    if not command_exists("ocrmypdf"):
        raise SystemExit(
            "pdf conversion produced empty markdown and OCR fallback is unavailable "
            "(install ocrmypdf + tesseract)"
        )
    languages = os.environ.get("HMK_OCR_LANGS", "eng")
    with tempfile.TemporaryDirectory(prefix="hmk-ocr-") as tmpdir:
        ocr_pdf = Path(tmpdir) / "ocr-output.pdf"
        cmd = [
            "ocrmypdf",
            "--force-ocr",
            "--rotate-pages",
            "--deskew",
            "--output-type",
            "pdf",
            "-l",
            languages,
            str(path),
            str(ocr_pdf),
        ]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if proc.returncode != 0:
            raise SystemExit(proc.stderr.strip() or f"command failed: {' '.join(cmd)}")
        return convert_pdf_native(ocr_pdf)


def convert_pdf(path: Path) -> str:
    text = convert_pdf_native(path)
    if text_looks_useful(text):
        return text
    return convert_pdf_via_ocr(path)


def extract_html_to_markdown(text: str) -> str:
    extracted = trafilatura.extract(
        text,
        output_format="markdown",
        include_links=True,
        include_formatting=True,
        favor_precision=True,
    )
    if extracted:
        return extracted
    soup = BeautifulSoup(text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return html_to_markdown(str(soup), heading_style="ATX")


def convert_html(path: Path) -> str:
    return extract_html_to_markdown(read_plain_text(path))


def convert_docx(path: Path) -> str:
    try:
        return run_capture(["pandoc", "-f", "docx", "-t", "gfm", str(path)])
    except SystemExit:
        with path.open("rb") as handle:
            result = mammoth.convert_to_html(handle)
        return html_to_markdown(result.value, heading_style="ATX")


def convert_via_pandoc(path: Path) -> str:
    return run_capture(["pandoc", "-t", "gfm", str(path)])


def convert_url(url: str) -> str:
    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        raise SystemExit(f"unable to fetch url: {url}")
    extracted = trafilatura.extract(
        downloaded,
        output_format="markdown",
        include_links=True,
        include_formatting=True,
        favor_precision=True,
    )
    if not extracted:
        raise SystemExit(f"unable to extract url content: {url}")
    return extracted


def convert_to_markdown(source: str) -> str:
    if is_url(source):
        return convert_url(source)

    path = Path(source)
    suffix = path.suffix.lower()
    if suffix in TEXT_SUFFIXES:
        return read_plain_text(path)
    if suffix in PDF_SUFFIXES:
        return convert_pdf(path)
    if suffix in HTML_SUFFIXES:
        return convert_html(path)
    if suffix in DOCX_SUFFIXES:
        return convert_docx(path)
    if suffix in PANDOC_SUFFIXES:
        return convert_via_pandoc(path)
    raise SystemExit(f"unsupported source format for now: {suffix or '[no extension]'}")


def default_title(source: str) -> str:
    if is_url(source):
        parsed = urlparse(source)
        return parsed.netloc + parsed.path
    return Path(source).stem


def main():
    parser = argparse.ArgumentParser(description="Convierte fuentes heterogeneas a markdown y las ingesta en la memoria local")
    parser.add_argument("--source", required=True, help="ruta local o URL")
    parser.add_argument("--shelf", required=True, choices=sorted(memoryctl.DEFAULT_SHELVES))
    parser.add_argument("--title")
    parser.add_argument("--tags", default="")
    parser.add_argument("--importance", type=float, default=0.5)
    parser.add_argument("--preview", action="store_true", help="solo convertir y mostrar markdown")
    args = parser.parse_args()

    markdown = convert_to_markdown(args.source).strip()
    if not markdown:
        raise SystemExit("conversion produced empty markdown")

    if args.preview:
        print(markdown)
        return

    chapter_id = memoryctl.add_text(
        shelf_name=args.shelf,
        title=args.title or default_title(args.source),
        raw=markdown,
        tags=memoryctl.parse_tags(args.tags),
        importance=args.importance,
        source_path=args.source,
        source_kind="url" if is_url(args.source) else "converted-file",
        replace=True,
    )
    print(json.dumps({"ok": True, "chapter_id": chapter_id}))


if __name__ == "__main__":
    main()
