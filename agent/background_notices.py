"""Hand a finished background result to a RUNNING turn as an automated notice.

``AIAgent.steer`` delivers text under the out-of-band user-message marker: the model is told a person
wrote it and gives it a person's authority. A background result is not that. This module is a separate,
small channel next to the steer buffer: the gateway posts a ``Notice`` to the agent that is running a
turn, and the loop appends it, at the next step boundary (after a tool batch, before the next model
call), as one machine-typed row whose text says it is an automated notice and not user input.

Exactly-once is the point of the buffer's lock. A posted notice is either drained into this turn's
messages, or handed back by ``close_turn`` when the turn ends (the owner then delivers it another way),
or refused because the turn is already closed (the owner then keeps its own copy). It is never in two
of those places and never in none.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Optional

# Same value as gateway.response_filters.INTERNAL_NOTIFICATION_DISPLAY_KIND (agent code does not import
# the gateway; a test pins the two together). History and compaction code treat any role=user row with a
# display_kind other than "steer" as runtime scaffolding, never as something the user wrote.
NOTICE_DISPLAY_KIND = "internal_notification"

from session_rules.turns import AUTOMATED_NOTICE  # noqa: E402  (shared with the row-merging passes)

_CREATE_LOCK = threading.Lock()


@dataclass
class Notice:
    """One result to hand over. ``text`` is exactly what the model reads; ``payload`` is the owner's own
    record, returned untouched by ``close_turn`` so the owner can deliver it another way."""
    text: str
    payload: Any = None


class _Box:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.closed = False
        self.items: list[Notice] = []


def with_automated_notice(text: str) -> str:
    """*text* with the automated-notice sentence on the line after its first line (its header), so a
    consumer that matches the start of the text or its footer still matches. Idempotent."""
    if AUTOMATED_NOTICE in text:
        return text
    head, sep, rest = text.partition("\n")
    return f"{head}\n{AUTOMATED_NOTICE}\n{rest}" if sep else f"{head}\n{AUTOMATED_NOTICE}"


def notice_user_row(text: str) -> dict[str, Any]:
    """The standalone ``role:user`` row a notice is delivered as (the only role-alternation-safe slot
    after a tool result). Typed so history, compaction and display treat it as machinery."""
    return {"role": "user", "content": text, "display_kind": NOTICE_DISPLAY_KIND}


def _box(agent: Any, create: bool) -> Optional[_Box]:
    box = vars(agent).get("_background_notices")
    if isinstance(box, _Box) or not create:
        return box if isinstance(box, _Box) else None
    with _CREATE_LOCK:
        box = vars(agent).get("_background_notices")
        if not isinstance(box, _Box):
            box = agent._background_notices = _Box()
        return box


def reopen_turn(agent: Any) -> None:
    """A new turn starts on *agent*: accept notices again. No-op for an agent that never had one."""
    box = _box(agent, create=False)
    if box is not None:
        with box.lock:
            box.closed = False


def post_notice(agent: Any, notice: Notice) -> bool:
    """Queue *notice* for the running turn. False once the turn is closed: nothing was queued."""
    box = _box(agent, create=True)
    assert box is not None
    with box.lock:
        if box.closed:
            return False
        box.items.append(notice)
        return True


def close_turn(agent: Any) -> list[Notice]:
    """End of turn: refuse further notices and return the ones the loop never took."""
    box = _box(agent, create=False)
    if box is None:
        return []
    with box.lock:
        box.closed = True
        left, box.items = box.items, []
        return left


def deliver_pending_notices(agent: Any, messages: list) -> int:
    """Step boundary: when the newest message is a tool result, append every queued notice as one row
    and return how many were delivered. Otherwise leave them queued for the next boundary (or for
    ``close_turn``), so a notice is never appended after anything but a finished tool batch."""
    box = _box(agent, create=False)
    if box is None or not messages:
        return 0
    last = messages[-1]
    if not isinstance(last, dict) or last.get("role") != "tool":
        return 0
    with box.lock:
        if not box.items:
            return 0
        taken, box.items = box.items, []
        try:
            messages.append(notice_user_row("\n\n".join(n.text for n in taken)))
        except Exception:
            box.items = taken + box.items
            raise
    return len(taken)
