"""Tests for corpus_policy module and selective embedding (v3.9.0).

Covers:
- File-level blocking (never-touch names, globs, name-contains)
- Content-level secret scan (structural credential patterns)
- Source kind classification
- Policy loading (fail-closed, caching)
- Integration: add_file blocks blocked files, classifies code/config
- Integration: add_text stores secrets with embed_disabled
- embedding_candidates skips embed_disabled chapters
- stats reports embed_disabled breakdown
- update_chapter re-scans content for secrets

All tests use temporary DBs via tmp_db_factory / env_isolation fixtures
from conftest.py.  The canonical DB (~/.hermes/agent-memory/library.db) is
never touched.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

# import corpus_policy module directly
SCRIPT_DIR = str(Path(__file__).resolve().parent.parent / "scripts")
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import corpus_policy
import memoryctl


# -------------------------------------------------------------------------
# File-level blocking
# -------------------------------------------------------------------------

REJECTED_NAMES = [
    (".env", "never-touch filename: .env"),
    (".git-credentials", "never-touch filename: .git-credentials"),
    (".npmrc", "never-touch filename: .npmrc"),
]

REJECTED_GLOBS = [
    ("foo.key", "glob: *.key"),
    ("some.pem", "glob: *.pem"),
    ("id_rsa", "glob: id_rsa*"),
    ("id_rsa.pub", "glob: id_rsa*"),
    ("id_ed25519", "glob: id_ed25519*"),
    ("terraform.tfstate", "glob: *.tfstate"),
    ("prod.kubeconfig", "glob: *.kubeconfig"),
]

REJECTED_CONTAINS = [
    ("my-secret.txt", "name-contains"),
    ("credentials.json", "name-contains"),
    ("passwords.csv", "name-contains"),
]

ACCEPTED_NAMES = [
    "README.md",
    "notes.txt",
    "foo.py",
    "config.yaml",
    "archive.json",
]


@pytest.mark.parametrize("fname,expected", REJECTED_NAMES)
def test_should_block_exact_name(fname, expected):
    blocked, reason = corpus_policy.should_block_file(fname)
    assert blocked, f"expected {fname!r} to be blocked"
    assert expected[:10] in reason, f"reason mismatch: {reason}"


@pytest.mark.parametrize("fname,expected", REJECTED_GLOBS)
def test_should_block_glob(fname, expected):
    blocked, reason = corpus_policy.should_block_file(fname)
    assert blocked, f"expected {fname!r} to be blocked, got reason={reason}"
    assert "glob" in reason or "never-touch" in reason, f"reason mismatch: {reason}"


@pytest.mark.parametrize("fname,expected", REJECTED_CONTAINS)
def test_should_block_contains(fname, expected):
    blocked, reason = corpus_policy.should_block_file(fname)
    assert blocked, f"expected {fname!r} to be blocked"
    assert "contains" in reason, f"reason mismatch: {reason}"


@pytest.mark.parametrize("fname", ACCEPTED_NAMES)
def test_should_accept_normal_names(fname):
    blocked, _ = corpus_policy.should_block_file(fname)
    assert not blocked, f"expected {fname!r} to be accepted"


# -------------------------------------------------------------------------
# Content secret scan
# -------------------------------------------------------------------------


def test_scan_private_key_block():
    text = "some content\n-----BEGIN RSA PRIVATE KEY-----\nMIIEpA...\n-----END RSA PRIVATE KEY-----\nmore content"
    reason = corpus_policy.scan_content_for_secrets(text)
    assert reason is not None
    assert "private key" in reason


def test_scan_openssh_key():
    text = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n"
    reason = corpus_policy.scan_content_for_secrets(text)
    assert reason is not None
    assert "OpenSSH" in reason


def test_scan_aws_key():
    text = "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE extra padding to reach minimum"
    reason = corpus_policy.scan_content_for_secrets(text)
    assert reason is not None
    assert "AWS" in reason


def test_scan_openai_key():
    text = 'OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz123456\n'
    reason = corpus_policy.scan_content_for_secrets(text)
    assert reason is not None
    assert "OpenAI" in reason or "sk-" in reason


def test_scan_github_token():
    text = "export GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz1234\n"
    reason = corpus_policy.scan_content_for_secrets(text)
    assert reason is not None
    assert "GitHub" in reason


def test_clean_content_passes():
    text = "# My Project Notes\n\nThis is a normal markdown file with no secrets.\n- bullet 1\n- bullet 2"
    reason = corpus_policy.scan_content_for_secrets(text)
    assert reason is None


def test_short_content_skipped():
    """Content under 40 chars is skipped to avoid false positives."""
    text = "short"
    reason = corpus_policy.scan_content_for_secrets(text)
    assert reason is None


# -------------------------------------------------------------------------
# Source kind classification
# -------------------------------------------------------------------------

CODE_TESTS = [
    ("app.py", "code"),
    ("run.sh", "code"),
    ("main.js", "code"),
    ("component.tsx", "code"),
    ("kernel.cu", "code"),
]

CONFIG_TESTS = [
    ("config.json", "config"),
    ("settings.yaml", "config"),
    ("docker-compose.yml", "config"),
    ("pyproject.toml", "config"),
    ("app.ini", "config"),
]

FILE_TESTS = [
    ("README.md", "file"),
    ("notes.txt", "file"),
    ("report.pdf", "file"),
    ("image.png", "file"),
]


@pytest.mark.parametrize("fname,expected", CODE_TESTS)
def test_classify_code(fname, expected):
    assert corpus_policy.classify_source_kind(fname) == expected


@pytest.mark.parametrize("fname,expected", CONFIG_TESTS)
def test_classify_config(fname, expected):
    assert corpus_policy.classify_source_kind(fname) == expected


@pytest.mark.parametrize("fname,expected", FILE_TESTS)
def test_classify_file(fname, expected):
    assert corpus_policy.classify_source_kind(fname) == expected


# -------------------------------------------------------------------------
# Policy loading
# -------------------------------------------------------------------------


def test_load_default_policy(monkeypatch):
    """Default policy loads without errors."""
    monkeypatch.delenv("HMK_CORPUS_POLICY", raising=False)
    # force cache clear
    corpus_policy._policy_cache = None
    policy = corpus_policy.load_policy()
    assert isinstance(policy, dict)
    assert "never_touch_names" in policy


def test_fail_closed_on_missing_env_policy(monkeypatch):
    """HMK_CORPUS_POLICY pointing to nonexistent file raises SystemExit."""
    monkeypatch.setenv("HMK_CORPUS_POLICY", "/nonexistent/policy.json")
    corpus_policy._policy_cache = None
    with pytest.raises(SystemExit) as exc:
        corpus_policy.load_policy()
    assert "not readable" in str(exc.value)


# -------------------------------------------------------------------------
# Integration: memoryctl add_file with corpus policy
# -------------------------------------------------------------------------


def test_add_file_blocks_protected_names(tmp_path, monkeypatch):
    """add_file refuses .env / .pem / etc."""
    db_base = tmp_path / "agent-memory"
    db_base.mkdir()
    monkeypatch.setenv("HMK_AGENT_MEMORY_BASE", str(db_base))
    # re-import memoryctl to resolve new env vars
    import importlib
    importlib.reload(memoryctl)

    secret_file = tmp_path / ".env"
    secret_file.write_text("SECRET=value")
    with pytest.raises(SystemExit) as exc:
        memoryctl.add_file(str(secret_file), shelf_name="evidence")
    assert "blocked" in str(exc.value) or "never-touch" in str(exc.value)


def test_add_file_classifies_code(tmp_path, monkeypatch):
    """add_file sets embed_disabled for code files."""
    db_base = tmp_path / "agent-memory"
    db_base.mkdir()
    monkeypatch.setenv("HMK_AGENT_MEMORY_BASE", str(db_base))
    import importlib
    importlib.reload(memoryctl)

    code_file = tmp_path / "test.py"
    code_file.write_text("print('hello')")
    chapter_id = memoryctl.add_file(str(code_file), shelf_name="library")

    # Verify embed_disabled is set
    con = memoryctl.connect()
    row = con.execute(
        "SELECT embed_disabled, embed_disable_reason FROM chapters WHERE id=?",
        (chapter_id,),
    ).fetchone()
    con.close()
    assert row["embed_disabled"] == 1
    assert "code" in row["embed_disable_reason"]


def test_add_text_with_secret_blocks_embedding(tmp_path, monkeypatch):
    """add_text stores secret content with embed_disabled."""
    db_base = tmp_path / "agent-memory"
    db_base.mkdir()
    monkeypatch.setenv("HMK_AGENT_MEMORY_BASE", str(db_base))
    import importlib
    importlib.reload(memoryctl)

    secret_text = "API key: api_key='sk-proj-abcdefghijklmnopqrstuvwxyz123456'"
    chapter_id = memoryctl.add_text(
        shelf_name="library",
        title="secret doc",
        raw=secret_text,
    )
    con = memoryctl.connect()
    row = con.execute(
        "SELECT embed_disabled, embed_disable_reason FROM chapters WHERE id=?",
        (chapter_id,),
    ).fetchone()
    con.close()
    assert row["embed_disabled"] == 1
    assert "sk-" in row["embed_disable_reason"] or "OpenAI" in row["embed_disable_reason"]


def test_clean_text_not_disabled(tmp_path, monkeypatch):
    """Normal content gets embed_disabled=0."""
    db_base = tmp_path / "agent-memory"
    db_base.mkdir()
    monkeypatch.setenv("HMK_AGENT_MEMORY_BASE", str(db_base))
    import importlib
    importlib.reload(memoryctl)

    chapter_id = memoryctl.add_text(
        shelf_name="library",
        title="clean doc",
        raw="# Hello\n\nThis is normal text.",
    )
    con = memoryctl.connect()
    row = con.execute(
        "SELECT embed_disabled FROM chapters WHERE id=?",
        (chapter_id,),
    ).fetchone()
    con.close()
    assert row["embed_disabled"] == 0


# -------------------------------------------------------------------------
# integration: embedding_candidates skips disabled
# -------------------------------------------------------------------------


def test_embedding_candidates_skips_disabled(tmp_path, monkeypatch):
    """embedding_candidates excludes embed_disabled chapters."""
    db_base = tmp_path / "agent-memory"
    db_base.mkdir()
    monkeypatch.setenv("HMK_AGENT_MEMORY_BASE", str(db_base))
    import importlib
    importlib.reload(memoryctl)

    # Create one clean, one disabled
    memoryctl.add_text(shelf_name="library", title="clean", raw="clean text")
    memoryctl.add_text(shelf_name="library", title="secret",
                       raw="api_key='sk-proj-abcdefghijklmnopqrstuvwxyz'")

    candidates = memoryctl.embedding_candidates(only_missing=False)
    assert any("clean" in c["title"] for c in candidates)
    assert not any("secret" in c["title"] for c in candidates)


# -------------------------------------------------------------------------
# integration: stats reports embed_disabled breakdown
# -------------------------------------------------------------------------


def test_stats_reports_embed_disabled(tmp_path, monkeypatch):
    """stats() shows embed_disabled count and reason breakdown."""
    db_base = tmp_path / "agent-memory"
    db_base.mkdir()
    monkeypatch.setenv("HMK_AGENT_MEMORY_BASE", str(db_base))
    import importlib
    importlib.reload(memoryctl)

    memoryctl.add_text(shelf_name="library", title="clean", raw="clean")
    memoryctl.add_text(shelf_name="library", title="code",
                       raw="print(1)", source_kind="code")
    memoryctl.add_text(shelf_name="library", title="secret",
                       raw="api_key='sk-proj-abcdefghijklmnopqrstuv'")

    info = memoryctl.stats()
    assert info["embed_disabled"] == 2
    reasons = {item["reason"] for item in info["embed_disabled_by_reason"]}
    assert "source_kind=code" in reasons
    assert any("sk-" in r or "OpenAI" in r for r in reasons)


# -------------------------------------------------------------------------
# update_chapter re-scans for secrets
# -------------------------------------------------------------------------


def test_update_chapter_rescans_secrets(tmp_path, monkeypatch):
    """update_chapter with new secret content sets embed_disabled."""
    db_base = tmp_path / "agent-memory"
    db_base.mkdir()
    monkeypatch.setenv("HMK_AGENT_MEMORY_BASE", str(db_base))
    import importlib
    importlib.reload(memoryctl)

    cid = memoryctl.add_text(shelf_name="library", title="safe", raw="safe text")
    result = memoryctl.update_chapter(cid, content="api_key='sk-proj-abcdefghijklmnopqrstuvwxyz'")
    assert result["embed_disabled"] == 1
    assert result["embed_disable_reason"] is not None
