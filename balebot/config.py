"""Environment-driven settings for the Bale AI chatbot.

Every value is configuration; nothing about a particular account, phone number
or system prompt is hard-coded here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful assistant replying inside the Bale messenger. "
    "Answer in the same language the user writes in. "
    "Keep replies short and concrete; Bale is a chat app, not a document editor."
)


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc


def _ids(name: str) -> frozenset[int]:
    raw = os.getenv(name, "")
    ids = set()
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            ids.add(int(chunk))
        except ValueError as exc:
            raise ValueError(
                f"{name} must be a comma-separated list of user ids"
            ) from exc
    return frozenset(ids)


@dataclass(frozen=True)
class Config:
    # --- Bale -------------------------------------------------------------
    session_file: Path
    proxy: str | None
    allowed_user_ids: frozenset[int]
    reply_mode: str  # "answer" (new message) or "reply" (quoted reply)
    typing_indicator: bool
    handle_groups: bool

    # --- LLM --------------------------------------------------------------
    gemini_api_key: str
    gemini_model: str
    temperature: float
    max_output_tokens: int
    system_prompt: str

    # --- Storage ----------------------------------------------------------
    db_path: Path
    max_history_turns: int

    # --- Runtime ----------------------------------------------------------
    log_level: str
    log_message_text: bool
    max_input_chars: int
    max_reply_chars: int
    busy_notice: str = field(
        default="⏳ هنوز مشغول پاسخ قبلی‌ام؛ چند لحظه صبر کن."
    )

    @property
    def has_allowlist(self) -> bool:
        return bool(self.allowed_user_ids)


def _read_system_prompt() -> str:
    prompt_file = os.getenv("SYSTEM_PROMPT_FILE", "").strip()
    if prompt_file:
        path = Path(prompt_file).expanduser()
        if not path.is_file():
            raise ValueError(f"SYSTEM_PROMPT_FILE does not exist: {path}")
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
    inline = os.getenv("SYSTEM_PROMPT", "").strip()
    return inline or DEFAULT_SYSTEM_PROMPT


def load_config(env_file: str | os.PathLike[str] | None = None) -> Config:
    """Load settings from the process environment (and a .env beside the code)."""
    if env_file is None:
        env_file = Path(__file__).resolve().parent.parent / ".env"
    load_dotenv(env_file, override=False)

    api_key = (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
    if not api_key:
        raise ValueError(
            "GEMINI_API_KEY is not set. Put it in bale_chatbot/.env or export it."
        )

    reply_mode = os.getenv("REPLY_MODE", "answer").strip().lower()
    if reply_mode not in ("answer", "reply"):
        raise ValueError("REPLY_MODE must be either 'answer' or 'reply'")

    base = Path(__file__).resolve().parent.parent
    session_file = Path(
        os.getenv("BALE_SESSION_FILE", str(base / "data" / "session.bale"))
    ).expanduser()
    db_path = Path(os.getenv("DB_PATH", str(base / "data" / "chat.db"))).expanduser()

    return Config(
        session_file=session_file,
        proxy=(os.getenv("BALE_PROXY") or "").strip() or None,
        allowed_user_ids=_ids("BALE_ALLOWED_USER_IDS"),
        reply_mode=reply_mode,
        typing_indicator=_bool("TYPING_INDICATOR", True),
        handle_groups=_bool("HANDLE_GROUPS", False),
        gemini_api_key=api_key,
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip(),
        temperature=_float("LLM_TEMPERATURE", 0.7),
        max_output_tokens=_int("LLM_MAX_OUTPUT_TOKENS", 1024),
        system_prompt=_read_system_prompt(),
        db_path=db_path,
        max_history_turns=_int("MAX_HISTORY_TURNS", 12),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
        log_message_text=_bool("LOG_MESSAGE_TEXT", False),
        max_input_chars=_int("MAX_INPUT_CHARS", 4000),
        max_reply_chars=_int("MAX_REPLY_CHARS", 3500),
    )
