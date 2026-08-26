"""Logging setup used by the app runner and the CLI."""

from __future__ import annotations

import logging
import sys

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format=_FORMAT,
        stream=sys.stdout,
        force=True,
    )
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
