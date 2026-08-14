"""Client for the posprint service.

The only place the upstream API key exists in this process. Nothing here is
reachable from the browser: the front end talks to /api/print, this talks to
posprint, and the key never crosses that boundary.
"""

from __future__ import annotations

import logging
from datetime import datetime

import httpx

log = logging.getLogger("posprintweb.upstream")


class UpstreamError(Exception):
    """The printer could not be reached, or refused the job.

    `reason` mirrors posprint's job reason so the page can say which of the two
    boring physical problems it is. "Out of paper" is actionable by whoever
    lives with the printer; "offline" is not the same errand.
    """

    def __init__(self, message: str, reason: str = "error") -> None:
        super().__init__(message)
        self.reason = reason


class Upstream:
    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"X-API-Key": api_key} if api_key else {}
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=self._base, headers=self._headers, timeout=self._timeout
        )

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def health(self) -> dict:
        """Best-effort upstream status. Never raises; the page still renders."""
        if self._client is None:
            return {"ok": False, "detail": "client not started"}
        try:
            r = await self._client.get("/health", timeout=5.0)
            body = r.json() if r.content else {}
            # posprint answers 503 for both "unplugged" and "out of paper", so
            # the state comes from the body, not the status code.
            return {
                "ok": r.status_code == 200,
                "state": body.get("state", "offline"),
                "device_present": bool(body.get("device_present")),
                "paper": (body.get("config") or {}).get("paper_mm"),
            }
        except Exception as exc:  # noqa: BLE001
            log.warning("upstream health check failed: %s", exc)
            # Unreachable posprint is an outage, not an empty roll. Saying
            # "out of paper" here would send someone to load a roll into a
            # printer whose service is down.
            return {"ok": False, "state": "offline", "detail": str(exc)}

    async def print_message(
        self,
        *,
        message: str,
        name: str,
        columns: int,
        when: datetime,
        note: str = "",
    ) -> dict:
        """Render a message as a fixed document and send it to the printer.

        The document is assembled here, from validated fields. Blocks are never
        forwarded from the client: that would re-expose `raw` and `drawer`, and
        the whole point of this service is that the public surface is one text
        field.
        """
        if self._client is None:
            raise UpstreamError("upstream client not started")

        header = name or "someone on the internet"
        blocks: list[dict] = [
            {"type": "text", "text": "INCOMING", "align": "center", "bold": True,
             "width": 2, "height": 2},
            {"type": "text", "text": when.strftime("%Y-%m-%d %H:%M"), "align": "center"},
            {"type": "rule", "char": "="},
            {"type": "text", "text": message, "wrap": True},
            {"type": "rule", "char": "-"},
            {"type": "text", "text": f"from: {header}", "align": "right"},
        ]
        if note:
            blocks.append({"type": "text", "text": note, "align": "center"})
        blocks.append({"type": "feed", "lines": 1})

        payload = {
            "blocks": blocks,
            "label": f"web:{header[:20]}",
            "wait": True,
            "timeout": 25.0,
        }

        try:
            r = await self._client.post("/print", json=payload)
        except httpx.HTTPError as exc:
            raise UpstreamError(f"could not reach the printer: {exc}") from exc

        if r.status_code in (200, 202):
            body = r.json() if r.content else {}
            return {"job_id": body.get("id", ""), "state": body.get("state", "queued")}

        if r.status_code == 401:
            # Ours to fix, not the visitor's. Log loudly, stay vague publicly.
            log.error("upstream rejected our API key (401) - check POSPRINTWEB_UPSTREAM_KEY")
            raise UpstreamError("the printer is misconfigured", "misconfigured")

        body = {}
        try:
            body = r.json() if r.content else {}
        except Exception:  # noqa: BLE001
            body = {}

        # A job that reached the spooler and failed comes back as 502 with the
        # job payload; a job refused before it got there is a 503 with a detail.
        reason = body.get("reason") or ""
        if reason == "out_of_paper":
            raise UpstreamError("the printer is out of paper", "out_of_paper")
        if reason == "offline" or r.status_code == 503:
            raise UpstreamError("the printer is offline", "offline")

        log.error(
            "upstream returned %s (%s): %s",
            r.status_code, reason or "-", body.get("error") or body.get("detail") or r.text[:200],
        )
        raise UpstreamError("the printer refused the job", reason or "error")
