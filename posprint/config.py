"""Configuration, entirely from environment variables.

Everything has a working default for a generic 80mm printer on /dev/usb/lp0, so
an empty environment still boots.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Paper width -> (printable dots at 203dpi, Font A columns).
PAPER_PROFILES: dict[int, tuple[int, int]] = {
    58: (384, 32),
    80: (576, 48),
}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer, got {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    # Empty means "auto-discover the first /dev/usb/lp*", which survives the
    # printer being re-enumerated as lp1 after a replug.
    device: str = ""
    paper_mm: int = 80
    dots: int = 576
    columns: int = 48
    codepage: str = "cp858"

    host: str = "0.0.0.0"
    port: int = 8080
    api_key: str = ""

    # Cheap printers drop bytes if fed faster than they can print. Chunking the
    # write and pausing between chunks costs nothing at receipt volumes.
    chunk_bytes: int = 4096
    chunk_delay_ms: int = 0
    queue_max: int = 100

    # Emit `ESC @` before and a cut after every job unless the job says otherwise.
    auto_init: bool = True
    auto_cut: bool = True

    @classmethod
    def from_env(cls) -> "Config":
        paper = _env_int("POSPRINT_PAPER_MM", 80)
        if paper not in PAPER_PROFILES:
            raise SystemExit(
                f"POSPRINT_PAPER_MM must be one of {sorted(PAPER_PROFILES)}, got {paper}"
            )
        dots, columns = PAPER_PROFILES[paper]

        return cls(
            device=os.environ.get("POSPRINT_DEVICE", "").strip(),
            paper_mm=paper,
            dots=_env_int("POSPRINT_DOTS", dots),
            columns=_env_int("POSPRINT_COLUMNS", columns),
            codepage=os.environ.get("POSPRINT_CODEPAGE", "cp858").strip().lower(),
            host=os.environ.get("POSPRINT_HOST", "0.0.0.0"),
            port=_env_int("POSPRINT_PORT", 8080),
            api_key=os.environ.get("POSPRINT_API_KEY", "").strip(),
            chunk_bytes=_env_int("POSPRINT_CHUNK_BYTES", 4096),
            chunk_delay_ms=_env_int("POSPRINT_CHUNK_DELAY_MS", 0),
            queue_max=_env_int("POSPRINT_QUEUE_MAX", 100),
            auto_init=_env_bool("POSPRINT_AUTO_INIT", True),
            auto_cut=_env_bool("POSPRINT_AUTO_CUT", True),
        )
