"""Unit tests for curation_acompletion in ocl/llm.py."""

from unittest.mock import AsyncMock, patch

import pytest

from ocl.llm import curation_acompletion


@pytest.fixture(autouse=True)
def _reset_settings(monkeypatch):
    """Ensure settings are clean between tests."""
    from ocl import config
    monkeypatch.setattr(config.settings, "curation_model", "")
    monkeypatch.setattr(config.settings, "curation_api_base", "")
    monkeypatch.setattr(config.settings, "curation_api_key", "")
    yield


async def test_curation_falls_back_to_acompletion(monkeypatch):
    """When curation_model is empty, curation_acompletion delegates to acompletion."""
    mock = AsyncMock(return_value="response")
    with patch("ocl.llm.acompletion", mock):
        await curation_acompletion(messages=[{"role": "user", "content": "hi"}])
    mock.assert_awaited_once()


async def test_curation_uses_independent_model(monkeypatch):
    """Configured curation_model uses independent model + api_base."""
    from ocl import config
    monkeypatch.setattr(config.settings, "curation_model", "openai/gpt-4o-mini")
    monkeypatch.setattr(config.settings, "curation_api_base", "https://api.example.com/v1")
    monkeypatch.setattr(config.settings, "curation_api_key", "sk-test")

    mock = AsyncMock(return_value="response")
    with patch("litellm.acompletion", mock):
        await curation_acompletion(messages=[{"role": "user", "content": "hi"}])

    call_kwargs = mock.call_args.kwargs
    assert call_kwargs["model"] == "gpt-4o-mini"
    assert call_kwargs["api_base"] == "https://api.example.com/v1"
    assert call_kwargs["custom_llm_provider"] == "openai"
    assert call_kwargs["api_key"] == "sk-test"


async def test_curation_no_channel_id_leak(monkeypatch):
    """channel_id kwarg must never reach litellm.acompletion."""
    from ocl import config
    monkeypatch.setattr(config.settings, "curation_model", "gpt-4o-mini")
    monkeypatch.setattr(config.settings, "curation_api_base", "https://api.example.com/v1")

    mock = AsyncMock(return_value="response")
    with patch("litellm.acompletion", mock):
        # curation_acompletion does not accept channel_id — this tests that
        # callers cannot accidentally pass it through
        await curation_acompletion(messages=[{"role": "user", "content": "hi"}])

    call_kwargs = mock.call_args.kwargs
    assert "channel_id" not in call_kwargs
