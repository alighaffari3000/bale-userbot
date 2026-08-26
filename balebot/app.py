"""Application wiring: config -> storage -> llm -> dispatcher -> client."""

from __future__ import annotations

import logging
import os
import stat
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path

from baleclient import Client, Dispatcher

from .client import ChatClient
from .config import Config, load_config
from .handlers import build_router
from .llm import GeminiLLM
from .logging_setup import setup_logging
from .storage import ConversationStore

logger = logging.getLogger(__name__)

LifespanType = Callable[[Client], AbstractAsyncContextManager[None]]


class SessionMissingError(RuntimeError):
    """Raised when the bot is started before an interactive login was done."""


def prepare_session_file(path: Path) -> None:
    """Make sure the session directory exists and the file is owner-only.

    `session.bale` holds the account JWT: whoever reads it is the account.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        except OSError as exc:
            logger.warning("Could not tighten permissions on %s: %s", path, exc)


def build_client(
    config: Config,
    dispatcher: Dispatcher | None,
    lifespan: LifespanType | None = None,
) -> ChatClient:
    prepare_session_file(config.session_file)
    return ChatClient(
        dispatcher=dispatcher,
        session_file=config.session_file,
        proxy=config.proxy,
        lifespan=lifespan,
    )


async def run(config: Config | None = None) -> None:
    """Start the chatbot and keep it running until the process is stopped."""
    config = config or load_config()
    setup_logging(config.log_level)

    if not config.session_file.exists():
        raise SessionMissingError(
            f"No Bale session at {config.session_file}. "
            "Run `python login.py` once to authenticate with your phone number."
        )

    store = ConversationStore(config.db_path)
    await store.init()

    llm = GeminiLLM(config)

    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(config, store, llm))

    @asynccontextmanager
    async def lifespan(client: ChatClient):
        logger.info(
            "Bale chatbot online as user %s (model=%s, history=%s turns, groups=%s)",
            client.id,
            config.gemini_model,
            config.max_history_turns,
            config.handle_groups,
        )
        try:
            yield
        finally:
            logger.info("Bale chatbot shutting down")

    client = build_client(config, dispatcher, lifespan)

    # `Client.start()` owns the reconnect loop: on any transport failure it
    # cleans the session up, waits, reconnects and re-handshakes.
    await client.start()
    await client.stop()
