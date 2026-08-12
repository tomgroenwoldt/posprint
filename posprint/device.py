"""Talking to the physical printer, and serialising access to it.

A thermal printer is a single, non-reentrant resource: two concurrent writes to
/dev/usb/lp0 interleave and produce garbage. Everything therefore funnels
through one worker thread draining a queue.
"""

from __future__ import annotations

import array
import errno
import glob
import logging
import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Literal

log = logging.getLogger("posprint.device")

# From linux/lp.h. usblp implements this and returns the USB printer-class
# status byte, which is the only out-of-band health signal these printers give.
LPGETSTATUS = 0x060B

DEVICE_GLOB = "/dev/usb/lp*"


class PrinterUnavailable(RuntimeError):
    """No usable printer device node right now (unplugged, powered off, no perms)."""


class PrinterWriteError(RuntimeError):
    """The device node existed but the write failed."""


@dataclass
class PrinterStatus:
    online: bool
    paper_ok: bool | None
    error: bool | None
    raw: int | None
    source: str

    def as_dict(self) -> dict:
        return {
            "online": self.online,
            "paper_ok": self.paper_ok,
            "error": self.error,
            "raw": self.raw,
            "source": self.source,
        }


def discover_device(configured: str = "") -> str:
    """Return a usable device path, or raise PrinterUnavailable.

    A configured path wins, but we still check it exists so the error message
    points at the real problem instead of surfacing later as a write failure.
    """
    if configured:
        if not os.path.exists(configured):
            raise PrinterUnavailable(
                f"configured device {configured} does not exist "
                "(printer off, USB not passed into the container, or usblp not loaded)"
            )
        return configured

    candidates = sorted(glob.glob(DEVICE_GLOB))
    if not candidates:
        raise PrinterUnavailable(
            f"no printer node matching {DEVICE_GLOB}. Check: printer powered on, "
            "`lsmod | grep usblp` on the Proxmox host, and the LXC bind mount of /dev/usb"
        )
    return candidates[0]


def read_status(path: str) -> PrinterStatus:
    """Query the printer via the usblp LPGETSTATUS ioctl.

    Not all clones populate the status byte honestly, so a successful ioctl with
    nonsense bits is still reported — the fields are advisory. If the ioctl is
    unsupported we fall back to "the node opened, so it's probably alive".

    fcntl is imported lazily rather than at module scope so this package stays
    importable on Windows, where it does not exist. That keeps the encoder and
    renderers testable on a dev machine; only this one function degrades.
    """
    try:
        import fcntl  # noqa: PLC0415 - Unix-only, see docstring
    except ImportError:
        fcntl = None  # type: ignore[assignment]

    # O_NONBLOCK and O_BINARY are platform-specific; absent flags degrade to 0.
    nonblock = getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, os.O_RDWR | nonblock)
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EPERM):
            raise PrinterUnavailable(
                f"permission denied on {path}; the container user needs write access "
                "(see deploy/99-posprint.rules)"
            ) from exc
        # O_RDWR fails on some unidirectional printers; retry write-only.
        try:
            fd = os.open(path, os.O_WRONLY | nonblock)
        except OSError as exc2:
            raise PrinterUnavailable(f"cannot open {path}: {exc2}") from exc2

    try:
        # usblp's LPGETSTATUS handler does copy_to_user(arg, &status, sizeof(int)),
        # so the buffer must be a full int. A 1-byte buffer gets EFAULT at best
        # and corrupts adjacent memory at worst.
        buf = array.array("i", [0])
        try:
            if fcntl is None:
                raise OSError("fcntl unavailable on this platform")
            fcntl.ioctl(fd, LPGETSTATUS, buf, True)
        except OSError:
            return PrinterStatus(
                online=True, paper_ok=None, error=None, raw=None, source="open-only"
            )

        raw = buf[0] & 0xFF
        # USB printer class status byte: bit5 paper-empty, bit4 select/online,
        # bit3 not-error (so error is the *inverse* of that bit).
        return PrinterStatus(
            online=bool(raw & 0x10),
            paper_ok=not bool(raw & 0x20),
            error=not bool(raw & 0x08),
            raw=raw,
            source="LPGETSTATUS",
        )
    finally:
        os.close(fd)


def write_bytes(
    path: str,
    data: bytes,
    chunk_bytes: int = 4096,
    chunk_delay_ms: int = 0,
) -> int:
    """Write a full job to the device, opening and closing around it.

    Opening per job rather than holding an fd is deliberate: after a replug the
    old fd is dead and every subsequent write fails with ENODEV until restart.
    At receipt volumes the extra open() is free.
    """
    # O_BINARY only exists on Windows, where it suppresses the CRT's \n -> \r\n
    # translation. A stray 0x0D in an ESC/POS stream corrupts binary blocks such
    # as raster images. It is a no-op on Linux, where the flag is absent.
    flags = os.O_WRONLY | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EPERM):
            raise PrinterUnavailable(f"permission denied on {path}") from exc
        if exc.errno in (errno.ENOENT, errno.ENODEV, errno.ENXIO):
            raise PrinterUnavailable(f"{path} vanished: {exc}") from exc
        raise PrinterWriteError(f"cannot open {path}: {exc}") from exc

    written = 0
    try:
        view = memoryview(data)
        while written < len(view):
            chunk = view[written : written + chunk_bytes]
            try:
                n = os.write(fd, chunk)
            except OSError as exc:
                if exc.errno in (errno.ENODEV, errno.ENXIO, errno.ESHUTDOWN):
                    raise PrinterUnavailable(
                        f"printer disconnected after {written} bytes: {exc}"
                    ) from exc
                raise PrinterWriteError(
                    f"write failed after {written} bytes: {exc}"
                ) from exc
            if n == 0:
                raise PrinterWriteError(f"device accepted 0 bytes at offset {written}")
            written += n
            if chunk_delay_ms:
                time.sleep(chunk_delay_ms / 1000.0)
        try:
            os.fsync(fd)
        except OSError:
            # Character devices are not required to support fsync.
            pass
    finally:
        os.close(fd)
    return written


JobState = Literal["queued", "printing", "done", "failed"]


@dataclass
class Job:
    id: str
    data: bytes
    label: str = ""
    state: JobState = "queued"
    error: str | None = None
    bytes_written: int = 0
    queued_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    _done: threading.Event = field(default_factory=threading.Event, repr=False)

    def wait(self, timeout: float | None = None) -> bool:
        return self._done.wait(timeout)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "state": self.state,
            "error": self.error,
            "bytes": len(self.data),
            "bytes_written": self.bytes_written,
            "queued_at": self.queued_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class PrintSpooler:
    """Single-consumer spooler. One thread, one printer, no interleaving."""

    def __init__(
        self,
        configured_device: str = "",
        chunk_bytes: int = 4096,
        chunk_delay_ms: int = 0,
        queue_max: int = 100,
        history: int = 50,
    ):
        self.configured_device = configured_device
        self.chunk_bytes = chunk_bytes
        self.chunk_delay_ms = chunk_delay_ms
        self._queue: queue.Queue[Job] = queue.Queue(maxsize=queue_max)
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._history = history
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self.last_error: str | None = None
        self.last_success_at: float | None = None

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stopping.clear()
        self._thread = threading.Thread(
            target=self._run, name="posprint-spooler", daemon=True
        )
        self._thread.start()
        log.info("spooler started")

    def stop(self, timeout: float = 5.0) -> None:
        # No sentinel value is pushed to wake the worker. An earlier version did,
        # and if the worker had already exited via its own poll timeout the
        # sentinel stayed in the queue and terminated the *next* worker the
        # instant it started, leaving every subsequent job stuck at "queued".
        # The 0.5s poll in _run() makes shutdown responsive enough on its own.
        self._stopping.set()
        if self._thread:
            self._thread.join(timeout)
        log.info("spooler stopped")

    # -- submission -------------------------------------------------------

    def submit(self, data: bytes, label: str = "") -> Job:
        job = Job(id=uuid.uuid4().hex[:12], data=data, label=label)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._trim_history()
        try:
            self._queue.put_nowait(job)
        except queue.Full as exc:
            job.state = "failed"
            job.error = "print queue is full"
            job._done.set()
            raise PrinterWriteError("print queue is full") from exc
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def recent(self, limit: int = 20) -> list[dict]:
        with self._lock:
            ids = self._order[-limit:][::-1]
            return [self._jobs[i].as_dict() for i in ids if i in self._jobs]

    def depth(self) -> int:
        return self._queue.qsize()

    def _trim_history(self) -> None:
        # Caller holds the lock. Keep finished jobs bounded so a long-running
        # service does not accumulate every receipt it ever printed in memory.
        while len(self._order) > self._history:
            old = self._order.pop(0)
            job = self._jobs.get(old)
            if job and job.state in ("queued", "printing"):
                self._order.insert(0, old)
                break
            self._jobs.pop(old, None)

    # -- worker -----------------------------------------------------------

    def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                job = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self._process(job)
            self._queue.task_done()

    def _process(self, job: Job) -> None:
        job.state = "printing"
        job.started_at = time.time()
        try:
            path = discover_device(self.configured_device)
            job.bytes_written = write_bytes(
                path, job.data, self.chunk_bytes, self.chunk_delay_ms
            )
            job.state = "done"
            self.last_error = None
            self.last_success_at = time.time()
            log.info("job %s printed %d bytes to %s", job.id, job.bytes_written, path)
        except (PrinterUnavailable, PrinterWriteError) as exc:
            job.state = "failed"
            job.error = str(exc)
            self.last_error = str(exc)
            log.warning("job %s failed: %s", job.id, exc)
        except Exception as exc:  # noqa: BLE001 - worker must never die
            job.state = "failed"
            job.error = f"unexpected error: {exc}"
            self.last_error = job.error
            log.exception("job %s crashed", job.id)
        finally:
            job.finished_at = time.time()
            job._done.set()

    # -- health -----------------------------------------------------------

    def health(self) -> dict:
        info: dict = {
            "queue_depth": self.depth(),
            "worker_alive": bool(self._thread and self._thread.is_alive()),
            "last_error": self.last_error,
            "last_success_at": self.last_success_at,
        }
        try:
            path = discover_device(self.configured_device)
            info["device"] = path
            info["device_present"] = True
            try:
                info["printer"] = read_status(path).as_dict()
            except PrinterUnavailable as exc:
                info["printer"] = {"online": False, "detail": str(exc)}
        except PrinterUnavailable as exc:
            info["device"] = self.configured_device or DEVICE_GLOB
            info["device_present"] = False
            info["detail"] = str(exc)
        return info
