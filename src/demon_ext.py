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
import sys
import threading
import time
from typing import Any

# --- vendored dependency: zstandard --------------------------------------------
# Bundled wheels live in <comp folder>/vendor/zstandard/<platform>/
# We prepend the matching platform directory to sys.path before import.
def _prepend_vendor_path() -> None:
    try:
        import platform
        sysname = platform.system().lower()
        machine = platform.machine().lower()
        if sysname == "darwin":
            plat = "darwin-arm64" if "arm" in machine else "darwin-x64"
        elif sysname == "windows":
            plat = "win-amd64"
        else:
            return  # other OSes: best-effort, user must install zstandard

        # `me` is injected by TD; outside TD this import block is no-op.
        try:
            comp = me.owner  # type: ignore[name-defined]  # noqa: F821
            base = comp.par.externaltox.eval() or ""
        except Exception:
            base = ""
        if not base:
            # Fall back to project folder.
            try:
                base = project.folder  # type: ignore[name-defined]  # noqa: F821
            except Exception:
                base = os.getcwd()
        vendor = os.path.join(os.path.dirname(base) if base.endswith(".tox") else base,
                              "vendor", "zstandard", plat)
        if os.path.isdir(vendor) and vendor not in sys.path:
            sys.path.insert(0, vendor)
    except Exception:
        pass

_prepend_vendor_path()

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
except NameError:
    import params as P  # type: ignore
    import wire  # type: ignore
    import queue_client as queue_mod  # type: ignore
    import oauth  # type: ignore
    import audio as audio_mod  # type: ignore


# Hard upper bound on source-audio duration. DEMON rejects longer.
MAX_SOURCE_SECONDS = 240


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

        # Audio buffers
        self._ring = audio_mod.RingBuffer(channels=2,
                                          max_samples=wire.SAMPLE_RATE * 30)
        self._epoch: int = 0  # bumped on swap_ready; used to drop stale slices

        self.log("DemonExt initialized")

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
        """Close WS + tell queue we're leaving (when in queue mode)."""
        with self._lock:
            if not self._connected and not self._session_id:
                return
            self._set_status("Disconnecting...")
            try:
                ws = self._ws()
                if ws is not None:
                    ws.par.active = False  # closes the socket
            except Exception:
                pass

            if self._session_id:
                base = self._read_par("Serverurl", "http://localhost:1318")
                try:
                    queue_mod.QueueClient(base, api_key=self._api_key or None).leave(
                        self._session_id
                    )
                except queue_mod.QueueError:
                    pass

            self._connected = False
            self._session_id = None
            self._ws_url = None
            self._expires_at_ms = None
            self._dirty.clear()
            self._ring.clear()
            self._set_status("Idle")

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
        self._send_text(wire.encode_params({wire_name: value}, self._playback_pos))

    def SetParams(self, d: dict[str, Any]) -> None:
        """Batch send a dict of param values (mixed TD-names and wire-names)."""
        raw: dict[str, Any] = {}
        for k, v in d.items():
            wn = self._resolve_wire_name(k)
            if wn:
                raw[wn] = v
        if raw:
            self._send_text(wire.encode_params(raw, self._playback_pos))

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

    def SetTimbreSource(self, chop: Any = None, name: str = "td_input") -> None:
        """Upload the current CHOP input (or a referenced CHOP op) as timbre."""
        pcm = self._snapshot_audio(chop)
        if pcm is None:
            self.log("SetTimbreSource: no audio input")
            return
        self._send_text(wire.encode_set_timbre_source(name))
        self._send_bytes(wire.encode_audio_frame(pcm, channels=2))

    def SetTimbreFixture(self, name: str | None = None) -> None:
        n = name if name is not None else (self._read_par("Timbrefixture", "") or "")
        if not n:
            return
        self._send_text(wire.encode_set_timbre_fixture(n))

    def ClearTimbreSource(self) -> None:
        self._send_text(wire.encode_clear_timbre_source())

    def SetStructureSource(self, chop: Any = None, fixture: str | None = None,
                           name: str = "td_input") -> None:
        if fixture:
            self._send_text(wire.encode_set_structure_fixture(fixture))
            return
        pcm = self._snapshot_audio(chop)
        if pcm is None:
            self.log("SetStructureSource: no audio input")
            return
        self._send_text(wire.encode_set_structure_source(name))
        self._send_bytes(wire.encode_audio_frame(pcm, channels=2))

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
                   fixture: str | None = None) -> None:
        tags = tags if tags is not None else (self._read_par("Swaptags", "") or None)
        key = key if key is not None else (self._read_par("Key", "auto") or "auto")
        time_signature = (time_signature if time_signature is not None
                          else (self._read_par("Timesignature", "auto") or "auto"))

        header = wire.encode_swap_source(
            tags=tags, key=key, time_signature=time_signature,
            fixture_name=fixture,
        )
        self._send_text(header)
        if not fixture:
            pcm = self._snapshot_audio(chop)
            if pcm is not None:
                self._send_bytes(wire.encode_audio_frame(pcm, channels=2))

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
        """Called by tick8ms Timer CHOP every ~8ms.

        Flushes any pending continuous-param changes as a single batch message.
        Also advances the local playback position estimate.
        """
        if not self._connected:
            return

        with self._lock:
            if not self._dirty:
                # Still advance playback position
                self._playback_pos += int(wire.SAMPLE_RATE * 0.008)
                return
            raw = dict(self._dirty)
            self._dirty.clear()

        try:
            self._send_text(wire.encode_params(raw, self._playback_pos))
        except Exception as e:
            self.log(f"OnTick send error: {e}")
        finally:
            self._playback_pos += int(wire.SAMPLE_RATE * 0.008)

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
        ws = self._ws()
        if ws is None:
            self._set_status("WebSocket DAT 'ws1' not found")
            return
        try:
            ws.par.active = False
            ws.par.netaddress = ws_url
            ws.par.active = True
        except Exception as e:
            self.log(f"Failed to open WS: {e}")
            self._set_status(f"WS open failed: {e}")
            return

        # Snapshot init params for revert-on-mid-session-edit
        self._last_init_values = self._collect_init_params()

        # Send config
        cfg = self._build_session_config()
        self._send_text(wire.encode_config(cfg))

        # Resolve the source audio. THREE paths, in order:
        #   1. `Source Audio File` par — explicit file path.
        #   2. Upstream wired CHOP's .par.file — if an Audio File In CHOP is
        #      plugged into the COMP, read the WHOLE file it references.
        #   3. Snapshot of audio_in's current samples — last resort, may be
        #      too short (audio cook block is ~30 ms).
        source_path = self._read_par("Sourcefile", "") or ""
        pcm = None
        source_label = ""

        if source_path:
            pcm = self._load_source_wav(source_path)
            if pcm is not None:
                source_label = os.path.basename(source_path)

        if pcm is None:
            wired_path = self._wired_chop_file_path()
            if wired_path:
                pcm = self._load_source_wav(wired_path)
                if pcm is not None:
                    source_label = f"wired CHOP file: {os.path.basename(wired_path)}"

        if pcm is None:
            pcm = self._snapshot_input_chop()
            if pcm is not None:
                source_label = "wired CHOP snapshot"
                if pcm.shape[1] < wire.SAMPLE_RATE:
                    self.log(
                        f"WARNING: source audio is only "
                        f"{pcm.shape[1] / wire.SAMPLE_RATE:.2f}s — "
                        f"DEMON needs >=1s. Set Source Audio File for a "
                        f"full-track read."
                    )

        if pcm is None:
            self._set_status(
                "Set Source Audio File or wire an Audio File In CHOP, then reconnect"
            )
            try:
                ws.par.active = False
            except Exception:
                pass
            return

        self._send_bytes(wire.encode_audio_frame(pcm, channels=2))

        self._connected = True
        self._set_status("Connected")
        self.log(f"sent {pcm.shape[1]} samples ({pcm.shape[1] / wire.SAMPLE_RATE:.2f}s) "
                 f"from {source_label}")

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
        cfg: dict[str, Any] = {}
        for p in P.PARAMS:
            if p.category == "init" and p.wire_name:
                cfg[p.wire_name] = self._read_par(p.name, p.default)
        cfg["enabled_loras"] = self._enabled_loras()
        cfg["lora_strengths"] = self._lora_strengths()
        # Politely request raw slices if the server supports it.
        cfg.setdefault("compression", "none")
        return cfg

    def _enabled_loras(self) -> list[str]:
        """Read which LoRAs are currently enabled (toggle pars are named
        like 'Lora_enable_<id>' once the catalog is loaded)."""
        out: list[str] = []
        for lora_id in self._lora_ids:
            par = self._par_by_name(f"Lora_enable_{lora_id}")
            if par and par.eval():
                out.append(lora_id)
        return out

    def _lora_strengths(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for lora_id in self._lora_ids:
            par = self._par_by_name(f"Lora_str_{lora_id}")
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
            self.log(f"swap_ready ch={data.get('channels')}")
        elif kind in ("timbre_set", "timbre_cleared", "structure_set",
                      "structure_cleared"):
            self.log(kind)
        elif kind in ("timbre_failed", "structure_failed", "swap_failed", "error"):
            self.log(f"server {kind}: {data.get('error') or data.get('message')}")
            self._set_status(f"Error: {kind}")
        else:
            self.log(f"unknown server message: {kind}")

    def _on_binary(self, buf: bytes) -> None:
        try:
            slice_ = wire.decode_slice(buf, zstd_dec=_ZSTD_DEC)
        except Exception as e:
            self.log(f"Bad slice: {e}")
            return

        # Reshape interleaved float32 -> (channels, samples_per_channel)
        ch = max(1, slice_.channels)
        n = slice_.pcm.size // ch
        if n <= 0:
            return
        pcm = slice_.pcm[: n * ch].reshape(n, ch).T
        self._ring.write(pcm)

    # -------- LoRA catalog ---------------------------------------------------

    def _apply_lora_catalog(self, catalog: list[dict]) -> None:
        """Update Table DAT + dynamic per-LoRA params on the Prompt+LoRA page."""
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
        try:
            page = self._page_by_name("Prompt+LoRA")
            if page is None:
                return
            existing = {p.name for p in page.pars}
            for entry in catalog:
                lid = entry.get("id", "")
                if not lid:
                    continue
                toggle_name = f"Lora_enable_{lid}"
                strength_name = f"Lora_str_{lid}"
                if toggle_name not in existing:
                    tp = page.appendToggle(toggle_name,
                                           label=f"{entry.get('name', lid)} (on)")
                    if tp:
                        tp[0].default = False
                if strength_name not in existing:
                    sp = page.appendFloat(strength_name,
                                          label=f"{entry.get('name', lid)} strength")
                    if sp:
                        sp[0].normMin = 0.0
                        sp[0].normMax = 1.8
                        sp[0].default = entry.get("strength", 1.0)
                        sp[0].clampMin = True
                        sp[0].clampMax = True
        except Exception as e:
            self.log(f"LoRA page update failed: {e}")

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
        ws = self._ws()
        if ws is None:
            return
        try:
            ws.sendText(payload)
        except Exception as e:
            self.log(f"sendText failed: {e}")

    def _send_bytes(self, payload: bytes) -> None:
        """Send a binary frame on the WebSocket DAT.

        TD versions differ on the method name:
          - TD 2023+ : ws.sendBinary(bytes)
          - older    : ws.sendBytes(bytes)
          - some     : ws.send(bytes, asBinary=True)

        We try them in order. Bail with a log on full failure rather than
        raising, so the caller can keep going (e.g. mark connected).
        """
        ws = self._ws()
        if ws is None:
            return
        for method_name in ("sendBinary", "sendBytes"):
            method = getattr(ws, method_name, None)
            if method is None:
                continue
            try:
                method(payload)
                return
            except Exception as e:
                self.log(f"{method_name} failed: {e}")
        # Last resort: send(...) with asBinary
        send = getattr(ws, "send", None)
        if send is not None:
            try:
                send(payload, asBinary=True)
                return
            except Exception as e:
                self.log(f"send(binary) failed: {e}")
        self.log("no working binary-send method on WebSocket DAT")

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
        """audio_out Script CHOP cook callback. Reads from ring buffer.

        Time Slice mode is ON on this CHOP, so TD calls onCook once per
        audio time-slice and tells us how many samples to emit. We pop
        that many from the ring buffer (zero-padded on underrun) and
        write them as a stereo CHOP at 48 kHz.
        """
        # `me.time.frame` doesn't directly tell us block size; the Script CHOP
        # passes its desired numSamples via scriptOp.numSamples when timeslice
        # is on. Fall back to 512 if it's zero or not yet set.
        n = 0
        try:
            n = int(scriptOp.numSamples)
        except Exception:
            n = 0
        if n <= 0:
            # Project audio block heuristic — read from project.audio if present,
            # otherwise default. The exact value doesn't matter for correctness;
            # any drift gets absorbed by the ring buffer.
            try:
                n = int(project.audioBlock)  # type: ignore[name-defined]  # noqa: F821
            except Exception:
                n = 512
        n = max(1, n)

        pcm = self._ring.read(n)  # (2, n) float32, zero-padded on underrun

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
