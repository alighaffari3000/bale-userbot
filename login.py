#!/usr/bin/env python
"""One-time interactive login for the personal Bale account.

Runs BaleClient's phone/OTP CLI (phone number -> OTP -> JWT) and stores the
result in the configured session file. Run it once, on a machine where you can
type the OTP; the bot itself never asks for credentials.
"""

import asyncio
import sys

from balebot.app import build_client, prepare_session_file
from balebot.config import load_config
from balebot.logging_setup import setup_logging


async def main() -> int:
    config = load_config()
    setup_logging(config.log_level)
    prepare_session_file(config.session_file)

    if config.session_file.exists():
        answer = input(
            f"A session already exists at {config.session_file}. Replace it? [y/N] "
        )
        if answer.strip().lower() not in ("y", "yes"):
            print("Keeping the existing session.")
            return 0
        config.session_file.unlink()

    client = build_client(config, dispatcher=None)
    # No session file -> this walks the phone/OTP flow and writes session.bale.
    await client._ensure_token_exists()
    prepare_session_file(config.session_file)

    print(f"\nLogged in as user id {client.id}. Session saved to {config.session_file}")
    print("Keep that file secret — it is the credential for your Bale account.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(1)
