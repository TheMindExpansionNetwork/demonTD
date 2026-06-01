# Changelog

All notable changes to demonTD. Format follows [Keep a Changelog](https://keepachangelog.com/).

## [0.2.2] — 2026-05-29

Two features pulled from the latest `rtmg-vst` PR (commit `b2e1953`):
**pod failover** and **scheduled curves**. Both are pure client-side
— no new wire surface, no new queue endpoints.

### Pod failover

When a hosted WS opens but never reaches the server `ready` handshake
(1011 keepalive, overloaded pod, VAE encode hang behind a Cloudflare
502, etc.), demonTD now releases the dead session and re-queues for
a different pod, up to 3 attempts. Reset on a successful `ready` so
mid-session drops are still treated as terminal disconnects.

- Behavior matches rtmg-vst's `RTMGSession::applyResult` failover
  branch: leave + re-join without pod_id pin.
- 1.5 s backoff between attempts so we don't hammer the queue.
- After 3 failed attempts the Status par reads "Pod failover
  exhausted (3 tries). Try Connect again later or switch to Direct
  mode."
- `_pending_audio` now lives across the WS cycle until the `ready`
  handler clears it, so failover re-sends the source on the next WS
  open without re-resolving PCM (which would be slow on a 24 s WAV).

### Scheduled curves

Replaces the old Curves page wholesale. The previous 5 `*_curve`
JSON-spec params (sde_denoise_curve, ode_noise_curve, x0_target_curve,
velocity_scale_curve, initial_noise_curve) sent keys the server
stopped applying — they were a static whole-buffer schedule the pod
ignored. The web client moved to **client-side per-frame sampling**
and writing the resulting scalar into the regular continuous-param
stream. This commit mirrors that behavior.

- **Schedulable params**: `Denoise`, `Hintstrength`, `Feedback`,
  `Shift` — matches `demon-public-demo/types/curves.ts`'s
  `SCHEDULEABLE_PARAMS`.
- **Master toggle**: `Schedulecurves` (off by default, NOT persisted
  on across sessions for the same reason the web client documents —
  a stale persisted-on schedule silently driving denoise on every
  reload was a footgun).
- **Per-param controls**: `<Name>curveenable` toggle + `<Name>curve`
  multiline Str holding a piecewise-linear JSON spec like
  `{"points": [[0, 0.5], [0.5, 1.0], [1, 0.3]]}`.
- **Sampler in `OnTick`**: t = `(ring.position / ring.frames) % 1.0`,
  evaluate piecewise-linear, map y∈[0,1] to the base param's
  [min, max], write into both `_dirty` (for the next wire flush)
  and the TD par's `.val` (so the user sees the slider move).
- **Manual override window**: 500 ms after the user moves a
  curve-bound slider directly, the curve yields for that param.
  Matches the web client's `isManualOverrideActive`.
- **Cache**: parsed control points are cached by spec STRING so
  editing the JSON invalidates the cache on next tick without
  re-parsing every tick. Capped at 64 entries.
- **`tests/test_curves.py`** — 7 unit tests covering the parser
  (valid input, endpoint clamping, x-sort, invalid → None) and
  evaluator (exact at control points, linear interp between, t
  clamping).

### Deferred to v0.3+

- Per-LoRA strength curves (web client supports `lora_str_<id>` keys).
  Needs dynamic-param plumbing.
- Catmull-Rom interpolation. Linear gets ~90% of the visual range.
- An in-TD curve editor. TD has no native curve-editor primitive;
  JSON spec is the pragmatic v0.2 UI.

BUILD_MARKER bumped to v0.2.13-failover-curves.

## [0.2.12] — 2026-05-29

**Audio stutter fix.** User reported a longstanding intermittent
stutter that comes and goes — survives across versions, doesn't
exist in `demon-public-demo`'s web client. Hot-path audit revealed
the PortAudio callback (`SpeakerOut._pa_callback`) was allocating
~10 numpy arrays per call (~100-200 KB), running at ~12 callbacks/sec
→ ~1-2 MB/sec of allocation churn on the audio thread. CPython's
gen-0 GC fires on whatever thread allocates; a GC pause on the
audio thread of even ~30 ms blows our 85 ms deadline → stutter. The
"resolves itself, then recurs" pattern matches GC quiesce/spike
cycles exactly.

### Changes

* **`LoopBuffer.read_into(out)`** — new method that fills a caller-
  provided buffer instead of allocating. Cached seam-crossfade scratch
  on the buffer instance (`_seam_t_scratch`, `_seam_one_minus_t_scratch`,
  `_seam_blend_scratch`). The existing `read()` stays as a thin
  alloc-and-delegate wrapper for non-audio-thread callers.
* **`SpeakerOut` pre-allocated scratch** — `_scratch_pcm`,
  `_scratch_interleaved_f32`, `_scratch_interleaved_i16`, sized at
  `_max_block_frames = max(frames_per_buffer * 4, 16384)` so a
  surprise PortAudio block size doesn't force a fallback alloc.
* **`_pa_callback` rewritten** to use the scratches + `np.copyto` +
  `out=` keyword args on every numpy op. Zero allocations in steady
  state. int16 path does in-place `np.clip` + `np.multiply` + a
  single `np.copyto` cast (no `astype` temp).
* **Audio-thread latency telemetry** — per-callback elapsed time
  measured via `time.perf_counter_ns()`; mean + max published once
  per second through `SpeakerOut.drain_latency_stats()`. `OnTick`
  reads + logs them under the Debug toggle. Lets the user confirm
  the fix worked (max << 85 ms) or, if not, prove the stutter is
  something else.
* **Always-on underrun log** — dropped the every-50th gate; every
  audio underrun now lands in the textport immediately.
* **`tests/test_audio.py` rewritten** — the obsolete RingBuffer-name
  tests were replaced with LoopBuffer-equivalents (init, read,
  read_into, patch / add_delta, seam crossfade, swap, clear) plus a
  new `test_loop_buffer_read_into_is_allocation_free` that uses
  `tracemalloc` to enforce zero hot-path allocations going forward.

### Measured improvement (local)

Before refactor: 200 calls of `_pa_callback` → ~tens of MB allocated.
After refactor: 200 calls → ~1372 bytes attributed to audio.py,
most of which is `tracemalloc` / lock-context-manager bookkeeping.
Per-callback latency: mean 0.058 ms, max 0.315 ms (PortAudio deadline
at 4096 frames / 48 kHz is ~85 ms — we use ~0.07% of available time).

BUILD_MARKER bumped to v0.2.12-no-audio-alloc.

## [0.2.11] — 2026-05-29

* Removed the **Sign out** pulse from the Session page. The paste-key
  flow + on-disk persistence cover the relevant lifecycle without it.
* README now has a dedicated **Quick start — Hosted mode** section with
  step-by-step API-key + paste-key + Connect instructions, plus a
  one-time setup note in the regular Quick Start about setting TD's
  Audio Device preference to None.
* The Session-page parameter-table entry in the README now reflects
  the v0.2 layout (Mode menu, hosted controls, queue readouts).

## [0.2.10] — 2026-05-29

**Real root cause of the v0.2.x audio failure**, after `scripts/probe_portaudio.py`
confirmed PortAudio + the user's device open fine outside TouchDesigner:

> TouchDesigner holds the default output device's Core Audio AudioUnit
> whenever its **Edit > Preferences > Audio > Audio Device** preference
> points at a real device. Once TD has the AudioUnit bound, Core Audio
> refuses to let our PortAudio thread call
> `AudioUnitSetProperty(kAudioUnitProperty_StreamFormat)` on the same
> device — the result is `kAudioUnitErr_InvalidPropertyValue (-10851)`
> wrapped as PortAudio's `paInternalError`.

The v0.2.9 lazy-probe fix didn't help because the eager probe wasn't
the cause; TD owning the device was.

### What's actually fixed in v0.2.10

* **Failure path now points at the real cause.** The "no usable combo"
  log line and the user-facing Status par both lead with "Set TD's
  Audio Device pref to None (Edit > Prefs > Audio) and re-pulse
  Connect" before the other workarounds. No more telling the user to
  fiddle with Audio MIDI Setup as the first thing to try.
* **README troubleshooting section rewritten** to put the TD-preference
  fix front and center with a concrete walkthrough, plus a pointer at
  `scripts/probe_portaudio.py` for users who want to verify.
* **`scripts/probe_portaudio.py` ships** as the diagnostic users can
  run from a terminal: same Pa_OpenDefaultStream call demonTD's v0.1.5
  made, against the bundled dylib, without TD in the picture.

The previous v0.2.6 / v0.2.8 / v0.2.9 fallback layers stay in place —
they cover edge cases where TD's preference is already None AND the
device still refuses our format (rare but real). The failure log just
explains which case fires first.

BUILD_MARKER bumped to v0.2.10-td-holds-device-msg.

## [0.2.9] — 2026-05-29

**Regression fix.** v0.2.4 added an eager `Pa_GetDefaultOutputDevice` +
`Pa_GetDeviceInfo` probe right before `Pa_OpenDefaultStream` so we
could log device info and feed the sample-rate fallback. PortAudio's
API documents `Pa_GetDeviceInfo` as a getter, but on macOS Sequoia it
triggers a Core Audio device-list refresh that touches the default-
output AudioUnit's stream-format property. After that touch, the
subsequent `AudioUnitSetProperty(kAudioUnitProperty_StreamFormat)` is
rejected with `kAudioUnitErr_InvalidPropertyValue` (-10851) — even
though it's the same call that succeeded in v0.1.5.

The fix is one move, no API changes: the device-info probe is now
**lazy**. `start()` calls `Pa_OpenDefaultStream` immediately (the
v0.1.5 known-good code path); only on failure does it probe device
info and run the v0.2.4–v0.2.8 fallback matrix (alternate rates,
buffer sizes, `Pa_OpenStream`+`PaStreamParameters`, `paInt16`).

For users where v0.1.5 worked: audio comes right back. For users on
genuinely-incompatible devices: same fallback coverage as v0.2.8, just
deferred until needed.

If you saw `[speaker_out] no usable rate / buffer / format / open-mode
combination` in v0.2.6–v0.2.8 logs on a device that previously worked,
v0.2.9 should restore it. If it doesn't, please file an issue with the
new `[speaker_out] direct Pa_OpenDefaultStream ... failed` line —
that's the v0.1.5-equivalent attempt failing for a genuinely different
reason, and we'll need a vendored libportaudio.dylib bump to fix it.

BUILD_MARKER bumped to v0.2.9-no-eager-probe.

## [0.2.8] — 2026-05-29

PortAudio compatibility expansion. User report: on macOS Sequoia with
"External Headphones" as default output, `Pa_OpenDefaultStream` failed
at every rate × buffer-size combination with `Pa internal err=-9986 /
hostErr code=-10851 'Audio Unit: Invalid Property Value'`. Core Audio
was refusing whatever stream format PortAudio's minimal-API path was
trying to set.

### Added

- **Layer 2: `Pa_OpenStream`** with explicit `PaStreamParameters` at
  the device's `defaultHighOutputLatency`. The high-latency hint gives
  PortAudio room to renegotiate the AudioUnit's format, which resolves
  -10851 on many Sequoia devices.
- **Layer 3: paInt16 fallback.** Some macOS Core Audio devices reject
  `paFloat32` even though PortAudio's docs claim auto-conversion. After
  every float32 attempt fails, we retry the whole matrix with `paInt16`
  and convert int16↔float32 inside the audio callback. Headroom drops
  ~3 dB and clipping is now hard at ±1.0, but you get audio out.
- **`Pa_IsFormatSupported` pre-probe** before each `Pa_OpenStream`
  attempt. Cleaner failure messages, and there are mailing-list reports
  that the probe "primes" the AudioUnit and resolves -10851 on some
  devices.
- **README "Audio output troubleshooting" section** with the three
  user-side workarounds (different default device, Audio MIDI Setup
  format, or toggle `Python Audio Out` off + wire your own
  `Audio Device Out CHOP`).

### Changed

- `speaker_out.start()` failure no longer kills the WS session. Status
  shows a clear "Audio output failed — toggle Python Audio Out off
  and wire your own Audio Device Out CHOP, or fix your default device
  and pulse Connect again." The hosted session stays alive (your
  reservation isn't burned) and the user can route audio out via the
  COMP's `out_chop` port instead.
- Logging gains a per-format prefix and the surrounding context for
  each (rate, buffer, open-API) attempt. `Pa_GetLastHostErrorInfo`
  prints the underlying Core Audio OSStatus + text on every failure.

BUILD_MARKER bumped to v0.2.8-pa-openstream-int16.

### Still failing?

If you see the new "no usable combination" message even after the
workarounds in the README, the next escalation is a vendored
PortAudio binary upgrade (the sounddevice-bundled dylib is ~12
months old and predates several Sequoia AudioUnit fixes). Tracking
that as a separate follow-up.

## [0.2.5] — 2026-05-29

Trim hosted-mode sign-in to paste-only. The browser-OAuth flow was
fragile (Web Server DAT rebind quirks, port-binding races, hangs when
the system browser launch failed silently) for a use case that's one
extra click to do manually: open the dashboard, copy the key, paste it
in.

### Removed

- `Sign in via browser` pulse on the Session page
- `SignInBrowser` + `Authenticate` extension methods
- `OnAuthCallback` + `OnHTTPRequest` extension methods
- `_oauth_server`, `_oauth_state`, `_oauth_port` internal state
- `oauth_server` WebServer DAT from the COMP topology (built-in TD op)
- Everything in `src/oauth.py` except `fetch_profile` + `OAuthError`
  (the paste-key validation path still uses these)
- `onHTTPRequest` callback function in the COMP's callbacks DAT

### Changed

- `Paste API Key` pulse now deep-links to
  https://app.daydream.live/dashboard/api-keys instead of the
  dashboard root — one less click for the user.
- `tests/test_oauth.py` rewritten around `fetch_profile`. 3 tests pass
  (down from 6, all of which tested removed surface).

BUILD_MARKER bumped to v0.2.5-paste-only. Note: rebuilding the .tox
removes the `oauth_server` op from the COMP. Old .tox files keep the
op but it's dormant and harmless.

## [0.2.1] — 2026-05-29

Catch-up sync with `demon-public-demo` since the v0.1.5 protocol pass.
The drift script (`scripts/check_protocol_drift.py`) flagged four new
server message types, two new client encoders, and four new
`SessionConfig` fields. v0.1.5's "log once per unknown kind"
defense-in-depth meant the textport stayed quiet, but the actual
handshakes are tightened up here.

### Server messages now recognized

- **`depth_applied`** — server ack of a runtime depth retune
  (`set_depth`). Logged for visibility; no UI surface (depth is
  Init-only in TD).
- **`params_echo`** — MCP-driven param mirror. Logged under Debug only,
  since TD has no MCP integration.
- **`prompt_blend_echo`** — MCP-driven prompt-blend update. Now mirrors
  the value back into the `Promptblend` continuous par so the TD UI
  reflects external control bus changes.
- **`stem_failed`** — surfaced as a visible log line (was hitting the
  unknown-kind dedupe).

### SessionConfig fields now sent

- **`prompt_b`** — secondary prompt for A/B blending. Wired to a new
  `Initpromptb` par on the Init page (default empty).
- **`client_id`** — per-machine identifier. Reuses the queue
  `deviceId` we already generate. Server stashes it into loguru
  contextvars so pod logs can be filtered by demonTD instance.
- **`use_server_fixture: false`** — sent explicitly. The JS client
  capability-probes `/api/server-info` before flipping this to true;
  TD sends false unconditionally to use the unchanged upload path.

### Out of scope (intentional, not drift)

- **`set_depth`** client encoder — runtime depth retune is a UX
  feature, not a protocol gap. Depth stays Init-only.
- **`loop_band`** client encoder — TD's LoopBuffer does its own seam
  crossfade locally; the band isn't a TD parameter.
- **`stem_source_mode`** — only sent when the user uploads a custom
  track and selects a stem mode in the web client. TD has no stems UX
  in v0.2.

The drift script now knows about the intentionally-omitted client
encoders + config field so future runs stay green.

## [0.2.0] — 2026-05-29

**Hosted mode.** The operator can now connect to the Daydream queue at
`music.daydream.live` and play on a managed pod — no more spinning up
your own VAST instance to demo it. Direct mode (your own pod URL) keeps
working unchanged.

### What's new

- **`Mode` menu** on the Session page (`Direct` / `Hosted`). Direct keeps
  the existing `Server URL` flow. Hosted POSTs `/api/queue/join` against
  the Daydream queue, polls until `active`, then connects to the
  server-signed `wss://` URL — same flow the Daydream web app uses, and
  the same protocol as the `rtmg-vst` plugin.
- **Two ways to sign in** (Session page pulses):
  - **Paste API Key** — opens `app.daydream.live` in your browser; paste
    your key into the TD dialog. Validates against `/users/profile`
    before saving.
  - **Sign in via browser** — full OAuth flow. TD spins up a local
    listener on a free port, your browser redirects there with the
    one-time token, the key is fetched and saved.
- **`Sign out`** wipes the stored key (preserves the device ID).
- **Queue status surfacing** while connecting + heartbeat-driven while
  active: `Queue Position`, `Expires in (s)`, `Deny reason` (for paywall
  / over-budget responses).
- **`Still playing?`** pulse hits `/api/queue/extend` to bump the
  session lifetime.
- **`POST /api/queue/claim` after WS open.** Cancels the server-side
  reservation-eviction timer (added in the latest VST PR, now in TD).
- **Stable device ID** persisted to `<prefs>/daydream_auth.json`. Sent
  on every join for analytics + rate-limit attribution.

### Persistence

API key + profile + device ID live in a per-user file, NOT in the .toe:
  - macOS: `~/Library/Application Support/derivative/daydream_auth.json`
  - Windows: `%APPDATA%/Derivative/daydream_auth.json`
  - Linux: `~/.local/share/derivative/daydream_auth.json`

That matches the rtmg-vst PropertiesFile approach and avoids leaking
your API key when you share a .toe.

### What's NOT changed

- Direct-mode flow is byte-for-byte identical to v0.1.5. If you've been
  pointing demonTD at your own pod URL, nothing about that path moves.
- Wire protocol is unchanged. v0.1.5's "log once per unknown kind"
  defense-in-depth stays as-is.

### Reference

Mirrors the queue + auth surface from the new RTMG VST PR
([daydreamlive/rtmg-vst#4](https://github.com/daydreamlive/rtmg-vst/pull/4)).

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
