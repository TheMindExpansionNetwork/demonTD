"""
Background-thread WebSocket client backed by the `websocket-client` library.

Why this exists
---------------
TouchDesigner's built-in WebSocket DAT can't actually transmit large binary
frames (~9 MB+) — it reports success but never delivers the bytes, so the
DEMON server drops us. Verified independently: an identical request from a
plain Python `websocket-client` succeeds and the server returns `ready`.

This module wraps `websocket-client` (vendored under
`vendor/websocket-client/`) in a background thread so TD can use a working
WebSocket from inside a COMP without going through the broken DAT.

Public API
----------
    ws = WSClient(
        url="ws://host:port/",
        on_open=lambda: ...,
        on_text=lambda s: ...,
        on_binary=lambda b: ...,
        on_close=lambda code, reason: ...,
        log=print,
    )
    ws.connect()
    ws.send_text("...")
    ws.send_binary(b"...")
    ws.close()

All callbacks run on the background recv thread. The owner (DemonExt) must
not block in callbacks and must marshal anything TD-touching to the cook
thread via `tdu.Dependency` / `op.cook()` if needed.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

# Lazy import — wired in DemonExt's _prepend_vendor_path step.
# Outside TD: websocket-client must be on PYTHONPATH.
import websocket  # type: ignore[import-not-found]


class WSClient:
    def __init__(
        self,
        url: str,
        on_open: Callable[[], None] | None = None,
        on_text: Callable[[str], None] | None = None,
        on_binary: Callable[[bytes], None] | None = None,
        on_close: Callable[[int | None, str | None], None] | None = None,
        log: Callable[[str], None] = print,
        timeout: float = 30.0,
    ):
        self.url = url
        self._on_open = on_open
        self._on_text = on_text
        self._on_binary = on_binary
        self._on_close = on_close
        self._log = log
        self._timeout = timeout

        self._ws: websocket.WebSocket | None = None
        self._thread: threading.Thread | None = None
        self._send_lock = threading.Lock()
        self._closing = False

    # --- state ----------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        ws = self._ws
        return ws is not None and ws.connected

    # --- lifecycle ------------------------------------------------------------

    def connect(self) -> None:
        """Open the WS and start the recv thread. Non-blocking."""
        if self._thread is not None and self._thread.is_alive():
            self._log(f"[ws_client] connect ignored — thread already running")
            return
        self._closing = False
        self._thread = threading.Thread(
            target=self._run, name=f"ws_client[{self.url}]", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        try:
            self._log(f"[ws_client] dialing {self.url}")
            self._ws = websocket.create_connection(self.url, timeout=self._timeout)
            self._log(f"[ws_client] connected to {self.url}")
        except Exception as e:
            self._log(f"[ws_client] connect failed: {type(e).__name__}: {e}")
            if self._on_close:
                try:
                    self._on_close(None, str(e))
                except Exception:
                    pass
            return

        if self._on_open:
            try:
                self._on_open()
            except Exception as e:
                self._log(f"[ws_client] on_open raised: {e}")

        # Recv loop
        close_code: int | None = None
        close_reason: str | None = None
        try:
            while not self._closing:
                try:
                    opcode, data = self._ws.recv_data(control_frame=False)
                except websocket.WebSocketConnectionClosedException as e:
                    close_reason = f"closed: {e}"
                    break
                except websocket.WebSocketTimeoutException:
                    # Timeout on recv is normal during quiet periods; loop
                    continue
                except Exception as e:
                    close_reason = f"recv error: {type(e).__name__}: {e}"
                    break

                if opcode == websocket.ABNF.OPCODE_TEXT:
                    if isinstance(data, bytes):
                        try:
                            text = data.decode("utf-8")
                        except UnicodeDecodeError:
                            text = data.decode("utf-8", errors="replace")
                    else:
                        text = data
                    if self._on_text:
                        try:
                            self._on_text(text)
                        except Exception as e:
                            self._log(f"[ws_client] on_text raised: {e}")
                elif opcode == websocket.ABNF.OPCODE_BINARY:
                    if self._on_binary:
                        try:
                            self._on_binary(data)
                        except Exception as e:
                            self._log(f"[ws_client] on_binary raised: {e}")
                elif opcode == websocket.ABNF.OPCODE_CLOSE:
                    close_reason = "server sent close"
                    break
                # Ping/Pong handled by recv_data's control_frame=False filter.
        finally:
            try:
                if self._ws is not None:
                    close_code = getattr(self._ws, "close_status_code", None)
                    self._ws.close()
            except Exception:
                pass
            self._log(f"[ws_client] closed (code={close_code}, reason={close_reason!r})")
            if self._on_close:
                try:
                    self._on_close(close_code, close_reason)
                except Exception:
                    pass
            self._ws = None

    def close(self, code: int = 1000, reason: str = "") -> None:
        """Close the connection and stop the recv thread."""
        self._closing = True
        ws = self._ws
        if ws is not None:
            try:
                ws.close(status=code, reason=reason.encode("utf-8") if reason else b"")
            except Exception:
                pass
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    # --- send -----------------------------------------------------------------

    def send_text(self, msg: str) -> bool:
        """Thread-safe text send. Returns True on success."""
        ws = self._ws
        if ws is None or not ws.connected:
            self._log(f"[ws_client] send_text: not connected")
            return False
        try:
            with self._send_lock:
                ws.send(msg, opcode=websocket.ABNF.OPCODE_TEXT)
            return True
        except Exception as e:
            self._log(f"[ws_client] send_text failed: {type(e).__name__}: {e}")
            return False

    def send_binary(self, payload: bytes) -> bool:
        """Thread-safe binary send. Returns True on success."""
        ws = self._ws
        if ws is None or not ws.connected:
            self._log(f"[ws_client] send_binary: not connected")
            return False
        try:
            with self._send_lock:
                ws.send(payload, opcode=websocket.ABNF.OPCODE_BINARY)
            return True
        except Exception as e:
            self._log(f"[ws_client] send_binary failed: {type(e).__name__}: {e}")
            return False
