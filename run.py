#!/usr/bin/env python
"""Entrypoint: start the Bale AI chatbot."""

import asyncio
import logging
import sys

from balebot.app import SessionMissingError, run

if __name__ == "__main__":
    try:
        asyncio.run(run())
    except SessionMissingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)
    except ValueError as exc:  # configuration problems
        print(f"config error: {exc}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("Interrupted, exiting.")
