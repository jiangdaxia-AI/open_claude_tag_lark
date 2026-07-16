"""LiteLLM helpers — key injection, model resolution, per-channel overrides.

LiteLLM reads provider keys from os.environ, not from arbitrary Python objects.
Call configure() once at startup to sync settings → os.environ.

Per-channel model override: add a line to CHANNEL.md frontmatter:
    llm_model: gpt-4o
or set LLM_MODEL per channel in tools.toml:
    [llm]
    model = "gpt-4o"
"""

from __future__ import annotations

import logging
import os

import litellm
import toml

from ocl.config import settings

logger = logging.getLogger(__name__)

# Suppress LiteLLM's verbose success logs
litellm.suppress_debug_info = True
litellm.set_verbose = False
litellm.drop_params = True
# Disable LiteLLM's internal logging callbacks that produce noisy error traces
# (the "Error creating standard logging object" errors come from its callback
# system trying to access TypedDict __annotations__ which fails on some versions)
litellm.success_callback = []
litellm.failure_callback = []
litellm._async_success_callback = []
litellm._async_failure_callback = []
# Suppress the pydantic serialization warnings from litellm's response models
import warnings as _warnings
_warnings.filterwarnings("ignore", message="Pydantic serializer warnings")
_warnings.filterwarnings("ignore", category=UserWarning, module="litellm")


def configure() -> None:
    """Sync API keys from settings → os.environ so LiteLLM can pick them up."""
    # Silence litellm's noisy internal loggers (ERROR-level traces from its
    # logging callback system that don't affect actual LLM calls)
    logging.getLogger("LiteLLM").setLevel(logging.CRITICAL)
    logging.getLogger("litellm").setLevel(logging.CRITICAL)
    logging.getLogger("litellm.litellm_core_utils").setLevel(logging.CRITICAL)

    _set_if_nonempty("ANTHROPIC_API_KEY", settings.anthropic_api_key)
    _set_if_nonempty("OPENAI_API_KEY", settings.openai_api_key)
    _set_if_nonempty("DEEPSEEK_API_KEY", settings.deepseek_api_key)
    _set_if_nonempty("GEMINI_API_KEY", settings.gemini_api_key)
    _set_if_nonempty("GROQ_API_KEY", settings.groq_api_key)

    logger.info("LLM configured — default model: %s", settings.llm_model)
    _log_available_providers()


def _set_if_nonempty(env_var: str, value: str) -> None:
    if value and not os.environ.get(env_var):
        os.environ[env_var] = value


def _log_available_providers() -> None:
    providers = []
    if os.environ.get("ANTHROPIC_API_KEY"):
        providers.append("Anthropic")
    if os.environ.get("OPENAI_API_KEY"):
        providers.append("OpenAI")
    if os.environ.get("DEEPSEEK_API_KEY"):
        providers.append("DeepSeek")
    if os.environ.get("GEMINI_API_KEY"):
        providers.append("Gemini")
    if os.environ.get("GROQ_API_KEY"):
        providers.append("Groq")
    if providers:
        logger.info("Available LLM providers: %s", ", ".join(providers))
    else:
        logger.warning("No LLM provider API keys found — set at least one in .env")


def resolve_model(channel_id: str | None = None) -> str:
    """Return the model to use, respecting per-channel overrides.

    Override order (highest wins):
      1. tools.toml [llm] model = "..." in channel dir
      2. LLM_MODEL env var / settings.llm_model
    """
    if channel_id:
        override = _channel_model_override(channel_id)
        if override:
            return override
    return settings.llm_model


def _channel_model_override(channel_id: str) -> str | None:
    tools_toml = settings.channels_dir / channel_id / "tools.toml"
    if not tools_toml.exists():
        return None
    try:
        config = toml.loads(tools_toml.read_text(encoding="utf-8"))
        return config.get("llm", {}).get("model") or None
    except Exception:
        return None


async def acompletion(channel_id: str | None = None, **kwargs):
    """Thin wrapper around litellm.acompletion that injects the resolved model.

    If llm_api_base is configured, routes through the OpenAI-compatible gateway
    (same pattern as vision_acompletion / curation_acompletion).
    """
    kwargs.setdefault("model", resolve_model(channel_id))

    if settings.llm_api_base:
        kwargs["api_base"] = settings.llm_api_base
        kwargs["custom_llm_provider"] = "openai"
        model_name = kwargs["model"]
        # Strip LiteLLM provider prefix (e.g. "openai/qwen3.5-flash" -> "qwen3.5-flash")
        if "/" in model_name:
            kwargs["model"] = model_name.split("/", 1)[1]
        if settings.llm_api_key:
            kwargs["api_key"] = settings.llm_api_key

    return await litellm.acompletion(**kwargs)


async def acompletion_stream(channel_id: str | None = None, **kwargs):
    """Streaming version of acompletion. Yields LiteLLM stream chunks.

    Same model/api_base/key injection logic as acompletion, but with stream=True.
    """
    kwargs.setdefault("model", resolve_model(channel_id))
    kwargs["stream"] = True

    if settings.llm_api_base:
        kwargs["api_base"] = settings.llm_api_base
        kwargs["custom_llm_provider"] = "openai"
        model_name = kwargs["model"]
        if "/" in model_name:
            kwargs["model"] = model_name.split("/", 1)[1]
        if settings.llm_api_key:
            kwargs["api_key"] = settings.llm_api_key

    async for chunk in await litellm.acompletion(**kwargs):
        yield chunk


async def vision_acompletion(channel_id: str | None = None, **kwargs):
    """Call the dedicated vision model for multimodal (image) requests.

    Falls back to the regular acompletion if no vision model is configured.
    The channel_id is consumed here and never forwarded to litellm —
    litellm would pass it through to the underlying OpenAI client, which
    raises TypeError on the unknown kwarg.
    """
    if not settings.vision_model:
        return await acompletion(channel_id=channel_id, **kwargs)

    model_name = settings.vision_model
    kwargs["model"] = model_name

    if settings.vision_api_base:
        kwargs["api_base"] = settings.vision_api_base
        kwargs["custom_llm_provider"] = "openai"
        # Strip LiteLLM provider prefix (e.g. "openai/gpt-4o" -> "gpt-4o")
        if "/" in model_name:
            kwargs["model"] = model_name.split("/", 1)[1]

    if settings.vision_api_key:
        kwargs["api_key"] = settings.vision_api_key

    return await litellm.acompletion(**kwargs)


async def curation_acompletion(**kwargs):
    """Call the dedicated curation model for memory compaction/curation tasks.

    Falls back to the regular acompletion if no curation model is configured.
    Unlike vision_acompletion, this does not accept channel_id — curation is
    a background task not tied to a specific channel.
    """
    if not settings.curation_model:
        return await acompletion(**kwargs)

    model_name = settings.curation_model
    kwargs["model"] = model_name

    if settings.curation_api_base:
        kwargs["api_base"] = settings.curation_api_base
        kwargs["custom_llm_provider"] = "openai"
        # Strip LiteLLM provider prefix (e.g. "openai/gpt-4o" -> "gpt-4o")
        if "/" in model_name:
            kwargs["model"] = model_name.split("/", 1)[1]

    if settings.curation_api_key:
        kwargs["api_key"] = settings.curation_api_key

    return await litellm.acompletion(**kwargs)
