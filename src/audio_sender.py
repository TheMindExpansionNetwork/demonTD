"""
UDP audio sender — bridges our recv-side ring buffer to TD's Audio
Stream In CHOP.

The Script CHOP / Audio Device Out path has been unreliable in TD 2025
(Time-Slice not propagating across COMP boundaries). Instead, we run
a small Python thread that pops audio blocks from the ring buffer at
audio rate and pushes them over localhost UDP as raw float32 LE PCM.

External wiring in TD:
  - Audio Stream In CHOP
      Network Address = 127.0.0.1
      Network Port    = <port>          (set by COMP's UDP Port param)
      Format          = Raw
      Sample Rate     = 48000
      Channels        = 2
      Sample Type     = 32-bit Float
  - Audio Stream In CHOP -> Audio Device Out CHOP

Latency target: ~20 ms (one 1024-sample block at 48 kHz).
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Callable

import numpy as np


class UdpAudioSender:
    """Pumps stereo float32 PCM samples to a localhost UDP target.

    Pulls a block of `block_size` samples per channel every
    `block_size / sample_rate` seconds. Reads from a callback supplied
    by the owner (so the ring buffer stays inside DemonExt without us
    importing TD).
    """

    def __init__(
        self,
        port: int,
        read_block: Callable[[int], np.ndarray],
        sample_rate: int = 48000,
        block_size: int = 1024,
        host: str = "127.0.0.1",
        log: Callable[[str], None] = print,
    ):
        self.port = int(port)
        self.host = host
        self.sample_rate = int(sample_rate)
        self.block_size = int(block_size)
        self._read_block = read_block
        self._log = log
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._n_packets = 0

    @property
    def packets_sent(self) -> int:
        return self._n_packets

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            self._log(f"[udp_audio] start ignored (already running)")
            return
        self._stop.clear()
        self._n_packets = 0
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        except Exception as e:
            self._log(f"[udp_audio] socket() failed: {e}")
            return
        self._thread = threading.Thread(
            target=self._run, name=f"udp_audio[:{self.port}]", daemon=True
        )
        self._thread.start()
        self._log(f"[udp_audio] streaming float32 stereo @ {self.sample_rate}Hz "
                  f"to {self.host}:{self.port} (block={self.block_size})")

    def stop(self) -> None:
        self._stop.set()
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        target = (self.host, self.port)
        interval = self.block_size / float(self.sample_rate)
        next_send = time.time()
        while not self._stop.is_set():
            try:
                pcm = self._read_block(self.block_size)  # (2, block_size) float32
            except Exception as e:
                self._log(f"[udp_audio] read failed: {e}")
                time.sleep(interval)
                continue

            # Ensure (2, N) shape and exact block size.
            if pcm is None or pcm.size == 0:
                payload = np.zeros((2, self.block_size), dtype=np.float32)
            else:
                payload = np.asarray(pcm, dtype=np.float32)
                if payload.ndim == 1:
                    payload = np.stack([payload, payload], axis=0)
                if payload.shape[0] == 1:
                    payload = np.repeat(payload, 2, axis=0)
                if payload.shape[0] > 2:
                    payload = payload[:2]
                if payload.shape[1] < self.block_size:
                    pad = np.zeros(
                        (payload.shape[0], self.block_size - payload.shape[1]),
                        dtype=np.float32,
                    )
                    payload = np.concatenate([payload, pad], axis=1)
                else:
                    payload = payload[:, : self.block_size]

            # Interleave (samples × channels) and serialize.
            interleaved = payload.T.reshape(-1).astype(np.float32, copy=False)
            data = interleaved.tobytes()

            sock = self._sock
            if sock is None:
                break
            try:
                sock.sendto(data, target)
                self._n_packets += 1
            except Exception as e:
                self._log(f"[udp_audio] sendto failed: {e}")

            # Pace to audio-block cadence. If we fell behind, catch up by
            # not sleeping (next iteration immediately).
            next_send += interval
            now = time.time()
            sleep_for = next_send - now
            if sleep_for > 0:
                # Clamp to a single block max so we don't oversleep on stall.
                time.sleep(min(sleep_for, interval))
            else:
                # Behind schedule — skip the sleep, reset baseline to now.
                if sleep_for < -interval * 4:
                    next_send = now
