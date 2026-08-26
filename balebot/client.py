"""Thin Client subclass that fixes self-message bookkeeping.

`baleclient.Client._should_ignore` (1.0.9) calls `list.remove()` with no
argument, which raises `TypeError` for every echo of a message we sent, and it
returns `True` for every non-message event. The override below keeps the same
intent — drop the server echo of our own outgoing messages — without the crash,
and caps the pending-id list so a long-running process cannot grow it forever.
"""

from __future__ import annotations

from typing import Any

from baleclient import Client

MAX_PENDING_IDS = 512


class ChatClient(Client):
    def _should_ignore(self, event_type: str, event: Any) -> bool:
        if event_type != "message":
            return False

        targets = self._ignored_messages.targets
        message_id = getattr(event, "message_id", None)

        if message_id is not None and message_id in targets:
            targets.remove(message_id)
            return True

        # Echoes we never saw would otherwise pin these ids forever.
        if len(targets) > MAX_PENDING_IDS:
            del targets[:-MAX_PENDING_IDS]

        return False
