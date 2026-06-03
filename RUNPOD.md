# RunPod + TouchDesigner (demonTD)

Use a GPU pod for DEMON and **TouchDesigner on your laptop** for visuals.

## Pod: start the WebSocket backend

From the [DEMON](https://github.com/daydreamlive/DEMON) repo (or [sonic-forage-heartbeat-runpod](https://github.com/TheMindExpansionNetwork/sonic-forage-heartbeat-runpod)):

```bash
export ACESTEP_MODELS_DIR=/workspace/.daydream-scope/models/demon
./demos/sonic_forage_jam/start_demon_td_backend.sh
```

Expose TCP **1318**. Your TD **Server URL**:

```text
wss://<RUNPOD_POD_ID>-1318.proxy.runpod.net/
```

## Laptop: TouchDesigner

1. Download `demonTD.tox` from [Releases](https://github.com/daydreamlive/demonTD/releases) (v0.2.11+).
2. Drag into a TD project.
3. Session → Mode **Direct** → Server URL = `wss://...` above → **Connect**.

Full walkthrough (audio prefs, stems, Python API):  
[DEMON demos/sonic_forage_jam/TOUCHDESIGNER.md](https://github.com/TheMindExpansionNetwork/sonic-forage-heartbeat-runpod/blob/main/demos/sonic_forage_jam/TOUCHDESIGNER.md) (in the sonic-forage fork).