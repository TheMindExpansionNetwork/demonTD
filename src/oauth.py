"""
Daydream profile validation for demonTD.

Historically this module also drove a browser-based OAuth flow (Web
Server DAT callback, token exchange, CSRF state, etc.). v0.2.5 ripped
that out: hosted sign-in is paste-only — the user copies a key from
https://app.daydream.live/dashboard/api-keys and pastes it into a TD
modal. All that's left here is the helper that VALIDATES a pasted key
against /users/profile before we persist it.

Kept as its own module (instead of being inlined into demon_ext.py) so
the validation is unit-testable without TD globals, and so other code
paths (e.g. future MCP / CLI use) can reuse `fetch_profile`.
"""

from __future__ import annotations

import json
from urllib import error as urlerror
from urllib import request as urlrequest

DAYDREAM_API_BASE = "https://api.daydream.live"


class OAuthError(Exception):
    """Raised when a Daydream API call fails or returns an unexpected
    shape. Kept under the legacy name `OAuthError` for back-compat with
    callers that imported it before the OAuth flow was removed."""


def _get_json(url: str, headers: dict, timeout: float = 15.0) -> dict:
    req = urlrequest.Request(
        url,
        method="GET",
        headers={**headers, "Accept": "application/json"},
    )
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urlerror.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8")
        except Exception:
            err_body = ""
        raise OAuthError(f"HTTP {e.code}: {err_body}") from e
    except urlerror.URLError as e:
        raise OAuthError(f"Network error: {e}") from e


def fetch_profile(api_key: str) -> dict:
    """Validate `api_key` by fetching /users/profile.

    Returns the parsed JSON dict on success. Raises OAuthError on any
    network / HTTP failure (so the caller can show a precise reason in
    the UI). Returns `{}` when the server returns an empty/non-dict
    body (treated as "unauthorized" by the caller).
    """
    raw = _get_json(
        f"{DAYDREAM_API_BASE}/users/profile",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    return raw if isinstance(raw, dict) else {}
