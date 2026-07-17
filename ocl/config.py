"""Global settings loaded from environment / .env file."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Feishu (lark) — credentials from https://open.feishu.cn/app
    feishu_app_id: str = ""             # required — validated at startup in ws_client.start()
    feishu_app_secret: str = ""
    feishu_bot_open_id: str = ""        # optional — if empty, no @bot filtering
    feishu_tenant_id: str = ""          # required — validated at startup in ws_client.start()

    # LLM — LiteLLM model string, e.g. "gpt-4o", "claude-sonnet-4-6",
    # "deepseek/deepseek-chat", "gemini/gemini-2.0-flash"
    # For an OpenAI-compatible gateway, set llm_model to "openai/<model_id>"
    # and provide llm_api_base + llm_api_key.
    llm_model: str = "claude-sonnet-4-6"
    llm_api_base: str = ""          # OpenAI-compatible base URL (optional)
    llm_api_key: str = ""           # API key for the custom gateway (optional)
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    deepseek_api_key: str = ""
    gemini_api_key: str = ""
    groq_api_key: str = ""

    # Vision — dedicated multimodal model (only used when images are sent)
    vision_model: str = ""
    vision_api_base: str = ""
    vision_api_key: str = ""

    # Tavily web search
    tavily_key: str = ""

    # Storage
    data_dir: Path = Path("./data")

    # Limits
    default_max_tokens_per_request: int = 50_000
    default_max_tokens_per_day: int = 500_000
    context_window_messages: int = 20

    # Heartbeat — optional separate (cheaper) model for proactive monitoring
    heartbeat_model: str = ""
    heartbeat_api_base: str = ""
    heartbeat_api_key: str = ""

    # Memory compaction
    memory_max_tokens: int = 2000
    memory_compact_threshold: float = 0.8
    memory_expiry_days_p1: int = 365
    memory_expiry_days_p2: int = 60
    memory_expiry_days_p3: int = 14

    # Curation model (independent)
    curation_model: str = ""
    curation_api_base: str = ""
    curation_api_key: str = ""

    # Mem0 semantic recall
    mem0_enabled: bool = False
    mem0_llm_model: str = ""
    mem0_llm_api_base: str = ""
    mem0_llm_api_key: str = ""
    mem0_embedder_model: str = "BAAI/bge-small-zh-v1.5"
    mem0_embedder_api_base: str = ""
    mem0_embedder_api_key: str = ""
    mem0_vector_store_path: str = ""
    mem0_search_top_k: int = 5

    # Layered memory (memU-style: global / task / working layers)
    memory_layered_enabled: bool = False
    memory_embedder_api_base: str = "https://api.siliconflow.cn/v1"
    memory_embedder_model: str = "BAAI/bge-m3"
    memory_embedder_api_key: str = ""
    memory_retrieve_top_k: int = 8
    memory_inject_max_chars: int = 3000

    # Web admin server (task board + agent management)
    web_admin_enabled: bool = False
    web_admin_host: str = "0.0.0.0"
    web_admin_port: int = 8765

    # Agent lifecycle
    agent_idle_timeout_seconds: int = 600

    # OpenSandbox — code execution sandbox
    sandbox_enabled: bool = False
    sandbox_domain: str = "127.0.0.1:8079"
    sandbox_protocol: str = "http"
    sandbox_image: str = "opensandbox/code-interpreter:v1.1.0"
    sandbox_timeout_minutes: int = 30

    @property
    def channels_dir(self) -> Path:
        return self.data_dir / "channels"

    @property
    def templates_dir(self) -> Path:
        """Global templates directory — one agents.toml defines all Bot agents.
        When a new channel receives its first message, the config is auto-initialized
        from this template. No per-channel manual setup needed.
        
        Located at channels/templates/ (project-level, shipped with the repo)
        so that a fresh clone has the templates available without needing
        runtime data to exist first."""
        # Project root is two levels up from this file (ocl/config.py)
        project_root = Path(__file__).resolve().parent.parent
        return project_root / "channels" / "templates"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "ocl.db"

    @property
    def agents_dir(self) -> Path:
        """Root directory for per-agent configs (under channels/<id>/agents/)."""
        return self.channels_dir


settings = Settings()  # type: ignore[call-arg]
