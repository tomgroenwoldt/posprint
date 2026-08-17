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


class PrintMessage(BaseModel):
    # These caps are far above the configured limits and exist only to stop a
    # multi-megabyte body from reaching the Unicode normaliser. The real limits
    # live in Config and are enforced in filters.py.
    message: str = Field(..., max_length=5000)
    name: str = Field("", max_length=200)
