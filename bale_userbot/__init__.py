"""A thin infrastructure layer over BaleClient for personal Bale accounts.

Gives you: one login/session story, an event-driven app runner with reconnect,
one vocabulary for every message kind, and one way to send or download any
attachment. It adds no product behaviour of its own — what a message *means*
is your application's business.
"""

from .patches import apply_wire_fixes

# BaleClient 1.0.9 destroys received text messages during validation; repair
# its models before anything can parse. See bale_userbot/patches.py.
apply_wire_fixes()

from .app import BaleApp, SessionMissingError, prepare_session_file  # noqa: E402
from .client import KitClient  # noqa: E402
from .config import Config  # noqa: E402
from .content import (  # noqa: E402
    MEDIA_KINDS,
    ContactInfo,
    LocationInfo,
    MediaInfo,
    MessageInfo,
    MessageKind,
    StickerImage,
    StickerInfo,
    describe,
    kind_of_document,
)
from .extras import (  # noqa: E402
    contact_content,
    json_block_content,
    json_content,
    location_content,
    react,
    send_contact,
    send_content,
    send_location,
    send_sticker,
    set_typing,
    sticker_content,
    unreact,
)
from .media import (  # noqa: E402
    detect_kind,
    download,
    resend,
    send_media,
    suggest_filename,
)
from .routing import (  # noqa: E402
    ChatScope,
    FromUsers,
    InChats,
    IsMedia,
    Kind,
    NotSelf,
    wrap_handler,
)

__version__ = "0.4.0"

__all__ = (
    "BaleApp",
    "ChatScope",
    "Config",
    "ContactInfo",
    "contact_content",
    "FromUsers",
    "InChats",
    "IsMedia",
    "KitClient",
    "Kind",
    "LocationInfo",
    "MEDIA_KINDS",
    "MediaInfo",
    "MessageInfo",
    "MessageKind",
    "NotSelf",
    "SessionMissingError",
    "StickerImage",
    "StickerInfo",
    "__version__",
    "describe",
    "detect_kind",
    "download",
    "json_block_content",
    "json_content",
    "kind_of_document",
    "location_content",
    "prepare_session_file",
    "react",
    "resend",
    "send_contact",
    "send_content",
    "send_location",
    "send_media",
    "send_sticker",
    "set_typing",
    "sticker_content",
    "suggest_filename",
    "unreact",
    "wrap_handler",
)
