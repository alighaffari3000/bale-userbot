"""balekit — a thin infrastructure layer over BaleClient for personal Bale accounts.

Gives you: one login/session story, an event-driven app runner with reconnect,
one vocabulary for every message kind, and one way to send or download any
attachment. It adds no product behaviour of its own — what a message *means*
is your application's business.
"""

from .app import BaleApp, SessionMissingError, prepare_session_file
from .client import KitClient
from .config import Config
from .content import (
    MEDIA_KINDS,
    MediaInfo,
    MessageInfo,
    MessageKind,
    describe,
    kind_of_document,
)
from .media import (
    detect_kind,
    download,
    resend,
    send_media,
    suggest_filename,
)
from .routing import (
    ChatScope,
    FromUsers,
    InChats,
    IsMedia,
    Kind,
    NotSelf,
    wrap_handler,
)

__version__ = "0.2.0"

__all__ = (
    "BaleApp",
    "ChatScope",
    "Config",
    "FromUsers",
    "InChats",
    "IsMedia",
    "KitClient",
    "Kind",
    "MEDIA_KINDS",
    "MediaInfo",
    "MessageInfo",
    "MessageKind",
    "NotSelf",
    "SessionMissingError",
    "__version__",
    "describe",
    "detect_kind",
    "download",
    "kind_of_document",
    "prepare_session_file",
    "resend",
    "send_media",
    "suggest_filename",
    "wrap_handler",
)
