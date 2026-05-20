# Changelog

All notable changes to demonTD. Format follows [Keep a Changelog](https://keepachangelog.com/).

## [0.1.1] — 2026-05-20

End-to-end audio playback now works on macOS. v0.1.0 shipped with the wire
protocol, schema, and extension class but the audio output path was broken
(Time Slice doesn't propagate across TD Base COMP boundaries, by design — see
README's "Audio routing" section). This release fixes that and a long list
of paper-cuts.

### Audio output (works now)

- **Python-side audio playback via PortAudio.** A small Python audio thread,
  bound to the bundled `libportaudio.dylib` via stdlib `ctypes`, plays the
  generated audio through the system default device. No TD CHOP audio chain
  crossing; no need to wire an external `Audio Device Out CHOP`.
- **LoopBuffer**, the audio model — replaces the original ring buffer.
  Mirrors `demon-public-demo/vendor/demon-ui/engine/audio/AudioPlayer.ts`:
  the server's initial buffer is the full track loop; subsequent slices
  patch positions at their `start_sample` indices; playback wraps
  continuously while content evolves.
- **`peek()` for visual reactivity.** A new `LoopBuffer.peek()` reads the
  current play position without advancing it. The `audio_out` Script CHOP
  uses this so Analyze CHOPs / FFTs / peak detectors can mirror what's
  playing without racing the audio thread. Previously the Script CHOP's
  `read()` was advancing the play head from frame_exec at 60 Hz while the
  audio thread also called `read()` at audio rate — the two consumers
  raced through the buffer, causing constant chop.
- **Wave-decode model**: `wire.decode_slice` correctly parses the 23-byte
  header, decompresses `SLICE_FLAG_DELTA` payloads via vendored
  `zstandard`, converts float16 → float32, and dispatches to
  `LoopBuffer.patch()` or `add_delta()` based on flag bit.

### Defaults aligned with demon-public-demo

So TD users get the same out-of-the-box sound as web users.

| param | old default | new default | source |
|---|---|---|---|
| `denoise` | 0.7 | 0.85 | manual tuning |
| `vae_window` | 3.0 | 6.0 | `useStartSession.ts buildConfig` |
| `fast_vae` | True | False | same |
| `Initprompt` | "instrumental music" | "heavy dubstep, deathstep, afxdump, growl heavy bass distortion" | same |
| Bach LoRA strength | server-reported (often 0) | **always 1.0 on DEFAULT_ON LoRAs** | server occasionally reports 0 before LoRA loaded |

### Initial-params seed on `ready`

Continuous param values (denoise, hint_strength, all 14 channel gains, DCW
block, etc.) used to only reach the server when the user moved a slider
mid-session. After `ready`, the server ran with its internal defaults
(`denoise = 0` = passthrough), so generated audio didn't kick in until the
user touched a control. Now every continuous param's current TD value is
seeded into the dirty set on `ready` so the next 8 ms tick sends a complete
params message immediately.

### Textport silence

Massive cleanup. Removed:
- `[DIAG sent_to_server]` / `[DIAG initial_buffer]` hex+peak dumps
- Per-Connect WAV dumps to `/tmp/demon-debug/` (gated behind Debug toggle now)
- Every-600-cook `OnCookRecv #N` loop_pos+peak log
- The broken `[POST]` block (was raising on every call because TD blocks
  `numChans` reads during cook)
- The sampled `_send_text #N ok` lines
- The `[callbacks.onCook #N]` sampled counter prints in `build_tox.py`'s
  callbacks DAT
- Vestigial TD WebSocket DAT receive prints (we use `ws_client.py` now)

Gated behind a new **Debug Logging** Session-page toggle (default off):
- Vendor-path discovery prints
- WS frame echoes
- OnTick state telemetry every 2 s
- SpeakerOut underrun sampled logs
- `/tmp/demon-debug/*.wav` dumps

### Vendored deps

- New: `vendor/sounddevice/` (pure-Python wrapper) +
  `vendor/sounddevice/_sounddevice_data/portaudio-binaries/libportaudio.dylib`
  (universal2 binary, ~230 KB). On macOS the build proactively strips
  `com.apple.quarantine` from the dylib so first-load works without user
  intervention.
- Existing: `vendor/zstandard/{darwin-arm64, darwin-x64, win-amd64}/`,
  `vendor/websocket-client/`.

### Internal changes

- `wire.decode_config` no longer strips empty strings from the config
  payload (server expects `fixture_name: ""` to be present and was closing
  the WS otherwise).
- `_playback_pos` is now sourced from `LoopBuffer.position` (the
  authoritative play head) rather than dead-reckoned in `OnTick`. Sent to
  server as `playback_pos` in seconds; matches `demon-public-demo`'s
  `session.player.positionSec`.
- Removed the `audio_clock` Constant CHOP and the internal `audiodevout`
  Audio Device Out CHOP (both attempts to force TD's audio chain that we
  superseded with SpeakerOut).
- Removed the `frame_exec onFrameStart` force-cook on `audio_out`. The
  Script CHOP only cooks when something downstream consumes it (correct
  TD pattern). SpeakerOut reads the LoopBuffer directly and doesn't
  depend on Script CHOP cooks.

### Known limitations (deferred)

- Windows build pending — currently macOS universal2 (arm64 + x86_64) only.
- TD-native audio chain access (Audio Filter CHOP, multi-device routing,
  recording via Audio File Out) requires either a Select CHOP reference
  pattern or a virtual loopback device (BlackHole). README's "Audio
  routing" section documents both.

---

## [0.1.0] — 2026-05-14

Initial source release. Wire protocol, queue API, OAuth, schema, and
extension class complete and unit-tested (52 tests). `.tox` artifact
generated from this repo via a headless TD build.

End-to-end audio playback was not yet wired in this release.
