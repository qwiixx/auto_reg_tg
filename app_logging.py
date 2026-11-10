"""Console logging for the registration service."""

import asyncio
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import uuid4

APP_LOGGERS = ("app_logging", "registration", "number_provider", "panel_jobs", "providers.hero_sms")
_attempt_id: ContextVar[str] = ContextVar("attempt_id", default="-")
logger = logging.getLogger(__name__)


class _ApplicationLogs(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.name not in APP_LOGGERS:
            return False
        record.attempt = _attempt_id.get()
        return True


def configure_logging() -> None:
    name = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    level = logging.getLevelNamesMapping().get(name, logging.INFO)
    handler = logging.StreamHandler()
    handler.addFilter(_ApplicationLogs())
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s [attempt=%(attempt)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[handler],
    )
    for name in APP_LOGGERS:
        logging.getLogger(name).setLevel(level)


@contextmanager
def log_attempt() -> Iterator[None]:
    token = _attempt_id.set(uuid4().hex[:12])
    try:
        yield
    except asyncio.CancelledError:
        logger.info("Registration attempt cancelled")
        raise
    finally:
        _attempt_id.reset(token)
