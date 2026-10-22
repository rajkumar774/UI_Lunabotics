"""Application-wide structured logging module."""

import logging
import sys
from typing import Optional

_DEFAULT_FORMAT = "[%(asctime)s] [%(levelname)s] [%(name)s]: %(message)s"
_DATE_FORMAT = "%H:%M:%S"


def setup_logger(
    name: str = "mission_control",
    level: str = "INFO",
    log_format: Optional[str] = None,
) -> logging.Logger:
    """Configures and returns the root application logger.

    Args:
        name: Root logger name.
        level: Log level string (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        log_format: Optional custom format string.

    Returns:
        Configured logging.Logger instance.
    """
    logger = logging.getLogger(name)
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logger.setLevel(numeric_level)

    # Avoid duplicate handlers if setup_logger is invoked multiple times
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(numeric_level)
        formatter = logging.Formatter(
            log_format or _DEFAULT_FORMAT,
            datefmt=_DATE_FORMAT,
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return logger


def get_logger(module_name: str) -> logging.Logger:
    """Returns a child logger scoped to the specified module name."""
    return logging.getLogger(f"mission_control.{module_name}")
