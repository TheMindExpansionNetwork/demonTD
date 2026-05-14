"""Unit tests for src/queue_client.py — mocks urllib via unittest.mock.

We patch `urllib.request.urlopen` (the actual underlying call) rather than
using `responses`, which only intercepts the `requests` library.
"""

from __future__ import annotations

import io
import json
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

import queue_client as queue_mod


class FakeResponse:
    """Minimal context-manager mimicking urlopen's return value."""

    def __init__(self, body: dict):
        self._body = json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def _make_http_error(status: int, body: str = "") -> HTTPError:
    return HTTPError(
        url="http://h/api/queue/x",
        code=status,
        msg=body,
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(body.encode("utf-8")),
    )


@patch("queue_client.urlrequest.urlopen")
def test_join_active_immediately(mock_urlopen):
    mock_urlopen.return_value = FakeResponse({
        "status": "active",
        "sessionId": "abc123",
        "wsUrl": "ws://pod/?t=tok",
        "expiresAt": 1_700_000_000_000,
        "sessionDurationMs": 600_000,
        "extensionsUsed": 0,
    })
    c = queue_mod.QueueClient("http://localhost:8000")
    resp = c.join()
    assert resp.status == "active"
    assert resp.session_id == "abc123"
    assert resp.ws_url == "ws://pod/?t=tok"

    # Verify the request was a POST with empty JSON body.
    req = mock_urlopen.call_args.args[0]
    assert req.method == "POST"
    assert req.full_url == "http://localhost:8000/api/queue/join"
    assert req.data == b"{}"


@patch("queue_client.urlrequest.urlopen")
def test_join_includes_bearer_when_api_key_set(mock_urlopen):
    mock_urlopen.return_value = FakeResponse({"status": "active", "sessionId": "x"})
    queue_mod.QueueClient("http://h", api_key="sk-test").join()
    req = mock_urlopen.call_args.args[0]
    assert req.get_header("Authorization") == "Bearer sk-test"


@patch("queue_client.urlrequest.urlopen")
def test_join_omits_bearer_when_no_key(mock_urlopen):
    mock_urlopen.return_value = FakeResponse({"status": "active", "sessionId": "x"})
    queue_mod.QueueClient("http://h").join()
    req = mock_urlopen.call_args.args[0]
    assert req.get_header("Authorization") is None


@patch("queue_client.urlrequest.urlopen")
def test_status_queued(mock_urlopen):
    mock_urlopen.return_value = FakeResponse({
        "status": "queued",
        "sessionId": "s1",
        "position": 3,
        "estimatedWaitMs": 12000,
    })
    c = queue_mod.QueueClient("http://h")
    resp = c.status("s1")
    assert resp.status == "queued"
    assert resp.position == 3

    req = mock_urlopen.call_args.args[0]
    assert req.method == "GET"
    assert "token=s1" in req.full_url


@patch("queue_client.urlrequest.urlopen")
def test_extend(mock_urlopen):
    mock_urlopen.return_value = FakeResponse({
        "status": "active",
        "sessionId": "s1",
        "expiresAt": 1_700_000_000_000,
        "extensionsUsed": 1,
    })
    c = queue_mod.QueueClient("http://h")
    resp = c.extend("s1")
    assert resp.extensions_used == 1


@patch("queue_client.urlrequest.urlopen")
def test_leave_swallows_errors(mock_urlopen):
    mock_urlopen.side_effect = _make_http_error(500, "boom")
    queue_mod.QueueClient("http://h").leave("s1")  # should not raise


@patch("queue_client.urlrequest.urlopen")
def test_http_error_raises_queue_error(mock_urlopen):
    mock_urlopen.side_effect = _make_http_error(429, "rate-limited")
    c = queue_mod.QueueClient("http://h")
    with pytest.raises(queue_mod.QueueError) as exc:
        c.join()
    assert "429" in str(exc.value)
