"""
Audio I/O helpers for the DEMON TouchDesigner operator.

- Thread-safe ring buffer for decoded server audio (WS receive thread writes,
  Script CHOP cook reads).
- float16 ↔ float32 helpers.
- Linear resample (cheap; for cosmetic SR mismatch — DEMON always operates
  at 48kHz internally).

Pure Python + numpy. No TD dependencies.
"""

from __future__ import annotations

import threading
from collections import deque

import numpy as np


class RingBuffer:
    """A thread-safe stereo PCM ring buffer.

    Stored as a deque of (channels, samples_in_chunk) float32 arrays — pop
    samples from the head, write whole arrays to the tail. Cheap and avoids
    re-allocating a large contiguous buffer on every WS slice.

    Reads return silence on underrun rather than blocking, so they're safe
    to call from the TD cook thread.
    """

    def __init__(self, channels: int = 2, max_samples: int = 48000 * 30):
        self.channels = channels
        self.max_samples = max_samples
        self._chunks: deque[np.ndarray] = deque()
        self._head_offset = 0  # samples already consumed from _chunks[0]
        self._total = 0        # total samples available
        self._lock = threading.Lock()

    @property
    def available(self) -> int:
        """Samples currently buffered (per channel)."""
        with self._lock:
            return self._total

    def clear(self) -> None:
        with self._lock:
            self._chunks.clear()
            self._head_offset = 0
            self._total = 0

    def write(self, pcm: np.ndarray) -> None:
        """Append PCM to the tail.

        pcm: shape (channels, samples) float32. If interleaved is given,
        de-interleave first.
        """
        pcm = np.ascontiguousarray(pcm, dtype=np.float32)
        if pcm.ndim == 1:
            # Assume interleaved
            samples = pcm.shape[0] // self.channels
            pcm = pcm.reshape(samples, self.channels).T
        elif pcm.ndim == 2 and pcm.shape[0] != self.channels:
            # (samples, channels) -> (channels, samples)
            pcm = pcm.T

        with self._lock:
            self._chunks.append(pcm)
            self._total += pcm.shape[1]
            # Trim from head if we exceeded max_samples (avoid runaway memory).
            while self._total > self.max_samples and self._chunks:
                head = self._chunks[0]
                head_remaining = head.shape[1] - self._head_offset
                if self._total - head_remaining >= self.max_samples:
                    self._total -= head_remaining
                    self._chunks.popleft()
                    self._head_offset = 0
                else:
                    drop = self._total - self.max_samples
                    self._head_offset += drop
                    self._total -= drop
                    break

    def read(self, num_samples: int) -> np.ndarray:
        """Pop num_samples per channel from the head.

        Returns shape (channels, num_samples) float32. On underrun, the
        missing portion is zero-padded so the return shape is always
        deterministic.
        """
        out = np.zeros((self.channels, num_samples), dtype=np.float32)
        if num_samples <= 0:
            return out

        with self._lock:
            written = 0
            while written < num_samples and self._chunks:
                head = self._chunks[0]
                head_len = head.shape[1] - self._head_offset
                if head_len <= 0:
                    self._chunks.popleft()
                    self._head_offset = 0
                    continue

                take = min(head_len, num_samples - written)
                out[:, written:written + take] = head[
                    :, self._head_offset:self._head_offset + take
                ]
                self._head_offset += take
                written += take
                self._total -= take

                if self._head_offset >= head.shape[1]:
                    self._chunks.popleft()
                    self._head_offset = 0

        return out


# -----------------------------------------------------------------------------
# Resampling
# -----------------------------------------------------------------------------

def linear_resample(pcm: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Cheap linear-interpolation resample.

    pcm: shape (channels, samples) float32.
    Returns shape (channels, new_samples).

    For higher fidelity, prefer a real polyphase filter — this is good enough
    for routing 48kHz↔44.1kHz inside TD where rate mismatch is small.
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
