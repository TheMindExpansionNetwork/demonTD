"""
DemonExt — the TouchDesigner extension class for the DEMON operator.

This module is loaded inside TD as the Extension of a Base COMP. It owns
session state, drives the WebSocket DAT, fans parameter changes out at the
8ms tick, and exposes a clean public API (PascalCase methods) for other TD
networks to call.

Internal operators expected inside the COMP
-------------------------------------------
- ws1            : WebSocket DAT (Receive Binary on)
- http_queue     : Web Client DAT (queue API calls; we mostly use src/queue.py
                   for HTTP, so http_queue is optional/legacy)
- oauth_server   : Web Server DAT (OAuth callback listener; started on demand)
- param_exec1    : Parameter Execute DAT pointing at this COMP's custom pages
- tick8ms        : Timer CHOP, segment 0.008s, cycles infinite
- heartbeat      : Timer CHOP, segment 5s, cycles infinite
- audio_in       : In CHOP (the COMP's CHOP input port)
- resample_in    : Resample CHOP, target 48000 Hz
- script_send    : Script CHOP (encodes input audio + sends on WS)
- audio_out      : Script CHOP feeding the COMP's CHOP output port
- resample_out   : Resample CHOP (48k -> project rate)
- lora_catalog   : Table DAT (server-provided LoRA list)
- state          : Table DAT (session state for UI binding)

Threading
---------
TD calls extension methods from the cook thread. WebSocket DAT callbacks fire
on its own thread (we keep that work minimal — parse + write to ring buffer).
Access to mutable state (self._dirty, self._connected, ring buffer) is
protected by locks where needed.
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
from typing import Any

# --- vendored dependencies -----------------------------------------------------
# Bundled libs live under <repo>/vendor/.
#   - zstandard: per-platform wheels for compressed audio slice decompression.
#   - websocket-client: pure-Python, replaces TD's broken WebSocket DAT.
def _prepend_vendor_paths() -> None:
    try:
        import platform
        sysname = platform.system().lower()
        machine = platform.machine().lower()
        if sysname == "darwin":
            zstd_plat = "darwin-arm64" if "arm" in machine else "darwin-x64"
        elif sysname == "windows":
            zstd_plat = "win-amd64"
        else:
            zstd_plat = None

        # Discover vendor/. Try several candidate base paths in order:
        #   1. Path that THIS DAT is file-synced from (most reliable)
        #   2. The COMP's externaltox load location
        #   3. The TD project folder
        #   4. cwd
        candidates: list[str] = []
        try:
            # `me` is the Text DAT this code is compiled into.
            dat_file = me.par.file.eval()  # type: ignore[name-defined]  # noqa: F821
            if dat_file:
                # demon_ext.py lives at <repo>/src/demon_ext.py — vendor is at <repo>/vendor.
                candidates.append(os.path.abspath(
                    os.path.join(os.path.dirname(dat_file), os.pardir, "vendor")))
        except Exception:
            pass
        try:
            comp = me.owner  # type: ignore[name-defined]  # noqa: F821
            extox = comp.par.externaltox.eval() or ""
            if extox:
                base = os.path.dirname(extox) if extox.endswith(".tox") else extox
                candidates.append(os.path.join(base, "vendor"))
                candidates.append(os.path.join(os.path.dirname(base), "vendor"))
        except Exception:
            pass
        try:
            pf = project.folder  # type: ignore[name-defined]  # noqa: F821
            if pf:
                for n in range(4):
                    p = pf
                    for _ in range(n):
                        p = os.path.dirname(p)
                    candidates.append(os.path.join(p, "vendor"))
        except Exception:
            pass
        candidates.append(os.path.join(os.getcwd(), "vendor"))

        vendor_root = None
        for c in candidates:
            if c and os.path.isdir(c):
                vendor_root = c
                break

        if not vendor_root:
            print(f"[demon_ext] WARNING: vendor/ not found. Tried: {candidates}")
            return

        print(f"[demon_ext] vendor at {vendor_root}")

        # zstandard (platform-specific)
        if zstd_plat:
            zstd_dir = os.path.join(vendor_root, "zstandard", zstd_plat)
            if os.path.isdir(zstd_dir) and zstd_dir not in sys.path:
                sys.path.insert(0, zstd_dir)
                print(f"[demon_ext]   + {zstd_dir}")
        # websocket-client (pure-Python)
        wsc_dir = os.path.join(vendor_root, "websocket-client")
        if os.path.isdir(wsc_dir) and wsc_dir not in sys.path:
            sys.path.insert(0, wsc_dir)
            print(f"[demon_ext]   + {wsc_dir}")
    except Exception as e:
        print(f"[demon_ext] _prepend_vendor_paths failed: {e}")

_prepend_vendor_paths()

try:
    import zstandard as zstd
    _ZSTD_DEC = zstd.ZstdDecompressor()
except Exception:
    _ZSTD_DEC = None

import numpy as np

# Sibling-module imports. Two environments:
#
#   1. TD: this file is the text of a Text DAT named `demon_ext` inside a
#      Base COMP. Sibling DATs (params, wire, etc.) are imported via the
#      TD-global `mod()` function — there is no real Python package.
#
#   2. Outside TD (unit tests): everything lives in src/ on sys.path, so
#      regular `import params` works.
#
# We pick the right one by checking whether `mod` is defined as a global.
try:
    _mod = mod  # type: ignore[name-defined]  # noqa: F821
    P = _mod('params')
    wire = _mod('wire')
    queue_mod = _mod('queue_client')
    oauth = _mod('oauth')
    audio_mod = _mod('audio')
    ws_client_mod = _mod('ws_client')
except NameError:
    import params as P  # type: ignore
    import wire  # type: ignore
    import queue_client as queue_mod  # type: ignore
    import oauth  # type: ignore
    import audio as audio_mod  # type: ignore
    import ws_client as ws_client_mod  # type: ignore


# Bump this on every meaningful change so the user can confirm at boot
# which build is actually loaded. Visible on the "DemonExt initialized" line.
BUILD_MARKER = "diag-dump-v1"

# Hard upper bound on source-audio duration. DEMON rejects longer.
MAX_SOURCE_SECONDS = 240

# Debug-only: where to dump WAV snapshots of decoded audio for offline
# inspection. Used by BUILD=diag-dump-* builds to isolate which side of
# the wire is corrupting bytes when playback comes out as static.
DEBUG_DUMP_DIR = "/tmp/demon-debug"


# -----------------------------------------------------------------------------
# DemonExt
# -----------------------------------------------------------------------------
class DemonExt:
    """The brain of the DEMON operator.

    All public methods are PascalCase (TD convention). Internal state and
    callbacks are snake_case.
    """

    # -------- lifecycle ------------------------------------------------------

    def __init__(self, ownerComp):
        self.ownerComp = ownerComp
        self._lock = threading.RLock()

        # Session state
        self._connected: bool = False
        self._session_id: str | None = None
        self._ws_url: str | None = None
        self._expires_at_ms: int | None = None
        self._extensions_used: int = 0
        self._playback_pos: int = 0  # samples

        # Auth
        self._api_key: str = ""
        self._oauth_state: str | None = None
        self._oauth_port: int | None = None

        # Param fanout
        self._dirty: dict[str, Any] = {}
        self._last_init_values: dict[str, Any] = {}

        # LoRA catalog (mirrors the Table DAT)
        self._lora_ids: list[str] = []

        # Audio buffer — DEMON's audio model is a LOOP, not a stream.
        # Server sends an initial buffer (typically 24s) that becomes the
        # full loop. Subsequent slices PATCH specific positions in the loop
        # via their `start_sample` field. Playback wraps continuously.
        # See src/audio.py for the LoopBuffer implementation.
        self._ring = audio_mod.LoopBuffer(channels=2)
        self._epoch: int = 0  # bumped on swap_ready; used to drop stale slices

        # WS client (Python thread; replaces TD's broken WebSocket DAT)
        self._wsc = None  # ws_client_mod.WSClient | None

        # Inbound message queue — populated by the WS recv thread, drained
        # on the main TD thread (OnTick / 8 ms timer). TD forbids touching
        # any operator from a non-main thread, so we MUST marshal here.
        self._inbound: "queue.Queue[tuple[str, Any]]" = queue.Queue()

        # Protocol state: after server sends `ready`, the FIRST binary
        # message is the raw float16 initial buffer (NO 23-byte header).
        # Subsequent binaries are slices. We flip this on `ready`.
        self._expecting_initial_buffer: bool = False

        self.log(f"DemonExt initialized — BUILD={BUILD_MARKER}")

    # -------- properties (public read-only) ----------------------------------

    @property
    def IsConnected(self) -> bool:
        return self._connected

    @property
    def Status(self) -> dict:
        with self._lock:
            return {
                "connected": self._connected,
                "session_id": self._session_id,
                "queue_position": self._read_par("Queueposition", 0),
                "expires_in": self._read_par("Expiresin", 0.0),
                "extensions_used": self._extensions_used,
                "buffered_samples": self._ring.available,
            }

    # -------- session lifecycle ----------------------------------------------

    def Connect(self, anonymous: bool | None = None,
                direct: bool | None = None) -> bool:
        """Open a session.

        Two modes:
          - Direct pod (default for localhost): skip queue, open WS straight
            at <serverurl> with the scheme swapped to ws://.
          - Queue: POST /api/queue/join, wait for active, open the returned wsUrl.
        """
        with self._lock:
            if self._connected:
                self.log("Connect: already connected")
                return True

            if anonymous is None:
                anonymous = bool(self._read_par("Anonymous", True))
            if direct is None:
                direct = bool(self._read_par("Directpod", True))

            base = self._read_par("Serverurl", "http://localhost:1318")
            api_key = None if anonymous else (self._read_par("Apikey", "") or None)
            self._api_key = api_key or ""

            if direct:
                ws_url = self._http_to_ws(base)
                self._session_id = None
                self._ws_url = ws_url
                self._expires_at_ms = None
                self._extensions_used = 0
                self._write_par("Queueposition", 0)
                self._set_status(f"Connecting to {ws_url}...")
                self._open_ws(ws_url)
                return True

            # Queue mode
            self._set_status("Joining queue...")
            client = queue_mod.QueueClient(base, api_key=api_key)
            try:
                resp = client.join()
            except queue_mod.QueueError as e:
                self._set_status(f"Join failed: {e}")
                self.log(f"Connect error: {e}")
                return False

            self._session_id = resp.session_id

            poll_start = time.time()
            while resp.status == "queued":
                self._set_status(f"Queued (position {resp.position})")
                self._write_par("Queueposition", resp.position or 0)
                if time.time() - poll_start > 300:
                    self._set_status("Queue timeout")
                    return False
                time.sleep(1.5)
                try:
                    resp = client.status(self._session_id or "")
                except queue_mod.QueueError as e:
                    self._set_status(f"Status poll failed: {e}")
                    return False

            if resp.status != "active":
                self._set_status(f"Unexpected status: {resp.status}")
                return False

            self._ws_url = resp.ws_url
            self._expires_at_ms = resp.expires_at
            self._extensions_used = resp.extensions_used or 0
            self._write_par("Queueposition", 0)

            if not self._ws_url:
                self._set_status("No wsUrl from server")
                return False

            self._open_ws(self._ws_url)
            return True

    @staticmethod
    def _http_to_ws(url: str) -> str:
        """http://h:p → ws://h:p, https://h:p → wss://h:p, otherwise verbatim."""
        if url.startswith("http://"):
            return "ws://" + url[len("http://"):]
        if url.startswith("https://"):
            return "wss://" + url[len("https://"):]
        return url

    def Disconnect(self) -> None:
        """Close the WS cleanly (status 1000) and free the server-side session.

        Sends a proper WebSocket close frame so DEMON's handle_client returns
        promptly and frees its GPU memory. Without this the server's session
        lingers until the TCP connection times out (~30s+), and rapid
        reconnects pile up sessions and OOM the GPU.
        """
        with self._lock:
            if not self._connected and not self._session_id and self._wsc is None:
                return
            self._set_status("Disconnecting...")
            wsc = self._wsc
            self._wsc = None
            session_id = self._session_id
            self._connected = False
            self._session_id = None
            self._ws_url = None
            self._expires_at_ms = None
            self._dirty.clear()
            self._ring.clear()

        # Outside the lock: blocking I/O.
        if wsc is not None:
            try:
                # status=1000 is "normal closure" — tells DEMON's websockets
                # lib we're done and to clean up the session.
                wsc.close(code=1000, reason="client disconnect")
                self.log("Disconnect: WS closed cleanly")
            except Exception as e:
                self.log(f"Disconnect: close failed: {e}")

        if session_id:
            base = self._read_par("Serverurl", "http://localhost:1318")
            try:
                queue_mod.QueueClient(base, api_key=self._api_key or None).leave(
                    session_id
                )
            except queue_mod.QueueError:
                pass

        self._set_status("Idle")

    # --- TD lifecycle hooks ---------------------------------------------------

    def Cleanup(self) -> None:
        """Called when the COMP is deleted or the project is closing.

        Forces a Disconnect so the GPU session on DEMON is freed. Safe to
        call from multiple paths (TD project exit, COMP delete, __del__).
        """
        try:
            self.log("Cleanup: tearing down session")
        except Exception:
            pass
        try:
            self.Disconnect()
        except Exception:
            pass

    def __del__(self):
        # Best-effort: when the extension instance is garbage-collected, send
        # a close frame. Python doesn't guarantee __del__ runs in interpreter
        # shutdown but TD's typical COMP-delete path should hit it.
        try:
            wsc = getattr(self, "_wsc", None)
            if wsc is not None:
                wsc.close(code=1000, reason="extension teardown")
        except Exception:
            pass

    # -------- auth -----------------------------------------------------------

    def Authenticate(self) -> None:
        """Daydream OAuth — coming soon. This release supports local pod only."""
        self._set_status("Hosted mode coming soon — use a local pod for now")
        self.log("Authenticate(): hosted/Daydream auth is disabled in this release.")

    def OnAuthCallback(self, query_string: str) -> tuple[int, str, str]:
        """Called by the Web Server DAT's onHTTPRequest handler with the
        query portion of /cb?token=...&state=...

        Returns (http_status, content_type, body) for the response page.
        """
        params = oauth.parse_callback_query(query_string)
        token = params.get("token")
        state = params.get("state")

        if not token or not state:
            return 400, "text/html", oauth.CALLBACK_ERR_HTML.format(
                reason="Missing token or state"
            )

        if state != (self._oauth_state or ""):
            return 400, "text/html", oauth.CALLBACK_ERR_HTML.format(
                reason="CSRF state mismatch"
            )

        try:
            profile = oauth.complete_auth(token)
        except oauth.OAuthError as e:
            return 500, "text/html", oauth.CALLBACK_ERR_HTML.format(
                reason=str(e)
            )

        # Persist
        self.SetApiKey(profile.api_key)
        self._set_status(
            f"Authenticated{(' as ' + profile.display_name) if profile.display_name else ''}"
        )

        # Shut down the listener.
        try:
            server = self._oauth_server()
            if server is not None:
                server.par.active = False
        except Exception:
            pass

        self._oauth_state = None
        self._oauth_port = None
        return 200, "text/html", oauth.CALLBACK_OK_HTML

    def SetApiKey(self, key: str) -> None:
        """Store a Daydream API key (also called by Paste API Key pulse)."""
        with self._lock:
            self._api_key = key or ""
        self._write_par("Apikey", self._api_key)

    def PromptForApiKey(self) -> None:
        """Open a modal asking the user to paste an API key."""
        try:
            import ui  # type: ignore[name-defined]  # noqa: F401
            value = ui.messageBox(  # type: ignore[name-defined]
                "Paste Daydream API key",
                "Paste your Daydream API key:",
                buttons=["OK", "Cancel"],
            )
            if value:
                self.SetApiKey(value)
        except Exception:
            self.log("PromptForApiKey: 'ui' unavailable; paste into the API Key par directly")

    # -------- continuous param push ------------------------------------------

    def SetParam(self, name: str, value: Any) -> None:
        """One-shot: send a single param update immediately, bypassing the
        8ms batch. Use for events you want immediate response on.

        name : either a TD par name (e.g. 'Denoise') or a wire name (e.g. 'denoise')
        """
        wire_name = self._resolve_wire_name(name)
        if not wire_name:
            self.log(f"SetParam: unknown param {name}")
            return
        playback_sec = self._playback_pos / wire.SAMPLE_RATE
        self._send_text(wire.encode_params({wire_name: value}, playback_sec))

    def SetParams(self, d: dict[str, Any]) -> None:
        """Batch send a dict of param values (mixed TD-names and wire-names)."""
        raw: dict[str, Any] = {}
        for k, v in d.items():
            wn = self._resolve_wire_name(k)
            if wn:
                raw[wn] = v
        if raw:
            playback_sec = self._playback_pos / wire.SAMPLE_RATE
            self._send_text(wire.encode_params(raw, playback_sec))

    # -------- discrete messages ---------------------------------------------

    def SendPrompt(self, tags: str | None = None, key: str | None = None,
                   time_signature: str | None = None) -> None:
        tags = tags if tags is not None else (self._read_par("Prompt", "") or "")
        key = key if key is not None else (self._read_par("Key", "auto") or "auto")
        time_signature = (time_signature if time_signature is not None
                          else (self._read_par("Timesignature", "auto") or "auto"))
        self._send_text(wire.encode_prompt(tags, key=key, time_signature=time_signature))
        self.log(f"prompt: {tags!r} key={key} ts={time_signature}")

    def SetPromptBlend(self, value: float | None = None) -> None:
        v = value if value is not None else float(self._read_par("Promptblend", 0.4))
        self._send_text(wire.encode_set_prompt_blend(v))

    def EnableLora(self, id: str, strength: float = 1.0) -> None:
        self._send_text(wire.encode_enable_lora(id, strength=strength))

    def DisableLora(self, id: str) -> None:
        self._send_text(wire.encode_disable_lora(id))

    def SetTimbreStrength(self, value: float) -> None:
        self._send_text(wire.encode_set_timbre_strength(float(value)))

    def SetTimbreSource(self, chop: Any = None, name: str = "td_timbre",
                        file_path: str | None = None) -> None:
        """Upload audio as a timbre reference.

        Resolution order (matching the main Connect source):
          1. `file_path` arg if provided
          2. Timbre Source File par (if set)
          3. Wired CHOP input's .par.file (if upstream is an Audio File In)
          4. Snapshot of audio_in samples (last resort)
        """
        pcm = self._resolve_source_pcm(
            file_par_name="Timbresourcefile",
            file_path=file_path,
            chop_arg=chop,
        )
        if pcm is None:
            self.log("SetTimbreSource: no audio available")
            return
        self._send_text(wire.encode_set_timbre_source(name))
        self._send_bytes(wire.encode_audio_frame(pcm, channels=2))
        self.log(f"timbre source sent: {pcm.shape[1]} samples "
                 f"({pcm.shape[1] / wire.SAMPLE_RATE:.2f}s)")

    def SetTimbreFixture(self, name: str | None = None) -> None:
        n = name if name is not None else (self._read_par("Timbrefixture", "") or "")
        if not n:
            return
        self._send_text(wire.encode_set_timbre_fixture(n))

    def ClearTimbreSource(self) -> None:
        self._send_text(wire.encode_clear_timbre_source())

    def SetStructureSource(self, chop: Any = None, fixture: str | None = None,
                           name: str = "td_structure",
                           file_path: str | None = None) -> None:
        """Upload audio (or a fixture name) as a structure reference.

        Resolution: explicit fixture → file_path arg → Structure Source File
        par → wired CHOP file → CHOP snapshot.
        """
        if fixture:
            self._send_text(wire.encode_set_structure_fixture(fixture))
            return
        pcm = self._resolve_source_pcm(
            file_par_name="Structuresourcefile",
            file_path=file_path,
            chop_arg=chop,
        )
        if pcm is None:
            self.log("SetStructureSource: no audio available")
            return
        self._send_text(wire.encode_set_structure_source(name))
        self._send_bytes(wire.encode_audio_frame(pcm, channels=2))
        self.log(f"structure source sent: {pcm.shape[1]} samples "
                 f"({pcm.shape[1] / wire.SAMPLE_RATE:.2f}s)")

    def SetStructureFixture(self, name: str | None = None) -> None:
        n = name if name is not None else (self._read_par("Structurefixture", "") or "")
        if not n:
            return
        self._send_text(wire.encode_set_structure_fixture(n))

    def ClearStructureSource(self) -> None:
        self._send_text(wire.encode_clear_structure_source())

    def SwapSource(self, chop: Any = None, tags: str | None = None,
                   key: str | None = None,
                   time_signature: str | None = None,
                   fixture: str | None = None,
                   file_path: str | None = None) -> None:
        """Replace the current source track. Resolution: fixture → file_path
        arg → Swap Source File par → wired CHOP file → CHOP snapshot."""
        tags = tags if tags is not None else (self._read_par("Swaptags", "") or None)
        key = key if key is not None else (self._read_par("Key", "auto") or "auto")
        time_signature = (time_signature if time_signature is not None
                          else (self._read_par("Timesignature", "auto") or "auto"))

        header = wire.encode_swap_source(
            tags=tags, key=key, time_signature=time_signature,
            fixture_name=fixture,
        )
        self._send_text(header)
        if fixture:
            return
        pcm = self._resolve_source_pcm(
            file_par_name="Swapsourcefile",
            file_path=file_path,
            chop_arg=chop,
        )
        if pcm is not None:
            self._send_bytes(wire.encode_audio_frame(pcm, channels=2))
            self.log(f"swap source sent: {pcm.shape[1]} samples "
                     f"({pcm.shape[1] / wire.SAMPLE_RATE:.2f}s)")

    # -------- TD callbacks ---------------------------------------------------

    def OnParChange(self, par) -> None:
        """Called by param_exec1 when any custom par changes.

        Routes:
          - pulse with discrete kind -> dispatch handler
          - init par while connected -> revert + warn
          - continuous par -> drop into _dirty
          - session/local par -> ignored
        """
        name = par.name
        schema = P.PARAM_BY_NAME.get(name)
        if not schema:
            return

        # 1. Pulse actions
        if schema.type == "Pulse":
            self._handle_pulse(name)
            return

        # 2. Init param edited mid-session -> revert + warn
        if name in P.INIT_PARAM_NAMES and self._connected:
            prior = self._last_init_values.get(name, schema.default)
            try:
                par.val = prior
            except Exception:
                pass
            self._set_status("Reconnect to apply Init changes")
            return

        # 3. Continuous param -> batch
        if name in P.CONTINUOUS_PARAM_NAMES and schema.wire_name:
            value = self._coerce_par_value(par, schema)
            with self._lock:
                self._dirty[schema.wire_name] = value

    def OnTick(self) -> None:
        """Called by tick8ms Timer CHOP every ~50ms (MAIN THREAD).

        Two jobs:
          1. Drain the WS recv thread's inbound message queue (so server
             messages can safely touch TD operators).
          2. Flush pending continuous-param changes as a single batched
             {type:"params"} message.
        """
        # First-tick beacon so we can confirm the timer is firing.
        if not getattr(self, "_ticked_once", False):
            self._ticked_once = True
            self.log("OnTick: timer is running (first tick)")
        # 1. Drain inbound from WS thread FIRST so connect/open/text events
        #    process before any param sends try to use the connection.
        self._drain_inbound()

        # Periodic ring-buffer telemetry so we can see audio flowing in.
        # Logs at most every ~2 s once connected.
        if self._connected:
            now = time.time()
            last = getattr(self, "_last_buf_log", 0.0)
            if now - last > 2.0:
                self._last_buf_log = now
                buffered = self._ring.available
                buf_s = buffered / wire.SAMPLE_RATE
                self.log(f"buffered={buffered} samples ({buf_s:.2f}s)")

        if not self._connected:
            return

        with self._lock:
            if not self._dirty:
                # No dirty params — nothing to send. Playback position is
                # tracked by OnCookRecv from the loop buffer's read head,
                # not dead-reckoned in OnTick.
                return
            raw = dict(self._dirty)
            self._dirty.clear()

        # Use the loop buffer's actual read position (in seconds) as
        # playback_pos. Mirrors demon-public-demo's session.player.positionSec.
        playback_sec = self._ring.position / wire.SAMPLE_RATE
        try:
            self._send_text(wire.encode_params(raw, playback_sec))
        except Exception as e:
            self.log(f"OnTick send error: {e}")

    def OnHeartbeat(self) -> None:
        """Called by the 5s heartbeat Timer CHOP. Polls /api/queue/status."""
        if not self._connected or not self._session_id:
            return
        base = self._read_par("Serverurl", "http://localhost:1318")
        try:
            resp = queue_mod.QueueClient(base, api_key=self._api_key or None).status(
                self._session_id
            )
        except queue_mod.QueueError as e:
            self.log(f"Heartbeat poll failed: {e}")
            return

        if resp.expires_at:
            self._expires_at_ms = resp.expires_at
            now_ms = time.time() * 1000
            self._write_par("Expiresin", max(0.0, (resp.expires_at - now_ms) / 1000))

        if resp.status != "active":
            self.log(f"Heartbeat saw status={resp.status}; disconnecting")
            self.Disconnect()

    def OnReceive(self, dat, rowIndex=None, message=None,
                  contents=None, peer=None) -> None:
        """WebSocket DAT callback for incoming messages.

        TD's onReceiveText passes a string in `message`. onReceiveBinary
        passes raw bytes in `contents`. (Older versions passed it as `bytes`
        — see callbacks DAT shim.)

        We log every entry so we can diagnose if/why the server's `ready`
        message doesn't arrive.
        """
        try:
            self.log(f"OnReceive: message={'<text len=' + str(len(message)) + '>' if isinstance(message, str) else None} "
                     f"contents={'<binary len=' + str(len(contents)) + '>' if isinstance(contents, (bytes, bytearray)) else None}")
            if isinstance(contents, (bytes, bytearray)) and len(contents) > 0:
                self._on_binary(contents)
            elif isinstance(message, str) and message:
                self._on_text(message)
        except Exception as e:
            self.log(f"OnReceive error: {type(e).__name__}: {e}")

    def OnHTTPRequest(self, request_uri: str) -> tuple[int, str, str]:
        """Called by oauth_server (Web Server DAT) onHTTPRequest hook.

        Returns (status, content_type, body).
        """
        # request_uri looks like '/cb?token=...&state=...'
        path, _, query = request_uri.partition("?")
        if path.rstrip("/") == "/cb":
            return self.OnAuthCallback(query)
        return 404, "text/plain", "Not found"

    # -------- WS open + I/O --------------------------------------------------

    def _open_ws(self, ws_url: str) -> None:
        """Open a Python WebSocket to DEMON.

        We do NOT use TD's built-in WebSocket DAT — its sendBinary silently
        fails on payloads above ~few MB. Instead we run a `websocket-client`
        connection in a background thread (see ws_client.py).

        Resolution order:
          1. Snapshot init params for the revert-on-mid-session-edit guard.
          2. Resolve source audio (afconvert may take seconds).
          3. Stash _pending_* so the on_open callback can flush them.
          4. Construct WSClient and connect.
        """
        # 1. Snapshot init params for revert-on-mid-session-edit
        self._last_init_values = self._collect_init_params()

        # 2. Resolve source audio (slow)
        self._set_status("Loading source audio...")
        cfg = self._build_session_config()
        pcm = self._resolve_source_pcm()
        if pcm is None:
            self._set_status(
                "Set Source Audio File or wire an Audio File In CHOP, then reconnect"
            )
            return

        # 3. Stash for the on_open callback.
        sf = self._read_par("Sourcefile", "")
        if sf:
            source_label = os.path.basename(sf)
        else:
            wired = self._wired_chop_file_path()
            source_label = (f"wired CHOP file: {os.path.basename(wired)}"
                            if wired else "wired CHOP snapshot")
        self._pending_config = wire.encode_config(cfg)
        self._pending_audio = wire.encode_audio_frame(pcm, channels=2)
        self._pending_source_label = source_label
        self._pending_audio_samples = pcm.shape[1]
        self.log(f"_open_ws: pending {pcm.shape[1]} samples "
                 f"({pcm.shape[1] / wire.SAMPLE_RATE:.2f}s) from {source_label}")
        # Diagnostic: dump the EXACT PCM we're about to encode + send. If
        # this WAV plays correctly in Audacity/QuickTime, our source is
        # good and any garbage in the echo comes from the server side.
        try:
            peak = float(np.max(np.abs(pcm))) if pcm.size > 0 else 0.0
            mabs = float(np.mean(np.abs(pcm))) if pcm.size > 0 else 0.0
            self.log(f"[DIAG sent_to_server] shape={pcm.shape} "
                     f"dtype={pcm.dtype} peak={peak:.4f} mean_abs={mabs:.4f}")
        except Exception:
            pass
        self._dump_wav(
            os.path.join(DEBUG_DUMP_DIR, "sent_to_server.wav"),
            pcm, channels=2,
        )

        # 4. Close any prior client, build a new one, connect.
        if self._wsc is not None:
            try:
                self._wsc.close()
            except Exception:
                pass
            self._wsc = None

        self._set_status(f"Opening {ws_url}...")
        try:
            self._wsc = ws_client_mod.WSClient(
                url=ws_url,
                on_open=self._on_ws_open,
                on_text=self._on_ws_text,
                on_binary=self._on_ws_binary,
                on_close=self._on_ws_close,
                log=self.log,
                timeout=30.0,
            )
            self._wsc.connect()
            self.log(f"_open_ws: WSClient.connect() scheduled (thread starting)")
        except Exception as e:
            self.log(f"_open_ws: WSClient construct/connect failed: {e}")
            self._set_status(f"WS open failed: {e}")
            self._wsc = None

    # --- WSClient callbacks (background recv thread) -------------------------
    #
    # CRITICAL: these run on the websocket-client recv thread. TD forbids
    # touching any operator from a non-main thread (raises a modal dialog,
    # may even crash). All we do here is enqueue the event. The main thread
    # drains the queue from OnTick().

    def _on_ws_open(self) -> None:
        self._inbound.put(("open", None))

    def _on_ws_text(self, msg: str) -> None:
        self._inbound.put(("text", msg))

    def _on_ws_binary(self, payload: bytes) -> None:
        self._inbound.put(("binary", payload))

    def _on_ws_close(self, code, reason) -> None:
        self._inbound.put(("close", (code, reason)))

    def _drain_inbound(self) -> None:
        """Main-thread per-frame work. Called by frame_exec every frame:
           1. Drain WS recv-thread events into TD-safe handlers.
           2. Send a params message every frame to keep DEMON's pipeline
              generating (server pauses without continuous param flow).
           3. Periodic telemetry log.
        """
        # 1. Drain inbound queue
        max_per_tick = 64
        for _ in range(max_per_tick):
            try:
                kind, payload = self._inbound.get_nowait()
            except queue.Empty:
                break
            try:
                if kind == "open":
                    self.log("[ws_client] open — flushing config + audio")
                    self._flush_pending()
                elif kind == "text":
                    self.log(f"[ws_client] <- text {len(payload)}B: {payload[:120]!r}")
                    self._on_text(payload)
                elif kind == "binary":
                    self._on_binary(payload)
                elif kind == "close":
                    code, reason = payload
                    self.log(f"[ws_client] closed code={code} reason={reason!r}")
                    self._connected = False
                    self._set_status(f"Disconnected ({reason or 'closed'})")
            except Exception as e:
                self.log(f"_drain_inbound({kind}) error: {type(e).__name__}: {e}")

        # 2. Send params. Match demon-public-demo at ~125 Hz where possible
        #    (JS uses TICK_MS=8 setInterval). Our frame_exec runs at frame
        #    rate (~60 Hz), so effectively we send per frame ≈ every 16 ms.
        #    Throttle floor to 8 ms to be safe; immediate on dirty changes.
        if self._connected:
            with self._lock:
                raw = dict(self._dirty) if self._dirty else {}
                self._dirty.clear()
            now = time.time()
            last_send = getattr(self, "_last_params_send", 0.0)
            elapsed = now - last_send
            if elapsed > 0.008 or raw:
                # playback_sec mirrors demon-public-demo's positionSec:
                # the loop buffer's current read head, in seconds.
                playback_sec = self._ring.position / wire.SAMPLE_RATE
                try:
                    self._send_text(wire.encode_params(raw, playback_sec))
                    self._last_params_send = now
                except Exception as e:
                    self.log(f"frame param send error: {e}")

        # 3. Telemetry (~every 2 s)
        if self._connected:
            now = time.time()
            last = getattr(self, "_last_telem_log", 0.0)
            if now - last > 2.0:
                self._last_telem_log = now
                buffered = self._ring.available
                buf_s = buffered / wire.SAMPLE_RATE
                n_bin = getattr(self, "_n_binary_frames", 0)
                n_cook = getattr(self, "_n_cook_recv", 0)
                self.log(
                    f"telemetry: buffered={buffered} ({buf_s:.2f}s)  "
                    f"binary_frames_recv={n_bin}  audio_out_cooks={n_cook}"
                )

    @staticmethod
    def _parse_ws_url(url: str) -> tuple[str | None, int]:
        """ws://host:port/path → ('host', port). Defaults port 80/443."""
        try:
            from urllib.parse import urlparse
            u = urlparse(url)
            if u.scheme not in ("ws", "wss") or not u.hostname:
                return None, 0
            default_port = 443 if u.scheme == "wss" else 80
            return u.hostname, u.port or default_port
        except Exception:
            return None, 0

    def OnWsConnect(self, dat) -> None:
        """Called by the callbacks DAT's onConnect. We held back the config
        + source-audio frames until the socket was actually open."""
        try:
            self.log(f"OnWsConnect: ws connected ({dat.par.netaddress.eval()})")
        except Exception:
            self.log("OnWsConnect: ws connected")
        self._flush_pending()

    def _flush_pending(self) -> None:
        cfg = getattr(self, "_pending_config", None)
        audio = getattr(self, "_pending_audio", None)
        if cfg is None or audio is None:
            return
        # Reset session-state counters at the start of every successful flush.
        self._playback_pos = 0
        self._n_binary_frames = 0
        self._n_cook_recv = 0
        self._auto_enable_done = False
        self._lora_catalog_sig = None
        self.log("_flush_pending: sending config + audio")
        try:
            self.log(f"_flush_pending: config = {cfg}")
        except Exception:
            pass
        self._send_text(cfg)
        self._send_bytes(audio)
        self._connected = True
        self._set_status("Connected")
        self.log(
            f"sent {self._pending_audio_samples} samples "
            f"({self._pending_audio_samples / wire.SAMPLE_RATE:.2f}s) "
            f"from {self._pending_source_label}"
        )
        # One-shot — clear so we don't double-send on reconnects.
        self._pending_config = None
        self._pending_audio = None

    def _convert_to_wav(self, src_path: str) -> str | None:
        """Convert any audio file to a 16-bit 48 kHz stereo WAV in a temp file.

        Tries `afconvert` first (built into macOS), then `ffmpeg` (often
        installed on Mac via brew and standard on Linux/Windows).

        Returns the temp .wav path on success, or None.
        Caller is responsible for unlink-ing the temp file.
        """
        import shutil
        import subprocess
        import tempfile

        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.close()
        out = tmp.name

        # macOS afconvert.
        # IMPORTANT: do NOT pass --channellayout. It makes afconvert write
        # WAVE_FORMAT_EXTENSIBLE (format code 65534), which Python's stdlib
        # `wave` module rejects with 'unknown format: 65534'. Plain
        # LEI16@48000 produces vanilla PCM WAV (format 1).
        if shutil.which("afconvert"):
            try:
                r = subprocess.run(
                    ["afconvert", "-f", "WAVE", "-d", "LEI16@48000",
                     src_path, out],
                    capture_output=True, timeout=120,
                )
                if r.returncode == 0 and os.path.getsize(out) > 44:
                    self.log(f"_convert_to_wav: afconvert -> {os.path.basename(out)}")
                    return out
                else:
                    err = (r.stderr or b"").decode("utf-8", "replace").strip()
                    if err:
                        self.log(f"_convert_to_wav: afconvert rc={r.returncode}: {err[:200]}")
            except Exception as e:
                self.log(f"_convert_to_wav: afconvert failed: {e}")

        # ffmpeg
        if shutil.which("ffmpeg"):
            try:
                r = subprocess.run(
                    ["ffmpeg", "-y", "-i", src_path,
                     "-acodec", "pcm_s16le", "-ar", "48000", "-ac", "2", out],
                    capture_output=True, timeout=120,
                )
                if r.returncode == 0 and os.path.getsize(out) > 44:
                    self.log(f"_convert_to_wav: ffmpeg -> {os.path.basename(out)}")
                    return out
            except Exception as e:
                self.log(f"_convert_to_wav: ffmpeg failed: {e}")

        try:
            os.unlink(out)
        except Exception:
            pass
        return None

    def _resolve_source_pcm(self,
                            file_par_name: str | None = None,
                            file_path: str | None = None,
                            chop_arg: Any = None) -> "np.ndarray | None":
        """Shared source-audio resolution used by Connect, Swap, Timbre, Structure.

        Order of preference:
          1. explicit file_path arg
          2. file path from `file_par_name` (e.g. 'Timbresourcefile')
          3. wired CHOP input's .par.file (if upstream is an Audio File In)
          4. snapshot of audio_in samples (last resort, may be too short)
        """
        # 1. explicit arg
        if file_path:
            pcm = self._load_source_wav(file_path)
            if pcm is not None:
                return pcm

        # 2. par-driven file path
        if file_par_name:
            par_path = self._read_par(file_par_name, "") or ""
            if par_path:
                pcm = self._load_source_wav(par_path)
                if pcm is not None:
                    return pcm

        # 3. wired CHOP's file
        wired = self._wired_chop_file_path()
        if wired:
            pcm = self._load_source_wav(wired)
            if pcm is not None:
                return pcm

        # 4. snapshot
        return self._snapshot_input_chop()

    def _wired_chop_file_path(self) -> str | None:
        """If an upstream CHOP (e.g. Audio File In) is wired into the COMP's
        first input, return its `par.file` value. Otherwise None.

        TD's Audio File In CHOP exposes a `file` par with the WAV path.
        """
        try:
            upstream_ops = self.ownerComp.inputs or []
        except Exception:
            return None
        for up in upstream_ops:
            if up is None:
                continue
            try:
                file_par = getattr(up.par, "file", None)
                if file_par is None:
                    continue
                path = file_par.eval()
                if path:
                    return path
            except Exception:
                continue
        return None

    def _snapshot_input_chop(self) -> "np.ndarray | None":
        """Snapshot the COMP's wired CHOP input as (2, samples) float32 at 48k.

        Reads from the `audio_in` In CHOP (the COMP's CHOP input port). If
        nothing is wired, or the upstream produces zero samples, returns None.

        This is a one-shot snapshot at Connect time — not continuous streaming.
        """
        try:
            src = self.ownerComp.op("audio_in")
        except Exception:
            return None
        if src is None:
            return None
        try:
            n = int(src.numSamples)
            ch_count = int(src.numChans)
        except Exception:
            return None
        if n <= 0 or ch_count <= 0:
            return None
        try:
            ch_count = min(2, ch_count)
            pcm = np.empty((ch_count, n), dtype=np.float32)
            for i in range(ch_count):
                pcm[i] = np.fromiter(src[i].vals, dtype=np.float32, count=n)
            try:
                src_rate = int(src.rate) if src.rate else wire.SAMPLE_RATE
            except Exception:
                src_rate = wire.SAMPLE_RATE
            if src_rate != wire.SAMPLE_RATE:
                pcm = audio_mod.linear_resample(pcm, src_rate, wire.SAMPLE_RATE)
            pcm = audio_mod.to_stereo(pcm)
            # Cap to DEMON's max in case the snapshot is huge.
            max_samples = MAX_SOURCE_SECONDS * wire.SAMPLE_RATE
            if pcm.shape[1] > max_samples:
                pcm = pcm[:, :max_samples]
            return pcm.astype(np.float32, copy=False)
        except Exception as e:
            self.log(f"_snapshot_input_chop failed: {e}")
            return None

    def _load_source_wav(self, path: str) -> "np.ndarray | None":
        """Load an audio file off disk → (2, samples) float32 at 48 kHz.

        Primary loader is stdlib `wave` (RIFF/WAV, 8/16/32-bit). If that
        fails — typically because the file is MP3 / AAC / M4A / AIFF /
        FLAC — we transparently convert it to a temp WAV via the platform
        converter (`afconvert` on macOS, `ffmpeg` elsewhere) and reload.

        Mono is duplicated to stereo. Source rate is linearly resampled
        to 48 kHz if needed.

        Returns None if the file can't be opened or decoded by any path.
        """
        if not path:
            self.log("_load_source_wav: no Source Audio File set")
            return None
        if not os.path.exists(path):
            self.log(f"_load_source_wav: file not found: {path}")
            return None

        try:
            import wave
            with wave.open(path, "rb") as wf:
                nchannels = wf.getnchannels()
                sampwidth = wf.getsampwidth()
                framerate = wf.getframerate()
                nframes = wf.getnframes()
                raw = wf.readframes(nframes)
        except Exception as e:
            # Not a WAV — try converting to WAV first.
            self.log(f"_load_source_wav: {os.path.basename(path)} is not a WAV "
                     f"({e}); attempting auto-conversion...")
            converted = self._convert_to_wav(path)
            if converted is None:
                self.log("_load_source_wav: conversion failed; convert your "
                         "source to a WAV manually (Audacity, QuickTime export, "
                         "ffmpeg) and set Source Audio File.")
                return None
            try:
                import wave
                with wave.open(converted, "rb") as wf:
                    nchannels = wf.getnchannels()
                    sampwidth = wf.getsampwidth()
                    framerate = wf.getframerate()
                    nframes = wf.getnframes()
                    raw = wf.readframes(nframes)
            except Exception as e2:
                self.log(f"_load_source_wav: post-conversion decode failed: {e2}")
                return None
            finally:
                try:
                    os.unlink(converted)
                except Exception:
                    pass

        # Decode raw bytes by sample width.
        try:
            if sampwidth == 2:
                pcm_i16 = np.frombuffer(raw, dtype=np.int16)
                pcm = pcm_i16.astype(np.float32) / 32768.0
            elif sampwidth == 3:
                # 24-bit packed — uncommon path.
                self.log("_load_source_wav: 24-bit WAV not supported; convert to 16-bit or 32-bit float")
                return None
            elif sampwidth == 4:
                # Either int32 or float32. wave doesn't tell us; assume float32.
                pcm = np.frombuffer(raw, dtype=np.float32).copy()
            elif sampwidth == 1:
                pcm_u8 = np.frombuffer(raw, dtype=np.uint8)
                pcm = (pcm_u8.astype(np.float32) - 128.0) / 128.0
            else:
                self.log(f"_load_source_wav: unsupported sample width: {sampwidth}")
                return None
        except Exception as e:
            self.log(f"_load_source_wav: decode failed: {e}")
            return None

        # De-interleave to (channels, samples)
        if nchannels > 1:
            try:
                pcm = pcm.reshape(-1, nchannels).T
            except Exception as e:
                self.log(f"_load_source_wav: de-interleave failed: {e}")
                return None
        else:
            pcm = pcm.reshape(1, -1)

        # Resample to 48 kHz if needed
        if framerate != wire.SAMPLE_RATE:
            pcm = audio_mod.linear_resample(pcm, framerate, wire.SAMPLE_RATE)

        # Force stereo (mono → duplicated L→R; >2 channels → first two)
        pcm = audio_mod.to_stereo(pcm)

        # Cap to DEMON's max source duration (240 s). Server rejects longer.
        max_samples = MAX_SOURCE_SECONDS * wire.SAMPLE_RATE
        if pcm.shape[1] > max_samples:
            self.log(
                f"WARNING: source is {pcm.shape[1] / wire.SAMPLE_RATE:.1f}s; "
                f"trimming to {MAX_SOURCE_SECONDS}s (DEMON max)."
            )
            pcm = pcm[:, :max_samples]

        return pcm.astype(np.float32, copy=False)

    def _build_session_config(self) -> dict[str, Any]:
        """Build the SessionConfig JSON to send right after WS open.

        Matches demon-public-demo's useStartSession.ts buildConfig() exactly,
        in the same field order. Sends all 13 fields every time (the JS
        client does too); the server type allows extras but we don't add
        any to minimize chance of a strict-parser rejection.
        """
        def init_val(td_name: str, default: Any) -> Any:
            return self._read_par(td_name, default)

        cfg: dict[str, Any] = {
            "sde":          bool(init_val("Sde", False)),
            "lora":         bool(init_val("Lora", True)),
            "depth":        int(init_val("Depth", 4)),
            "vae_window":   float(init_val("Vaewindow", 3.0)),
            "crop":         float(init_val("Crop", 0.0)),
            "steps":        int(init_val("Steps", 8)),
            "fast_vae":     bool(init_val("Fastvae", True)),
            "walk_window":  bool(init_val("Walkwindow", False)),
            "walk_window_s": float(init_val("Walkwindows", 60.0)),
            "enabled_loras": self._enabled_loras(),
            "prompt":       str(init_val("Initprompt", "instrumental music")),
            "lora_strengths": self._lora_strengths(),
            "fixture_name": str(init_val("Fixturename", "")),
        }
        return cfg

    @staticmethod
    def _lora_par_safe(lid: str) -> str:
        """Sanitize a LoRA id into a TD-legal par-name suffix.

        TD rules: custom par name must begin uppercase, then lowercase
        letters and digits only (no underscores), and a 'sequence parameter'
        cannot end with a digit. We strip non-alphanumerics, lowercase,
        and append 'x' if trailing-digit.
        """
        safe = "".join(c for c in lid if c.isalnum()).lower()
        if safe and safe[-1].isdigit():
            safe += "x"
        return safe or "unnamed"

    def _enabled_loras(self) -> list[str]:
        """Read which LoRAs are currently enabled. Toggle pars use the
        sanitized name (e.g. 'Loraenablebach' for id='bach')."""
        out: list[str] = []
        for lora_id in self._lora_ids:
            safe = self._lora_par_safe(lora_id)
            par = self._par_by_name(f"Loraenable{safe}")
            if par and par.eval():
                out.append(lora_id)
        return out

    def _lora_strengths(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for lora_id in self._lora_ids:
            safe = self._lora_par_safe(lora_id)
            par = self._par_by_name(f"Lorastr{safe}")
            if par:
                out[lora_id] = float(par.eval())
        return out

    # -------- WS message handlers --------------------------------------------

    def _on_text(self, msg: str) -> None:
        try:
            data = wire.decode_control(msg)
        except Exception as e:
            self.log(f"Bad WS text: {e}")
            return

        kind = data.get("type", "")
        if kind == "ready":
            self.log(f"server ready: ch={data.get('channels')} sr={data.get('sample_rate')}")
            # The very next binary frame is the initial buffer (raw float16
            # PCM with NO header — NOT slice format). Flag it so _on_binary
            # decodes accordingly.
            self._expecting_initial_buffer = True
            self._ready_channels = int(data.get("channels", 2)) or 2
            cat = data.get("lora_catalog") or []
            self._apply_lora_catalog(cat)
        elif kind == "lora_catalog":
            self._apply_lora_catalog(data.get("catalog") or [])
        elif kind == "params_update":
            # Server-echoed param values; could be displayed but we don't overwrite UI.
            pass
        elif kind == "prompt_applied":
            self.log(f"prompt applied: {data.get('tags')}")
        elif kind == "swap_ready":
            self._epoch += 1
            self._ring.clear()
            # Next binary is again the raw float16 initial buffer for the new track.
            self._expecting_initial_buffer = True
            self._ready_channels = int(data.get("channels", 2)) or 2
            self.log(f"swap_ready ch={data.get('channels')}")
        elif kind in ("timbre_set", "timbre_cleared", "structure_set",
                      "structure_cleared"):
            self.log(kind)
        elif kind in ("timbre_failed", "structure_failed", "swap_failed", "error"):
            self.log(f"server {kind}: {data.get('error') or data.get('message')}")
            self._set_status(f"Error: {kind}")
        else:
            self.log(f"unknown server message: {kind}")

    def _dump_wav(self, path: str, pcm: np.ndarray, channels: int,
                  sample_rate: int = 48000) -> None:
        """Diagnostic: write a (channels, frames) float32 ndarray as int16
        WAV. Best-effort — failures log but don't propagate."""
        try:
            import wave
            os.makedirs(os.path.dirname(path), exist_ok=True)
            pcm = np.asarray(pcm, dtype=np.float32)
            # Normalize shape to (channels, frames).
            if pcm.ndim == 1:
                # Assume interleaved.
                frames = pcm.shape[0] // channels
                pcm = pcm[: frames * channels].reshape(frames, channels).T
            elif pcm.ndim == 2 and pcm.shape[0] != channels and pcm.shape[1] == channels:
                pcm = pcm.T
            frames = pcm.shape[1]
            # Re-interleave for WAV.
            interleaved = pcm.T.reshape(-1)
            clipped = np.clip(interleaved, -1.0, 1.0)
            i16 = np.int16(clipped * 32767.0)
            with wave.open(path, "wb") as wf:
                wf.setnchannels(channels)
                wf.setsampwidth(2)
                wf.setframerate(sample_rate)
                wf.writeframes(i16.tobytes())
            self.log(f"_dump_wav: wrote {path} ({frames} frames, ch={channels})")
        except Exception as e:
            self.log(f"_dump_wav failed for {path}: {e}")

    def _on_binary(self, buf: bytes) -> None:
        # Counter for telemetry
        self._n_binary_frames = getattr(self, "_n_binary_frames", 0) + 1

        # The first binary frame after `ready` (or `swap_ready`) is the
        # raw float16 initial buffer — interleaved PCM, no 23-byte slice
        # header. This becomes the full loop content. Subsequent frames
        # are slices that patch specific positions in the loop.
        if self._expecting_initial_buffer:
            self._expecting_initial_buffer = False
            ch = getattr(self, "_ready_channels", 2) or 2
            try:
                u16 = np.frombuffer(bytes(buf), dtype=np.uint16)
                pcm = u16.view(np.float16).astype(np.float32)
                n = pcm.size // ch
                if n <= 0:
                    self.log(f"initial buffer: empty")
                    return
                # ---- DIAGNOSTIC ----
                try:
                    head_hex = bytes(buf[:32]).hex(" ")
                    peak = float(np.max(np.abs(pcm))) if pcm.size > 0 else 0.0
                    mabs = float(np.mean(np.abs(pcm))) if pcm.size > 0 else 0.0
                    self.log(
                        f"[DIAG initial_buffer] bytes={len(buf)} "
                        f"head32={head_hex}"
                    )
                    self.log(
                        f"[DIAG initial_buffer] decoded peak={peak:.4f} "
                        f"mean_abs={mabs:.4f} first10={pcm[:10].tolist()}"
                    )
                except Exception as e:
                    self.log(f"[DIAG initial_buffer] log failed: {e}")
                # Dump to disk for offline inspection.
                self._dump_wav(
                    os.path.join(DEBUG_DUMP_DIR, "initial_buffer.wav"),
                    pcm[: n * ch], ch,
                )
                # Initialize the loop with this buffer (channels, frames).
                self._ring.init(pcm[: n * ch], channels=ch)
                # Reset per-session slice debug counter so dumps are
                # named slice_0/1/2 on every reconnect.
                self._debug_slice_count = 0
                self.log(
                    f"initial buffer: {n} frames ({n / wire.SAMPLE_RATE:.2f}s) "
                    f"ch={ch} — loop initialized"
                )
            except Exception as e:
                self.log(f"initial buffer decode failed: {e}")
            return

        # Streaming slice (23-byte header + raw/zstd float16). Each slice
        # PATCHES the loop at slice.start_sample. Flag bit 1 = delta (mix),
        # otherwise overwrite. Mirrors useStartSession.ts.
        try:
            slice_ = wire.decode_slice(buf, zstd_dec=_ZSTD_DEC)
        except Exception as e:
            self.log(f"Bad slice ({len(buf)}B, flags=0x{buf[0]:02x}): {e}")
            return

        ch = max(1, slice_.channels)
        n = slice_.pcm.size // ch
        if n <= 0:
            return

        # ---- DIAGNOSTIC: log + dump first 3 slices ----
        idx = getattr(self, "_debug_slice_count", 0)
        if idx < 3:
            try:
                peak = float(np.max(np.abs(slice_.pcm))) if slice_.pcm.size > 0 else 0.0
                mabs = float(np.mean(np.abs(slice_.pcm))) if slice_.pcm.size > 0 else 0.0
                self.log(
                    f"[DIAG slice_{idx}] flags={slice_.flags} "
                    f"start_sample={slice_.start_sample} num_samples={slice_.num_samples} "
                    f"channels={slice_.channels} peak={peak:.4f} mean_abs={mabs:.4f} "
                    f"first10={slice_.pcm[:10].tolist()}"
                )
            except Exception:
                pass
            self._dump_wav(
                os.path.join(DEBUG_DUMP_DIR, f"slice_{idx}.wav"),
                slice_.pcm[: n * ch], ch,
            )
            self._debug_slice_count = idx + 1

        if slice_.flags == wire.SLICE_FLAG_DELTA:
            self._ring.add_delta(slice_.start_sample, slice_.pcm[: n * ch])
        else:
            self._ring.patch(slice_.start_sample, slice_.pcm[: n * ch])

    # -------- LoRA catalog ---------------------------------------------------

    def _apply_lora_catalog(self, catalog: list[dict]) -> None:
        """Update Table DAT + dynamic per-LoRA params on the Prompt+LoRA page.

        The server echoes lora_catalog on every state change (e.g. when we
        send enable_lora). Skip redundant work if the catalog shape hasn't
        changed — otherwise we churn the UI 100x/second and starve the
        receive thread.
        """
        sig = tuple(sorted(e.get("id", "") for e in catalog))
        if sig == getattr(self, "_lora_catalog_sig", None):
            return
        self._lora_catalog_sig = sig

        table = self.ownerComp.op("lora_catalog")
        if table is not None:
            try:
                table.clear()
                table.appendRow(["id", "name", "default_strength"])
                for entry in catalog:
                    table.appendRow([
                        entry.get("id", ""),
                        entry.get("name") or entry.get("id", ""),
                        entry.get("strength", 1.0),
                    ])
            except Exception as e:
                self.log(f"lora_catalog write failed: {e}")

        ids = [e.get("id", "") for e in catalog if e.get("id")]
        with self._lock:
            self._lora_ids = ids

        # Dynamically add Toggle + Float par per LoRA on the Prompt+LoRA page.
        # Par names must be TD-legal: start uppercase, only lowercase/digits
        # afterwards, no underscores, no trailing digit. Use _lora_par_safe.
        #
        # NOTE: appendToggle/appendFloat return a ParGroup whose truthiness
        # is unsupported in TD — must check `is not None` not `if tp:`.
        DEFAULT_ON = {"bach"}  # LoRAs to enable by default

        try:
            page = self._page_by_name("Prompt+LoRA")
            if page is None:
                self.log("LoRA page: 'Prompt+LoRA' not found")
                return
            existing = {p.name for p in page.pars}
            n_added = 0
            for entry in catalog:
                lid = entry.get("id", "")
                if not lid:
                    continue
                safe = self._lora_par_safe(lid)
                toggle_name = f"Loraenable{safe}"
                strength_name = f"Lorastr{safe}"
                default_on = lid in DEFAULT_ON

                if toggle_name not in existing:
                    try:
                        tp = page.appendToggle(
                            toggle_name,
                            label=f"{entry.get('name', lid)} on"
                        )
                        if tp is not None:
                            try:
                                tp[0].default = default_on
                                tp[0].val = default_on
                            except Exception:
                                pass
                        n_added += 1
                    except Exception as e:
                        self.log(f"LoRA toggle {toggle_name} failed: "
                                 f"{type(e).__name__}: {e}")

                if strength_name not in existing:
                    try:
                        sp = page.appendFloat(
                            strength_name,
                            label=f"{entry.get('name', lid)} strength"
                        )
                        if sp is not None:
                            try:
                                sp[0].normMin = 0.0
                                sp[0].normMax = 1.8
                                sp[0].clampMin = True
                                sp[0].clampMax = True
                                default_strength = float(entry.get("strength", 1.0))
                                sp[0].default = default_strength
                                sp[0].val = default_strength
                            except Exception:
                                pass
                        n_added += 1
                    except Exception as e:
                        self.log(f"LoRA float {strength_name} failed: "
                                 f"{type(e).__name__}: {e}")
            self.log(f"LoRA page: added {n_added} pars for "
                     f"{len(catalog)} LoRAs")

            # Auto-enable default-on LoRAs ONCE per session. The server
            # echoes a new lora_catalog every time we send enable_lora,
            # which re-triggers this handler — so a naive re-enable loops
            # forever, blocking the recv thread and starving audio slices.
            if not getattr(self, "_auto_enable_done", False):
                self._auto_enable_done = True
                for entry in catalog:
                    lid = entry.get("id", "")
                    if lid in DEFAULT_ON:
                        # Server-reported strength may be 0 before the LoRA
                        # is loaded — always send 1.0 for default-on LoRAs.
                        strength = 1.0
                        try:
                            self._send_text(wire.encode_enable_lora(lid, strength))
                            self.log(f"auto-enabled LoRA {lid} (strength {strength})")
                        except Exception as e:
                            self.log(f"auto-enable {lid} failed: {e}")
        except Exception as e:
            self.log(f"LoRA page update failed: {type(e).__name__}: {e}")

    # -------- Pulse handlers -------------------------------------------------

    def _handle_pulse(self, name: str) -> None:
        dispatch = {
            "Connect": lambda: self.Connect(),
            "Disconnect": lambda: self.Disconnect(),
            "Authenticate": lambda: self.Authenticate(),
            "Pasteapikey": lambda: self.PromptForApiKey(),
            "Stillplaying": lambda: self._extend_session(),
            "Sendprompt": lambda: self.SendPrompt(),
            "Setpromptblend": lambda: self.SetPromptBlend(),
            "Swapsource": lambda: self.SwapSource(),
            "Settimbresource": lambda: self.SetTimbreSource(),
            "Cleartimbresource": lambda: self.ClearTimbreSource(),
            "Settimbrefixture": lambda: self.SetTimbreFixture(),
            "Setstructuresource": lambda: self.SetStructureSource(),
            "Clearstructuresource": lambda: self.ClearStructureSource(),
            "Setstructurefixture": lambda: self.SetStructureFixture(),
        }
        fn = dispatch.get(name)
        if fn:
            try:
                fn()
            except Exception as e:
                self.log(f"Pulse {name} failed: {e}")

    def _extend_session(self) -> None:
        if not self._session_id:
            return
        base = self._read_par("Serverurl", "http://localhost:1318")
        try:
            resp = queue_mod.QueueClient(base, api_key=self._api_key or None).extend(
                self._session_id
            )
            if resp.expires_at:
                self._expires_at_ms = resp.expires_at
            self._extensions_used = resp.extensions_used or self._extensions_used
            self._set_status("Extended")
        except queue_mod.QueueError as e:
            self.log(f"Extend failed: {e}")

    # -------- helpers --------------------------------------------------------

    def _send_text(self, payload: str) -> None:
        """Send a text frame via the Python WS client."""
        wsc = self._wsc
        if wsc is None:
            self.log("_send_text: no WS client")
            return
        ok = wsc.send_text(payload)
        # Don't log every single send — at 60 Hz this floods the textport.
        # Only log failures + sample 1 in 600 to keep visibility for debugging.
        self._n_send_text = getattr(self, "_n_send_text", 0) + 1
        if not ok:
            self.log(f"_send_text: {len(payload)} chars FAILED")
        elif self._n_send_text % 600 == 1:
            self.log(f"_send_text #{self._n_send_text}: {len(payload)} chars ok (sampled)")

    def _send_bytes(self, payload: bytes) -> None:
        """Send a binary frame via the Python WS client."""
        wsc = self._wsc
        if wsc is None:
            self.log("_send_bytes: no WS client")
            return
        ok = wsc.send_binary(payload)
        self.log(f"_send_bytes: {len(payload)} B {'ok' if ok else 'FAILED'}")

    def _snapshot_audio(self, chop) -> np.ndarray | None:
        """Grab the current samples from a CHOP (param-reference, op-path, or
        the resampled COMP input). Returns (channels, samples) float32 or None.
        """
        try:
            if chop is None:
                chop = self.ownerComp.op("resample_in")
            if isinstance(chop, str):
                chop = self.ownerComp.op(chop) or op(chop)  # noqa: F821
            if chop is None:
                return None
            # CHOP samples
            ch_count = chop.numChans
            if ch_count <= 0:
                return None
            samples = chop.numSamples
            pcm = np.empty((ch_count, samples), dtype=np.float32)
            for i in range(ch_count):
                pcm[i] = np.fromiter(chop[i].vals, dtype=np.float32, count=samples)
            return audio_mod.to_stereo(pcm)
        except Exception as e:
            self.log(f"_snapshot_audio failed: {e}")
            return None

    def _resolve_wire_name(self, name: str) -> str | None:
        if name in P.PARAM_BY_WIRE:
            return name
        p = P.PARAM_BY_NAME.get(name)
        return p.wire_name if p else None

    def _coerce_par_value(self, par, schema: P.Param) -> Any:
        if schema.type == "Toggle":
            return bool(par.eval())
        if schema.type == "Int":
            return int(par.eval())
        if schema.type == "Menu":
            return str(par.eval())
        if schema.type == "Str":
            v = par.eval()
            # Curves want JSON-parsed values, not strings.
            if schema.wire_name and schema.wire_name.endswith("_curve") and v:
                try:
                    return json.loads(v)
                except json.JSONDecodeError:
                    self.log(f"Bad JSON in {schema.name}; sending as string")
                    return v
            return v
        return float(par.eval())

    def _collect_init_params(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for p in P.PARAMS:
            if p.category == "init":
                out[p.name] = self._read_par(p.name, p.default)
        return out

    # -------- TD plumbing ----------------------------------------------------

    def _ws(self):
        try:
            return self.ownerComp.op("ws1")
        except Exception:
            return None

    def _oauth_server(self):
        try:
            return self.ownerComp.op("oauth_server")
        except Exception:
            return None

    def _par_by_name(self, name: str):
        try:
            return getattr(self.ownerComp.par, name)
        except AttributeError:
            return None

    def _page_by_name(self, page_name: str):
        for page in self.ownerComp.customPages:
            if page.name == page_name:
                return page
        return None

    def _read_par(self, name: str, default: Any = None) -> Any:
        par = self._par_by_name(name)
        if par is None:
            return default
        try:
            return par.eval()
        except Exception:
            return default

    def _write_par(self, name: str, value: Any) -> None:
        par = self._par_by_name(name)
        if par is None:
            return
        try:
            par.val = value
        except Exception:
            pass

    def _set_status(self, msg: str) -> None:
        self._write_par("Status", msg)
        self.log(f"status: {msg}")

    # -------- logging --------------------------------------------------------

    def log(self, msg: str) -> None:
        try:
            print(f"[demon] {msg}")
        except Exception:
            pass

    # -------- script CHOP cook hooks ----------------------------------------
    # These are called from the script_send / audio_out Script CHOPs.

    def OnCookSend(self, scriptOp) -> None:
        """script_send Script CHOP cook — no-op.

        This release does NOT stream live audio into DEMON. The source track
        is loaded once from the Source Audio File par at Connect time. This
        Script CHOP exists only because the .tox topology still includes it;
        we output a single silent sample so it cooks without errors.
        """
        scriptOp.clear()
        scriptOp.numSamples = 1
        scriptOp.appendChan("dummy")

    def OnCookRecv(self, scriptOp) -> None:
        """audio_out Script CHOP cook callback. Reads from the loop buffer.

        FRAME-PUMP mode (Time Slice = False):
          - Called from frame_exec onFrameStart at frame rate (~60 Hz).
          - We produce a frame-sized block of samples each cook.
          - 60 cooks/sec × 800 samples = 48000 samples/sec = audio rate.

        The loop buffer wraps automatically: when position reaches end,
        it continues reading from frame 0. That's DEMON's intended model
        — the track loops forever as the server keeps patching the loop
        content via slices.
        """
        self._n_cook_recv = getattr(self, "_n_cook_recv", 0) + 1
        if self._n_cook_recv == 1:
            try:
                self.log(f"OnCookRecv: FIRST cook — numSamples="
                         f"{scriptOp.numSamples} loop_frames={self._ring.frames}")
            except Exception:
                pass

        # Frame-pump: read exactly one frame's worth of samples per cook.
        # cookRate is TD's frame rate (typically 60). At 60fps, 48000/60 = 800
        # samples per cook. If frame rate dips, we'll briefly under-produce
        # which manifests as a tiny audible glitch — acceptable.
        try:
            fps = project.cookRate  # type: ignore[name-defined]  # noqa: F821
            if fps <= 0:
                fps = 60.0
        except Exception:
            fps = 60.0
        n = max(1, int(wire.SAMPLE_RATE / fps))

        pos_before = self._ring.position
        pcm = self._ring.read(n)

        # Track playback position from the loop. _playback_pos is the
        # current play head in frames (= samples per channel). Sent to
        # the server as `playback_pos` in params messages, in seconds.
        self._playback_pos = self._ring.position

        # Diagnose every Nth cook: did we read real audio, or silence?
        if self._n_cook_recv % 600 == 0:
            try:
                peak = float(np.max(np.abs(pcm))) if pcm.size > 0 else 0.0
                self.log(
                    f"OnCookRecv #{self._n_cook_recv}: n={n} "
                    f"loop_pos_before={pos_before} loop_pos_after={self._ring.position} "
                    f"loop_frames={self._ring.frames} peak={peak:.4f}"
                )
            except Exception:
                pass

        scriptOp.clear()
        try:
            scriptOp.rate = wire.SAMPLE_RATE
        except Exception:
            pass
        scriptOp.numSamples = n
        try:
            scriptOp.appendChan("chan1").vals = pcm[0].tolist()
            scriptOp.appendChan("chan2").vals = pcm[1].tolist()
        except Exception as e:
            self.log(f"OnCookRecv write failed: {e}")
