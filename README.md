# demon-td

TouchDesigner operator for **DEMON** — real-time AI music generation.

Drop a single `.tox` into any TouchDesigner project, point it at a DEMON server (local pod or hosted via Daydream), and route audio in/out using standard CHOP wiring. Every public DEMON parameter is exposed through native TD parameter pages, and the whole thing is scriptable from Python.

> Status: v0.1 — wire protocol, schema, and extension class are complete and unit-tested. The `.tox` artifact is generated from this repo via a headless TD build (see [Building](#building)).

---

## What it does

- Connects to a DEMON backend via the standard queue → WebSocket protocol used by `demon-public-demo`.
- Optional **Daydream OAuth** via browser-popup + a local 127.0.0.1 callback listener (paste-API-key fallback for restricted networks).
- Exposes **~70 parameters** across 7 pages — every DEMON public param, plus session/auth controls.
- Streams audio **in** (from any CHOP) and **out** (to any CHOP) using native ports — like `Audio Filter` and `Audio Spectrum`.
- **Python API** for driving the op from other parts of a TD project.
- Mirrors traditional TD vendor-op UX: pulse actions, menu enums, header dividers, status read-outs.

## Quick start

1. Download `demonTD.tox` from the latest [GitHub release](#releases), or [build it yourself](#building).
2. Drag `demonTD.tox` into a fresh `.toe`.
3. Open the COMP's parameter pages. On the **Session** page:
   - Set `Server URL` (default `http://localhost:8000` for a local DEMON pod).
   - Leave `Anonymous` ON for local pods, or pulse `Authenticate` to sign in with Daydream.
   - Pulse `Connect`.
4. Wire an Audio CHOP into the COMP's input port, and the COMP's output into an `Audio Device Out`.
5. Type a prompt into `Prompt` and pulse `Send Prompt`.
6. Move `Denoise`, `Hint Strength`, LoRA toggles, etc. — they stream to the server at the 8 ms tick rate.

## Parameter pages

| Page | What's there |
|---|---|
| **Session** | Connect / Disconnect / Authenticate / Paste API Key, Server URL, API Key, Status, Queue Position, Expires In, Still Playing |
| **Init** | Session-start params (`sde`, `lora`, `depth`, `vae_window`, `crop`, `steps`, `fast_vae`, `walk_window`, `walk_window_s`, `Initial Prompt`, `Fixture Name`). Editing while connected reverts + prompts you to Reconnect. |
| **Prompt+LoRA** | `Send Prompt`, `Prompt` (multiline), `Key` (menu), `Time Signature` (menu), `Prompt Blend`, `LoRA Blend` + dynamic per-LoRA enable+strength rows populated from the server's `lora_catalog`. |
| **Synthesis** | `Denoise`, `Seed`, `Feedback`, `Shift`, `Hint Strength`, `Timbre Strength`, `Guidance Scale`, `CFG Rescale`, `ODE Noise`, `Periodicity`, 8× channel groups (`ch_g0`–`ch_g7`), 6× keystone channels (`ch13, ch14, ch19, ch23, ch29, ch56`). |
| **RCFG+DCW** | `RCFG Mode` menu, DCW block (`enabled`, `mode`, `scaler`, `high_scaler`, `wavelet`, `mult_blend`, `mag_phase`, `soft_thresh`). |
| **Curves** | JSON spec strings for `sde_denoise_curve`, `ode_noise_curve`, `x0_target_curve`, `velocity_scale_curve`, `initial_noise_curve`. |
| **Sources** | `Swap Source`, `Set/Clear Timbre Source`, `Set/Clear Structure Source`, fixture-name fields for both. |

## Python API

From anywhere in your TD project:

```python
demon = op('/project1/demon1')

demon.Connect(anonymous=True)
demon.SendPrompt('uplifting techno', key='Am', time_signature='4')
demon.SetParams({'denoise': 0.8, 'guidance_scale': 7.5, 'ch_g0': 1.2})
demon.EnableLora('vintage_synth', strength=0.6)
demon.SwapSource(op('audiofilein1'), tags='dark ambient')

# Status
print(demon.IsConnected, demon.Status)
```

Full public surface:

```text
# Properties
IsConnected -> bool
Status      -> dict

# Session
Connect(anonymous: bool | None = None) -> bool
Disconnect()
Authenticate()
SetApiKey(key)
PromptForApiKey()

# Continuous
SetParam(name, value)        # one-shot, immediate
SetParams(d: dict)           # batch

# Discrete
SendPrompt(tags=None, key=None, time_signature=None)
SetPromptBlend(value=None)
EnableLora(id, strength=1.0)
DisableLora(id)
SetTimbreStrength(value)
SetTimbreSource(chop=None, name="td_input")
SetTimbreFixture(name=None)
ClearTimbreSource()
SetStructureSource(chop=None, fixture=None, name="td_input")
SetStructureFixture(name=None)
ClearStructureSource()
SwapSource(chop=None, tags=None, key=None, time_signature=None, fixture=None)
```

## Architecture at a glance

```
[ CHOP input ] ─► Resample 48k ─► Script CHOP ─► WebSocket DAT ─► DEMON
                                                       │
   custom params ─► ParExec DAT ─► DemonExt._dirty     │
                                       │               │
                            Timer 0.008s ─► OnTick ────┘
                                                       │
                            DEMON ◄── WS ── Web Client DAT (queue/status)
                                                       │
   DEMON ─► WebSocket DAT (binary slice) ─► DemonExt.OnReceive
                                                       │
                                              Ring buffer (thread-safe)
                                                       │
                                              Script CHOP (audio_out)
                                                       │
                                            Resample → [ CHOP output ]
```

- **One COMP = one session.** Spin up multiple `demonTD.tox` copies for parallel sessions.
- **Continuous param fanout**: parameter changes coalesce into a single `{type:"params", raw:{...}}` message every 8 ms (matching DEMON's tick). A frantic slider drag becomes ≤ 125 Hz of dispatch, not 60×.
- **Discrete messages** (Send Prompt, Enable LoRA, Swap Source, …) bypass the tick — sent immediately.
- **Audio out is non-blocking.** WS slices land in a ring buffer; the Script CHOP reads from it on cook. Underrun returns silence — the TD cook thread never blocks on I/O.

## Repo layout

```
demon-td/
  src/                       # Python source (file-synced into the COMP's Text DATs)
    params.py                # SOURCE OF TRUTH for the parameter schema
    wire.py                  # WS message encoders + slice decoder
    queue_client.py          # /api/queue/{join,status,extend,leave} HTTP
    oauth.py                 # Daydream sign-in + token exchange
    audio.py                 # ring buffer, resample, to_stereo
    demon_ext.py             # DemonExt — the extension class loaded by TD
  vendor/zstandard/          # bundled wheels per platform (placeholder)
    darwin-arm64/ darwin-x64/ win-amd64/
  build/
    build_tox.py             # headless TD: regenerate demonTD.tox from src/
    template.toe             # base scaffold (generated on first build)
  examples/
    minimal.toe              # demo project referencing demonTD.tox
  tests/
    test_wire.py             test_params.py
    test_queue_client.py     test_audio.py
    test_oauth.py
  dist/                      # gitignored; demonTD.tox lives here after build
  pyproject.toml             # dev deps only — runtime is all stdlib + bundled
  README.md
```

## Building

The `.tox` is built by running the build script under TouchDesigner's CLI:

```bash
# macOS
/Applications/TouchDesigner.app/Contents/MacOS/TouchDesigner \
    -python /Users/you/git/demon-td/build/build_tox.py

# Windows
"C:\Program Files\Derivative\TouchDesigner\bin\TouchDesigner.exe" \
    -python C:\path\to\demon-td\build\build_tox.py
```

The script:
1. Loads (or scaffolds) `build/template.toe`.
2. Ensures every internal op exists with correct wiring.
3. File-syncs each `src/*.py` into a corresponding Text DAT.
4. Regenerates the COMP's custom parameter pages from `src/params.py`.
5. Saves `dist/demonTD.tox` and exits.

The schema in `src/params.py` is the single source of truth — adding a param is a one-line edit there plus, if it needs custom routing, one new branch in `DemonExt.OnParChange`.

## Bundled zstandard

DEMON may send `zstd`-compressed float16 audio slices. We bundle `zstandard` wheels under `vendor/zstandard/{darwin-arm64, darwin-x64, win-amd64}/` and prepend them to `sys.path` from `demon_ext.py` at startup. The handshake also requests `compression: "none"` when supported by the server, falling back to zstd transparently.

To populate the vendor dirs:

```bash
pip download zstandard --no-deps --platform macosx_11_0_arm64  --only-binary=:all: -d vendor/zstandard/darwin-arm64
pip download zstandard --no-deps --platform macosx_11_0_x86_64 --only-binary=:all: -d vendor/zstandard/darwin-x64
pip download zstandard --no-deps --platform win_amd64          --only-binary=:all: -d vendor/zstandard/win-amd64
# Then unpack each wheel into its dir:
for d in vendor/zstandard/*/; do
  whl=$(ls "$d"/*.whl 2>/dev/null | head -1); [ -z "$whl" ] && continue
  unzip -q -o "$whl" -d "$d" && rm "$whl"
done
```

## Development

```bash
python -m venv .venv
.venv/bin/pip install pytest responses numpy zstandard ruff
PYTHONPATH=src .venv/bin/pytest tests/ -v
```

All Python in `src/` is unit-testable outside TouchDesigner — the WS protocol, queue API, OAuth flow, ring buffer, and param schema each have their own tests, 52 in total.

`src/demon_ext.py` is the only module that imports TD globals (`me`, `op`, `project`, etc.), and it does so lazily inside methods so the test runner can still import it.

## Wire protocol references

- `demon-public-demo/vendor/demon-ui/engine/protocol.ts` — WS handshake + every message encoder, slice decoder
- `demon-public-demo/types/protocol.ts` — `SessionConfig`, slice flag constants, ready message shape
- `demon-public-demo/lib/queue/client.ts` — queue API response shape
- `demon-public-demo/lib/auth/daydream.ts` — Daydream OAuth flow

## Releases

`.tox` artifacts are attached to GitHub releases. To produce one locally, run the build under TD per the [Building](#building) section.

## Out of scope (for now)

- Visual curve editor (curves accept raw JSON in v1).
- MIDI/OSC mapping helpers — route via standard TD MIDI In + CHOP Export.
- Multi-session orchestration from a single COMP.
- C++ Custom CHOP — a Base COMP with CHOP I/O ports gives equivalent UX with much less build complexity.

## License

TBD — the rest of the DEMON ecosystem.
