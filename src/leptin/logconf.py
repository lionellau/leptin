"""Structured logging for Leptin.

Logs go to **stderr** only — stdout is the MCP JSON-RPC channel and must stay
clean. The level is controlled by ``LEPTIN_LOG`` (DEBUG | INFO | WARNING |
ERROR; default WARNING), so a production deployment is quiet by default and a
developer can opt into detail without code changes.
"""

from __future__ import annotations

import logging
import os
import sys

_CONFIGURED = False
_warned_once: set[str] = set()


def _configure() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    level_name = os.environ.get("LEPTIN_LOG", "WARNING").upper()
    level = getattr(logging, level_name, logging.WARNING)
    logger = logging.getLogger("leptin")
    logger.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("[leptin] %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    _CONFIGURED = True


def get_logger(name: str = "leptin") -> logging.Logger:
    _configure()
    return logging.getLogger(name if name.startswith("leptin") else f"leptin.{name}")


def warn_once(key: str, message: str) -> None:
    """Emit a warning at most once per process (for repeated degradation paths)."""
    if key in _warned_once:
        return
    _warned_once.add(key)
    get_logger().warning(message)
