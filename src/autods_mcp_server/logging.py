"""structlog configuration.

JSON renderer in non-local environments (machine-parseable for the log
shipper). Pretty console renderer in local for developer ergonomics.
"""

import logging
import sys

import structlog

from autods_mcp_server.settings import Settings


def configure_logging(settings: Settings) -> None:
    """Configure stdlib logging + structlog once at app startup.

    Safe to call multiple times — structlog.configure() replaces config,
    and the stdlib handler is reset deterministically.
    """
    level = logging.getLevelNamesMapping().get(settings.log_level.upper(), logging.INFO)

    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(handler)
    root.setLevel(level)

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
