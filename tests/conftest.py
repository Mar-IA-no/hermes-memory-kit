"""Pytest fixtures for hmk-memory provider tests.

Each fixture is scoped to a single test (function scope) so that env-var
mutations don't leak between cases.
"""
from __future__ import annotations

import importlib.util as iu
import os
import sqlite3
import sys
from pathlib import Path

import pytest

# Resolve plugin __init__.py from the kit layout. CWD-independent.
_PROVIDER_INIT = (
    Path(__file__).resolve().parent.parent
    / "templates"
    / "plugins"
    / "hmk-memory"
    / "__init__.py"
)


def _create_chapters_table(con: sqlite3.Connection, *, with_engram: bool) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS shelves (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE
        );
        CREATE TABLE IF NOT EXISTS books (
            id INTEGER PRIMARY KEY,
            shelf_id INTEGER NOT NULL,
            slug TEXT NOT NULL,
            title TEXT,
            source_kind TEXT,
            created_at INTEGER,
            updated_at INTEGER,
            FOREIGN KEY (shelf_id) REFERENCES shelves(id)
        );
        """
    )
    if with_engram:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS chapters (
                id INTEGER PRIMARY KEY,
                book_id INTEGER NOT NULL,
                ordinal INTEGER,
                title TEXT,
                spr TEXT,
                raw TEXT,
                tokens INTEGER,
                importance REAL,
                created_at INTEGER,
                updated_at INTEGER,
                last_access INTEGER,
                access_count INTEGER,
                tags_json TEXT,
                engram_type TEXT NOT NULL DEFAULT 'semantic'
                    CHECK (engram_type IN ('episodic','semantic','procedural')),
                event_ts INTEGER NULL,
                actor TEXT NULL,
                location_json TEXT NULL,
                FOREIGN KEY (book_id) REFERENCES books(id)
            )
            """
        )
    else:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS chapters (
                id INTEGER PRIMARY KEY,
                book_id INTEGER NOT NULL,
                ordinal INTEGER,
                title TEXT,
                spr TEXT,
                raw TEXT,
                tokens INTEGER,
                importance REAL,
                created_at INTEGER,
                updated_at INTEGER,
                last_access INTEGER,
                access_count INTEGER,
                tags_json TEXT,
                FOREIGN KEY (book_id) REFERENCES books(id)
            )
            """
        )


@pytest.fixture
def tmp_db_factory(tmp_path):
    """Build a temp library.db. Caller chooses ENGRAM presence via with_engram.

    Returns a callable: ``factory(with_engram=True)`` -> ``Path`` to the
    base dir (not the .db file). Set ``HMK_AGENT_MEMORY_BASE`` to that base.
    """

    def _make(with_engram: bool = True) -> Path:
        base = tmp_path / "agent-memory"
        base.mkdir(exist_ok=True)
        db_path = base / "library.db"
        con = sqlite3.connect(str(db_path))
        _create_chapters_table(con, with_engram=with_engram)
        con.commit()
        con.close()
        return base

    return _make


@pytest.fixture
def env_isolation(monkeypatch):
    """Clear all relevant env vars so each test starts from a known baseline.

    Tests that need a configured environment should monkeypatch the vars they
    care about *after* this fixture has run.
    """
    for k in (
        "HMK_AGENT_MEMORY_BASE",
        "AGENT_MEMORY_BASE",
        "HMK_BASE_DIR",
        "HMK_DB_PATH",
        "HERMES_DB_PATH",
        "HMK_HERMES_HOME",
        "HERMES_HOME",
        "HMK_PROVIDER_RETRIEVER",
        "HMK_PROVIDER_LIMIT",
        "HMK_PROVIDER_THRESHOLD",
        "HMK_PROVIDER_BUDGET_TOKENS",
        "HMK_PROVIDER_QUOTA_EPISODIC",
        "HMK_PROVIDER_QUOTA_SEMANTIC",
        "HMK_PROVIDER_QUOTA_PROCEDURAL",
        "HMK_PROVIDER_SHELVES",
        "HMK_MEMORYCTL_PATH",
    ):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def provider_module():
    """Import the plugin's __init__.py via importlib.

    The plugin lives in ``templates/plugins/hmk-memory/`` (with a hyphen),
    which is not a valid Python identifier, so we cannot ``import`` it
    normally. The module is given a stable internal name so repeated imports
    in the same process land on the same object.
    """
    name = "hmk_memory_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = iu.spec_from_file_location(name, _PROVIDER_INIT)
    mod = iu.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    sys.modules[name] = mod
    return mod
