# Changelog

All notable changes to demonTD. Format follows [Keep a Changelog](https://keepachangelog.com/).

## [0.1.5] — 2026-05-27

Compatibility update for the current DEMON server build. Reports of
"no generated audio plays, just the source loops, textport is flooded
with error spam" trace to three server-side changes since v0.1.4 shipped.

### What broke

The server now:
1. **Always emits zstd-compressed slices** (flag=0x01). v0.1.4 had a
   try/except around `import zstandard` that silently swallowed errors;
   if TD's bundled Python couldn't load the vendored binary, `_ZSTD_DEC`
   was None and every slice failed `decode_slice` with "no decompressor
   was provided" — no generated audio audible.
2. **Emits a `stem_assets` JSON message** followed by two large
   binary blobs with new flag bits (e.g. 0x07). Server-side stem
   separation feature. We don't handle stems, but were logging "Bad
   slice" for each blob.
3. **Sends slices with future-feature flag bits** beyond {0,1}. Decoded
   as "Bad slice" with the same spam.

### What's fixed

- **`SessionConfig.compression = "none"` fallback.** If our vendored
  `zstandard` fails to load, we ask the server to emit raw float16
  slices instead of zstd-compressed. ~1.5× more bandwidth on the recv
  path, but works without depending on a binary load that the user's
  TD bundle may not support. The actual zstd load failure now logs
  its specific reason at boot.
- **`stem_assets` recognized.** The two binary blobs that follow are
  consumed silently (counter-tracked). No textport spam.
- **Slice flags > 1 silently skipped.** Logged ONCE per unknown flag
  value per session, then quiet. Future server features won't flood
  the textport.
- **`Reconnect to apply Init changes` deduped.** The status string is
  only set when it differs from the current Status value, so touching
  multiple Init pars in rapid succession doesn't produce 14 identical
  status lines.

### Files changed
- `src/demon_ext.py`:
  - zstd load failure now logs reason; SessionConfig compression
    fallback.
  - `_on_text`: `stem_assets` ack; unknown-message dedupe.
  - `_on_binary`: skip stem blobs (announced by `stem_assets`),
    skip unknown-flag slices, dedupe slice-decode errors.
  - `OnParChange`: dedupe Reconnect status set.

BUILD_MARKER → v0.1.5-demon-compat.

## [0.1.4] — 2026-05-20

Two audio-thread improvements landing the playback path at zero
underruns over hundreds of callbacks of testing.

### Vectorized loop-seam read

v0.1.3's seam crossfade used a per-frame Python loop inside
`LoopBuffer.read` — ~2k iterations per audio callback at audio
rate. Each iteration did several numpy ops. Total cost was small
(~2 ms per callback) but Python overhead caught by TD's main-
thread GIL pressure occasionally pushed wrap-spanning callbacks
past their 43 ms deadline → ~5% audible stutter rate.

The read is now split into vectorized runs of (a) bulk copy from
contiguous buffer ranges, (b) numpy-vectorized crossfade over the
tail seam. Crossfade math is identical to the AudioWorklet, just
batched. Per-callback Python overhead dropped from ~2k iterations
to ~3 numpy ops.

### Bigger PortAudio block (4096 frames)

Audio latency floor: ~43 ms (2048 frames) → ~85 ms (4096 frames).
Doubles the audio callback's deadline so wrap-spanning callbacks
have headroom even when TD's main thread holds the GIL for >40 ms.

Verified clean: `[speaker_out] stopped (cb_count=615 underruns=0)`
after ~52 s of normal use with TD activity. Pre-fix the same
session produced occasional audible glitches.

### Files changed
- `src/audio.py` — `LoopBuffer.read` vectorized; `SpeakerOut`
  default `frames_per_buffer` 2048 → 4096.
- `src/demon_ext.py` — bump `BUILD_MARKER` to `4k-buffer-v1`.

BUILD_MARKER → 4k-buffer-v1.

## [0.1.3] — 2026-05-20

Single fix: loop seam crossfade.

### Bug

User reported occasional "flashes" of source audio mixed into the
generated output. This did NOT happen in `demon-public-demo`'s web
client. Hours of speculation about delta math and server-side
behavior were red herrings.

### Root cause

`LoopBuffer.read()` was hard-wrapping the playhead from
`frames - 1` to `0`. The web client's `AudioWorklet` doesn't — it
crossfades the last 50 ms of the loop with the FIRST 50 ms, then
wraps the playhead to `position = seam` (= 2400 frames at 48 kHz)
so those leading frames aren't replayed verbatim.

The DEMON server's slice positions don't start at frame 0 (first
slices land around start_sample = 3840, 107520, 211200…). So the
first ~80 ms of the loop tend to remain unpatched source content
for a long time. Hard-wrapping replayed that source content on
every 24-second loop boundary — exactly the "occasional flash"
cadence.

### Fix

Ported the worklet's seam crossfade into `LoopBuffer.read()`:
- New `seam_seconds=0.05` parameter on `LoopBuffer.__init__`
  (default 50 ms; matches `SEAM_FADE_SECONDS` in
  `demon-public-demo/public/audio-worklet.js`).
- `read()` now does a per-frame loop with two paths: bulk copy in
  the middle of the loop, crossfade math in the tail-seam region.
- On wrap, jumps to `position = seam_frames`, not 0.

Bonus: this also smooths the small audio discontinuity that hard
wraps were producing on every loop boundary, even when the leading
samples weren't audibly source.

### Files changed
- `src/audio.py` — `LoopBuffer.__init__` and `LoopBuffer.read`.
- `src/demon_ext.py` — pass `sample_rate=wire.SAMPLE_RATE` to the
  LoopBuffer constructor. Bump `BUILD_MARKER` to `seam-crossfade-v1`.

BUILD_MARKER → seam-crossfade-v1.

## [0.1.2] — 2026-05-20

Polish + Windows build. No behavior changes for working flows.

### UX
- **Session page decluttered.** The disabled `Hosted Mode (coming soon)`
  header and its eight greyed-out children (`Anonymous`, `Direct Pod`,
  `Authenticate`, `Paste API Key`, `API Key`, `Queue Position`, `Expires In`,
  `Still Playing`) are gone for v0.1.x. The supporting code in
  `demon_ext.py` is unchanged — defaults via `_read_par` fallbacks keep
  the direct-anonymous mode that's always been the only working path.
  Hosted mode reappears in v0.2 when it actually works.
- **Source Audio File pre-flight.** Pulsing Connect without a source
  file (and no wired CHOP) now bails immediately with a clear status
  message AND a TD popup dialog, instead of half-attempting a connect
  and burying the error in textport.
- **Server URL default** is now `ws://localhost:8765/` (DEMON's
  realtime_motion_graph_web port) instead of the bogus
  `http://localhost:1318`.

### Windows build (untested by maintainer)
- Vendored `libportaudio64bit.dll` and `libportaudio64bit-asio.dll` from
  the `sounddevice` Windows wheel into
  `vendor/sounddevice/_sounddevice_data/portaudio-binaries/`.
- Cross-platform path resolution in `demon_ext.py` picks the right
  binary at runtime: `.dylib` on macOS, `.dll` on Windows, `.so` on
  Linux (Linux not vendored — falls through to a system install).
- `SpeakerOut._load_lib` candidate list extended with Windows + Linux
  paths.

### Internal
- Removed `_playback_pos` redundant += updates (already done in v0.1.1,
  reconfirmed here).

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
