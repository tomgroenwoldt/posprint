"""HTTP API for the POS printer."""

from __future__ import annotations

import base64
import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import Config
from .device import Job, PrinterUnavailable, PrinterWriteError, PrintSpooler
from .escpos import EscposError, status_page
from .models import (
    DrawerRequest,
    JobResponse,
    PrintRequest,
    RawPrintRequest,
    ReceiptRequest,
    TextPrintRequest,
)
from .render import new_builder, render_document, render_receipt, render_text

log = logging.getLogger("posprint")

cfg = Config.from_env()
spooler = PrintSpooler(
    configured_device=cfg.device,
    chunk_bytes=cfg.chunk_bytes,
    chunk_delay_ms=cfg.chunk_delay_ms,
    queue_max=cfg.queue_max,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    if not cfg.api_key:
        log.warning(
            "POSPRINT_API_KEY is unset - the API is unauthenticated. "
            "Anyone who can reach this port can print and open the cash drawer."
        )
    spooler.start()
    try:
        yield
    finally:
        spooler.stop()


app = FastAPI(
    title="posprint",
    version="1.0.0",
    summary="HTTP front end for a USB ESC/POS receipt printer",
    lifespan=lifespan,
)

# The API key is the real gate; CORS is opened so browser dashboards on the LAN
# (Home Assistant cards, small tools) can call in directly.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def require_key(request: Request) -> None:
    if not cfg.api_key:
        return
    supplied = request.headers.get("x-api-key") or ""
    if not supplied:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            supplied = auth[7:]
    if not secrets.compare_digest(supplied, cfg.api_key):
        raise HTTPException(status_code=401, detail="invalid or missing API key")


Auth = Depends(require_key)


def _job_response(job: Job, wait: bool, timeout: float) -> Response:
    """Map spooler state onto HTTP.

    200 printed, 202 still in flight (or fire-and-forget), 502 the printer
    refused. The job id is always returned so callers can poll /jobs/{id}.
    """
    if wait:
        job.wait(timeout)

    payload = JobResponse(**job.as_dict()).model_dump()

    if job.state == "done":
        return JSONResponse(payload, status_code=200)
    if job.state == "failed":
        return JSONResponse(payload, status_code=502)
    return JSONResponse(payload, status_code=202)


def _submit(data: bytes, label: str, wait: bool, timeout: float) -> Response:
    try:
        job = spooler.submit(data, label=label)
    except PrinterWriteError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return _job_response(job, wait, timeout)


@app.exception_handler(EscposError)
async def _escpos_error(request: Request, exc: EscposError) -> JSONResponse:
    return JSONResponse({"detail": str(exc)}, status_code=422)


@app.exception_handler(PrinterUnavailable)
async def _unavailable(request: Request, exc: PrinterUnavailable) -> JSONResponse:
    return JSONResponse({"detail": str(exc)}, status_code=503)


# -- introspection --------------------------------------------------------


@app.get("/health", summary="Liveness + printer reachability (unauthenticated)")
def health() -> JSONResponse:
    info = spooler.health()
    info["config"] = {
        "paper_mm": cfg.paper_mm,
        "columns": cfg.columns,
        "dots": cfg.dots,
        "codepage": cfg.codepage,
        "auth": bool(cfg.api_key),
    }
    ok = info["device_present"] and info["worker_alive"]
    return JSONResponse(info, status_code=200 if ok else 503)


@app.get("/jobs", dependencies=[Auth], summary="Recent jobs, newest first")
def jobs(limit: int = 20) -> dict:
    return {"jobs": spooler.recent(limit=max(1, min(limit, 100)))}


@app.get("/jobs/{job_id}", dependencies=[Auth])
def job_detail(job_id: str) -> JobResponse:
    job = spooler.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job id")
    return JobResponse(**job.as_dict())


# -- printing -------------------------------------------------------------


@app.post("/print", dependencies=[Auth], summary="Print a block document")
def print_document(req: PrintRequest) -> Response:
    return _submit(render_document(req, cfg), req.label or "document", req.wait, req.timeout)


@app.post("/print/text", dependencies=[Auth], summary="Print plain text")
def print_text(req: TextPrintRequest) -> Response:
    return _submit(render_text(req, cfg), req.label or "text", req.wait, req.timeout)


@app.post("/print/receipt", dependencies=[Auth], summary="Print a formatted receipt")
def print_receipt(req: ReceiptRequest) -> Response:
    return _submit(render_receipt(req, cfg), req.label or "receipt", req.wait, req.timeout)


@app.post("/print/raw", dependencies=[Auth], summary="Send raw ESC/POS bytes")
def print_raw(req: RawPrintRequest) -> Response:
    data = base64.b64decode(req.data_base64)
    if not data:
        raise HTTPException(status_code=422, detail="empty payload")
    return _submit(data, req.label or "raw", req.wait, req.timeout)


@app.post("/print/image", dependencies=[Auth], summary="Upload and print an image")
async def print_image(
    file: UploadFile = File(..., description="PNG/JPEG/BMP"),
    dither: bool = Form(True),
    max_width: int | None = Form(None),
    cut: bool = Form(True),
    wait: bool = Form(True),
    timeout: float = Form(60.0),
) -> Response:
    payload = await file.read()
    if not payload:
        raise HTTPException(status_code=422, detail="uploaded file was empty")

    b = new_builder(cfg)
    if cfg.auto_init:
        b.init()
    b.align("center")
    b.image(payload, max_width=max_width, dither=dither)
    b.align("left")
    if cut:
        b.cut()
    return _submit(b.bytes(), file.filename or "image", wait, timeout)


@app.post("/print/test", dependencies=[Auth], summary="Print a self-test page")
def print_test(wait: bool = True, timeout: float = 60.0) -> Response:
    health_info = spooler.health()
    info = [
        ("device", str(health_info.get("device", "?"))),
        ("paper", f"{cfg.paper_mm}mm"),
        ("codepage", cfg.codepage),
        ("auth", "on" if cfg.api_key else "OFF"),
    ]
    b = status_page(new_builder(cfg), info)
    return _submit(b.bytes(), "self-test", wait, timeout)


@app.post("/drawer", dependencies=[Auth], summary="Kick the cash drawer")
def drawer(req: DrawerRequest) -> Response:
    b = new_builder(cfg)
    b.drawer_kick(pin=req.pin, on_ms=req.on_ms, off_ms=req.off_ms)
    return _submit(b.bytes(), "drawer", True, 15.0)


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()
