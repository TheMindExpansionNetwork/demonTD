"""Unit tests for src/audio.py — ring buffer + resample helpers."""

from __future__ import annotations

import numpy as np
import pytest

import audio as audio_mod


def test_ring_buffer_basic_write_read():
    rb = audio_mod.RingBuffer(channels=2, max_samples=10_000)
    chunk = np.array([[1, 2, 3, 4], [5, 6, 7, 8]], dtype=np.float32)
    rb.write(chunk)
    assert rb.available == 4
    out = rb.read(4)
    assert out.shape == (2, 4)
    np.testing.assert_array_equal(out, chunk)
    assert rb.available == 0


def test_ring_buffer_underrun_returns_silence():
    rb = audio_mod.RingBuffer(channels=2, max_samples=10_000)
    rb.write(np.array([[1, 2], [3, 4]], dtype=np.float32))
    out = rb.read(5)
    assert out.shape == (2, 5)
    np.testing.assert_array_equal(out[:, :2], [[1, 2], [3, 4]])
    np.testing.assert_array_equal(out[:, 2:], np.zeros((2, 3), dtype=np.float32))


def test_ring_buffer_multiple_chunks_partial_read():
    rb = audio_mod.RingBuffer(channels=1, max_samples=10_000)
    rb.write(np.array([[1, 2, 3]], dtype=np.float32))
    rb.write(np.array([[4, 5]], dtype=np.float32))
    rb.write(np.array([[6, 7, 8, 9]], dtype=np.float32))
    assert rb.available == 9
    np.testing.assert_array_equal(rb.read(4)[0], [1, 2, 3, 4])
    np.testing.assert_array_equal(rb.read(4)[0], [5, 6, 7, 8])
    np.testing.assert_array_equal(rb.read(1)[0], [9])


def test_ring_buffer_max_samples_trims_head():
    rb = audio_mod.RingBuffer(channels=1, max_samples=10)
    rb.write(np.array([list(range(8))], dtype=np.float32))
    rb.write(np.array([list(range(8, 16))], dtype=np.float32))  # over cap of 10
    assert rb.available <= 10
    tail = rb.read(rb.available)[0]
    # Tail is some contiguous suffix of the written sequence
    assert tail[-1] == 15.0


def test_ring_buffer_interleaved_write():
    rb = audio_mod.RingBuffer(channels=2, max_samples=10_000)
    interleaved = np.array([1, 5, 2, 6, 3, 7, 4, 8], dtype=np.float32)
    rb.write(interleaved)
    out = rb.read(4)
    np.testing.assert_array_equal(out, [[1, 2, 3, 4], [5, 6, 7, 8]])


def test_linear_resample_passthrough_when_equal_rate():
    pcm = np.array([[1, 2, 3, 4]], dtype=np.float32)
    out = audio_mod.linear_resample(pcm, 48000, 48000)
    np.testing.assert_array_equal(out, pcm)


def test_linear_resample_downsample_length():
    pcm = np.random.randn(2, 480).astype(np.float32)
    out = audio_mod.linear_resample(pcm, 48000, 24000)
    assert out.shape == (2, 240)


def test_linear_resample_upsample_length():
    pcm = np.random.randn(2, 100).astype(np.float32)
    out = audio_mod.linear_resample(pcm, 44100, 48000)
    assert out.shape[0] == 2
    assert abs(out.shape[1] - int(100 * 48000 / 44100)) <= 1


def test_to_stereo_mono_to_stereo():
    mono = np.array([1, 2, 3], dtype=np.float32)
    out = audio_mod.to_stereo(mono)
    assert out.shape == (2, 3)
    np.testing.assert_array_equal(out[0], out[1])


def test_to_stereo_stereo_passthrough():
    s = np.array([[1, 2], [3, 4]], dtype=np.float32)
    out = audio_mod.to_stereo(s)
    np.testing.assert_array_equal(out, s)


def test_to_stereo_quad_to_stereo():
    q = np.random.randn(4, 5).astype(np.float32)
    out = audio_mod.to_stereo(q)
    assert out.shape == (2, 5)
    np.testing.assert_array_equal(out, q[:2])
