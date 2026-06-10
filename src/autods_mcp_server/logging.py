"""structlog configuration.

JSON renderer in non-local environments (machine-parseable for the log
shipper). Pretty console renderer in local for developer ergonomics.
"""

import logging
import sys

import structlog

from autods_mcp_server.settings import Settings


def resolve_level(settings: Settings) -> int:
    """Map the configured ``LOG_LEVEL`` string to a stdlib level int.

    Unknown values fall back to INFO. Shared with the app factory so the
    "are we in debug mode?" decision is made the same way everywhere.
    """
    return logging.getLevelNamesMapping().get(settings.log_level.upper(), logging.INFO)


def configure_logging(settings: Settings) -> None:
    """Configure stdlib logging + structlog once at app startup.

    Safe to call multiple times — structlog.configure() replaces config,
    and the stdlib handler is reset deterministically.
    """
    level = resolve_level(settings)

    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(handler)
    root.setLevel(level)

    # Silence third-party loggers whose INFO output duplicates our own
    # structured lines or is pure lifecycle noise:
    #   - uvicorn.access mirrors RequestContextMiddleware's "request" log;
    #   - httpx's "HTTP Request: ..." mirrors the tool_call audit line
    #     (upstream_url/upstream_status/latency_ms);
    #   - the mcp SDK ("StreamableHTTP session manager started", "Processing
    #     request of type CallToolRequest", "Terminating session: ...") —
    #     CallToolRequest overlaps the tool_call audit; the rest is session
    #     lifecycle chatter. Setting the parent "mcp" logger covers its
    #     mcp.server.* children, which inherit the level.
    # Keep them only when explicitly debugging (LOG_LEVEL=debug); otherwise
    # raise to WARNING so genuine problems still surface.
    noisy_level = level if level <= logging.DEBUG else logging.WARNING
    for noisy in ("uvicorn.access", "httpx", "mcp"):
        logging.getLogger(noisy).setLevel(noisy_level)

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if settings.is_local:
        renderer: structlog.types.Processor = structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name) if name else structlog.get_logger()
