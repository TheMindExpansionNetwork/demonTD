# Examples

## `minimal.toe`

The smallest possible setup:

```
audiofilein1  ──►  demon1  ──►  audiodevout1
                     ▲
              (custom params)
```

1. Open the file in TouchDesigner.
2. Make sure DEMON is running locally on the default port, or set `demon1`'s `Server URL` to a hosted endpoint.
3. Pulse `Connect`.
4. Type a prompt and pulse `Send Prompt`.

## `chop_driven.toe`

Drives DEMON params from a CHOP network — useful for sensor input, MIDI, or LFO-modulated generation.

```
midiin1 ──► math1 (range map) ──► export to demon1.par.Denoise
                              └─► export to demon1.par.Hintstrength
audiofilein1 ──► demon1 ──► audiodevout1
```

Each TD param can be CHOP-exported in the standard way; the param-execute DAT inside the COMP picks up the evaluated value at the 8ms tick.

---

These `.toe` files are generated alongside the `.tox` on first build by the headless build script. Until then, you can construct them by hand: drag `dist/demonTD.tox` into a fresh `.toe`, wire as shown above.
