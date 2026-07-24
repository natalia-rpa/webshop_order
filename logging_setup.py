"""Application logging -> static/logs/app.log"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path


def setup_logging(
    log_file: str = "static/logs/app.log",
    log_level: str = "INFO",
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
) -> logging.Logger:
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("webshop_order")
    logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    logger.handlers.clear()
    logger.propagate = False

    formatter = logging.Formatter(
        "%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Match existing robots that include milliseconds in the timestamp.
    formatter.default_msec_format = "%s,%03d"

    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)

    return logger


def get_logger() -> logging.Logger:
    logger = logging.getLogger("webshop_order")
    if not logger.handlers:
        return setup_logging()
    return logger
