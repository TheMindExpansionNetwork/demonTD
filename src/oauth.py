"""
Daydream OAuth flow for the DEMON TouchDesigner operator.

Mirrors demon-public-demo/lib/auth/daydream.ts:
  1. Launch system browser → https://app.daydream.live/sign-in/local with
     redirect_url=http://127.0.0.1:<port>/cb and a CSRF state nonce.
  2. A local HTTP listener (the COMP's Web Server DAT) catches the callback.
  3. Exchange the one-time token for a long-lived API key against
     https://api.daydream.live/v1/api-key.
  4. (Optional) fetch profile at /users/profile for display.

This module is the pure-HTTP brain; the TD-specific Web Server DAT wiring
lives in demon_ext.py. Functions here are stdlib-only and unit-testable.
"""

from __future__ import annotations

import json
import secrets
import socket
import webbrowser
from dataclasses import dataclass
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

DAYDREAM_AUTH_URL = "https://app.daydream.live/sign-in/local"
DAYDREAM_API_BASE = "https://api.daydream.live"
API_KEY_NAME = "dd_demon_td"


class OAuthError(Exception):
    pass


@dataclass
class AuthProfile:
    api_key: str
    user_id: str | None = None
    email: str | None = None
    display_name: str | None = None
    is_admin: bool = False


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def generate_state(num_bytes: int = 32) -> str:
    """Generate a CSRF state nonce: 64 hex chars (== 32 random bytes)."""
    return secrets.token_hex(num_bytes)


def find_free_port(low: int = 50000, high: int = 60000,
                   attempts: int = 32) -> int:
    """Find an unused TCP port in [low, high] on 127.0.0.1.

    Used to pick a port for the Web Server DAT that catches the OAuth callback.
    """
    for _ in range(attempts):
        port = secrets.randbelow(high - low) + low
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            continue
        finally:
            s.close()
        return port
    raise OAuthError(f"Could not find a free port in [{low}, {high}]")


def build_signin_url(port: int, state: str,
                     utm_source: str = "daydream-td") -> str:
    """Build the Daydream sign-in URL with our local callback."""
    redirect_url = f"http://127.0.0.1:{port}/cb"
    qs = urlparse.urlencode({
        "redirect_url": redirect_url,
        "state": state,
        "utm_source": utm_source,
    })
    return f"{DAYDREAM_AUTH_URL}?{qs}"


def parse_callback_query(query_string: str) -> dict[str, str]:
    """Parse the ?token=...&state=...&userId=... query string into a dict."""
    return {k: v[0] for k, v in urlparse.parse_qs(query_string).items() if v}


def open_browser(url: str) -> bool:
    """Open URL in the system browser. Returns False if no browser available."""
    try:
        return webbrowser.open(url, new=2)  # new=2 -> new tab if possible
    except webbrowser.Error:
        return False


# -----------------------------------------------------------------------------
# Token exchange + profile fetch
# -----------------------------------------------------------------------------

def _post_json(url: str, body: dict, headers: dict, timeout: float = 15.0) -> dict:
    req = urlrequest.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={**headers, "Content-Type": "application/json",
                 "Accept": "application/json"},
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


def _get_json(url: str, headers: dict, timeout: float = 15.0) -> dict:
    req = urlrequest.Request(url, method="GET",
                             headers={**headers, "Accept": "application/json"})
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


def exchange_token_for_api_key(short_lived_token: str) -> str:
    """Exchange the OAuth one-time token for a long-lived API key."""
    result = _post_json(
        f"{DAYDREAM_API_BASE}/v1/api-key",
        body={"name": API_KEY_NAME},
        headers={"Authorization": f"Bearer {short_lived_token}"},
    )
    api_key = result.get("api_key") or result.get("apiKey") or result.get("key")
    if not api_key:
        raise OAuthError(f"API key response missing api_key: {result}")
    return api_key


def fetch_profile(api_key: str) -> dict:
    """Fetch /users/profile. Best-effort, returns {} on failure."""
    try:
        return _get_json(
            f"{DAYDREAM_API_BASE}/users/profile",
            headers={"Authorization": f"Bearer {api_key}"},
        )
    except OAuthError:
        return {}


def complete_auth(token: str) -> AuthProfile:
    """One-shot: token → api_key → profile. Caller has already verified state."""
    api_key = exchange_token_for_api_key(token)
    raw = fetch_profile(api_key)
    return AuthProfile(
        api_key=api_key,
        user_id=raw.get("id") or raw.get("userId") or raw.get("user_id"),
        email=raw.get("email"),
        display_name=raw.get("email") or raw.get("name") or raw.get("username"),
        is_admin=bool(raw.get("isAdmin")),
    )


# -----------------------------------------------------------------------------
# Callback page HTML
# -----------------------------------------------------------------------------

CALLBACK_OK_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Daydream sign-in complete</title>
<style>
  body{font-family:-apple-system,system-ui,sans-serif;display:grid;place-items:center;
       min-height:100vh;margin:0;background:#0c0c10;color:#e5e7eb}
  .card{padding:2rem 2.5rem;border:1px solid #27272a;border-radius:12px;text-align:center;background:#18181b}
  h1{margin:0 0 .5rem;font-size:1.25rem}
  p{margin:0;color:#a1a1aa}
</style></head>
<body><div class="card"><h1>You're signed in.</h1><p>You can close this tab and return to TouchDesigner.</p></div>
<script>setTimeout(()=>window.close(),400);</script></body></html>
"""

CALLBACK_ERR_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Sign-in failed</title>
<style>
  body{font-family:-apple-system,system-ui,sans-serif;display:grid;place-items:center;
       min-height:100vh;margin:0;background:#0c0c10;color:#e5e7eb}
  .card{padding:2rem 2.5rem;border:1px solid #7f1d1d;border-radius:12px;text-align:center;background:#18181b;max-width:32rem}
  h1{margin:0 0 .5rem;font-size:1.25rem;color:#fca5a5}
  p{margin:0;color:#a1a1aa;font-family:ui-monospace,monospace;font-size:.85rem}
</style></head>
<body><div class="card"><h1>Sign-in failed</h1><p>{reason}</p></div></body></html>
"""
