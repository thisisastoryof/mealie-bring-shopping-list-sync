import logging
import sys

import structlog

from app.config import settings


def configure_logging() -> None:
    """Configure structlog for human-readable console output with levels.

    Swap ``ConsoleRenderer`` for ``JSONRenderer`` if you ship logs to a collector.
    """
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)

    # httpx/httpcore emit one INFO line per HTTP request. Next to our structured
    # action logs that's just transport noise, so silence them unless the app is
    # explicitly in DEBUG (where the request trace is useful for troubleshooting).
    http_level = logging.DEBUG if level <= logging.DEBUG else logging.WARNING
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(http_level)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
