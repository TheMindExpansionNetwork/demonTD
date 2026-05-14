"""
TD build script — generates dist/demon.tox from the schema in
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
6. Saves the COMP as dist/demon.tox and exits.

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
    """Drop existing custom pages and rebuild from P.PARAMS."""
    for page in list(demon.customPages):
        page.destroy()

    page_lookup = {}
    for page_name in P.PAGES:
        page_lookup[page_name] = demon.appendCustomPage(page_name)

    for p in sorted(P.PARAMS, key=lambda x: (x.page, x.order)):
        page = page_lookup.get(p.page)
        if page is None:
            page = demon.appendCustomPage(p.page)
            page_lookup[p.page] = page

        label = p.label or p.name

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
            else:
                print(f"!! unknown par type {p.type} for {p.name}")
                continue
        except Exception as e:
            print(f"!! failed to append {p.type} {p.name}: {e}")
            continue

        # par is a ParGroup; don't bool() it. Indexing returns the underlying
        # Par object we can set attributes on.
        if par is None:
            continue
        try:
            p0 = par[0]
        except Exception as e:
            print(f"!! failed to index par {p.name}: {e}")
            continue

        # Defaults and ranges
        if p.default is not None and p.type not in ("Pulse", "Header"):
            try:
                p0.default = p.default
                p0.val = p.default
            except Exception:
                pass
        if p.min is not None:
            try:
                p0.normMin = p.min
                p0.clampMin = p.clamp_min
            except Exception:
                pass
        if p.max is not None:
            try:
                p0.normMax = p.max
                p0.clampMax = p.clamp_max
            except Exception:
                pass
        if p.help:
            try:
                p0.help = p.help
            except Exception:
                pass
        if p.menu_names:
            try:
                p0.menuNames = list(p.menu_names)
                p0.menuLabels = list(p.menu_labels or p.menu_names)
            except Exception:
                pass
        if p.readonly:
            try:
                p0.readOnly = True
            except Exception:
                pass
        if p.multiline:
            try:
                p0.style = "Str"  # multiline edit is a TD-side widget choice
            except Exception:
                pass


# -----------------------------------------------------------------------------
# DAT sync
# -----------------------------------------------------------------------------
SRC_FILES = ["params.py", "wire.py", "queue_client.py", "oauth.py", "audio.py",
             "demon_ext.py"]


def sync_text_dats(demon):
    """Ensure each src/*.py has a corresponding Text DAT.

    For the build (this script), we use absolute paths to the .py files —
    that way the build works regardless of where the host .toe is saved.
    The .tox we export keeps the synced text inline; users dropping the
    .tox into their own project don't need src/ at all.
    """
    for fname in SRC_FILES:
        dat_name = fname.replace(".py", "")
        dat = demon.op(dat_name)
        if dat is None:
            dat = demon.create(textDAT, dat_name)
        abs_path = os.path.join(SRC_DIR, fname)
        try:
            dat.par.file = abs_path
            dat.par.syncfile = True
            dat.par.loadonstart = True
            # Force a read so the text is in the DAT right now.
            try:
                dat.par.loadonstartpulse.pulse()
            except Exception:
                pass
        except Exception as e:
            print(f"!! sync {fname}: {e}")


def wire_extension(demon):
    """Point the COMP's extension at the demon_ext Text DAT."""
    try:
        demon.par.extname1 = "DemonExt"
        demon.par.extension1 = "op('demon_ext').module.DemonExt(me)"
        demon.par.promoteextension1 = True
    except Exception as e:
        print(f"!! extension wire failed: {e}")


def wire_callbacks(demon):
    """Set up parexec/timer/ws callbacks via a single callbacks Text DAT."""
    cb = demon.op("callbacks")
    if cb is None:
        cb = demon.create(textDAT, "callbacks")
        cb.nodeX, cb.nodeY = -400, 400
    cb.text = CALLBACKS_PY

    # parameter execute DAT -> callbacks
    pe = demon.op("param_exec1")
    if pe is not None:
        try:
            pe.par.op = "."
            pe.par.dat = "callbacks"
            pe.par.valuechange = True
            pe.par.pulse = True
        except Exception:
            pass

    ws = demon.op("ws1")
    if ws is not None:
        try:
            ws.par.callbacks = "callbacks"
            ws.par.format = 1  # binary+text auto
            ws.par.receivebinary = True
        except Exception:
            pass

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


CALLBACKS_PY = '''# auto-generated by build_tox.py
# Routes TD callbacks into the DemonExt extension.

def onValueChange(par, prev):
    me.parent().ext.DemonExt.OnParChange(par)

def onPulse(par):
    me.parent().ext.DemonExt.OnParChange(par)

def onTimer(timerOp, segment):
    name = timerOp.name
    if name == "tick8ms":
        me.parent().ext.DemonExt.OnTick()
    elif name == "heartbeat":
        me.parent().ext.DemonExt.OnHeartbeat()

def onReceiveText(dat, rowIndex, message):
    me.parent().ext.DemonExt.OnReceive(dat, rowIndex=rowIndex, message=message)

def onReceiveBinary(dat, contents):
    me.parent().ext.DemonExt.OnReceive(dat, bytes=contents)

def onConnect(dat):
    pass

def onDisconnect(dat):
    pass

def onHTTPRequest(webServerDAT, request, response):
    uri = request.get("uri", "")
    status, ctype, body = me.parent().ext.DemonExt.OnHTTPRequest(uri)
    response["statusCode"] = status
    response["statusReason"] = "OK" if status == 200 else "Error"
    response["data"] = body
    response["content-type"] = ctype
    return response

def cookSend(scriptOp):
    me.parent().ext.DemonExt.OnCookSend(scriptOp)

def cookRecv(scriptOp):
    me.parent().ext.DemonExt.OnCookRecv(scriptOp)
'''


# -----------------------------------------------------------------------------
# Audio wiring
# -----------------------------------------------------------------------------
def wire_audio(demon):
    """Connect Audio CHOP I/O ports through resample → script chops."""
    audio_in = demon.op("audio_in")
    resample_in = demon.op("resample_in")
    script_send = demon.op("script_send")
    audio_out = demon.op("audio_out")
    resample_out = demon.op("resample_out")
    out_chop = demon.op("out_chop")

    try:
        if resample_in is not None:
            resample_in.par.method = "Linear"
            resample_in.par.rate = 48000
        if resample_out is not None:
            resample_out.par.method = "Linear"
            resample_out.par.rate = 48000
    except Exception:
        pass

    try:
        if audio_in is not None and resample_in is not None:
            resample_in.inputConnectors[0].connect(audio_in)
        if resample_in is not None and script_send is not None:
            script_send.inputConnectors[0].connect(resample_in)
        if audio_out is not None and resample_out is not None:
            resample_out.inputConnectors[0].connect(audio_out)
        if resample_out is not None and out_chop is not None:
            out_chop.inputConnectors[0].connect(resample_out)
    except Exception as e:
        print(f"audio wiring: {e}")

    # script_send needs a "cookSend" callback hook; script_recv needs "cookRecv".
    try:
        if script_send is not None:
            script_send.par.callbacks = "callbacks"
            # If the DAT has a function selector par:
            try:
                script_send.par.func = "cookSend"
            except Exception:
                pass
        if audio_out is not None:
            audio_out.par.callbacks = "callbacks"
            try:
                audio_out.par.func = "cookRecv"
            except Exception:
                pass
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

    out_path = os.path.join(DIST_DIR, "demon.tox")
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
