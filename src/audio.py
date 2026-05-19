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


class SpeakerOut:
    """Plays a LoopBuffer to the system default audio device at audio rate.

    Thin wrapper around sounddevice.OutputStream. Idempotent start/stop.
    Loads sounddevice lazily — if the vendored library can't be loaded
    (e.g. macOS Gatekeeper quarantine on the dylib), `start()` logs the
    error and returns False so the caller can fall back gracefully.
    """

    def __init__(self, loop: "LoopBuffer",
                 sample_rate: int = 48000,
                 channels: int = 2,
                 log=print):
        self._loop = loop
        self._sample_rate = sample_rate
        self._channels = channels
        self._log = log
        self._stream = None
        self._underrun_count = 0
        self._callback_count = 0

    @property
    def underrun_count(self) -> int:
        return self._underrun_count

    @property
    def is_running(self) -> bool:
        s = self._stream
        try:
            return s is not None and s.active
        except Exception:
            return False

    def start(self) -> bool:
        """Open the OutputStream and start audio playback. Returns True on
        success, False on any error (already logged)."""
        if self._stream is not None:
            return True
        try:
            import sounddevice as sd
        except Exception as e:
            self._log(f"[speaker_out] sounddevice import failed: {e}")
            return False
        try:
            stream = sd.OutputStream(
                samplerate=self._sample_rate,
                channels=self._channels,
                dtype="float32",
                blocksize=0,            # let PortAudio pick
                latency="low",
                callback=self._callback,
            )
            stream.start()
            self._stream = stream
            self._log(
                f"[speaker_out] started sd.OutputStream "
                f"sr={self._sample_rate} ch={self._channels} "
                f"latency={stream.latency:.4f}s"
            )
            return True
        except Exception as e:
            self._log(f"[speaker_out] OutputStream.start failed: {e}")
            self._stream = None
            return False

    def stop(self) -> None:
        s = self._stream
        self._stream = None
        if s is None:
            return
        try:
            s.stop()
            s.close()
            self._log(f"[speaker_out] stopped (cb_count={self._callback_count} "
                      f"underruns={self._underrun_count})")
        except Exception as e:
            self._log(f"[speaker_out] stop failed: {e}")

    def _callback(self, outdata, frames, time_info, status):
        """PortAudio audio callback. Runs in PortAudio's thread."""
        self._callback_count += 1
        # status is a CallbackFlags object. If output underflowed, count it.
        if status:
            try:
                if status.output_underflow:
                    self._underrun_count += 1
            except Exception:
                pass
        try:
            pcm = self._loop.read(frames)  # (channels, frames) float32
        except Exception:
            outdata.fill(0.0)
            return
        # PortAudio expects (frames, channels) interleaved by default.
        # LoopBuffer hands us (channels, frames) planar — transpose.
        if pcm.shape == (self._channels, frames):
            outdata[:] = pcm.T
        elif pcm.size == 0:
            outdata.fill(0.0)
        else:
            # Defensive: if shape mismatches, fill silence rather than
            # let numpy raise from inside an audio callback.
            outdata.fill(0.0)
