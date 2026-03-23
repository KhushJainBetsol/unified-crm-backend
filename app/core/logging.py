"""
app/core/log_config.py

Simple logging setup using Python's built-in logging module.
No extra packages required.
"""

import logging
import sys

from app.core.settings import get_settings

settings = get_settings()


def configure_logging() -> None:
    """Configure root logger for the application."""
    level = logging.DEBUG if settings.DEBUG else logging.INFO

    logging.basicConfig(
        stream=sys.stdout,
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Silence noisy third-party loggers
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if settings.DEBUG else logging.WARNING
    )
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def get_logger(name: str = __name__) -> logging.Logger:
    return logging.getLogger(name)
