"""Logging setup used by the app runner and the CLI."""

from __future__ import annotations

import logging
import sys

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def setup_logging(level: str = "INFO") -> None:
    # getattr on a lowercase name finds logging.debug/info/... — the module's
    # convenience *functions* — which basicConfig then rejects with TypeError.
    level_no = getattr(logging, str(level).upper(), logging.INFO)
    logging.basicConfig(
        level=level_no if isinstance(level_no, int) else logging.INFO,
        format=_FORMAT,
        stream=sys.stdout,
        force=True,
    )
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
