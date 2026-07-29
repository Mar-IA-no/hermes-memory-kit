"""Tests for memoryctl.update_chapter / delete_chapter (v3.8.1).

These exercise the real memoryctl module against a throwaway library.db —
no mocks — because the riskiest invariants live in the SQL: FTS5
contentless-table delete/insert, FK cascade into chapter_embeddings and
chapter_links, and empty-book pruning.
"""
from __future__ import annotations

import importlib.util as iu
import json
import sqlite3
import sys
from pathlib import Path

import pytest

_MEMORYCTL_PATH = (
    Path(__file__).resolve().parent.parent / "scripts" / "memoryctl.py"
)


def _load_memoryctl(monkeypatch, base: Path):
    """Import scripts/memoryctl.py fresh, bound to `base` as the memory base.

    memoryctl resolves HMK_AGENT_MEMORY_BASE / HMK_DB_PATH at import time,
    so each test gets its own module instance pointed at its own tmp DB.
    """
    monkeypatch.setenv("HMK_AGENT_MEMORY_BASE", str(base))
    monkeypatch.delenv("HMK_DB_PATH", raising=False)
    name = "memoryctl_under_test"
    sys.modules.pop(name, None)
    spec = iu.spec_from_file_location(name, _MEMORYCTL_PATH)
    mod = iu.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    sys.modules[name] = mod
    return mod


@pytest.fixture
def mc(tmp_path, monkeypatch):
    base = tmp_path / "agent-memory"
    base.mkdir()
    mod = _load_memoryctl(monkeypatch, base)
    mod.init_db()
    return mod


def _fts_hits(mc, term):
    con = mc.connect()
    rows = con.execute(
        "SELECT rowid FROM chapters_fts WHERE chapters_fts MATCH ?", (term,)
    ).fetchall()
    con.close()
    return {r[0] for r in rows}


def _seed_embedding(mc, chapter_id):
    con = mc.connect()
    con.execute(
        """
        INSERT INTO chapter_embeddings(chapter_id, provider, model, input_text_hash, dims, embedding_json, created_at, updated_at)
        VALUES(?, 'test', 'test-model', 'deadbeef', 2, '[0.1, 0.2]', 1, 1)
        """,
        (chapter_id,),
    )
    con.commit()
    con.close()


# ---- update_chapter ----------------------------------------------------


def test_update_content_rewrites_fts_and_drops_embeddings(mc):
    cid = mc.add_text(shelf_name="library", title="alpha-note", raw="uniquealpha content", tags=["a"])
    _seed_embedding(mc, cid)
    assert cid in _fts_hits(mc, "uniquealpha")

    report = mc.update_chapter(cid, content="uniquebeta replacement")

    assert report["content_changed"] is True
    assert report["embeddings_dropped"] == 1
    assert cid not in _fts_hits(mc, "uniquealpha")
    assert cid in _fts_hits(mc, "uniquebeta")
    chapter = mc.expand(cid)
    assert chapter["raw"] == "uniquebeta replacement"
    assert chapter["tags"] == ["a"]  # untouched fields preserved


def test_update_preserves_embeddings_when_content_unchanged(mc):
    cid = mc.add_text(shelf_name="library", title="gamma-note", raw="same body", importance=0.5)
    _seed_embedding(mc, cid)

    report = mc.update_chapter(cid, importance=0.9)

    assert report["content_changed"] is False
    assert report["embeddings_dropped"] == 0
    assert mc.expand(cid)["importance"] == pytest.approx(0.9)


def test_update_bumps_book_updated_at(mc, monkeypatch):
    cid = mc.add_text(shelf_name="library", title="book-bump", raw="body")
    con = mc.connect()
    before = con.execute(
        "SELECT updated_at FROM books WHERE id=(SELECT book_id FROM chapters WHERE id=?)", (cid,)
    ).fetchone()[0]
    con.close()
    future = before + 1000
    monkeypatch.setattr(mc, "now_ts", lambda: future)

    mc.update_chapter(cid, importance=0.7)

    con = mc.connect()
    after = con.execute(
        "SELECT updated_at FROM books WHERE id=(SELECT book_id FROM chapters WHERE id=?)", (cid,)
    ).fetchone()[0]
    con.close()
    assert after == future


def test_update_title_syncs_book_and_slug(mc):
    cid = mc.add_text(shelf_name="library", title="old-title", raw="body")

    mc.update_chapter(cid, title="new-title")

    chapter = mc.expand(cid)
    assert chapter["title"] == "new-title"
    assert chapter["book_title"] == "new-title"
    con = mc.connect()
    slug = con.execute(
        "SELECT slug FROM books WHERE id=(SELECT book_id FROM chapters WHERE id=?)", (cid,)
    ).fetchone()[0]
    con.close()
    assert slug == "new-title"


def test_update_title_slug_collision_aborts(mc):
    mc.add_text(shelf_name="library", title="taken", raw="first")
    cid = mc.add_text(shelf_name="library", title="free", raw="second")

    with pytest.raises(SystemExit):
        mc.update_chapter(cid, title="taken")
    # chapter untouched after the aborted rename
    assert mc.expand(cid)["title"] == "free"


def test_update_tags_replace_set(mc):
    cid = mc.add_text(shelf_name="library", title="taggy", raw="body", tags=["old"])

    mc.update_chapter(cid, tags=["new", "fresh"])

    assert mc.expand(cid)["tags"] == ["new", "fresh"]


def test_update_requires_a_field(mc):
    cid = mc.add_text(shelf_name="library", title="noop", raw="body")
    with pytest.raises(SystemExit):
        mc.update_chapter(cid)


def test_update_missing_chapter_raises(mc):
    with pytest.raises(SystemExit):
        mc.update_chapter(9999, content="x")


# ---- delete_chapter ----------------------------------------------------


def test_delete_removes_chapter_fts_embeddings_links_and_prunes_book(mc):
    cid = mc.add_text(shelf_name="library", title="doomed", raw="uniquedoomed body")
    other = mc.add_text(shelf_name="library", title="survivor", raw="survivor body")
    _seed_embedding(mc, cid)
    mc.add_link(other, cid, "related")

    report = mc.delete_chapter(cid)

    assert report["embeddings_removed"] == 1
    assert report["links_removed"] == 1
    assert report["book_deleted"] is True
    assert len(report["raw_sha256"]) == 64
    assert cid not in _fts_hits(mc, "uniquedoomed")
    con = mc.connect()
    assert con.execute("SELECT COUNT(*) FROM chapters WHERE id=?", (cid,)).fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM chapter_embeddings WHERE chapter_id=?", (cid,)).fetchone()[0] == 0
    assert con.execute(
        "SELECT COUNT(*) FROM chapter_links WHERE src_chapter_id=? OR dst_chapter_id=?", (cid, cid)
    ).fetchone()[0] == 0
    con.close()
    # the survivor chapter is intact
    assert mc.expand(other)["title"] == "survivor"


def test_delete_keep_book_preserves_empty_book(mc):
    cid = mc.add_text(shelf_name="library", title="keeper-book", raw="body")
    book_id = mc.expand(cid)["book_id"]

    report = mc.delete_chapter(cid, prune_book=False)

    assert report["book_deleted"] is False
    con = mc.connect()
    assert con.execute("SELECT COUNT(*) FROM books WHERE id=?", (book_id,)).fetchone()[0] == 1
    con.close()


def test_delete_missing_chapter_raises(mc):
    with pytest.raises(SystemExit):
        mc.delete_chapter(9999)


# ---- CLI surface --------------------------------------------------------


def test_cli_update_and_delete_roundtrip(mc, capsys):
    cid = mc.add_text(shelf_name="library", title="cli-note", raw="clialpha body")

    sys.argv = ["memoryctl.py", "update", "--id", str(cid), "--raw", "clibeta body", "--tags", "x,y"]
    mc.main()
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["content_changed"] is True
    assert mc.expand(cid)["tags"] == ["x", "y"]

    sys.argv = ["memoryctl.py", "delete", "--id", str(cid)]
    mc.main()
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["book_deleted"] is True
