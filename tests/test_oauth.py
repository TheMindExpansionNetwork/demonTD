"""Unit tests for src/oauth.py.

v0.2.5 reduced oauth.py to a single validation helper (`fetch_profile`)
+ its error type. The historic OAuth-flow helpers (generate_state,
find_free_port, build_signin_url, parse_callback_query, OAuth token
exchange) were removed along with the Sign-in-via-browser pulse.
"""

from __future__ import annotations

import io
import json
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

import oauth


class FakeResponse:
    """Minimal urlopen-context-manager stand-in."""

    def __init__(self, body: dict):
        self._body = json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self._body


def _http_error(status: int, body: str = "") -> HTTPError:
    return HTTPError(
        url="https://api.daydream.live/users/profile",
        code=status,
        msg=body,
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(body.encode("utf-8")),
    )


@patch("oauth.urlrequest.urlopen")
def test_fetch_profile_success(mock_urlopen):
    mock_urlopen.return_value = FakeResponse({
        "id": "user_abc",
        "email": "hunter@livepeer.org",
        "isAdmin": False,
    })
    profile = oauth.fetch_profile("sk-test")
    assert profile["id"] == "user_abc"
    assert profile["email"] == "hunter@livepeer.org"

    # Bearer header is on the request.
    req = mock_urlopen.call_args.args[0]
    assert req.method == "GET"
    assert req.full_url == "https://api.daydream.live/users/profile"
    assert req.get_header("Authorization") == "Bearer sk-test"
    # User-Agent advertises the client to the cloud orchestrator
    # (DaydreamDEMON-TD/<ver>, mirrors rtmg-vst#7). urllib stores header
    # keys capitalized, so it reads back as "User-agent".
    ua = req.get_header("User-agent")
    assert ua == oauth.USER_AGENT
    assert ua.startswith("DaydreamDEMON-TD/")


@patch("oauth.urlrequest.urlopen")
def test_fetch_profile_unauthorized_raises(mock_urlopen):
    mock_urlopen.side_effect = _http_error(401, '{"error": "unauthorized"}')
    with pytest.raises(oauth.OAuthError) as exc:
        oauth.fetch_profile("bad-key")
    assert "401" in str(exc.value)


@patch("oauth.urlrequest.urlopen")
def test_fetch_profile_non_dict_returns_empty(mock_urlopen):
    # If the server ever returns a JSON array or bare value, treat as
    # "no profile" rather than a key-shape crash. PromptForApiKey reads
    # this empty dict and rejects the key with a user-friendly error.
    mock_urlopen.return_value = FakeResponse([])  # type: ignore[arg-type]
    assert oauth.fetch_profile("sk-test") == {}
