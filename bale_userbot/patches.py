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

Beyond `MessageContent`, four more 1.0.9 defects are repaired here, each found
by hitting it on a live account:

* `Member.date` (field "3") is declared `Optional[int]`, but in a group's
  `GetFullGroup` response that field carries the member's *display name*. One
  member with a name kills the whole response — including the group title that
  parsed fine. Channels return no member list, which is why only plain groups
  came back nameless. The name is moved aside rather than discarded: see
  `MEMBER_NAME_KEY`.
* `CallableObject.call` inspects filter signatures with
  `param.annotation.__name__`; any filter defined in a module using
  `from __future__ import annotations` turns annotations into strings and the
  dispatcher dies on the first incoming update.
* `MessageResponse.add_message` requires `info.context` and `method_data`,
  which the HTTP fallback in `session.post` never provides — so any request
  made before the websocket is up crashes instead of returning a response.
* One dialog whose last message has an exotic shape (a bot keyboard variant,
  a document missing required fields) fails `PeerData` validation and takes
  the whole `LoadDialogs` page with it, silently truncating the chat list.

Swapping the function inside `__pydantic_decorators__` and forcing a schema
rebuild is the only way in: pydantic compiled the original into the model's
core schema at class-creation time. Models that embed `MessageContent` are
rebuilt too, in case something validated (and thus built schemas) before
bale-userbot was imported.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from types import MethodType
from typing import Any

from baleclient.types import MessageContent

logger = logging.getLogger(__name__)

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


_INT_MEMBER_FIELDS = ("2", "3", "5", "6")

#: Where a display name found in the join-date field is kept. `Member` allows
#: extra fields, so it survives as `member.display_name`.
MEMBER_NAME_KEY = "display_name"


def _fixed_member_fields(cls: type, data: Any) -> Any:
    """`Member.fix_fields`, plus: move non-numeric values out of int fields.

    In a group's `GetFullGroup` response, field "3" — declared as the join
    date — carries the member's display name. `Optional[int]` then rejects the
    member, pydantic rejects the response, and the group title is lost with it.

    The value has to leave field "3" for the member to parse at all, but a name
    is worth keeping: put aside under `MEMBER_NAME_KEY` it saves a `LoadUsers`
    round trip for every member that carries one. Anything else non-numeric is
    dropped, as before.
    """
    if not isinstance(data, dict):
        return data

    for key in list(data.keys()):
        value = data[key]
        if isinstance(value, dict) and len(value) == 1 and "1" in value:
            data[key] = value["1"]
        elif not value:
            data.pop(key)

    if "7" in data and not isinstance(data["7"], list):
        data["7"] = [data["7"]]

    for key in _INT_MEMBER_FIELDS:
        if key in data and not isinstance(data[key], int):
            value = data.pop(key)
            if key == "3" and isinstance(value, str) and value.strip():
                data[MEMBER_NAME_KEY] = value

    return data


async def _fixed_callable_call(self, *args: Any, **kwargs: Any) -> Any:
    """`CallableObject.call` without the `param.annotation.__name__` crash.

    The original assumes every annotation is a class. A filter defined in a
    module using `from __future__ import annotations` has *string* annotations,
    and the dispatcher then dies on the first incoming update. `getattr` keeps
    the original behaviour for real classes and simply skips the rest.
    """
    import asyncio as _asyncio
    import contextvars as _contextvars
    import inspect as _inspect
    from functools import partial as _partial

    callback = _inspect.unwrap(self.callback)
    sig = _inspect.signature(callback)
    filtered_kwargs = {}

    for name, param in sig.parameters.items():
        if name in kwargs:
            filtered_kwargs[name] = kwargs[name]
        elif (
            getattr(param.annotation, "__name__", param.annotation) == "Client"
            and "client" in kwargs
        ):
            filtered_kwargs[name] = kwargs["client"]

    wrapped = _partial(callback, *args, **filtered_kwargs)
    if self.awaitable:
        return await wrapped()

    loop = _asyncio.get_event_loop()
    context = _contextvars.copy_context()
    wrapped = _partial(context.run, wrapped)
    return await loop.run_in_executor(None, wrapped)


def _make_fixed_add_message(original):
    """Wrap `MessageResponse.add_message` to survive the HTTP fallback.

    `session.post` validates responses without a context and without
    `method_data`; the original validator dereferences both and crashes. With
    neither there is nothing to reconstruct the echoed message from, so the
    optional `message` field is simply left empty.
    """

    def _fixed_add_message(cls: type, data: Any, info: Any) -> Any:
        if not isinstance(data, dict) or "message" in data:
            return data
        if info.context is None or data.get("method_data") is None:
            data["message"] = None
            return data
        return original(data, info)

    return _fixed_add_message


def _make_lenient_dialogs(cls: type):
    """A before-validator for `DialogResponse` that saves what it can.

    Every entry is trial-validated. An entry that fails is retried with its
    last-message content (field "7") replaced by an empty `MessageContent` —
    the content is only a chat-list preview, while the peer id and sort date
    are what callers actually need. Only an entry that still fails is dropped,
    and loudly.
    """
    from baleclient.types import PeerData

    def _lenient(cls_: type, data: Any) -> Any:
        if not isinstance(data, dict) or "3" not in data:
            return data
        entries = data["3"] if isinstance(data["3"], list) else [data["3"]]

        kept = []
        for entry in entries:
            try:
                PeerData.model_validate(entry)
            except Exception:
                stripped = dict(entry) if isinstance(entry, dict) else entry
                if isinstance(stripped, dict):
                    stripped["7"] = {}
                    try:
                        PeerData.model_validate(stripped)
                    except Exception:
                        logger.warning(
                            "dropping unparseable dialog entry for peer %r",
                            entry.get("1") if isinstance(entry, dict) else entry,
                        )
                        continue
                    kept.append(stripped)
                    continue
                logger.warning("dropping unparseable dialog entry %r", entry)
                continue
            kept.append(entry)

        data["3"] = kept
        return data

    return _lenient


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

    # -- Member: display-name string in an int field (GetFullGroup) --------
    from baleclient.types import Member

    member_validators = Member.__pydantic_decorators__.model_validators
    member_validators["fix_fields"].func = MethodType(_fixed_member_fields, Member)
    Member.model_rebuild(force=True)

    # -- dispatcher: string annotations from postponed evaluation ----------
    from baleclient.dispatcher.event.handler import CallableObject

    CallableObject.call = _fixed_callable_call

    # -- MessageResponse: HTTP fallback has no context ---------------------
    from baleclient.types.responses import MessageResponse

    response_validators = MessageResponse.__pydantic_decorators__.model_validators
    original_add = response_validators["add_message"].func
    response_validators["add_message"].func = MethodType(
        _make_fixed_add_message(original_add), MessageResponse
    )
    MessageResponse.model_rebuild(force=True)

    # -- DialogResponse: one exotic dialog kills the page ------------------
    from baleclient.types.responses import DialogResponse

    dialog_validators = DialogResponse.__pydantic_decorators__.model_validators
    lenient = deepcopy(dialog_validators["validate_list"])
    lenient.cls_var_name = "_bale_userbot_lenient_dialogs"
    lenient.func = MethodType(_make_lenient_dialogs(DialogResponse), DialogResponse)
    dialog_validators["_bale_userbot_lenient_dialogs"] = lenient
    DialogResponse.model_rebuild(force=True)

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
