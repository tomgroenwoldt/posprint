"""Turning API documents into ESC/POS byte streams."""

from __future__ import annotations

import base64
from datetime import datetime

from .config import Config
from .escpos import EscposBuilder
from .models import (
    BarcodeBlock,
    ColumnsBlock,
    CutBlock,
    DrawerBlock,
    FeedBlock,
    ImageBlock,
    PrintRequest,
    QRBlock,
    RawBlock,
    ReceiptRequest,
    RuleBlock,
    TextBlock,
    TextPrintRequest,
)


def new_builder(cfg: Config) -> EscposBuilder:
    return EscposBuilder(width_chars=cfg.columns, dots=cfg.dots, codepage=cfg.codepage)


def money(value: float, currency: str) -> str:
    """Symbols hug the number, ISO codes trail it — matches how receipts read."""
    text = f"{value:,.2f}"
    if len(currency) == 1 and not currency.isalnum():
        return f"{currency}{text}"
    return f"{text} {currency}" if currency else text


def render_document(req: PrintRequest, cfg: Config) -> bytes:
    b = new_builder(cfg)
    if cfg.auto_init:
        b.init()

    explicit_cut = any(isinstance(block, CutBlock) for block in req.blocks)

    for block in req.blocks:
        if block.align:
            b.align(block.align)

        if isinstance(block, TextBlock):
            b.bold(block.bold)
            b.underline(block.underline)
            b.size(block.width, block.height)
            b.font(block.font)
            if block.invert:
                b.invert(True)
            # Magnified text occupies proportionally fewer columns.
            effective = max(1, cfg.columns // block.width)
            if block.font == "b":
                effective = int(effective * 1.5)
            if block.wrap:
                b.wrapped(block.text, width=effective)
            else:
                b.text(block.text)
            if block.invert:
                b.invert(False)
            b.reset_styles()

        elif isinstance(block, ColumnsBlock):
            b.bold(block.bold)
            b.columns(block.left, block.right)
            b.bold(False)

        elif isinstance(block, RuleBlock):
            b.rule(block.char)

        elif isinstance(block, FeedBlock):
            b.feed(block.lines)

        elif isinstance(block, BarcodeBlock):
            b.barcode(
                block.data,
                symbology=block.symbology,
                height=block.height,
                width=block.width,
                hri=block.hri,
            )

        elif isinstance(block, QRBlock):
            b.qr(block.data, size=block.size, ecc=block.ecc)

        elif isinstance(block, ImageBlock):
            b.image(
                base64.b64decode(block.data_base64),
                max_width=block.max_width,
                dither=block.dither,
            )

        elif isinstance(block, CutBlock):
            b.cut(partial=block.partial, feed_before=block.feed_before)

        elif isinstance(block, DrawerBlock):
            b.drawer_kick(pin=block.pin, on_ms=block.on_ms, off_ms=block.off_ms)

        elif isinstance(block, RawBlock):
            b.raw(base64.b64decode(block.data_base64))

        if block.align:
            b.align("left")

    should_cut = cfg.auto_cut if req.cut is None else req.cut
    if should_cut and not explicit_cut:
        b.cut()
    return b.bytes()


def render_text(req: TextPrintRequest, cfg: Config) -> bytes:
    b = new_builder(cfg)
    if cfg.auto_init:
        b.init()
    b.align(req.align).bold(req.bold).size(req.width, req.height)
    b.wrapped(req.text, width=max(1, cfg.columns // req.width))
    b.reset_styles()

    should_cut = cfg.auto_cut if req.cut is None else req.cut
    if should_cut:
        b.cut()
    return b.bytes()


def render_receipt(req: ReceiptRequest, cfg: Config) -> bytes:
    b = new_builder(cfg)
    if cfg.auto_init:
        b.init()

    if req.title:
        b.align("center").size(2, 2).bold(True)
        b.wrapped(req.title, width=max(1, cfg.columns // 2))
        b.reset_styles()
    if req.subtitle:
        b.align("center").wrapped(req.subtitle)
        b.align("left")
    if req.title or req.subtitle:
        b.feed(1)

    b.align("left")
    for line in req.header_lines:
        b.wrapped(line)
    if req.header_lines:
        b.rule("-")

    for item in req.items:
        total = item.total
        if total is None and item.unit_price is not None:
            total = item.unit_price * item.qty

        # Whole quantities read better without a trailing .0
        qty = f"{item.qty:g}"
        name = item.name if item.qty == 1 else f"{qty} x {item.name}"
        b.columns(name, money(total, req.currency) if total is not None else "")

        if item.unit_price is not None and item.qty != 1:
            b.text(f"    @ {money(item.unit_price, req.currency)}")
        if item.note:
            b.wrapped(f"    {item.note}")

    if req.items:
        b.rule("-")

    for label, value in (
        ("Subtotal", req.subtotal),
        ("Tax", req.tax),
    ):
        if value is not None:
            b.columns(label, money(value, req.currency))

    if req.total is not None:
        b.bold(True).size(1, 2)
        # Double-height halves nothing horizontally, so the column width holds.
        b.columns("TOTAL", money(req.total, req.currency))
        b.reset_styles()

    for label, value in (("Paid", req.paid), ("Change", req.change)):
        if value is not None:
            b.columns(label, money(value, req.currency))

    if any(v is not None for v in (req.subtotal, req.tax, req.total, req.paid, req.change)):
        b.rule("=")

    b.feed(1)
    b.align("center")
    if req.barcode:
        b.barcode(req.barcode, "code128", height=60, width=2)
        b.feed(1)
    if req.qr:
        b.qr(req.qr, size=6)
        b.feed(1)

    for line in req.footer_lines:
        b.wrapped(line)
    b.align("left")

    if not req.footer_lines and not req.qr and not req.barcode:
        b.text(datetime.now().strftime("%Y-%m-%d %H:%M"))

    if req.open_drawer:
        b.drawer_kick()

    should_cut = cfg.auto_cut if req.cut is None else req.cut
    if should_cut:
        b.cut()
    return b.bytes()
