"""
DEMON queue API client.

Pure HTTP, no TouchDesigner dependencies — uses stdlib urllib so it runs in
TD's bundled Python with no extra wheels.

Endpoints (from demon-public-demo/lib/queue/client.ts):
  POST /api/queue/join     -> allocate or queue a session
  GET  /api/queue/status   -> poll position; bumps server heartbeat
  POST /api/queue/extend   -> bump expiry ("Still playing?")
  POST /api/queue/leave    -> release a session
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest


@dataclass
class QueueResponse:
    status: str                      # "active" | "queued" | "unknown"
    session_id: str | None = None
    position: int | None = None      # 1-based when queued
    estimated_wait_ms: int | None = None
    session_duration_ms: int | None = None
    ws_url: str | None = None        # server-signed; only set when active
    expires_at: int | None = None    # absolute ms timestamp
    extensions_used: int | None = None
    raw: dict[str, Any] = None       # type: ignore[assignment]

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "QueueResponse":
        return cls(
            status=d.get("status", "unknown"),
            session_id=d.get("sessionId"),
            position=d.get("position"),
            estimated_wait_ms=d.get("estimatedWaitMs"),
            session_duration_ms=d.get("sessionDurationMs"),
            ws_url=d.get("wsUrl"),
            expires_at=d.get("expiresAt"),
            extensions_used=d.get("extensionsUsed"),
            raw=d,
        )


class QueueError(Exception):
    pass


class QueueClient:
    """Minimal HTTP client for the DEMON queue endpoints.

    Stateless apart from the configured base URL + optional API key.
    Each call is a one-shot request; reconnect logic lives in DemonExt.

    Timeouts default to 10s per request. Network errors raise QueueError.
    """

    def __init__(self, base_url: str, api_key: str | None = None,
                 timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or None
        self.timeout = timeout

    # ----- helpers ------------------------------------------------------------

    def _headers(self, json_body: bool = False) -> dict[str, str]:
        h = {"Accept": "application/json"}
        if json_body:
            h["Content-Type"] = "application/json"
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _request(self, method: str, path: str, *,
                 body: dict[str, Any] | None = None,
                 query: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        if query:
            url += "?" + urlparse.urlencode(query)

        data = None
        headers = self._headers(json_body=body is not None)
        if body is not None:
            data = json.dumps(body).encode("utf-8")

        req = urlrequest.Request(url, data=data, method=method, headers=headers)
        try:
            with urlrequest.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urlerror.HTTPError as e:
            try:
                err_body = e.read().decode("utf-8")
            except Exception:
                err_body = ""
            raise QueueError(f"HTTP {e.code} on {method} {path}: {err_body}") from e
        except urlerror.URLError as e:
            raise QueueError(f"Network error on {method} {path}: {e}") from e

        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise QueueError(f"Bad JSON from {path}: {raw[:200]}") from e

    # ----- public API ---------------------------------------------------------

    def join(self) -> QueueResponse:
        """POST /api/queue/join. Allocates or queues a session."""
        d = self._request("POST", "/api/queue/join", body={})
        return QueueResponse.from_dict(d)

    def status(self, session_id: str) -> QueueResponse:
        """GET /api/queue/status?token=<sessionId>. Also bumps server heartbeat."""
        d = self._request("GET", "/api/queue/status", query={"token": session_id})
        return QueueResponse.from_dict(d)

    def extend(self, session_id: str) -> QueueResponse:
        """POST /api/queue/extend with {sessionId}. The 'Still playing?' button."""
        d = self._request("POST", "/api/queue/extend", body={"sessionId": session_id})
        return QueueResponse.from_dict(d)

    def leave(self, session_id: str) -> None:
        """POST /api/queue/leave with {sessionId}. Best-effort, ignores errors."""
        try:
            self._request("POST", "/api/queue/leave", body={"sessionId": session_id})
        except QueueError:
            pass
