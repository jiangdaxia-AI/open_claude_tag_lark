"""ocl doctor — health check command.

Usage: ocl doctor

Checks:
  1. Feishu credentials (app_id, app_secret, tenant_id)
  2. LLM configuration (model, api_base, api_key)
  3. Data directory access
  4. SQLite databases (messages, tasks, reminders, ledger)
  5. Agent template (agents.toml exists and parses)
  6. MCP server configs (if any)
  7. Network connectivity to Feishu API
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from ocl.config import settings


def run_doctor() -> int:
    """Run all health checks. Returns exit code (0=all pass, 1=some fail)."""
    checks_passed = 0
    checks_failed = 0
    warnings = 0

    def ok(msg: str) -> None:
        nonlocal checks_passed
        print(f"  ✓ {msg}")
        checks_passed += 1

    def fail(msg: str) -> None:
        nonlocal checks_failed
        print(f"  ✗ {msg}")
        checks_failed += 1

    def warn(msg: str) -> None:
        nonlocal warnings
        print(f"  ⚠ {msg}")
        warnings += 1

    # 1. Feishu credentials
    print("\n📋 Feishu Configuration:")
    if settings.feishu_app_id:
        ok(f"FEISHU_APP_ID = {settings.feishu_app_id[:16]}...")
    else:
        fail("FEISHU_APP_ID is not set")
    if settings.feishu_app_secret:
        ok("FEISHU_APP_SECRET is set")
    else:
        fail("FEISHU_APP_SECRET is not set")
    if settings.feishu_tenant_id:
        ok(f"FEISHU_TENANT_ID = {settings.feishu_tenant_id}")
    else:
        fail("FEISHU_TENANT_ID is not set")

    # 2. LLM configuration
    print("\n🤖 LLM Configuration:")
    ok(f"LLM_MODEL = {settings.llm_model}")
    if settings.llm_api_base:
        ok(f"LLM_API_BASE = {settings.llm_api_base}")
    else:
        warn("LLM_API_BASE not set (using provider default)")
    if settings.llm_api_key:
        ok("LLM_API_KEY is set")
    elif settings.anthropic_api_key:
        ok("ANTHROPIC_API_KEY is set")
    elif settings.openai_api_key:
        ok("OPENAI_API_KEY is set")
    else:
        fail("No LLM API key found")

    # 3. Data directory
    print("\n📁 Data Directory:")
    data_dir = Path(settings.data_dir)
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        ok(f"Data dir: {data_dir.resolve()}")
    except Exception as e:
        fail(f"Cannot access data dir {data_dir}: {e}")

    # Check channels dir
    channels_dir = data_dir / "channels"
    if channels_dir.exists():
        channel_count = sum(1 for d in channels_dir.iterdir() if d.is_dir())
        ok(f"Channels dir exists ({channel_count} channels)")
    else:
        warn("Channels dir does not exist yet (created on first message)")

    # Check templates
    templates_dir = data_dir / "templates"
    agents_toml = templates_dir / "agents.toml"
    if agents_toml.exists():
        try:
            import toml
            data = toml.loads(agents_toml.read_text(encoding="utf-8"))
            agent_count = len(data.get("agent", []))
            ok(f"Agent template: {agent_count} agents defined")
            for a in data.get("agent", []):
                if not a.get("feishu_app_id"):
                    warn(f"Agent '{a.get('id')}' has no feishu_app_id")
        except Exception as e:
            fail(f"Failed to parse agents.toml: {e}")
    else:
        warn("No global template at data/templates/agents.toml")

    # 4. SQLite databases
    print("\n💾 Databases:")
    ws_dir = data_dir / "workspaces"
    for db_name in ["messages.db", "tasks.db", "reminders.db", "ledger.db"]:
        db_path = ws_dir / db_name
        if db_path.exists():
            ok(f"{db_name} exists ({db_path.stat().st_size // 1024}KB)")
        else:
            warn(f"{db_name} not created yet")

    # 5. MCP configs
    print("\n🔌 MCP Servers:")
    try:
        from ocl.tools.mcp_config import load_all_channel_mcp_configs
        configs = load_all_channel_mcp_configs()
        if configs:
            for ch_id, servers in configs.items():
                ok(f"Channel {ch_id[:16]}...: {len(servers)} MCP server(s)")
        else:
            ok("No MCP servers configured (optional)")
    except Exception as e:
        warn(f"MCP config check skipped: {e}")

    # 6. Network connectivity (quick check)
    print("\n🌐 Network:")
    try:
        import httpx
        resp = httpx.get("https://open.feishu.cn", timeout=5)
        if resp.status_code < 500:
            ok(f"Feishu API reachable (HTTP {resp.status_code})")
        else:
            fail(f"Feishu API returned HTTP {resp.status_code}")
    except Exception as e:
        fail(f"Cannot reach Feishu API: {e}")

    # Summary
    print(f"\n{'='*40}")
    print(f"  ✓ Passed: {checks_passed}")
    print(f"  ⚠ Warnings: {warnings}")
    print(f"  ✗ Failed: {checks_failed}")
    print(f"{'='*40}\n")

    return 0 if checks_failed == 0 else 1
