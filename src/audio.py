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

    def __init__(self, channels: int = 2):
        self.channels = channels
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
        automatically. If the buffer is uninitialized, returns silence.
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

            pos = self._position
            written = 0
            while written < num_frames:
                end_in_buf = frames - pos
                take = min(end_in_buf, num_frames - written)
                out[:, written:written + take] = buf[:, pos:pos + take]
                written += take
                pos = (pos + take) % frames

            self._position = pos
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
_paNoFlag = 0
_paContinue = 0
_paOutputUnderflow = 4


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
        # System-installed PortAudio (e.g. homebrew) as fallback.
        candidates.extend([
            "/opt/homebrew/lib/libportaudio.dylib",
            "/usr/local/lib/libportaudio.dylib",
            "libportaudio.dylib",  # let dlopen search DYLD path
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
        log(f"[speaker_out] could not load libportaudio.dylib: {last_err}")
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
                 dylib_path: str | None = None):
        self._loop = loop
        self._sample_rate = float(sample_rate)
        self._channels = int(channels)
        self._log = log
        self._dylib_path = dylib_path
        self._stream: int | None = None  # raw c_void_p value
        self._stream_ptr = None  # holds the C pointer alive
        self._underrun_count = 0
        self._callback_count = 0
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
        on success, False on any error (already logged)."""
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
        stream_ptr = _ctypes.c_void_p()
        err = lib.Pa_OpenDefaultStream(
            _ctypes.byref(stream_ptr),
            0,                       # no input
            self._channels,
            _paFloat32,
            self._sample_rate,
            0,                       # framesPerBuffer = 0: PortAudio picks
            _ctypes.cast(self._c_callback, _ctypes.c_void_p),
            None,
        )
        if err != 0:
            msg = lib.Pa_GetErrorText(err) or b"unknown"
            self._log(f"[speaker_out] Pa_OpenDefaultStream failed: "
                      f"{msg.decode(errors='replace')}")
            return False
        err = lib.Pa_StartStream(stream_ptr)
        if err != 0:
            msg = lib.Pa_GetErrorText(err) or b"unknown"
            self._log(f"[speaker_out] Pa_StartStream failed: "
                      f"{msg.decode(errors='replace')}")
            lib.Pa_CloseStream(stream_ptr)
            return False
        self._stream = stream_ptr.value
        self._stream_ptr = stream_ptr
        self._log(f"[speaker_out] started PortAudio default stream "
                  f"sr={self._sample_rate} ch={self._channels}")
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
        """PortAudio callback (audio thread)."""
        self._callback_count += 1
        if status_flags & _paOutputUnderflow:
            self._underrun_count += 1
        try:
            pcm = self._loop.read(int(frames))  # (channels, frames) float32
        except Exception:
            # On read error, write silence so the audio thread doesn't underrun.
            _ctypes.memset(out_buf, 0, int(frames) * self._channels * 4)
            return _paContinue
        # Convert planar (channels, frames) -> interleaved (frames, channels).
        try:
            interleaved = np.ascontiguousarray(pcm.T, dtype=np.float32)
            n_bytes = int(frames) * self._channels * 4
            if interleaved.nbytes >= n_bytes and out_buf:
                _ctypes.memmove(out_buf, interleaved.ctypes.data, n_bytes)
            else:
                _ctypes.memset(out_buf, 0, n_bytes)
        except Exception:
            try:
                _ctypes.memset(out_buf, 0, int(frames) * self._channels * 4)
            except Exception:
                pass
        return _paContinue
