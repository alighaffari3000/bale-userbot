"""Settings, read from the environment. Nothing account-specific in code."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _ids(name: str) -> frozenset[int]:
    ids = set()
    for chunk in os.getenv(name, "").replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            ids.add(int(chunk))
        except ValueError as exc:
            raise ValueError(f"{name} must be a comma-separated list of ids") from exc
    return frozenset(ids)


@dataclass
class Config:
    """Runtime settings for a `BaleApp`."""

    session_file: Path = field(default_factory=lambda: Path("./data/session.bale"))
    proxy: str | None = None
    download_dir: Path = field(default_factory=lambda: Path("./data/downloads"))
    allowed_user_ids: frozenset[int] = frozenset()
    handle_private: bool = True
    handle_groups: bool = False
    ignore_self: bool = True
    serialize_per_chat: bool = True
    log_level: str = "INFO"
    log_message_text: bool = False

    def __post_init__(self) -> None:
        # BaleClient rewrites any session path whose suffix is not ".bale"
        # (Client.__init__). Matching it here keeps bale-userbot's existence
        # checks, chmod and `login --replace` on the file actually used.
        session = Path(self.session_file)
        if session.suffix.lower() != ".bale":
            object.__setattr__(self, "session_file", session.with_suffix(".bale"))

    @classmethod
    def from_env(cls, env_file: str | os.PathLike[str] | None = None) -> Config:
        if env_file is None:
            env_file = Path.cwd() / ".env"
        load_dotenv(env_file, override=False)
        return cls(
            session_file=Path(
                os.getenv("BALE_SESSION_FILE", "./data/session.bale")
            ).expanduser(),
            proxy=(os.getenv("BALE_PROXY") or "").strip() or None,
            download_dir=Path(
                os.getenv("BALE_DOWNLOAD_DIR", "./data/downloads")
            ).expanduser(),
            allowed_user_ids=_ids("BALE_ALLOWED_USER_IDS"),
            handle_private=_bool("HANDLE_PRIVATE", True),
            handle_groups=_bool("HANDLE_GROUPS", False),
            ignore_self=_bool("IGNORE_SELF", True),
            serialize_per_chat=_bool("SERIALIZE_PER_CHAT", True),
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
            log_message_text=_bool("LOG_MESSAGE_TEXT", False),
        )
