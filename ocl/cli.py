"""CLI entry point.

Usage:
  open-claude-tag-lark              — start the Feishu gateway (default)
  open-claude-tag-lark doctor       — run health checks
"""

import logging
import sys

import structlog


def main() -> None:
    # Configure standard logging FIRST — many modules use logging.getLogger()
    # and without a handler, their output is silently discarded.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stderr,
    )

    # Force Feishu WebSocket to bypass system proxy (xray/clash on localhost:10808).
    # websockets 15 reads system proxy settings even when proxy=None is passed.
    # Adding NO_PROXY ensures msg-frontier.feishu.cn connects directly.
    import os as _os
    _no_proxy = _os.environ.get("NO_PROXY", "")
    feishu_hosts = "msg-frontier.feishu.cn,open.feishu.cn,*.feishu.cn,*.larksuite.com"
    if feishu_hosts not in _no_proxy:
        _os.environ["NO_PROXY"] = f"{_no_proxy},{feishu_hosts}" if _no_proxy else feishu_hosts
        _os.environ["no_proxy"] = _os.environ["NO_PROXY"]

    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(),
    )

    # Subcommand support
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "doctor":
            from ocl.doctor import run_doctor
            sys.exit(run_doctor())
        elif cmd == "--help" or cmd == "-h":
            print("Usage: open-claude-tag-lark [doctor]  — start gateway or run health check")
            sys.exit(0)

    # Default: start the Feishu gateway
    # ws_client.start() is synchronous and blocking by design — lark_oapi
    # captures its own event loop at module import, so we must NOT wrap this
    # in asyncio.run().
    from ocl.gateway.feishu.ws_client import start

    start()


if __name__ == "__main__":
    main()
