# Local testing guide

End-to-end loop on your Mac: launch a local DEMON pod, build the `.tox` from this repo, drop it into a TouchDesigner project, connect, hear audio.

## Prereqs

- macOS with TouchDesigner installed at `/Applications/TouchDesigner.app`
- Python 3.11 (TD ships its own; you only need this for unit tests)
- The DEMON repo cloned next to this one at `~/git/DEMON`
- `uv` installed (DEMON uses it): `brew install uv`

## 1. Start a local DEMON pod

From the DEMON repo. The pod listens on a single port for both HTTP and WebSocket.

```bash
cd ~/git/DEMON
uv run python -u -m demos.realtime_motion_graph_web --port 8765
```

The pod's WebSocket lives at `ws://127.0.0.1:8765/`. There is **no queue API** at this level — that's only in `demon-public-demo`. The TD operator's "Direct Pod" mode handles this.

> **No GPU? `--ui-only`** — pass `--ui-only` to start a stub that handshakes but generates no audio. Useful for verifying connect/auth/param fanout without waiting for engines to build.

## 2. Build `demon.tox`

TouchDesigner is GUI-first; there's no true headless mode. The realistic
build workflow is:

1. Open TouchDesigner. **File → New** for a blank project.
2. Drop a **Text DAT** in the network. Name it `build`.
3. On the DAT: set `File` to `~/git/demon-td/build/build_tox.py`, toggle
   `Load on Start` and `Sync to File` on. The script content loads in.
4. Right-click the DAT → **Run Script**.
5. Watch the Textport (`Alt+T`) for `[build_tox] wrote .../dist/demon.tox`.

The script:
- Scaffolds `build/template.toe` on first run (commit it after).
- Creates a Base COMP `demon` at `/project1/demon` with all internal ops.
- File-syncs each `src/*.py` into a Text DAT inside the COMP.
- Regenerates the custom parameter pages from `src/params.py`.
- Saves `dist/demon.tox`.

Re-running is idempotent — it updates whatever drifted.

> **Heads-up**: `build_tox.py` was written from the wire-protocol spec and
> unit-tested outside TD. The first time you run it, expect 1–2 TD-version-
> specific tweaks (e.g. an op-class name or par-creation API quirk). Fixes
> belong in `build/build_tox.py`. Open a PR — most TD versions just need
> one or two lines changed.

## 3. Drag into a TD project

1. Open TouchDesigner. New `.toe`.
2. Drag `dist/demon.tox` from Finder into the network — a Base COMP named `demon1` appears.
3. Click the COMP, open the **Session** parameter page.

## 4. Connect

With the defaults:

| Field | Value |
|---|---|
| Direct Pod | **on** (default) |
| Anonymous | **on** |
| Server URL | `http://localhost:8765` |
| API Key | (empty) |

Pulse **Connect**. The `Status` par should read `"Connected"` within a second. If not, see [Troubleshooting](#troubleshooting).

## 5. Hear audio

1. Drop an `Audio File In CHOP` (`audiofilein1`), load any 48k stereo WAV.
2. Wire `audiofilein1` → `demon1`'s CHOP input port.
3. Wire `demon1`'s CHOP output → `Audio Device Out CHOP`.
4. On the **Prompt+LoRA** page, type `"uplifting techno"` in `Prompt` and pulse **Send Prompt**.
5. Move sliders on the **Synthesis** page (`Denoise`, `Hint Strength`, `ch_g0…ch_g7`) and listen for changes.

## 6. Try the Python API

In a Text DAT inside the same project:

```python
demon = op('/project1/demon1')
demon.SetParams({'denoise': 0.85, 'guidance_scale': 8.0})
demon.SendPrompt('dark ambient drone', key='auto')
```

## 7. (Optional) Test queue mode against demon-public-demo

```bash
cd ~/git/demon-public-demo
npm install
npm run dev   # starts at http://localhost:3000
```

In TD:
- Set `Server URL` → `http://localhost:3000`
- Toggle `Direct Pod` **off**
- Pulse `Connect`

You should see queue position updates while `Status` shifts from `Joining queue...` → `Connected`.

## 8. (Optional) Test Daydream OAuth

- Toggle `Anonymous` **off**
- Pulse `Authenticate` → a browser tab opens to `app.daydream.live`
- Sign in; the tab auto-closes; `API Key` populates with a secret value
- Pulse `Connect` (queue mode against the hosted endpoint)

If `127.0.0.1` callbacks are blocked on your network, pulse `Paste API Key` instead and paste the key from your Daydream dashboard.

## Troubleshooting

**`Status: WS open failed`** — verify the pod is reachable: `curl -i http://localhost:8765/` should return something other than connection-refused.

**No audio coming out** — check that the COMP's CHOP output is reaching an `Audio Device Out`, and that the pod logged a `ready` message (visible in DEMON's terminal). The first ~1 second after connect is the priming-buffer silence.

**`zstandard not available`** — the COMP requests `compression: "none"` at handshake, which DEMON honors. If you see this warning the connection still works, but if the server insists on compressed slices, populate `vendor/zstandard/<platform>/` per the README and rebuild.

**Init param greyed-out / reverts** — that's the intended UX. Pulse `Disconnect`, edit the Init page, pulse `Connect` to apply.

**Build script errors about `OPCLASS_LOOKUP`** — open `build/template.toe` interactively in TD first to let it bootstrap, save it, then re-run the build.
