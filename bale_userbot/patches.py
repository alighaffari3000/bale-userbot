"""Repairs for `BaleClient 1.0.9` model validators, applied at import time.

`MessageContent._check_empty` (a pydantic before-validator) destroys incoming
messages parsed from wire dicts: it nulls field "15" — the text — so every
plain-text message read from history loses its body, and it turns field "5" —
the empty/forward flag — into `True` on mere *presence*, so a text message
this library itself sent (whose encoder writes an explicit `empty=0`) comes
back from history classified as a forward stub. Verified against a live
account: without this patch `describe()` returns FORWARD/UNKNOWN with no text
for received text messages; with it they parse correctly.

The replacement is strictly more permissive: it keeps "15" when it looks like
text (the schema-less decoder can deliver it as a dict or a bare string),
nulls it only for shapes `TextMessage` cannot hold, and reads "5" by value.

A second repair covers the same class of bug one level down: `ServiceMessage`
and `Thumbnail` declare required fields that real payloads can omit, and one
missing field raises out of `MessageContent`, which on the websocket path is
swallowed silently — the whole update vanishes. Those sub-blocks are nulled
instead, so the rest of the message still arrives.

Sending is untouched — outgoing content is built by field name and never
enters this code path with numeric keys.

Swapping the function inside `__pydantic_decorators__` and forcing a schema
rebuild is the only way in: pydantic compiled the original into the model's
core schema at class-creation time. Models that embed `MessageContent` are
rebuilt too, in case something validated (and thus built schemas) before
bale-userbot was imported.
"""

from __future__ import annotations

from copy import deepcopy
from types import MethodType
from typing import Any

from baleclient.types import MessageContent

_APPLIED = False

#: Name of the extra validator this module installs on `MessageContent`.
_DROP_NAME = "_bale_userbot_drop_unbuildable_blocks"


def _is_text_submessage(value: Any) -> bool:
    """True when a dict can actually build a `TextMessage` (`value` is a str)."""
    return isinstance(value, dict) and isinstance(value.get("1"), str)


def _fixed_check_empty(cls: type, data: Any) -> Any:
    if not isinstance(data, dict):
        return data

    if "5" in data:
        flag = data["5"]
        # Anything that is not a varint keeps 1.0.9's presence semantics: an
        # empty submessage still means "this is an empty/forward stub".
        data["5"] = bool(flag) if isinstance(flag, (bool, int)) else True

    text = data.get("15")
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    if isinstance(text, str):
        # The decoder collapses a single-field submessage into its string.
        data["15"] = {"1": text}
    elif text is not None and not _is_text_submessage(text):
        # `TextMessage.value` is a required str: any other shape would fail
        # validation and take the whole message (and its update) down with
        # it. Dropping just the text degrades to the pre-patch behaviour.
        data["15"] = None

    return data


def _drop_unbuildable_blocks(cls: type, data: Any) -> Any:
    """Null sub-blocks whose required fields are missing.

    `ServiceMessage` requires both text ("1") and ext ("2"), and `Thumbnail`
    requires width ("1") and height ("2") — yet `DocumentMessage`'s own
    normalize_thumb keeps any thumb dict carrying only an image. A message
    missing either raises, and on the websocket path that exception is
    swallowed whole: the entire update disappears. Nulling the offending
    block keeps the rest of the message.
    """
    if not isinstance(data, dict):
        return data

    service = data.get("11")
    if isinstance(service, dict) and not ("1" in service and "2" in service):
        data["11"] = None

    document = data.get("4")
    if isinstance(document, dict):
        thumb = document.get("6")
        if isinstance(thumb, dict) and not ("1" in thumb and "2" in thumb):
            document["6"] = None

    return data


def apply_wire_fixes() -> None:
    """Install the fixed validator. Idempotent; call before any parsing."""
    global _APPLIED
    if _APPLIED:
        return

    validators = MessageContent.__pydantic_decorators__.model_validators
    validators["_check_empty"].func = MethodType(_fixed_check_empty, MessageContent)

    # A second before-validator, added the same way the model declares its
    # own. Both run before pydantic builds the sub-models.
    decorator = deepcopy(validators["_check_empty"])
    decorator.cls_var_name = _DROP_NAME
    decorator.func = MethodType(_drop_unbuildable_blocks, MessageContent)
    validators[_DROP_NAME] = decorator

    MessageContent.model_rebuild(force=True)

    # Any model whose already-built schema embeds the old MessageContent
    # schema must be rebuilt as well. Failures are fine: a model that cannot
    # rebuild yet will build lazily (defer_build) and pick up the fix then.
    import baleclient.types as types_module
    import baleclient.types.responses as responses_module

    for module in (types_module, responses_module):
        for name in dir(module):
            obj = getattr(module, name)
            if _is_rebuildable_model(obj):
                try:
                    obj.model_rebuild(force=True)
                except Exception:
                    pass

    _APPLIED = True


def _is_rebuildable_model(obj: object) -> bool:
    from pydantic import BaseModel

    return (
        isinstance(obj, type)
        and issubclass(obj, BaseModel)
        and obj is not MessageContent
    )
