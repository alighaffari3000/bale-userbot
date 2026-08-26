"""Logging setup used by the app runner and the CLI."""

from __future__ import annotations

import logging
import sys
from typing import TextIO

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def setup_logging(level: str = "INFO", stream: TextIO | None = None) -> None:
    """Configure the root logger.

    `stream` exists for the CLI: a command that prints JSON on stdout has to
    put its log lines somewhere else, or the output is not parseable.
    """
    # getattr on a lowercase name finds logging.debug/info/... — the module's
    # convenience *functions* — which basicConfig then rejects with TypeError.
    level_no = getattr(logging, str(level).upper(), logging.INFO)
    logging.basicConfig(
        level=level_no if isinstance(level_no, int) else logging.INFO,
        format=_FORMAT,
        stream=stream or sys.stdout,
        force=True,
    )
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
