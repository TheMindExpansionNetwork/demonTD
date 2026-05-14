"""Unit tests for src/oauth.py — pure-Python pieces (no network)."""

from __future__ import annotations

import oauth


def test_generate_state_is_hex_64_chars():
    s = oauth.generate_state()
    assert len(s) == 64
    int(s, 16)  # must be valid hex


def test_generate_state_unique():
    s1 = oauth.generate_state()
    s2 = oauth.generate_state()
    assert s1 != s2


def test_find_free_port_in_range():
    port = oauth.find_free_port(50000, 51000)
    assert 50000 <= port <= 51000


def test_build_signin_url_includes_required_params():
    state = "abc123"
    url = oauth.build_signin_url(50001, state)
    assert url.startswith("https://app.daydream.live/sign-in/local?")
    assert "redirect_url=http%3A%2F%2F127.0.0.1%3A50001%2Fcb" in url
    assert f"state={state}" in url
    assert "utm_source=daydream-td" in url


def test_parse_callback_query():
    params = oauth.parse_callback_query("token=tok123&state=abc&userId=u9")
    assert params == {"token": "tok123", "state": "abc", "userId": "u9"}


def test_parse_callback_query_empty():
    assert oauth.parse_callback_query("") == {}
