"""Request schemas for the public API.

Deliberately tiny. The upstream posprint API is a rich document format with
raw-bytes and cash-drawer blocks; none of that is reachable from here. A visitor
gets a message and a name, and the document is built server-side.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class GalleryDecision(BaseModel):
    """One moderation decision, from the admin page."""

    id: int = Field(..., ge=1)
    action: Literal["approve", "hide", "reset"]


class HeldDecision(BaseModel):
    """One decision about the hold queue.

    `id` is ignored by the actions that do not name a single message; it stays
    required so a mistyped action cannot silently empty the queue.
    """

    id: int = Field(1, ge=1)
    action: Literal["print", "discard", "empty", "lift"]


class PrintMessage(BaseModel):
    # These caps are far above the configured limits and exist only to stop a
    # multi-megabyte body from reaching the Unicode normaliser. The real limits
    # live in Config and are enforced in filters.py.
    message: str = Field(..., max_length=5000)
    # Required, but not enforced by the schema: an empty one should come
    # back as a sentence from check_name, not as a validation error about
    # a field, which is what a visitor would actually see.
    name: str = Field("", max_length=200)
    # Proof of work. Optional on the model so a request without one reaches the
    # route and is refused there with something a page can act on, rather than
    # becoming a 422 about a missing field.
    challenge: str = Field("", max_length=200)
    counter: int = Field(0, ge=0)
    # Only looked at during a siege, where solving the puzzle prints now
    # instead of queueing. Absent the rest of the time.
    captcha_token: str = Field("", max_length=200)
    captcha_answer: int = Field(-1, ge=-1, le=64)
