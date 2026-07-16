"""Unit tests for ocl/memory/mem0_store.py."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _reset_module():
    import ocl.memory.mem0_store as mod
    mod._mem0_instance = None
    mod._mem0_initialised = False


def test_get_mem0_disabled_returns_none():
    _reset_module()
    import ocl.memory.mem0_store as mod
    with patch.object(mod, "settings") as mock_settings:
        mock_settings.mem0_enabled = False
        result = mod.get_mem0()
    assert result is None


def test_get_mem0_singleton():
    import ocl.memory.mem0_store as mod
    _reset_module()

    mock_instance = MagicMock()
    mock_memory_cls = MagicMock()
    mock_memory_cls.from_config = MagicMock(return_value=mock_instance)

    fake_mem0_mod = MagicMock()
    fake_mem0_mod.Memory = mock_memory_cls
    sys.modules["mem0"] = fake_mem0_mod

    mock_settings = MagicMock()
    mock_settings.mem0_enabled = True
    mock_settings.mem0_vector_store_path = "/tmp/vectors"
    mock_settings.mem0_embedder_model = "BAAI/bge-small-zh-v1.5"
    mock_settings.mem0_embedder_api_base = ""
    mock_settings.mem0_embedder_api_key = ""
    mock_settings.mem0_llm_model = "gpt-4o-mini"
    mock_settings.mem0_llm_api_base = "https://api.openai.com/v1"
    mock_settings.mem0_llm_api_key = "sk-test"
    mock_settings.data_dir = Path("/data")

    with patch.object(mod, "settings", mock_settings):
        r1 = mod.get_mem0()
        r2 = mod.get_mem0()

    assert r1 is r2
    mock_memory_cls.from_config.assert_called_once()


def test_build_config_all_local():
    import ocl.memory.mem0_store as mod

    mock_settings = MagicMock()
    mock_settings.mem0_vector_store_path = ""
    mock_settings.data_dir = Path("/data")
    mock_settings.mem0_embedder_model = "BAAI/bge-small-zh-v1.5"
    mock_settings.mem0_embedder_api_base = ""
    mock_settings.mem0_embedder_api_key = ""
    mock_settings.mem0_llm_model = "gpt-4o-mini"
    mock_settings.mem0_llm_api_base = "https://api.openai.com/v1"
    mock_settings.mem0_llm_api_key = "sk-test"

    with patch.object(mod, "settings", mock_settings):
        cfg = mod._build_config()

    assert cfg["vector_store"]["provider"] == "chroma"
    assert cfg["vector_store"]["config"]["collection_name"] == "ocl"
    assert cfg["vector_store"]["config"]["path"] == str(Path("/data") / "mem0_vectors")
    assert cfg["embedder"]["provider"] == "huggingface"
    assert cfg["embedder"]["config"]["model"] == "BAAI/bge-small-zh-v1.5"
    assert cfg["llm"]["provider"] == "openai"
    assert cfg["llm"]["config"]["model"] == "gpt-4o-mini"
    assert cfg["llm"]["config"]["temperature"] == 0.1
    assert cfg["llm"]["config"]["max_tokens"] == 1000


async def test_mem0_add_calls_to_thread():
    import ocl.memory.mem0_store as mod
    mock_mem0 = MagicMock()
    mock_mem0.add = MagicMock(return_value=None)

    with patch.object(mod, "get_mem0", return_value=mock_mem0):
        with patch.object(mod.asyncio, "to_thread", new_callable=AsyncMock) as mock_thread:
            await mod.mem0_add("ch1", "u1", "hello", "hi there")

    mock_thread.assert_awaited_once()
    call_args = mock_thread.call_args
    assert call_args.args[0] == mock_mem0.add
    assert call_args.kwargs["user_id"] == "u1"
    assert call_args.kwargs["agent_id"] == "ch1"


async def test_mem0_add_failure_swallowed():
    import ocl.memory.mem0_store as mod
    mock_mem0 = MagicMock()

    with patch.object(mod, "get_mem0", return_value=mock_mem0):
        with patch.object(mod.asyncio, "to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.side_effect = RuntimeError("boom")
            await mod.mem0_add("ch1", "u1", "hello", "hi")


async def test_mem0_search_returns_formatted():
    import ocl.memory.mem0_store as mod
    mock_mem0 = MagicMock()
    results = [{"memory": "User likes cats"}, {"memory": "User is from Beijing"}]

    with patch.object(mod, "get_mem0", return_value=mock_mem0):
        with patch.object(mod.asyncio, "to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = results
            out = await mod.mem0_search("ch1", "u1", "what do I like?")

    assert out == "- User likes cats\n- User is from Beijing"


async def test_mem0_search_empty_returns_empty_string():
    import ocl.memory.mem0_store as mod
    mock_mem0 = MagicMock()

    with patch.object(mod, "get_mem0", return_value=mock_mem0):
        with patch.object(mod.asyncio, "to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = []
            out = await mod.mem0_search("ch1", "u1", "anything")

    assert out == ""


async def test_mem0_search_failure_returns_empty_string():
    import ocl.memory.mem0_store as mod
    mock_mem0 = MagicMock()

    with patch.object(mod, "get_mem0", return_value=mock_mem0):
        with patch.object(mod.asyncio, "to_thread", new_callable=AsyncMock) as mock_thread:
            mock_thread.side_effect = Exception("network error")
            out = await mod.mem0_search("ch1", "u1", "anything")

    assert out == ""
