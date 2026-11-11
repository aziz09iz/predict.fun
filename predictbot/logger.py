"""Logging setup: rotating file at ``data/bot.log`` plus console output."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from .config import LOG_FILE

_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def setup_logging(level: str = "INFO") -> logging.Logger:
    """Create and return the root bot logger.

    Writes to both stderr and ``data/bot.log`` with rotation, so the log file
    never grows unbounded on a long-running host.
    """
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("predictbot")
    if logger.handlers:
        return logger

    logger.setLevel(level.upper() if level.upper() in _LEVELS else "INFO")

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)

    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    return logger