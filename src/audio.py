"""
Audio I/O helpers for the DEMON TouchDesigner operator.

This module mirrors demon-public-demo's audio model: DEMON streams audio as
a LOOP. The server first sends an "initial buffer" containing the full
track (typically 24s = 1,152,000 samples at 48 kHz). After that, each
binary slice carries a `start_sample` (start frame in the loop) and PCM
data that PATCHES that region of the loop. Playback advances continuously
and wraps at end-of-buffer.

This is NOT a FIFO/streaming model — slices don't arrive in playback
order, they target arbitrary positions. The earlier RingBuffer
implementation treated slices as appended audio, which produced silence
+ glitches because slice positions didn't line up with the consumer's
read head.

Reference: demon-public-demo/vendor/demon-ui/engine/audio/AudioPlayer.ts

No TD dependencies.
"""

from __future__ import annotations

import threading

import numpy as np


class LoopBuffer:
    """Fixed-size stereo PCM loop buffer with positional patching.

    Storage layout: (channels, frames) float32. The playback position
    advances on each `read()` call and wraps modulo `frames`. Slices
    (server → client) are written via `patch()` or `add_delta()` at
    explicit start-frame offsets — they DO NOT advance the read head.

    Reads return silence on uninitialized buffer rather than blocking.
    """

    def __init__(self, channels: int = 2, sample_rate: int = 48000,
                 seam_seconds: float = 0.05):
        self.channels = channels
        self._sample_rate = int(sample_rate)
        # Seam crossfade length (frames) — last N frames of the loop are
        # blended with the first N frames as the playhead approaches end-
        # of-buffer. Mirrors demon-public-demo/public/audio-worklet.js
        # `SEAM_FADE_SECONDS = 0.05` (50 ms at 48 kHz = 2400 frames).
        # On wrap we jump to position=_seam_frames (NOT 0) so the leading
        # samples that were folded into the crossfade aren't replayed.
        # This is what stops the "source flash every loop wrap" you'd
        # otherwise hear: the first ~50 ms of the buffer (which the
        # server's slice positions don't typically cover) plays only once.
        self._seam_frames = max(0, int(self._sample_rate * seam_seconds))
        self._buffer: np.ndarray | None = None  # shape (channels, frames)
        self._frames: int = 0
        self._position: int = 0  # next read frame
        self._lock = threading.Lock()

    @property
    def frames(self) -> int:
        """Total frames in the loop (per channel). 0 if uninitialized."""
        return self._frames

    @property
    def position(self) -> int:
        """Current playback position in frames (per channel)."""
        with self._lock:
            return self._position

    @property
    def available(self) -> int:
        """Compatibility shim with RingBuffer.available for telemetry.
        In a loop model, "available" is always the loop size — the loop
        always has content (silence or audio). Reports frames * 2 to mirror
        the RingBuffer behavior (which counted total samples across channels)
        in legacy logs."""
        return self._frames

    def clear(self) -> None:
        with self._lock:
            self._buffer = None
            self._frames = 0
            self._position = 0

    def init(self, pcm: np.ndarray, channels: int | None = None) -> None:
        """Initialize the loop with the server's initial buffer.

        Parameters
        ----------
        pcm : np.ndarray
            Either 1D interleaved (L0,R0,L1,R1,...) or 2D (channels, frames)
            float32. Sets the loop size to this length.
        channels : int, optional
            Override the channel count. Defaults to self.channels.
        """
        ch = int(channels or self.channels)
        pcm = np.ascontiguousarray(pcm, dtype=np.float32)
        if pcm.ndim == 1:
            frames = pcm.shape[0] // ch
            buf = pcm[: frames * ch].reshape(frames, ch).T
        elif pcm.ndim == 2:
            if pcm.shape[0] == ch:
                buf = pcm
                frames = pcm.shape[1]
            else:
                buf = pcm.T
                frames = pcm.shape[0]
        else:
            raise ValueError(f"unsupported pcm.ndim={pcm.ndim}")

        with self._lock:
            self.channels = ch
            self._buffer = np.ascontiguousarray(buf, dtype=np.float32)
            self._frames = frames
            self._position = 0

    def swap(self, pcm: np.ndarray, channels: int | None = None) -> None:
        """Replace the entire loop buffer (server `swap_ready` path).

        Resets playback position to 0 like AudioPlayer.swap() does.
        """
        self.init(pcm, channels=channels)

    def patch(self, start_frame: int, pcm: np.ndarray) -> None:
        """Overwrite frames[start_frame : start_frame + N] with `pcm`.

        Wraps if the write region crosses the loop end.
        """
        self._write(start_frame, pcm, add=False)

    def add_delta(self, start_frame: int, pcm: np.ndarray) -> None:
        """Additive blend (used for SLICE_FLAG_DELTA payloads)."""
        self._write(start_frame, pcm, add=True)

    def _write(self, start_frame: int, pcm: np.ndarray, add: bool) -> None:
        pcm = np.ascontiguousarray(pcm, dtype=np.float32)
        ch = self.channels
        if pcm.ndim == 1:
            n = pcm.shape[0] // ch
            pcm_2d = pcm[: n * ch].reshape(n, ch).T
        elif pcm.ndim == 2 and pcm.shape[0] == ch:
            pcm_2d = pcm
            n = pcm.shape[1]
        elif pcm.ndim == 2 and pcm.shape[1] == ch:
            pcm_2d = pcm.T
            n = pcm.shape[0]
        else:
            return

        if n <= 0:
            return

        with self._lock:
            buf = self._buffer
            frames = self._frames
            if buf is None or frames == 0:
                return
            start = start_frame % frames
            end = start + n
            if end <= frames:
                if add:
                    buf[:, start:end] += pcm_2d
                else:
                    buf[:, start:end] = pcm_2d
            else:
                first_chunk = frames - start
                if add:
                    buf[:, start:] += pcm_2d[:, :first_chunk]
                else:
                    buf[:, start:] = pcm_2d[:, :first_chunk]
                rem = n - first_chunk
                # If pcm is larger than the whole loop, last block wins.
                if rem >= frames:
                    if add:
                        buf[:, :] += pcm_2d[:, first_chunk:first_chunk + frames]
                    else:
                        buf[:, :] = pcm_2d[:, first_chunk:first_chunk + frames]
                else:
                    if add:
                        buf[:, :rem] += pcm_2d[:, first_chunk:]
                    else:
                        buf[:, :rem] = pcm_2d[:, first_chunk:]

    def read(self, num_frames: int) -> np.ndarray:
        """Read `num_frames` frames at the playback position; advance head.

        Returns shape (channels, num_frames) float32. Wraps the loop
        automatically with a seam crossfade. If the buffer is
        uninitialized, returns silence.

        Seam crossfade: as the playhead approaches end-of-buffer, the
        last `_seam_frames` (50 ms by default) are blended with the
        FIRST `_seam_frames` of the buffer. On wrap, the playhead jumps
        to `_seam_frames` (NOT 0) so the leading samples aren't
        replayed verbatim. Mirrors the AudioWorklet at
        `demon-public-demo/public/audio-worklet.js` lines 191–239.

        Vectorized: the read is divided into runs of (a) bulk-copy
        from a contiguous region of the buffer, and (b) crossfade
        over the tail-seam region. Each run is a couple of numpy
        operations — no per-sample Python loop. Important: this
        function is called from the PortAudio audio callback thread,
        so it MUST be fast. The previous per-frame implementation
        was ~2k Python iterations per callback at audio rate; when
        TD's main thread held the GIL for >40 ms (network panel
        render, GPU sync, big cook, GC), the audio thread missed
        its deadline and you'd hear a stutter.

        IMPORTANT: This is the AUTHORITATIVE play head. Only one consumer
        (the actual audio output thread — SpeakerOut._pa_callback) should
        call this. Other consumers (e.g. the Script CHOP cook callback,
        for visual reactivity) must use `peek()` so they don't race the
        head forward and cause the audio thread to skip samples.
        """
        ch = self.channels
        out = np.zeros((ch, num_frames), dtype=np.float32)
        if num_frames <= 0:
            return out

        with self._lock:
            buf = self._buffer
            frames = self._frames
            if buf is None or frames == 0:
                return out

            seam = min(self._seam_frames, frames // 4)
            seam_start = frames - seam  # first frame inside the tail seam
            pos = self._position
            written = 0

            while written < num_frames:
                need = num_frames - written
                if pos < seam_start:
                    # Pre-seam: bulk copy contiguous range from buf.
                    take = min(seam_start - pos, need)
                    out[:, written:written + take] = buf[:, pos:pos + take]
                    pos += take
                    written += take
                else:
                    # In seam: vectorized crossfade over [pos, pos+take).
                    # t goes from (seam - (frames-pos))/seam at the first
                    # frame to (seam - (frames-(pos+take-1)))/seam at the
                    # last; tail samples come from buf[:, pos:pos+take]
                    # and head samples come from
                    # buf[:, seam-(frames-pos) : seam-(frames-pos)+take].
                    max_take = frames - pos
                    take = min(max_take, need)
                    tail_indices = np.arange(pos, pos + take)
                    dist_from_end = frames - tail_indices  # shape (take,)
                    t_vals = (seam - dist_from_end).astype(np.float32) / seam
                    head_indices = seam - dist_from_end
                    tail = buf[:, tail_indices]
                    head = buf[:, head_indices]
                    out[:, written:written + take] = (
                        tail * (1.0 - t_vals) + head * t_vals
                    )
                    pos += take
                    written += take
                    if pos >= frames:
                        # Wrap to `seam`, NOT 0 — the first seam frames
                        # were folded into the crossfade above.
                        pos = seam if seam > 0 else 0

            self._position = pos
            return out

    def peek(self, num_frames: int,
             position: int | None = None) -> np.ndarray:
        """Read `num_frames` frames WITHOUT advancing the play head.

        For non-authoritative consumers (e.g. a Script CHOP that wants
        to mirror the current audio for visual reactivity but must not
        affect what the actual speaker thread plays). Returns shape
        (channels, num_frames) float32; wraps the loop automatically;
        returns silence if uninitialized.

        `position` lets you read from an explicit frame offset instead
        of the current play head. Defaults to the play head.
        """
        ch = self.channels
        out = np.zeros((ch, num_frames), dtype=np.float32)
        if num_frames <= 0:
            return out

        with self._lock:
            buf = self._buffer
            frames = self._frames
            if buf is None or frames == 0:
                return out

            pos = self._position if position is None else (position % frames)
            written = 0
            while written < num_frames:
                end_in_buf = frames - pos
                take = min(end_in_buf, num_frames - written)
                out[:, written:written + take] = buf[:, pos:pos + take]
                written += take
                pos = (pos + take) % frames

            # Critically: do NOT update self._position here.
            return out

    def seek(self, position_frames: int) -> None:
        """Set the playback position. Wraps modulo loop size."""
        with self._lock:
            if self._frames > 0:
                self._position = position_frames % self._frames


# Back-compat alias: existing call sites use `RingBuffer`. Map it to
# LoopBuffer with a similar interface so the rest of the code base
# doesn't have to be touched everywhere.
RingBuffer = LoopBuffer


# -----------------------------------------------------------------------------
# Resampling
# -----------------------------------------------------------------------------

def linear_resample(pcm: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Cheap linear-interpolation resample.

    pcm: shape (channels, samples) float32.
    Returns shape (channels, new_samples).
    """
    if src_rate == dst_rate or pcm.size == 0:
        return pcm

    pcm = np.asarray(pcm, dtype=np.float32)
    if pcm.ndim == 1:
        pcm = pcm.reshape(1, -1)

    n_in = pcm.shape[1]
    n_out = max(1, int(round(n_in * dst_rate / src_rate)))
    if n_out == n_in:
        return pcm

    x_in = np.linspace(0.0, 1.0, num=n_in, endpoint=False, dtype=np.float32)
    x_out = np.linspace(0.0, 1.0, num=n_out, endpoint=False, dtype=np.float32)

    out = np.empty((pcm.shape[0], n_out), dtype=np.float32)
    for ch in range(pcm.shape[0]):
        out[ch] = np.interp(x_out, x_in, pcm[ch]).astype(np.float32, copy=False)
    return out


def to_stereo(pcm: np.ndarray) -> np.ndarray:
    """Force a CHOP-shaped array to stereo (2, samples). Mono → duplicated L→R."""
    pcm = np.asarray(pcm, dtype=np.float32)
    if pcm.ndim == 1:
        return np.stack([pcm, pcm], axis=0)
    if pcm.shape[0] == 1:
        return np.repeat(pcm, 2, axis=0)
    if pcm.shape[0] == 2:
        return pcm
    if pcm.shape[0] > 2:
        return pcm[:2]
    raise ValueError(f"Unsupported PCM shape: {pcm.shape}")


# -----------------------------------------------------------------------------
# SpeakerOut — Python-side audio playback via sounddevice / PortAudio.
# -----------------------------------------------------------------------------
#
# We use this to bypass TD's CHOP audio chain (which cannot pull a Script
# CHOP at audio rate across a Base COMP output boundary in TD 2025). The
# OutputStream callback runs in PortAudio's own audio thread; we just pull
# samples from the LoopBuffer and write them into the output buffer.
#
# The TD Script CHOP `audio_out` path is left functional so users can still
# tap the audio for visual reactivity in their networks — both paths read
# from the same thread-safe LoopBuffer.


import ctypes as _ctypes
import os as _os


# PortAudio C constants we need
_paFloat32 = 0x00000001
_paInt16   = 0x00000008
_paNoFlag = 0
_paContinue = 0
_paOutputUnderflow = 4
_paNoDevice = -1
_paFormatIsSupported = 0
_paFramesPerBufferUnspecified = 0
_paInternalError = -9986       # PaError: generic Pa internal failure
_paInvalidSampleRate = -9997   # PaError: rate the device can't deliver
_paUnanticipatedHostError = -9999  # PaError: host (CoreAudio) bubbled an err

# Mirror of PortAudio's PaDeviceInfo struct so we can read device-config
# metadata for diagnostic logging when Pa_OpenDefaultStream fails.
# Field order per portaudio.h (struct version 2). Field types match the
# ABI — sizeof(PaTime)==8 (double), int==4, char*==8 on 64-bit.
class _PaDeviceInfo(_ctypes.Structure):
    _fields_ = [
        ("structVersion",            _ctypes.c_int),
        ("name",                     _ctypes.c_char_p),
        ("hostApi",                  _ctypes.c_int),
        ("maxInputChannels",         _ctypes.c_int),
        ("maxOutputChannels",        _ctypes.c_int),
        ("defaultLowInputLatency",   _ctypes.c_double),
        ("defaultLowOutputLatency",  _ctypes.c_double),
        ("defaultHighInputLatency",  _ctypes.c_double),
        ("defaultHighOutputLatency", _ctypes.c_double),
        ("defaultSampleRate",        _ctypes.c_double),
    ]


# Mirror of PortAudio's PaHostErrorInfo. Pa_GetLastHostErrorInfo()
# returns this with the OS-level error code that wrapped into a generic
# paUnanticipatedHostError / paInternalError. On macOS that's a
# CoreAudio OSStatus (a four-char code) — much more diagnosable than
# the Pa-level "Internal PortAudio error".
class _PaHostErrorInfo(_ctypes.Structure):
    _fields_ = [
        ("hostApiType",  _ctypes.c_int),
        ("errorCode",    _ctypes.c_long),
        ("errorText",    _ctypes.c_char_p),
    ]


# Mirror of PortAudio's PaStreamParameters. The "right" way to open a
# stream — Pa_OpenDefaultStream is a wrapper around this that picks
# a tight default latency. Some macOS devices reject the tight default
# with kAudioUnitErr_InvalidPropertyValue; passing the device's
# defaultHighOutputLatency here gives PortAudio room to negotiate.
class _PaStreamParameters(_ctypes.Structure):
    _fields_ = [
        ("device",                    _ctypes.c_int),
        ("channelCount",              _ctypes.c_int),
        ("sampleFormat",              _ctypes.c_ulong),
        ("suggestedLatency",          _ctypes.c_double),
        ("hostApiSpecificStreamInfo", _ctypes.c_void_p),
    ]


class SpeakerOut:
    """Plays a LoopBuffer to the system default audio device at audio rate.

    Uses stdlib `ctypes` to bind directly to libportaudio.dylib. We DON'T use
    the `sounddevice` Python wrapper because it depends on cffi/`_cffi_backend`
    which TouchDesigner 2025's bundled Python doesn't ship.

    Lifecycle: `start()` opens the default output stream and begins audio
    callback at the requested sample rate; `stop()` halts and closes the
    stream. Both are idempotent.

    Thread model: PortAudio invokes `_pa_callback` from its own audio thread
    at hardware buffer cadence (~5 ms typical). The callback reads the
    requested `frames` from the LoopBuffer (thread-safe) and copies an
    interleaved float32 buffer into PortAudio's output pointer via
    `ctypes.memmove`. No Python allocations beyond the LoopBuffer.read call.
    """

    # Class-level lazy-loaded PortAudio binding. Shared across instances so
    # we don't re-dlopen on every Connect/Disconnect cycle.
    _lib: "_ctypes.CDLL | None" = None
    _lib_initialized: bool = False

    @classmethod
    def _load_lib(cls, vendor_dylib_path: str | None = None,
                  log=print) -> "_ctypes.CDLL | None":
        if cls._lib is not None:
            return cls._lib
        candidates: list[str] = []
        if vendor_dylib_path:
            candidates.append(vendor_dylib_path)
        # System-installed PortAudio fallbacks, per platform.
        import platform as _platform
        sysname = _platform.system().lower()
        if sysname == "darwin":
            candidates.extend([
                "/opt/homebrew/lib/libportaudio.dylib",
                "/usr/local/lib/libportaudio.dylib",
                "libportaudio.dylib",  # let dlopen search DYLD path
            ])
        elif sysname == "windows":
            candidates.extend([
                "libportaudio64bit.dll",
                "libportaudio.dll",  # some installs drop the bitness suffix
            ])
        else:
            candidates.extend([
                "libportaudio.so",
                "libportaudio.so.2",
            ])
        last_err = None
        for path in candidates:
            try:
                lib = _ctypes.CDLL(path)
                cls._configure_lib(lib)
                cls._lib = lib
                log(f"[speaker_out] loaded PortAudio from {path}")
                return lib
            except OSError as e:
                last_err = e
                continue
        log(f"[speaker_out] could not load PortAudio binary: {last_err}")
        return None

    @staticmethod
    def _configure_lib(lib: "_ctypes.CDLL") -> None:
        """Set argtypes / restype for the PortAudio C functions we call.
        Mostly defensive on 64-bit; ctypes does the right thing for void* /
        int / double if signatures aren't declared, but being explicit avoids
        size mismatches on edge cases."""
        lib.Pa_Initialize.restype = _ctypes.c_int
        lib.Pa_Terminate.restype = _ctypes.c_int
        lib.Pa_GetErrorText.argtypes = [_ctypes.c_int]
        lib.Pa_GetErrorText.restype = _ctypes.c_char_p
        lib.Pa_OpenDefaultStream.argtypes = [
            _ctypes.POINTER(_ctypes.c_void_p),  # PaStream**
            _ctypes.c_int,                       # numInputChannels
            _ctypes.c_int,                       # numOutputChannels
            _ctypes.c_ulong,                     # PaSampleFormat
            _ctypes.c_double,                    # sampleRate
            _ctypes.c_ulong,                     # framesPerBuffer
            _ctypes.c_void_p,                    # PaStreamCallback*
            _ctypes.c_void_p,                    # userData
        ]
        lib.Pa_OpenDefaultStream.restype = _ctypes.c_int
        lib.Pa_StartStream.argtypes = [_ctypes.c_void_p]
        lib.Pa_StartStream.restype = _ctypes.c_int
        lib.Pa_StopStream.argtypes = [_ctypes.c_void_p]
        lib.Pa_StopStream.restype = _ctypes.c_int
        lib.Pa_CloseStream.argtypes = [_ctypes.c_void_p]
        lib.Pa_CloseStream.restype = _ctypes.c_int
        # Device-inspection APIs — used for diagnostic dumps when
        # Pa_OpenDefaultStream fails. Pa_GetDeviceInfo returns a pointer
        # into Pa's internal table; we read it as a struct (declared
        # below as PaDeviceInfo).
        lib.Pa_GetDefaultOutputDevice.restype = _ctypes.c_int
        lib.Pa_GetDeviceCount.restype = _ctypes.c_int
        lib.Pa_GetDeviceInfo.argtypes = [_ctypes.c_int]
        lib.Pa_GetDeviceInfo.restype = _ctypes.POINTER(_PaDeviceInfo)
        lib.Pa_GetHostApiInfo.argtypes = [_ctypes.c_int]
        lib.Pa_GetHostApiInfo.restype = _ctypes.c_void_p
        # Surfaces the OS-level error wrapped inside paUnanticipated /
        # paInternalError. Read after a failed Pa_OpenDefaultStream to
        # see the actual CoreAudio OSStatus.
        lib.Pa_GetLastHostErrorInfo.restype = _ctypes.POINTER(_PaHostErrorInfo)
        # Full Pa_OpenStream — used as the fallback when
        # Pa_OpenDefaultStream's tight built-in latency is rejected
        # by Core Audio (kAudioUnitErr_InvalidPropertyValue / -10851).
        lib.Pa_OpenStream.argtypes = [
            _ctypes.POINTER(_ctypes.c_void_p),       # PaStream**
            _ctypes.POINTER(_PaStreamParameters),    # input params (or NULL)
            _ctypes.POINTER(_PaStreamParameters),    # output params
            _ctypes.c_double,                        # sampleRate
            _ctypes.c_ulong,                         # framesPerBuffer
            _ctypes.c_ulong,                         # streamFlags (paNoFlag)
            _ctypes.c_void_p,                        # PaStreamCallback*
            _ctypes.c_void_p,                        # userData
        ]
        lib.Pa_OpenStream.restype = _ctypes.c_int
        lib.Pa_IsFormatSupported.argtypes = [
            _ctypes.POINTER(_PaStreamParameters),
            _ctypes.POINTER(_PaStreamParameters),
            _ctypes.c_double,
        ]
        lib.Pa_IsFormatSupported.restype = _ctypes.c_int

    # ctypes callback type. Kept as class-level to avoid recreating the
    # CFUNCTYPE on every instance (which would trigger libffi closure
    # allocation churn).
    _CB_TYPE = _ctypes.CFUNCTYPE(
        _ctypes.c_int,        # return: PaContinue / Complete / Abort
        _ctypes.c_void_p,     # input buffer
        _ctypes.c_void_p,     # output buffer
        _ctypes.c_ulong,      # frame count
        _ctypes.c_void_p,     # PaStreamCallbackTimeInfo*
        _ctypes.c_ulong,      # status flags
        _ctypes.c_void_p,     # userData
    )

    def __init__(self, loop: "LoopBuffer",
                 sample_rate: int = 48000,
                 channels: int = 2,
                 log=print,
                 dylib_path: str | None = None,
                 frames_per_buffer: int = 4096):
        self._loop = loop
        self._sample_rate = float(sample_rate)
        self._channels = int(channels)
        # Larger blocks = fewer Python callbacks per second = less GIL
        # contention with TD's main thread. 4096 frames @ 48 kHz = ~85 ms.
        # That's our audio latency floor; acceptable for a generative
        # session, and avoids occasional stutters when a wrap-spanning
        # callback coincides with TD doing heavy work on the main thread.
        # 2048 had occasional misses (~5% glitch rate); 4096 doubles our
        # deadline headroom for the audio callback to complete.
        self._frames_per_buffer = int(frames_per_buffer)
        self._log = log
        self._dylib_path = dylib_path
        self._stream: int | None = None  # raw c_void_p value
        self._stream_ptr = None  # holds the C pointer alive
        self._underrun_count = 0
        self._callback_count = 0
        # Negotiated by start(). Defaults to paFloat32 (preferred); falls
        # back to paInt16 when Core Audio refuses float32 on the user's
        # device. The pa_callback reads this to decide which dtype to
        # write into PortAudio's output buffer.
        self._sample_format_pa: int = _paFloat32
        # Keep a strong reference to the bound CFUNCTYPE so it doesn't get
        # garbage-collected while the audio thread is calling into it.
        self._c_callback = self._CB_TYPE(self._pa_callback)

    @property
    def underrun_count(self) -> int:
        return self._underrun_count

    @property
    def is_running(self) -> bool:
        return self._stream is not None

    def start(self) -> bool:
        """Open the default output stream and start playback. Returns True
        on success, False on any error (already logged).

        Order of operations
        -------------------
        1. **Direct open** — Pa_OpenDefaultStream at the requested
           (sample_rate, frames_per_buffer, paFloat32). This is the
           v0.1.5 known-good code path; for most users it succeeds and
           we return immediately.
        2. **Only on failure**: probe the default device's metadata
           (sample rate, defaultHighOutputLatency), then run the
           v0.2.4 - v0.2.8 fallback matrix (alternate rates / buffer
           sizes, Pa_OpenStream with explicit PaStreamParameters,
           paInt16 sample format).

        The eager probe used to run BEFORE step 1; that introduced a
        regression on macOS Sequoia where Pa_GetDeviceInfo touches the
        default-output AudioUnit's stream-format property and the
        subsequent AudioUnitSetProperty(kAudioUnitProperty_StreamFormat)
        is rejected with kAudioUnitErr_InvalidPropertyValue (-10851).
        Deferring the probe to the failure branch restores the v0.1.5
        path while keeping the fallbacks available for devices that
        actually need them.
        """
        if self._stream is not None:
            return True
        lib = self._load_lib(self._dylib_path, log=self._log)
        if lib is None:
            return False
        # Pa_Initialize is idempotent — fine to call repeatedly.
        if not SpeakerOut._lib_initialized:
            err = lib.Pa_Initialize()
            if err != 0:
                msg = lib.Pa_GetErrorText(err) or b"unknown"
                self._log(f"[speaker_out] Pa_Initialize failed: "
                          f"{msg.decode(errors='replace')}")
                return False
            SpeakerOut._lib_initialized = True

        def _host_error_detail() -> str:
            """Read Pa_GetLastHostErrorInfo for the OS-level reason."""
            try:
                hei_ptr = lib.Pa_GetLastHostErrorInfo()
                if not hei_ptr:
                    return ""
                hei = hei_ptr.contents
                txt = (hei.errorText or b"").decode(errors="replace")
                return (
                    f" hostErr code={hei.errorCode} "
                    f"text={txt!r}"
                )
            except Exception:
                return ""

        # --- Step 1: direct v0.1.5-style open ---------------------------------
        # Do this BEFORE any device probe so Pa_GetDeviceInfo doesn't
        # poison the AudioUnit state on Sequoia. On success we short-
        # circuit out and never run the fallback matrix.
        stream_ptr = _ctypes.c_void_p()
        chosen_rate = self._sample_rate
        chosen_buf = self._frames_per_buffer
        chosen_format = _paFloat32
        err = lib.Pa_OpenDefaultStream(
            _ctypes.byref(stream_ptr),
            0,                              # no input
            self._channels,
            _paFloat32,
            self._sample_rate,
            self._frames_per_buffer,
            _ctypes.cast(self._c_callback, _ctypes.c_void_p),
            None,
        )
        if err != 0:
            msg = (lib.Pa_GetErrorText(err) or b"unknown").decode(
                errors="replace")
            self._log(
                f"[speaker_out] direct Pa_OpenDefaultStream@"
                f"{self._sample_rate}Hz buf={self._frames_per_buffer} "
                f"failed: {msg} (err={err}){_host_error_detail()} "
                f"— running fallback matrix"
            )

        # --- Step 2: only on failure, probe + run fallback matrix --------------
        device_rate: float | None = None
        device_index: int = -1
        device_high_latency: float = 0.020   # 20 ms — safe default
        device_max_out: int = self._channels
        if err != 0:
            try:
                dev = int(lib.Pa_GetDefaultOutputDevice())
                if dev >= 0:
                    device_index = dev
                    info_ptr = lib.Pa_GetDeviceInfo(dev)
                    if info_ptr:
                        info = info_ptr.contents
                        device_rate = float(info.defaultSampleRate)
                        device_high_latency = float(info.defaultHighOutputLatency)
                        device_max_out = int(info.maxOutputChannels)
                        self._log(
                            f"[speaker_out] default output: "
                            f"dev={dev} name={(info.name or b'?').decode(errors='replace')} "
                            f"maxOut={device_max_out} "
                            f"defaultSampleRate={device_rate} "
                            f"defaultHighOutputLatency={device_high_latency:.4f}s"
                        )
            except Exception as e:
                self._log(f"[speaker_out] device-info probe failed: {e}")

        rates = [self._sample_rate]
        if device_rate and abs(device_rate - self._sample_rate) > 1.0:
            rates.append(device_rate)
        bufsizes = [self._frames_per_buffer, _paFramesPerBufferUnspecified]

        def _try_open(sample_format: int, fmt_label: str
                      ) -> tuple[int, _ctypes.c_void_p, float, int]:
            """Try every (rate, bufsize, open-API) combination at one
            sample format. Returns (err, stream_ptr, chosen_rate,
            chosen_buf). err==0 on success.

            Three open layers:
              1. Pa_OpenDefaultStream (simple API, tight default latency)
              2. Pa_OpenStream + PaStreamParameters at the device's
                 defaultHighOutputLatency (more room for Core Audio to
                 renegotiate). Skipped if device probe failed.

            Before each Pa_OpenStream attempt, IsFormatSupported probes
            the format — both as a cleaner failure mode and because some
            macOS users on the PortAudio mailing list report that calling
            IsFormatSupported first "primes" the AudioUnit and resolves
            -10851 (kAudioUnitErr_InvalidPropertyValue) on subsequent
            opens.
            """
            local_stream = _ctypes.c_void_p()
            local_err = -1
            local_rate = self._sample_rate
            local_buf = self._frames_per_buffer

            # Layer 1: Pa_OpenDefaultStream matrix.
            for rate in rates:
                for bufsz in bufsizes:
                    local_stream = _ctypes.c_void_p()
                    local_err = lib.Pa_OpenDefaultStream(
                        _ctypes.byref(local_stream),
                        0,                              # no input
                        self._channels,
                        sample_format,
                        float(rate),
                        int(bufsz),
                        _ctypes.cast(self._c_callback, _ctypes.c_void_p),
                        None,
                    )
                    if local_err == 0:
                        return (local_err, local_stream,
                                float(rate), int(bufsz))
                    msg = (lib.Pa_GetErrorText(local_err) or b"unknown"
                           ).decode(errors="replace")
                    bufsz_label = "auto" if bufsz == 0 else str(bufsz)
                    self._log(
                        f"[speaker_out] {fmt_label} "
                        f"Pa_OpenDefaultStream@{rate}Hz buf={bufsz_label} "
                        f"failed: {msg} (err={local_err})"
                        f"{_host_error_detail()}"
                    )

            # Layer 2: Pa_OpenStream with explicit PaStreamParameters at
            # defaultHighOutputLatency. Needs a working device probe.
            if device_index < 0:
                return local_err, local_stream, local_rate, local_buf
            self._log(
                f"[speaker_out] {fmt_label} falling back to Pa_OpenStream "
                f"+ defaultHighOutputLatency={device_high_latency:.4f}s"
            )
            for rate in rates:
                out_params = _PaStreamParameters(
                    device=device_index,
                    channelCount=self._channels,
                    sampleFormat=sample_format,
                    suggestedLatency=device_high_latency,
                    hostApiSpecificStreamInfo=None,
                )
                # IsFormatSupported probe. If it says no, log and skip
                # the Pa_OpenStream call entirely.
                supported = lib.Pa_IsFormatSupported(
                    None, _ctypes.byref(out_params), float(rate))
                if supported != _paFormatIsSupported:
                    smsg = (lib.Pa_GetErrorText(supported) or b"unknown"
                            ).decode(errors="replace")
                    self._log(
                        f"[speaker_out] {fmt_label} "
                        f"Pa_IsFormatSupported@{rate}Hz: {smsg} "
                        f"(err={supported}) — skipping"
                    )
                    continue
                for bufsz in bufsizes:
                    local_stream = _ctypes.c_void_p()
                    local_err = lib.Pa_OpenStream(
                        _ctypes.byref(local_stream),
                        None,                       # no input
                        _ctypes.byref(out_params),
                        float(rate),
                        int(bufsz),
                        _paNoFlag,
                        _ctypes.cast(self._c_callback, _ctypes.c_void_p),
                        None,
                    )
                    if local_err == 0:
                        return (local_err, local_stream,
                                float(rate), int(bufsz))
                    msg = (lib.Pa_GetErrorText(local_err) or b"unknown"
                           ).decode(errors="replace")
                    bufsz_label = "auto" if bufsz == 0 else str(bufsz)
                    self._log(
                        f"[speaker_out] {fmt_label} "
                        f"Pa_OpenStream@{rate}Hz buf={bufsz_label} "
                        f"failed: {msg} (err={local_err})"
                        f"{_host_error_detail()}"
                    )
            return local_err, local_stream, local_rate, local_buf

        # Outer loop: prefer float32 (lossless), fall back to int16 if
        # every float32 attempt fails. Some Core Audio devices reject
        # float32 even though PortAudio's docs say it should auto-convert.
        # int16 is the workaround; we convert in the pa_callback.
        # Gated on `err != 0` from step 1 — if the direct open already
        # succeeded, stream_ptr / chosen_* are already correctly set and
        # we skip the matrix entirely.
        if err != 0:
            formats = [
                (_paFloat32, "float32"),
                (_paInt16,   "int16"),
            ]
            for sample_format, fmt_label in formats:
                err, stream_ptr, chosen_rate, chosen_buf = _try_open(
                    sample_format, fmt_label)
                if err == 0:
                    chosen_format = sample_format
                    if sample_format == _paInt16:
                        self._log(
                            "[speaker_out] WARNING: opened with paInt16 "
                            "(float32 rejected by Core Audio). Headroom "
                            "drops ~3 dB; clipping is now hard at \xb11.0."
                        )
                    if chosen_rate != self._sample_rate:
                        self._log(
                            f"[speaker_out] WARNING: opened at {chosen_rate} Hz "
                            f"instead of {self._sample_rate} Hz. Audio will "
                            f"pitch by "
                            f"~{(chosen_rate / self._sample_rate - 1.0) * 100:+.2f}%. "
                            f"Set your default output to "
                            f"{int(self._sample_rate)} Hz in Audio MIDI "
                            f"Setup to fix."
                        )
                    if chosen_buf == _paFramesPerBufferUnspecified:
                        self._log(
                            "[speaker_out] using paFramesPerBufferUnspecified "
                            "(device negotiated its own block size)"
                        )
                    break

        if err != 0:
            self._log(
                "[speaker_out] no usable rate / buffer / format / open-mode "
                "combination.\n"
                "  >>> Most likely cause: TouchDesigner is holding the "
                "output device's Core Audio AudioUnit. Fix: "
                "Edit > Preferences > Audio > Audio Device > None (save; "
                "no restart needed). Then re-pulse Connect.\n"
                "  Confirm by running `python3 scripts/probe_portaudio.py` "
                "from a terminal — if it succeeds outside TD, TD is the "
                "culprit.\n"
                "  Other workarounds:\n"
                "    * Toggle 'Python Audio Out' OFF and wire `out_chop` "
                "to your own Audio Device Out CHOP outside the COMP "
                "(lets TD keep ownership of the device).\n"
                "    * macOS System Settings > Sound > Output: switch to "
                "a different device.\n"
                "    * Audio MIDI Setup: set the device's Format to "
                "'Stereo 48000 Hz, 32-bit Float'."
            )
            return False

        self._sample_format_pa = chosen_format
        err = lib.Pa_StartStream(stream_ptr)
        if err != 0:
            msg = lib.Pa_GetErrorText(err) or b"unknown"
            self._log(f"[speaker_out] Pa_StartStream failed: "
                      f"{msg.decode(errors='replace')}")
            lib.Pa_CloseStream(stream_ptr)
            return False
        self._stream = stream_ptr.value
        self._stream_ptr = stream_ptr
        self._sample_rate = chosen_rate
        self._frames_per_buffer = chosen_buf
        # When we opened with paFramesPerBufferUnspecified, PortAudio
        # decides per-callback what block size to deliver. Logging "auto"
        # is more honest than printing 0 as a frames-per-buffer count.
        buf_label = "auto" if chosen_buf == 0 else str(chosen_buf)
        latency_label = (
            "device-negotiated" if chosen_buf == 0
            else f"~{chosen_buf / self._sample_rate * 1000:.1f}ms"
        )
        fmt_label = "int16" if chosen_format == _paInt16 else "float32"
        self._log(
            f"[speaker_out] started PortAudio default stream "
            f"sr={self._sample_rate} ch={self._channels} "
            f"format={fmt_label} "
            f"frames_per_buffer={buf_label} (latency {latency_label})"
        )
        return True

    def stop(self) -> None:
        lib = SpeakerOut._lib
        stream = self._stream
        self._stream = None
        if stream is None or lib is None:
            return
        try:
            lib.Pa_StopStream(_ctypes.c_void_p(stream))
            lib.Pa_CloseStream(_ctypes.c_void_p(stream))
            self._log(f"[speaker_out] stopped (cb_count={self._callback_count} "
                      f"underruns={self._underrun_count})")
        except Exception as e:
            self._log(f"[speaker_out] stop failed: {e}")
        self._stream_ptr = None

    def _pa_callback(self, in_buf, out_buf, frames, time_info, status_flags,
                     user_data) -> int:
        """PortAudio callback (audio thread).

        Kept minimal: read N frames from LoopBuffer, write to PortAudio's
        output pointer in whatever sample format we managed to open with.
        No allocations beyond what numpy does inside LoopBuffer.read.

        `self._sample_format_pa` records the format we negotiated (set by
        `start()` after a successful open). float32 is the preferred path;
        int16 is the degraded fallback when Core Audio refuses float32.
        """
        self._callback_count += 1
        if status_flags & _paOutputUnderflow:
            self._underrun_count += 1
            # Periodically log underruns so we know if the GIL is starving us.
            if self._underrun_count <= 3 or self._underrun_count % 50 == 0:
                try:
                    self._log(f"[speaker_out] underrun "
                              f"(count={self._underrun_count}, "
                              f"cb={self._callback_count}, "
                              f"frames={frames})")
                except Exception:
                    pass
        n = int(frames)
        is_int16 = (self._sample_format_pa == _paInt16)
        bytes_per_sample = 2 if is_int16 else 4
        n_bytes = n * self._channels * bytes_per_sample
        try:
            pcm = self._loop.read(n)  # (channels, frames) float32
            if is_int16:
                # Soft-clip then scale to int16 range. Clip floor matters —
                # raw multiply of an out-of-range float by 32767 wraps
                # negative on overflow, generating loud noise.
                clipped = np.clip(pcm, -1.0, 1.0)
                interleaved = np.ascontiguousarray(
                    (clipped * 32767.0).T.astype(np.int16)
                )
            else:
                interleaved = np.ascontiguousarray(pcm.T, dtype=np.float32)
            _ctypes.memmove(out_buf, interleaved.ctypes.data, n_bytes)
        except Exception:
            # Any failure: write silence so the audio thread doesn't break.
            try:
                _ctypes.memset(out_buf, 0, n_bytes)
            except Exception:
                pass
        return _paContinue
