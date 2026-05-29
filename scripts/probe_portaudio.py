#!/usr/bin/env python3
"""
Standalone PortAudio probe — does the bundled libportaudio.dylib open
your default output device when called from plain terminal Python,
without TouchDesigner in the picture?

Why this exists
---------------
User report: demonTD's v0.2.9 `start()` does literally one thing first:
`Pa_OpenDefaultStream(channels=2, paFloat32, 48000, 4096, cb)`. That
call is byte-identical to the v0.1.5 build that played audio fine.
But in v0.2.9 it fails with `kAudioUnitErr_InvalidPropertyValue`.
Nothing in the user's macOS / device / format / hardware changed.

Two hypotheses left:
  (a) Something TD does before our SpeakerOut runs poisons Core Audio
      (e.g. TD's own audio engine holds the device, or some COMP cook
      initializes an AudioUnit that locks the StreamFormat property).
  (b) The bundled libportaudio.dylib has a real incompatibility with
      the current macOS that's environment-dependent.

This script tests (b) in isolation. It loads the exact same dylib via
ctypes, calls Pa_Initialize + Pa_OpenDefaultStream with the same
arguments demonTD uses, and reports the result.

Usage
-----
    python3 scripts/probe_portaudio.py

Expected outcomes
-----------------
  * PRINTS "OK — opened default output stream" → PortAudio + your device
    are fine in isolation. The problem is TD pre-poisoning Core Audio.
    Workaround: toggle Python Audio Out OFF and wire your own
    Audio Device Out CHOP. Real fix: track down which TD op is grabbing
    the AudioUnit.
  * PRINTS "FAIL ... err=-9986 hostErr=-10851" → identical failure
    outside TD. The bundled libportaudio.dylib has a genuine
    incompatibility. Real fix: bump the vendored binary.
"""
from __future__ import annotations

import ctypes
import os
import sys

# PaSampleFormat
paFloat32 = 0x00000001
paInt16   = 0x00000008

# PaErrorCode names we care about
PA_ERRORS = {
    0:     "paNoError",
    -9986: "paInternalError",
    -9997: "paInvalidSampleRate",
    -9998: "paInvalidChannelCount",
    -9999: "paUnanticipatedHostError",
}


class _PaHostErrorInfo(ctypes.Structure):
    _fields_ = [
        ("hostApiType", ctypes.c_int),
        ("errorCode",   ctypes.c_long),
        ("errorText",   ctypes.c_char_p),
    ]


def _audio_callback(in_buf, out_buf, frames, time_info, status_flags,
                    user_data):
    """Silent — we never start the stream, just open it."""
    if out_buf and frames:
        try:
            ctypes.memset(out_buf, 0, frames * 2 * 4)  # ch=2, float32
        except Exception:
            pass
    return 0   # paContinue


_CB_TYPE = ctypes.CFUNCTYPE(
    ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong,
    ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p,
)


def main() -> int:
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates = [
        os.path.join(
            repo_root, "vendor", "sounddevice", "_sounddevice_data",
            "portaudio-binaries", "libportaudio.dylib"),
        # Homebrew fallbacks, in case the user wants to test a system PA
        "/opt/homebrew/lib/libportaudio.dylib",
        "/usr/local/lib/libportaudio.dylib",
    ]
    dylib_path = next((p for p in candidates if os.path.isfile(p)), None)
    if not dylib_path:
        print(f"ERROR: no libportaudio.dylib at any of: {candidates}",
              file=sys.stderr)
        return 2

    print(f"using libportaudio: {dylib_path}")
    lib = ctypes.CDLL(dylib_path)

    # Just the surface we need.
    lib.Pa_Initialize.restype = ctypes.c_int
    lib.Pa_Terminate.restype = ctypes.c_int
    lib.Pa_GetErrorText.argtypes = [ctypes.c_int]
    lib.Pa_GetErrorText.restype = ctypes.c_char_p
    lib.Pa_GetLastHostErrorInfo.restype = ctypes.POINTER(_PaHostErrorInfo)
    lib.Pa_OpenDefaultStream.argtypes = [
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_int, ctypes.c_int,
        ctypes.c_ulong, ctypes.c_double, ctypes.c_ulong,
        ctypes.c_void_p, ctypes.c_void_p,
    ]
    lib.Pa_OpenDefaultStream.restype = ctypes.c_int
    lib.Pa_CloseStream.argtypes = [ctypes.c_void_p]
    lib.Pa_CloseStream.restype = ctypes.c_int

    err = lib.Pa_Initialize()
    if err != 0:
        msg = (lib.Pa_GetErrorText(err) or b"unknown").decode()
        print(f"FAIL: Pa_Initialize failed: {msg} (err={err})")
        return 1

    # Keep a strong ref to the C callback so it isn't GC'd mid-open.
    cb = _CB_TYPE(_audio_callback)

    # Try the exact same call demonTD's v0.1.5 SpeakerOut made.
    stream = ctypes.c_void_p()
    err = lib.Pa_OpenDefaultStream(
        ctypes.byref(stream),
        0,            # no input
        2,            # stereo
        paFloat32,
        48000.0,
        4096,         # framesPerBuffer
        ctypes.cast(cb, ctypes.c_void_p),
        None,
    )

    if err == 0:
        print(f"OK — opened default output stream "
              f"(stereo paFloat32 48000Hz buf=4096). "
              f"This means PortAudio + your device are fine in isolation; "
              f"something TD does before SpeakerOut runs is poisoning "
              f"Core Audio state.")
        lib.Pa_CloseStream(stream)
        lib.Pa_Terminate()
        return 0

    err_name = PA_ERRORS.get(err, f"err={err}")
    msg = (lib.Pa_GetErrorText(err) or b"unknown").decode()
    print(f"FAIL: Pa_OpenDefaultStream returned {err_name} ({err}): {msg}")

    # Surface the host (Core Audio) error code.
    hei_ptr = lib.Pa_GetLastHostErrorInfo()
    if hei_ptr:
        hei = hei_ptr.contents
        txt = (hei.errorText or b"").decode()
        print(f"  hostErr: code={hei.errorCode} text={txt!r}")
        if hei.errorCode == -10851:
            print("  -10851 = kAudioUnitErr_InvalidPropertyValue (Core "
                  "Audio refused StreamFormat). Identical to the TD "
                  "failure — points to a bundled-binary incompatibility, "
                  "NOT a TD-specific issue.")

    lib.Pa_Terminate()
    return 1


if __name__ == "__main__":
    sys.exit(main())
