# Bundled zstandard wheels

Place the unpacked `zstandard` wheel contents per platform here. At runtime,
`demon_ext.py` detects the platform and prepends the matching directory to
`sys.path` before `import zstandard`.

Populate via:

```bash
# From the repo root
for plat in darwin-arm64 darwin-x64 win-amd64; do mkdir -p "vendor/zstandard/$plat"; done

pip download zstandard --no-deps --platform macosx_11_0_arm64  --only-binary=:all: -d /tmp/zwheel-arm
pip download zstandard --no-deps --platform macosx_11_0_x86_64 --only-binary=:all: -d /tmp/zwheel-x64
pip download zstandard --no-deps --platform win_amd64          --only-binary=:all: -d /tmp/zwheel-win

unzip -q /tmp/zwheel-arm/*.whl -d vendor/zstandard/darwin-arm64
unzip -q /tmp/zwheel-x64/*.whl -d vendor/zstandard/darwin-x64
unzip -q /tmp/zwheel-win/*.whl -d vendor/zstandard/win-amd64
```

After unpacking each `.whl`, the directory should look like:

```
vendor/zstandard/darwin-arm64/
  zstandard/
    __init__.py
    backend_cffi.py
    ...
    _cffi.abi3.so
```

The build script copies these into the final `.tox` so users don't need to install anything.

## Fallback

If a platform isn't bundled, `demon_ext.py` falls back to whatever `zstandard` is on TD's regular `sys.path` (which on a fresh TD install is nothing). In that case, audio slices arriving with `flags == SLICE_FLAG_DELTA` will be rejected with a clear error and the COMP will request raw (`compression: "none"`) on the next connect, which DEMON honors when available.
