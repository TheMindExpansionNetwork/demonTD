"""
TD build script — generates dist/demonTD.tox from the schema in
src/params.py and the Python source in src/.

How to run
----------
TouchDesigner is GUI-first, so this runs from inside TD:

  1. Open TouchDesigner.
  2. Drop a Text DAT in the network.
  3. Set its File par to <repo>/build/build_tox.py and turn on `Sync to File`.
  4. Right-click the DAT → Run Script.
  5. Watch Alt+T (Textport) for `[build_tox] wrote ...`.

What it does
------------
1. Loads build/template.toe (an empty .toe with a single Base COMP `demon`).
   If no template exists yet, scaffolds one from scratch.
2. Ensures every internal operator listed in TOPOLOGY exists with correct
   wiring, callbacks, and parameter bindings.
3. Adds file-synced Text DATs for each src/*.py.
4. Generates the COMP's custom parameter pages from params.PARAMS.
5. Sets the COMP's Extension to point at the demon_ext Text DAT.
6. Saves the COMP as dist/demonTD.tox and exits.

This script is idempotent — re-running it on a previously built .toe just
updates whatever has drifted.
"""

# NOTE: When this script runs, TD has injected the standard globals:
#   project, op, ops, parent, me, tdu, ui, root, etc.
#
# We import sys.path bootstrap to load our own modules.

import os
import sys

# Resolve repo paths. Inside TD, this file runs from a Text DAT and __file__
# is unreliable, so prefer me.par.file (set on the DAT pointing at this .py)
# and fall back to __file__ when running outside TD.
def _resolve_here() -> str:
    try:
        path = me.par.file.eval()  # type: ignore[name-defined]  # noqa: F821
        if path:
            return os.path.dirname(os.path.abspath(path))
    except Exception:
        pass
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except NameError:
        return os.getcwd()

HERE = _resolve_here()
REPO_ROOT = os.path.dirname(HERE)
SRC_DIR = os.path.join(REPO_ROOT, "src")
DIST_DIR = os.path.join(REPO_ROOT, "dist")
TEMPLATE_TOE = os.path.join(HERE, "template.toe")

print(f"[build_tox] HERE={HERE}")
print(f"[build_tox] SRC_DIR={SRC_DIR}")
print(f"[build_tox] DIST_DIR={DIST_DIR}")

if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

if not os.path.isdir(SRC_DIR):
    raise SystemExit(
        f"[build_tox] src/ not found at {SRC_DIR}.\n"
        f"  The build script must live in <repo>/build/ alongside src/.\n"
        f"  If you're running it from a Text DAT, make sure the DAT's 'File'\n"
        f"  par points at <repo>/build/build_tox.py (not a copy elsewhere)."
    )

# Invalidate cached modules so re-running the build picks up edits to
# params.py / wire.py / etc. TD's Python keeps sys.modules across script
# runs, so without this we'd use a stale Param dataclass and AttributeError
# on any newly-added field.
for _modname in ("params", "wire", "queue_client", "oauth", "audio", "ws_client"):
    sys.modules.pop(_modname, None)

import params as P  # noqa: E402  pylint: disable=wrong-import-position


# -----------------------------------------------------------------------------
# Internal topology — operators inside the Base COMP `demon`
# -----------------------------------------------------------------------------
TOPOLOGY = [
    # (op_name, OPClass-string, init params dict, position tuple)
    ("extension1",     "textDAT",       {}, (-600, 400)),
    ("ws1",            "websocketDAT",  {}, (-600, 200)),
    ("oauth_server",   "webserverDAT",  {}, (-600, 100)),
    ("param_exec1",    "parameterexecuteDAT", {}, (-600, 0)),
    ("tick8ms",        "timerCHOP",     {}, (-400, 0)),
    ("heartbeat",      "timerCHOP",     {}, (-400, -100)),
    ("audio_in",       "inCHOP",        {}, (-800, -200)),
    ("resample_in",    "resampleCHOP",  {}, (-600, -200)),
    ("script_send",    "scriptCHOP",    {}, (-400, -200)),
    ("audio_out",      "scriptCHOP",    {}, (400, -200)),
    ("resample_out",   "resampleCHOP",  {}, (600, -200)),
    ("out_chop",       "outCHOP",       {}, (800, -200)),
    ("lora_catalog",   "tableDAT",      {}, (-200, 400)),
    ("state",          "tableDAT",      {}, (-200, 300)),
]


def ensure_demon_comp():
    """Return the Base COMP `demon` at /project1/demon, creating if needed."""
    root_comp = op("/project1") if op("/project1") else root
    demon = root_comp.op("demon")
    if demon is None:
        demon = root_comp.create(baseCOMP, "demon")
    return demon


def ensure_internal_ops(demon):
    for name, optype_str, init_pars, pos in TOPOLOGY:
        existing = demon.op(name)
        if existing is None:
            try:
                cls = OPCLASS_LOOKUP[optype_str]
            except KeyError:
                print(f"!! unknown OP class {optype_str}; skipping {name}")
                continue
            o = demon.create(cls, name)
        else:
            o = existing
        try:
            o.nodeX, o.nodeY = pos
        except Exception:
            pass
        for pname, pval in init_pars.items():
            try:
                setattr(o.par, pname, pval)
            except Exception:
                pass
    return demon


# We declare OPCLASS_LOOKUP after TD globals are present.
def get_opclass_lookup():
    return {
        "baseCOMP":            baseCOMP,
        "textDAT":             textDAT,
        "tableDAT":            tableDAT,
        "websocketDAT":        websocketDAT,
        "webserverDAT":        webserverDAT,
        "parameterexecuteDAT": parameterexecuteDAT,
        "timerCHOP":           timerCHOP,
        "inCHOP":              inCHOP,
        "outCHOP":             outCHOP,
        "scriptCHOP":          scriptCHOP,
        "resampleCHOP":        resampleCHOP,
    }


# -----------------------------------------------------------------------------
# Param-page generation
# -----------------------------------------------------------------------------
def regenerate_param_pages(demon):
    """Drop existing custom pages and rebuild from P.PARAMS.

    Wraps every per-param operation in try/except. A failure on any one
    parameter prints a `!!` line but does NOT break the loop, so the rest
    of the schema continues to populate.
    """
    for page in list(demon.customPages):
        try:
            page.destroy()
        except Exception as e:
            print(f"!! destroy page {page.name}: {e}")

    page_lookup = {}
    for page_name in P.PAGES:
        try:
            page_lookup[page_name] = demon.appendCustomPage(page_name)
        except Exception as e:
            print(f"!! appendCustomPage({page_name}): {e}")

    n_added = 0
    n_failed = 0
    for p in sorted(P.PARAMS, key=lambda x: (x.page, x.order)):
        try:
            ok = _add_one_param(demon, page_lookup, p)
        except Exception as e:
            ok = False
            print(f"!! UNCAUGHT exception adding {p.page}/{p.name} ({p.type}): "
                  f"{type(e).__name__}: {e}")
        if ok:
            n_added += 1
        else:
            n_failed += 1

    print(f"[build_tox]   pages: added {n_added} pars, {n_failed} failed")


def _add_one_param(demon, page_lookup, p) -> bool:
    """Append one parameter from the schema. Returns True on success."""
    page = page_lookup.get(p.page)
    if page is None:
        try:
            page = demon.appendCustomPage(p.page)
            page_lookup[p.page] = page
        except Exception as e:
            print(f"!! couldn't create page {p.page}: {e}")
            return False

    label = p.label or p.name

    par = None
    try:
        if p.type == "Pulse":
            par = page.appendPulse(p.name, label=label)
        elif p.type == "Header":
            par = page.appendHeader(p.name, label=label)
        elif p.type == "Toggle":
            par = page.appendToggle(p.name, label=label)
        elif p.type == "Int":
            par = page.appendInt(p.name, label=label)
        elif p.type == "Float":
            par = page.appendFloat(p.name, label=label)
        elif p.type == "Str":
            par = page.appendStr(p.name, label=label)
        elif p.type == "Menu":
            par = page.appendMenu(p.name, label=label)
        elif p.type == "File":
            par = page.appendFile(p.name, label=label)
        else:
            print(f"!! unknown par type {p.type} for {p.name}")
            return False
    except Exception as e:
        print(f"!! append {p.type} {p.page}/{p.name}: {type(e).__name__}: {e}")
        return False

    if par is None:
        print(f"!! append returned None for {p.page}/{p.name}")
        return False

    try:
        p0 = par[0]
    except Exception as e:
        print(f"!! index par {p.name}: {e}")
        return False

    # Apply range FIRST so clamping doesn't squash a default-being-set later.
    # Use `min`/`max` not `normMin`/`normMax` — those are slider-display
    # only; the actual clamp uses min/max. Setting both makes the slider
    # and the clamp agree.
    if p.min is not None:
        for attr in ("min", "normMin"):
            try:
                setattr(p0, attr, p.min)
            except Exception:
                pass
        try:
            p0.clampMin = p.clamp_min
        except Exception:
            pass
    if p.max is not None:
        for attr in ("max", "normMax"):
            try:
                setattr(p0, attr, p.max)
            except Exception:
                pass
        try:
            p0.clampMax = p.clamp_max
        except Exception:
            pass
    # Now defaults + initial value.
    if p.default is not None and p.type not in ("Pulse", "Header"):
        try:
            p0.default = p.default
        except Exception:
            for alt in ("tupletDefaultValue", "defaultValue"):
                try:
                    setattr(p0, alt, p.default)
                    break
                except Exception:
                    continue
        try:
            p0.val = p.default
        except Exception as e:
            print(f"!! val on {p.name}: {e}")
    if p.help:
        try:
            p0.help = p.help
        except Exception:
            pass
    if p.menu_names:
        try:
            p0.menuNames = list(p.menu_names)
            p0.menuLabels = list(p.menu_labels or p.menu_names)
        except Exception as e:
            print(f"!! menu on {p.name}: {e}")
    if p.readonly:
        try:
            p0.readOnly = True
        except Exception:
            pass
    if not p.enable:
        try:
            for sub in par:
                try:
                    sub.enable = False
                except Exception:
                    pass
        except Exception as e:
            print(f"!! enable=False on {p.name}: {e}")
    if p.multiline:
        try:
            p0.style = "Str"
        except Exception:
            pass

    return True


# -----------------------------------------------------------------------------
# DAT sync
# -----------------------------------------------------------------------------
SRC_FILES = ["params.py", "wire.py", "queue_client.py", "oauth.py", "audio.py",
             "ws_client.py", "demon_ext.py"]


def sync_text_dats(demon):
    """Ensure each src/*.py has a corresponding Text DAT.

    Two things happen per file:
      1. We READ THE FILE DIRECTLY and set dat.text — this guarantees
         the content is in the DAT immediately, so `op('demon_ext').module`
         resolves on the same frame the extension is wired.
      2. We set par.file + syncfile so the DAT round-trips to disk for
         hot-reload during development. (When the .tox is exported, the
         text travels inline; users dropping the .tox into a fresh project
         don't need src/ on disk.)
    """
    for fname in SRC_FILES:
        dat_name = fname.replace(".py", "")
        dat = demon.op(dat_name)
        if dat is None:
            dat = demon.create(textDAT, dat_name)
        abs_path = os.path.join(SRC_DIR, fname)
        try:
            # utf-8-sig strips a leading BOM if present. TD's tokenizer
            # rejects U+FEFF, and some editors / IDE auto-saves add one.
            with open(abs_path, "r", encoding="utf-8-sig") as fh:
                text = fh.read()
            # Defensive: also strip any in-text BOMs from accidental
            # multi-encode passes.
            if text.startswith("﻿"):
                text = text.lstrip("﻿")
            dat.text = text
            print(f"[build_tox]   loaded {fname} ({len(text)} chars)")
        except Exception as e:
            print(f"!! could not read {abs_path}: {e}")
            continue
        try:
            dat.par.file = abs_path
            # Important: keep one-way (file -> DAT) load only.
            # Bidirectional sync re-writes the .py file from the DAT every
            # cook, which can round-trip BOMs or other in-memory artifacts
            # back to disk. Build-time scripts edit src/ on disk; we want
            # TD to read those changes, not overwrite them.
            dat.par.syncfile = False
            dat.par.loadonstart = True
            dat.par.writepulse.pulse() if hasattr(dat.par, "writepulse") else None
        except Exception as e:
            print(f"!! sync flags on {fname}: {e}")


def wire_extension(demon):
    """Point the COMP's extension at the demon_ext Text DAT.

    For the .module property to resolve, the DAT must contain valid Python
    AT THE TIME the extension expression is evaluated. We verify the
    sibling module loads cleanly before wiring it as an extension —
    that way `.module is None` errors surface here as a clear traceback
    rather than as a mystery NoneType later.
    """
    # ---- diagnostic: can we load demon_ext as a module right now? ----
    demon_ext_dat = demon.op("demon_ext")
    if demon_ext_dat is None:
        print("!! demon_ext DAT not found inside the COMP")
        return
    text_len = len(demon_ext_dat.text or "")
    print(f"[build_tox]   demon_ext DAT: {text_len} bytes of text")

    try:
        m = demon_ext_dat.module
    except Exception as e:
        print(f"!! demon_ext.module raised: {type(e).__name__}: {e}")
        m = None

    if m is None:
        print("!! demon_ext.module is None — the DAT failed to compile.")
        print("   Likely cause: sibling-module import inside demon_ext.py "
              "couldn't resolve. Check that params/wire/queue_client/oauth/"
              "audio Text DATs all exist and have text.")
        for sibling in ("params", "wire", "queue_client", "oauth", "audio"):
            d = demon.op(sibling)
            if d is None:
                print(f"     !! sibling '{sibling}' is missing!")
            else:
                blen = len(d.text or "")
                try:
                    sm = d.module
                except Exception as e:
                    sm = None
                    print(f"     !! sibling '{sibling}' ({blen}B): .module raised {type(e).__name__}: {e}")
                if sm is not None:
                    print(f"     ok '{sibling}' ({blen}B) -> module {sm.__name__}")
                elif d is not None:
                    print(f"     !! sibling '{sibling}' ({blen}B): .module is None")
        # Don't wire the extension if it's known broken — leave it unwired
        # so the COMP still saves and the user can manually fix.
        return

    if not hasattr(m, "DemonExt"):
        print(f"!! demon_ext.module loaded but has no DemonExt class. "
              f"Module dir: {sorted(d for d in dir(m) if not d.startswith('_'))}")
        return

    # ---- wire it ----
    try:
        try:
            demon.par.extension1 = ""
        except Exception:
            pass
        demon.par.extname1 = "DemonExt"
        demon.par.extension1 = "op('./demon_ext').module.DemonExt(me)"
        demon.par.promoteextension1 = True
        try:
            demon.par.reinitextensions.pulse()
        except Exception:
            pass
        # Verify
        try:
            ext_inst = demon.ext.DemonExt
            print(f"[build_tox]   extension wired: {type(ext_inst).__name__}")
        except Exception as e:
            print(f"!! extension verify failed: {type(e).__name__}: {e}")
    except Exception as e:
        print(f"!! extension wire failed: {e}")


def wire_callbacks(demon):
    """Set up parexec/timer/ws callbacks.

    For most TD callback-bearing ops (WebSocket DAT, Timer CHOP, Script CHOP,
    Web Server DAT) you set `par.callbacks = "callbacks"` pointing at a
    shared Text DAT.

    Parameter Execute DAT is the exception — its callback functions live in
    its OWN text, not in an external DAT. So we write the parexec callbacks
    directly into param_exec1's text.
    """
    cb = demon.op("callbacks")
    if cb is None:
        cb = demon.create(textDAT, "callbacks")
        cb.nodeX, cb.nodeY = -400, 400
    cb.text = CALLBACKS_PY

    # Parameter Execute DAT — callbacks live in its own text.
    pe = demon.op("param_exec1")
    if pe is not None:
        pe.text = PARAM_EXEC_PY
        try:
            # Watch the parent COMP's custom pars. Inside a Base COMP, the
            # parexec DAT lives one level below the COMP itself, so `..` is
            # the right reference.
            pe.par.op = ".."
        except Exception as e:
            print(f"!! param_exec1.par.op: {e}")
        for par_name in ("pars",):
            try:
                setattr(pe.par, par_name, "*")
            except Exception:
                pass
        for par_name in ("valuechange", "onvaluechange"):
            try:
                setattr(pe.par, par_name, True)
                break
            except Exception:
                pass
        for par_name in ("pulse", "onpulse"):
            try:
                setattr(pe.par, par_name, True)
                break
            except Exception:
                pass
        try:
            pe.par.active = True
        except Exception:
            pass

    ws = demon.op("ws1")
    if ws is not None:
        try:
            ws.par.callbacks = "callbacks"
        except Exception as e:
            print(f"!! ws.par.callbacks: {e}")

        # Don't fight TD on format/binary settings — defaults route both text
        # and binary to their respective onReceive callbacks. Setting Format
        # explicitly was causing issues. Just dump what's available so we
        # know.
        try:
            print("[build_tox]   ws1 pars:")
            for p in ws.customPars:
                try:
                    print(f"     custom: {p.name} = {p.eval()!r}")
                except Exception:
                    pass
            for pname in ("active", "netaddress", "port", "timeout",
                          "callbacks", "executeloc", "fromop"):
                par = getattr(ws.par, pname, None)
                if par is not None:
                    try:
                        print(f"     {pname} = {par.eval()!r}")
                    except Exception:
                        print(f"     {pname} (uneval)")
        except Exception as e:
            print(f"!! ws par dump: {e}")

    for name in ("tick8ms", "heartbeat"):
        t = demon.op(name)
        if t is not None:
            try:
                t.par.callbacks = "callbacks"
                if name == "tick8ms":
                    t.par.length = 0.008
                    t.par.cycle = True
                else:
                    t.par.length = 5.0
                    t.par.cycle = True
            except Exception:
                pass

    ws_server = demon.op("oauth_server")
    if ws_server is not None:
        try:
            ws_server.par.callbacks = "callbacks"
            ws_server.par.active = False  # only on during Auth
        except Exception:
            pass

    for name in ("script_send", "audio_out"):
        s = demon.op(name)
        if s is not None:
            try:
                s.par.callbacks = "callbacks"
            except Exception:
                pass


PARAM_EXEC_PY = '''# auto-generated by build_tox.py
# Parameter Execute DAT callbacks. Live in this DAT's OWN text (not in an
# external callbacks DAT) — that's TD's parexec convention.

def _ext():
    return parent().ext.DemonExt

def onValueChange(par, prev):
    try:
        _ext().OnParChange(par)
    except Exception as e:
        print(f"[param_exec onValueChange] {par.name}: {e}")

def onPulse(par):
    try:
        _ext().OnParChange(par)
    except Exception as e:
        print(f"[param_exec onPulse] {par.name}: {e}")

def onExpressionChange(par, val, prev): pass
def onExportChange(par, val, prev): pass
def onEnableChange(par, val, prev): pass
def onModeChange(par, val, prev): pass
'''


CALLBACKS_PY = '''# auto-generated by build_tox.py
# Routes TD callbacks into the DemonExt extension.
#
# TD's various op types each call fixed-name functions. We dispatch by
# op name so one callbacks DAT serves the whole COMP.

def _ext():
    return me.parent().ext.DemonExt

def onValueChange(par, prev):
    _ext().OnParChange(par)

def onPulse(par):
    _ext().OnParChange(par)

def onTimer(timerOp, segment):
    name = timerOp.name
    ext = _ext()
    if name == "tick8ms":
        ext.OnTick()
    elif name == "heartbeat":
        ext.OnHeartbeat()

def onReceiveText(dat, rowIndex, message):
    try:
        print(f"[ws onReceiveText] len={len(message) if message else 0}: {message[:200] if message else ''!r}")
    except Exception:
        pass
    _ext().OnReceive(dat, rowIndex=rowIndex, message=message)

def onReceiveBinary(dat, contents):
    try:
        print(f"[ws onReceiveBinary] len={len(contents) if contents else 0}")
    except Exception:
        pass
    _ext().OnReceive(dat, contents=contents)

def onConnect(dat):
    try:
        print(f"[ws onConnect] {dat.name} netaddress={dat.par.netaddress.eval()}")
    except Exception:
        pass
    try:
        _ext().OnWsConnect(dat)
    except Exception as e:
        print(f"[ws onConnect] OnWsConnect failed: {e}")

def onDisconnect(dat):
    # TD WebSocket DAT exposes the last close code/reason on the DAT.
    # Surface anything available so we can diagnose server-side kicks.
    info_bits = []
    for attr in ("closeCode", "closeReason", "lastError", "errors"):
        v = getattr(dat, attr, None)
        if v is not None and not callable(v):
            info_bits.append(f"{attr}={v!r}")
    try:
        print(f"[ws onDisconnect] {dat.name} " + (" ".join(info_bits) or ""))
    except Exception:
        pass

def onHTTPRequest(webServerDAT, request, response):
    uri = request.get("uri", "")
    status, ctype, body = _ext().OnHTTPRequest(uri)
    response["statusCode"] = status
    response["statusReason"] = "OK" if status == 200 else "Error"
    response["data"] = body
    response["content-type"] = ctype
    return response

# Script CHOP cook hook. TD calls onCook(scriptOp) on the configured DAT.
# We dispatch by the calling op's name.
def onCook(scriptOp):
    name = scriptOp.name
    ext = _ext()
    if name == "script_send":
        ext.OnCookSend(scriptOp)
    elif name == "audio_out":
        ext.OnCookRecv(scriptOp)
'''


# -----------------------------------------------------------------------------
# Audio wiring
# -----------------------------------------------------------------------------
def wire_audio(demon):
    """Wire the audio-OUT chain (audio_out Script CHOP → resample → Out CHOP).

    NOTE: This release does NOT stream audio IN from a CHOP. The source track
    is uploaded once from the Source Audio File par at Connect time. The
    audio_in / resample_in / script_send ops still exist for back-compat
    with the saved .tox topology but are NOT connected to anything;
    script_send.onCook is a no-op.
    """
    audio_out = demon.op("audio_out")
    resample_out = demon.op("resample_out")
    out_chop = demon.op("out_chop")

    # Configure audio_out Script CHOP for audio-rate, time-sliced output.
    if audio_out is not None:
        try:
            audio_out.par.callbacks = "callbacks"
        except Exception:
            pass
        # Time Slice on — TD pulls a block per audio cycle from downstream.
        for parname in ("timeslice", "timeslicemode"):
            try:
                setattr(audio_out.par, parname, True)
                break
            except Exception:
                pass
        # 2-channel default; the cook callback will set the actual sample count.
        try:
            audio_out.par.channelnames = "chan1 chan2"
        except Exception:
            pass

    # Connect audio_out → out_chop DIRECTLY.
    # resample_out remains in the topology for back-compat but is
    # disconnected — Audio Device Out handles its own resampling.
    if resample_out is not None:
        try:
            # Detach any input it might have from a previous build run.
            for c in resample_out.inputConnectors:
                if c.connections:
                    c.disconnect()
        except Exception:
            pass

    try:
        if audio_out is not None and out_chop is not None:
            # Clear out_chop's existing connections first to avoid stale wiring.
            try:
                for c in out_chop.inputConnectors:
                    if c.connections:
                        c.disconnect()
            except Exception:
                pass
            out_chop.inputConnectors[0].connect(audio_out)
    except Exception as e:
        print(f"audio wiring: {e}")

    # script_send is a vestigial no-op; still hook up its callbacks so it doesn't error.
    script_send = demon.op("script_send")
    if script_send is not None:
        try:
            script_send.par.callbacks = "callbacks"
        except Exception:
            pass


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
def main():
    """Build the demon COMP into the currently-open TD project.

    This function is NON-DESTRUCTIVE:
      - Never closes / saves / quits the host TD project.
      - Only creates the `demon` COMP and its children.
      - Saves out the .tox file to dist/.
      - Leaves TD running with the COMP visible so the user can inspect.

    Safe to re-run; ops are upserted, not duplicated.
    """
    global OPCLASS_LOOKUP
    OPCLASS_LOOKUP = get_opclass_lookup()

    os.makedirs(DIST_DIR, exist_ok=True)

    print("[build_tox] creating demon COMP...")
    demon = ensure_demon_comp()
    print(f"[build_tox]   COMP at {demon.path}")

    print("[build_tox] ensuring internal ops...")
    ensure_internal_ops(demon)

    print("[build_tox] syncing source DATs...")
    sync_text_dats(demon)

    print("[build_tox] wiring callbacks...")
    wire_callbacks(demon)

    print("[build_tox] wiring audio...")
    wire_audio(demon)

    print("[build_tox] regenerating parameter pages...")
    regenerate_param_pages(demon)

    print("[build_tox] wiring extension...")
    wire_extension(demon)

    out_path = os.path.join(DIST_DIR, "demonTD.tox")
    print(f"[build_tox] saving {out_path}")
    demon.save(out_path)

    print(f"[build_tox] DONE — wrote {out_path}")
    print(f"[build_tox] Inspect /project1/demon, or drag {out_path} into a fresh .toe.")


# Entry: always run when executed inside TD.
# Inside TD this file is usually loaded into a Text DAT and run via the DAT's
# `Run Script` action. We don't gate on __name__ because TD doesn't set it
# to "__main__" in that path.
try:
    main()
except Exception as exc:
    print(f"[build_tox] FAILED: {exc}")
    import traceback
    traceback.print_exc()
