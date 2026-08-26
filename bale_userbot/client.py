"""Client subclass that fixes self-message bookkeeping.

`baleclient.Client._should_ignore` (1.0.9) calls `list.remove()` with no
argument, so every echo of a message we sent raises `TypeError` inside the
dispatch task, and the pending-id list is never drained. It also reports every
non-message event as "ignore", which drops edits, deletions and the rest.
"""

from __future__ import annotations

from typing import Any

from baleclient import Client

#: Upper bound on ids of sent messages still waiting for their echo.
MAX_PENDING_IDS = 512


class KitClient(Client):
    def _should_ignore(self, event_type: str, event: Any) -> bool:
        if event_type != "message":
            return False

        targets = self._ignored_messages.targets
        message_id = getattr(event, "message_id", None)

        if message_id is not None and message_id in targets:
            targets.remove(message_id)
            return True

        # Echoes that never arrive would otherwise pin these ids forever.
        if len(targets) > MAX_PENDING_IDS:
            del targets[:-MAX_PENDING_IDS]

        return False
