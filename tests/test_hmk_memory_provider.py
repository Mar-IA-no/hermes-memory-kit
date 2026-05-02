"""Unit tests for the hmk-memory MemoryProvider plugin."""
from __future__ import annotations

import os
import pytest


# ---- name property + smoke -------------------------------------------------

def test_name(provider_module):
    p = provider_module.HMKMemoryProvider()
    assert p.name == "hmk-memory"


def test_register_invokes_ctx(provider_module):
    captured = []

    class Ctx:
        def register_memory_provider(self, p):
            captured.append(p)

    provider_module.register(Ctx())
    assert len(captured) == 1
    assert captured[0].name == "hmk-memory"


# ---- is_available variants -------------------------------------------------

def test_is_available_no_db(provider_module, env_isolation):
    p = provider_module.HMKMemoryProvider()
    assert p.is_available() is False


def test_is_available_only_db_path_set(
    provider_module, env_isolation, monkeypatch, tmp_db_factory
):
    """HMK_DB_PATH alone is NOT enough — BASE_DIR must also resolve."""
    base = tmp_db_factory(with_engram=True)
    monkeypatch.setenv("HMK_DB_PATH", str(base / "library.db"))
    # Note: HMK_AGENT_MEMORY_BASE intentionally NOT set.
    p = provider_module.HMKMemoryProvider()
    assert p.is_available() is False


def test_is_available_engram_db(
    provider_module, env_isolation, monkeypatch, tmp_db_factory
):
    base = tmp_db_factory(with_engram=True)
    monkeypatch.setenv("HMK_AGENT_MEMORY_BASE", str(base))
    p = provider_module.HMKMemoryProvider()
    assert p.is_available() is True


def test_is_available_legacy_db_no_engram(
    provider_module, env_isolation, monkeypatch, tmp_db_factory
):
    base = tmp_db_factory(with_engram=False)
    monkeypatch.setenv("HMK_AGENT_MEMORY_BASE", str(base))
    p = provider_module.HMKMemoryProvider()
    # Provider stays available even without ENGRAM — falls back to hybrid_pack.
    assert p.is_available() is True


def test_is_available_corrupt_db(
    provider_module, env_isolation, monkeypatch, tmp_path
):
    base = tmp_path / "agent-memory"
    base.mkdir()
    (base / "library.db").write_bytes(b"not a sqlite file at all")
    monkeypatch.setenv("HMK_AGENT_MEMORY_BASE", str(base))
    p = provider_module.HMKMemoryProvider()
    assert p.is_available() is False


# ---- initialize ------------------------------------------------------------

def test_initialize_stores_session_and_hermes_home(
    provider_module, env_isolation, monkeypatch, tmp_db_factory, tmp_path
):
    base = tmp_db_factory(with_engram=True)
    monkeypatch.setenv("HMK_AGENT_MEMORY_BASE", str(base))
    p = provider_module.HMKMemoryProvider()
    hh = str(tmp_path / "hermes-home")
    p.initialize(session_id="sess-1", hermes_home=hh)
    assert p._session_id == "sess-1"
    assert p._hermes_home == hh


def test_initialize_uses_engram_pack_when_columns_present(
    provider_module, env_isolation, monkeypatch, tmp_db_factory
):
    base = tmp_db_factory(with_engram=True)
    monkeypatch.setenv("HMK_AGENT_MEMORY_BASE", str(base))
    p = provider_module.HMKMemoryProvider()
    p.initialize(session_id="s", hermes_home="/x")
    assert p._engram_available is True
    assert p._retriever == "engram_pack"


def test_initialize_falls_back_to_hybrid_pack_without_engram(
    provider_module, env_isolation, monkeypatch, tmp_db_factory
):
    base = tmp_db_factory(with_engram=False)
    monkeypatch.setenv("HMK_AGENT_MEMORY_BASE", str(base))
    p = provider_module.HMKMemoryProvider()
    p.initialize(session_id="s", hermes_home="/x")
    assert p._engram_available is False
    assert p._retriever == "hybrid_pack"


def test_initialize_reads_env_overrides(
    provider_module, env_isolation, monkeypatch, tmp_db_factory
):
    base = tmp_db_factory(with_engram=True)
    monkeypatch.setenv("HMK_AGENT_MEMORY_BASE", str(base))
    monkeypatch.setenv("HMK_PROVIDER_LIMIT", "12")
    monkeypatch.setenv("HMK_PROVIDER_THRESHOLD", "0.5")
    monkeypatch.setenv("HMK_PROVIDER_BUDGET_TOKENS", "800")
    monkeypatch.setenv("HMK_PROVIDER_QUOTA_EPISODIC", "1")
    monkeypatch.setenv("HMK_PROVIDER_SHELVES", "library, evidence ,plans")
    p = provider_module.HMKMemoryProvider()
    p.initialize(session_id="s", hermes_home="/x")
    assert p._limit == 12
    assert p._threshold == 0.5
    assert p._budget == 800
    assert p._quotas["episodic"] == 1
    assert p._shelves == ["library", "evidence", "plans"]


# ---- prefetch + render -----------------------------------------------------

class _FakeMC:
    """Stand-in for memoryctl with controllable returns."""

    def __init__(self, return_value=None, raises=None):
        self._rv = return_value
        self._exc = raises
        self.engram_calls = []
        self.hybrid_calls = []

    def engram_pack(self, **kwargs):
        self.engram_calls.append(kwargs)
        if self._exc:
            raise self._exc
        return self._rv

    def hybrid_pack(self, **kwargs):
        self.hybrid_calls.append(kwargs)
        if self._exc:
            raise self._exc
        return self._rv


def _initialized(provider_module, monkeypatch, tmp_db_factory, *, with_engram=True):
    base = tmp_db_factory(with_engram=with_engram)
    monkeypatch.setenv("HMK_AGENT_MEMORY_BASE", str(base))
    p = provider_module.HMKMemoryProvider()
    p.initialize(session_id="s", hermes_home="/x")
    return p


def test_prefetch_empty_query_returns_empty(
    provider_module, env_isolation, monkeypatch, tmp_db_factory
):
    p = _initialized(provider_module, monkeypatch, tmp_db_factory)
    p._memoryctl = _FakeMC(return_value={"items": []})
    assert p.prefetch("") == ""
    assert p.prefetch("   ") == ""


def test_prefetch_calls_engram_pack_when_engram(
    provider_module, env_isolation, monkeypatch, tmp_db_factory
):
    fake = _FakeMC(
        return_value={
            "items": [
                {"id": 7, "shelf": "mc-skills", "engram_type": "procedural", "spr": "build a fence with sticks"},
                {"id": 8, "shelf": "library", "engram_type": "semantic", "spr": "ferns grow near water"},
            ]
        }
    )
    p = _initialized(provider_module, monkeypatch, tmp_db_factory, with_engram=True)
    p._memoryctl = fake
    out = p.prefetch("how to fence")
    assert "## 🧠 Memoria relevante" in out
    assert "[procedural|mc-skills]" in out
    assert "[mem:7]" in out
    assert len(fake.engram_calls) == 1
    assert fake.engram_calls[0]["query"] == "how to fence"
    assert fake.engram_calls[0]["quotas"] == {"episodic": 2, "semantic": 4, "procedural": 2}
    assert not fake.hybrid_calls


def test_prefetch_calls_hybrid_pack_when_no_engram(
    provider_module, env_isolation, monkeypatch, tmp_db_factory
):
    fake = _FakeMC(
        return_value={
            "items": [
                {"id": 7, "shelf": "library", "spr": "no engram column here"},
            ]
        }
    )
    p = _initialized(provider_module, monkeypatch, tmp_db_factory, with_engram=False)
    p._memoryctl = fake
    out = p.prefetch("query")
    # Without engram_type, the rendered tag is just the shelf name.
    assert "[library]" in out
    assert "[mem:7]" in out
    assert fake.hybrid_calls and not fake.engram_calls


def test_prefetch_handles_empty_items(
    provider_module, env_isolation, monkeypatch, tmp_db_factory
):
    p = _initialized(provider_module, monkeypatch, tmp_db_factory)
    p._memoryctl = _FakeMC(return_value={"items": []})
    assert p.prefetch("anything") == ""


def test_prefetch_swallows_exceptions(
    provider_module, env_isolation, monkeypatch, tmp_db_factory
):
    p = _initialized(provider_module, monkeypatch, tmp_db_factory)
    p._memoryctl = _FakeMC(raises=RuntimeError("boom"))
    # Failure must NOT propagate — prefetch returns "" and Hermes keeps going.
    assert p.prefetch("anything") == ""


# ---- system_prompt_block adapts to retriever -------------------------------

def test_system_prompt_block_engram_mode(
    provider_module, env_isolation, monkeypatch, tmp_db_factory
):
    p = _initialized(provider_module, monkeypatch, tmp_db_factory, with_engram=True)
    block = p.system_prompt_block()
    assert "balanced retrieval over episodic" in block


def test_system_prompt_block_hybrid_mode(
    provider_module, env_isolation, monkeypatch, tmp_db_factory
):
    p = _initialized(provider_module, monkeypatch, tmp_db_factory, with_engram=False)
    block = p.system_prompt_block()
    assert "lexical+semantic hybrid retrieval" in block


# ---- required no-op methods ------------------------------------------------

def test_get_tool_schemas_empty(provider_module):
    assert provider_module.HMKMemoryProvider().get_tool_schemas() == []


def test_handle_tool_call_raises_not_implemented(provider_module):
    p = provider_module.HMKMemoryProvider()
    with pytest.raises(NotImplementedError):
        p.handle_tool_call("anything", {})


def test_get_config_schema_empty_for_env_var_only(provider_module):
    assert provider_module.HMKMemoryProvider().get_config_schema() == []


def test_save_config_noop(provider_module, tmp_path):
    p = provider_module.HMKMemoryProvider()
    # Should not raise, should not write anything.
    p.save_config({"foo": "bar"}, str(tmp_path))
    assert list(tmp_path.iterdir()) == []


def test_shutdown_noop(provider_module):
    provider_module.HMKMemoryProvider().shutdown()
