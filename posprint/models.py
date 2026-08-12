"""Request/response schemas.

The core abstraction is a *document*: an ordered list of typed blocks. Anything
you can put on a receipt is a block, which keeps the API one endpoint wide
instead of one-endpoint-per-feature.
"""

from __future__ import annotations

import base64
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, field_validator

Align = Literal["left", "center", "right"]


class _Block(BaseModel):
    align: Align | None = None


class TextBlock(_Block):
    type: Literal["text"] = "text"
    text: str
    bold: bool = False
    underline: Literal[0, 1, 2] = 0
    invert: bool = False
    width: int = Field(1, ge=1, le=8, description="Horizontal magnification")
    height: int = Field(1, ge=1, le=8, description="Vertical magnification")
    font: Literal["a", "b"] = "a"
    wrap: bool = Field(True, description="Word-wrap to the paper width")


class ColumnsBlock(_Block):
    type: Literal["columns"] = "columns"
    left: str
    right: str
    bold: bool = False


class RuleBlock(_Block):
    type: Literal["rule"] = "rule"
    char: str = Field("-", min_length=1, max_length=1)


class FeedBlock(_Block):
    type: Literal["feed"] = "feed"
    lines: int = Field(1, ge=0, le=255)


class BarcodeBlock(_Block):
    type: Literal["barcode"] = "barcode"
    data: str
    symbology: Literal[
        "upca", "upce", "ean13", "ean8", "code39", "itf", "codabar", "code93", "code128"
    ] = "code128"
    height: int = Field(64, ge=1, le=255)
    width: int = Field(3, ge=2, le=6)
    hri: Literal["none", "above", "below", "both"] = "below"


class QRBlock(_Block):
    type: Literal["qr"] = "qr"
    data: str
    size: int = Field(6, ge=1, le=16)
    ecc: Literal["L", "M", "Q", "H"] = "M"


class ImageBlock(_Block):
    type: Literal["image"] = "image"
    data_base64: str = Field(description="PNG/JPEG/BMP bytes, base64 encoded")
    max_width: int | None = Field(None, ge=8, description="Dots; clamped to paper width")
    dither: bool = True

    @field_validator("data_base64")
    @classmethod
    def _valid_base64(cls, v: str) -> str:
        try:
            base64.b64decode(v, validate=True)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"data_base64 is not valid base64: {exc}") from exc
        return v


class CutBlock(_Block):
    type: Literal["cut"] = "cut"
    partial: bool = True
    feed_before: int = Field(4, ge=0, le=255)


class DrawerBlock(_Block):
    type: Literal["drawer"] = "drawer"
    pin: Literal[0, 1] = 0
    on_ms: int = Field(100, ge=0, le=510)
    off_ms: int = Field(200, ge=0, le=510)


class RawBlock(_Block):
    type: Literal["raw"] = "raw"
    data_base64: str = Field(description="Raw ESC/POS bytes, base64 encoded")

    @field_validator("data_base64")
    @classmethod
    def _valid_base64(cls, v: str) -> str:
        try:
            base64.b64decode(v, validate=True)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"data_base64 is not valid base64: {exc}") from exc
        return v


Block = Annotated[
    Union[
        TextBlock,
        ColumnsBlock,
        RuleBlock,
        FeedBlock,
        BarcodeBlock,
        QRBlock,
        ImageBlock,
        CutBlock,
        DrawerBlock,
        RawBlock,
    ],
    Field(discriminator="type"),
]


class PrintRequest(BaseModel):
    blocks: list[Block] = Field(min_length=1)
    label: str = Field("", description="Free-text tag shown in /jobs")
    cut: bool | None = Field(
        None, description="Override the auto-cut default for this job"
    )
    wait: bool = Field(True, description="Block until the job leaves the queue")
    timeout: float = Field(30.0, gt=0, le=300)


class TextPrintRequest(BaseModel):
    """Shortcut for the 90% case: print some text, cut the paper."""

    text: str
    align: Align = "left"
    bold: bool = False
    width: int = Field(1, ge=1, le=8)
    height: int = Field(1, ge=1, le=8)
    cut: bool | None = None
    label: str = ""
    wait: bool = True
    timeout: float = Field(30.0, gt=0, le=300)


class ReceiptItem(BaseModel):
    name: str
    qty: float = 1
    unit_price: float | None = None
    total: float | None = None
    note: str | None = None


class ReceiptRequest(BaseModel):
    title: str | None = None
    subtitle: str | None = None
    header_lines: list[str] = Field(default_factory=list)
    items: list[ReceiptItem] = Field(default_factory=list)
    subtotal: float | None = None
    tax: float | None = None
    total: float | None = None
    paid: float | None = None
    change: float | None = None
    currency: str = "EUR"
    footer_lines: list[str] = Field(default_factory=list)
    qr: str | None = Field(None, description="Printed at the bottom, e.g. an order URL")
    barcode: str | None = None
    open_drawer: bool = False
    cut: bool | None = None
    label: str = ""
    wait: bool = True
    timeout: float = Field(30.0, gt=0, le=300)


class RawPrintRequest(BaseModel):
    data_base64: str
    label: str = ""
    wait: bool = True
    timeout: float = Field(30.0, gt=0, le=300)

    @field_validator("data_base64")
    @classmethod
    def _valid_base64(cls, v: str) -> str:
        try:
            base64.b64decode(v, validate=True)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"data_base64 is not valid base64: {exc}") from exc
        return v


class DrawerRequest(BaseModel):
    pin: Literal[0, 1] = 0
    on_ms: int = Field(100, ge=0, le=510)
    off_ms: int = Field(200, ge=0, le=510)


class JobResponse(BaseModel):
    id: str
    state: str
    label: str = ""
    error: str | None = None
    bytes: int = 0
    bytes_written: int = 0
    queued_at: float | None = None
    started_at: float | None = None
    finished_at: float | None = None
