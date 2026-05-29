"""
Declarative parameter schema for the DEMON TouchDesigner operator.

This is the SOURCE OF TRUTH. The build script (build/build_tox.py)
generates the COMP's custom parameter pages from this list, and the
extension (demon_ext.py) routes parameter changes by looking up entries
here.

Adding a new param = one entry in PARAMS.
Adding a new discrete message = one entry in DISCRETE_MESSAGES.

Param categories
----------------
- "init": session-start params (immutable while connected)
- "continuous": fanned-out at the 8ms tick as `{type:"params", raw:{...}}`
- "session": local-only (connection state, auth, status)
- "discrete": triggers a one-shot WS message (pulse / toggle)

TD parameter types
------------------
- "Float", "Int", "Toggle", "Str", "Menu", "Pulse", "Header"
- Menu pars carry `menu_names` and `menu_labels` (lists)
- Pulse pars carry `is_pulse=True`
- Header pars are layout-only, no value
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Param:
    name: str                       # TD par name (PascalCase preferred for visibility)
    wire_name: str | None           # The key sent on the wire (None for local/UI-only)
    page: str                       # TD parameter page label
    type: str                       # "Float" | "Int" | "Toggle" | "Str" | "Menu" | "Pulse" | "Header" | "File"
    category: str                   # "init" | "continuous" | "session" | "discrete"
    default: Any = None
    min: float | None = None
    max: float | None = None
    clamp_min: bool = False         # whether to hard-clamp at min
    clamp_max: bool = False         # whether to hard-clamp at max
    label: str | None = None        # display label (defaults to name)
    help: str = ""
    menu_names: tuple[str, ...] = ()
    menu_labels: tuple[str, ...] = ()
    multiline: bool = False
    secret: bool = False            # hide value in .tox serialization (for API keys)
    readonly: bool = False
    enable: bool = True             # False = par appears greyed-out / non-editable
    section_header: bool = False    # this is a Header par
    order: int = 0


# -----------------------------------------------------------------------------
# Page 1: Session (connection, auth, status)
# -----------------------------------------------------------------------------
SESSION_PARAMS: list[Param] = [
    Param("Connect", None, "Session", "Pulse", "session", order=10,
          help="Open a session against the configured server."),
    Param("Disconnect", None, "Session", "Pulse", "session", order=20,
          help="Close the active WebSocket session."),
    Param("Serverurl", None, "Session", "Str", "session",
          default="ws://localhost:8765/", order=30, label="Server URL",
          help="DEMON pod WebSocket URL. ws://localhost:8765/ is the default "
               "port used by DEMON's demos.realtime_motion_graph_web; for a "
               "Vast.ai-hosted pod use the Direct WS line printed by "
               "scripts/vast/launch.sh (e.g. ws://1.2.3.4:44105/)."),
    Param("Sourcefile", None, "Session", "File", "session",
          default="", order=40, label="Source Audio File",
          help="WAV file to send as the source/reference audio on Connect. "
               "DEMON conditions its generation on this input. REQUIRED."),
    Param("Status", None, "Session", "Str", "session", default="Idle",
          order=50, readonly=True,
          help="Current connection status."),
    Param("Speakerout", None, "Session", "Toggle", "session", default=True,
          order=55, label="Python Audio Out",
          help="Play generated audio directly via Python sounddevice / "
               "PortAudio to the system default output. Required because "
               "TD's CHOP audio chain doesn't pull a Script CHOP at audio "
               "rate across a Base COMP boundary. Toggle off if you want "
               "to route the audio only via the COMP's out_chop port to "
               "your own external Audio Device Out CHOP."),
    # ---------- Hosted mode (v0.2+) ----------
    # Mode toggles between Direct (a pod URL the user supplies) and
    # Hosted (the Daydream queue at music.daydream.live). Connect()
    # branches on this. OnParChange("Mode") greys out the unused-mode
    # params for visual clarity.
    Param("Mode", None, "Session", "Menu", "session", default="direct",
          order=60, label="Mode",
          menu_names=("direct", "hosted"),
          menu_labels=("Direct (your pod)", "Hosted (Daydream queue)"),
          help="Direct: connect to your own DEMON pod via Server URL. "
               "Hosted: join the Daydream queue and play on a managed pod."),
    Param("Baseurl", None, "Session", "Str", "session",
          default="https://music.daydream.live", order=62, label="Hosted Base URL",
          help="Daydream queue API root. Override only if you're testing "
               "against a staging deployment."),
    Param("Apikey", None, "Session", "Str", "session", default="",
          order=64, label="API Key", secret=True,
          help="Daydream API key. Sent as Authorization: Bearer <key>. "
               "Set via the Paste API Key or Sign in via browser pulses; "
               "stored in <prefs>/daydream_auth.json, not in the .toe."),
    Param("Signedinas", None, "Session", "Str", "session", default="",
          order=66, label="Signed in as", readonly=True,
          help="Daydream account email/username, populated after sign-in."),
    Param("Pasteapikey", None, "Session", "Pulse", "session", order=68,
          label="Paste API Key",
          help="Opens app.daydream.live in your browser and prompts for "
               "your API key. Validates against /users/profile before saving."),
    Param("Signinbrowser", None, "Session", "Pulse", "session", order=70,
          label="Sign in via browser",
          help="Open the Daydream sign-in flow in your browser. On success, "
               "the browser tab redirects to a local listener and your "
               "session is signed in automatically."),
    Param("Signout", None, "Session", "Pulse", "session", order=72,
          label="Sign out",
          help="Wipe the stored API key + profile. Device ID is preserved."),
    Param("Queueposition", None, "Session", "Int", "session", default=0,
          order=74, label="Queue Position", readonly=True,
          help="1-based queue position while waiting. 0 once active."),
    Param("Expiresin", None, "Session", "Float", "session", default=0.0,
          order=76, label="Expires in (s)", readonly=True,
          help="Seconds until the hosted session expires. Hit Still Playing "
               "to extend."),
    Param("Denyreason", None, "Session", "Str", "session", default="",
          order=78, label="Deny reason", readonly=True,
          help="Populated when the server returns over_budget / paywall."),
    Param("Stillplaying", None, "Session", "Pulse", "session", order=80,
          label="Still playing?",
          help="POST /api/queue/extend — bumps the session by one duration."),

    Param("Debug", None, "Session", "Toggle", "session", default=False,
          order=999, label="Debug Logging",
          help="When on, the extension prints verbose textport diagnostics "
               "(per-tick state, sample-decode hex dumps, slice/initial "
               "buffer WAV files to /tmp/demon-debug/, ws frame echoes). "
               "Off by default — keep off unless investigating a bug."),
]


# -----------------------------------------------------------------------------
# Page 2: Init (session-start params; immutable while connected)
# -----------------------------------------------------------------------------
INIT_PARAMS: list[Param] = [
    Param("Sde", "sde", "Init", "Toggle", "init", default=False, order=10,
          help="Use Score Distillation Energy mode instead of ODE."),
    Param("Lora", "lora", "Init", "Toggle", "init", default=True, order=20,
          help="Enable LoRA adapter support."),
    Param("Depth", "depth", "Init", "Int", "init", default=4,
          min=1, max=8, clamp_min=True, clamp_max=True, order=30,
          help="DiT pipeline depth (latency/quality tradeoff)."),
    Param("Vaewindow", "vae_window", "Init", "Float", "init", default=6.0,
          min=0.5, max=10.0, clamp_min=True, clamp_max=True, order=40,
          label="VAE Window", help="VAE decoder sliding window in seconds. "
                                   "Matches demon-public-demo default."),
    Param("Crop", "crop", "Init", "Float", "init", default=0.0,
          min=0.0, max=120.0, clamp_min=True, order=50,
          help="Crop input audio to N seconds (0 = no crop)."),
    Param("Steps", "steps", "Init", "Int", "init", default=8,
          min=1, max=32, clamp_min=True, clamp_max=True, order=60,
          help="Generation steps per latent frame."),
    Param("Fastvae", "fast_vae", "Init", "Toggle", "init", default=False, order=70,
          label="Fast VAE", help="Use dreamvae distilled decoder (TensorRT only). "
                                  "Off matches demon-public-demo default."),
    Param("Walkwindow", "walk_window", "Init", "Toggle", "init", default=False, order=80,
          label="Walk Window", help="For long sources, use 60s engine at boundaries."),
    Param("Walkwindows", "walk_window_s", "Init", "Float", "init", default=60.0,
          min=1.0, max=240.0, clamp_min=True, order=90,
          label="Walk Window (s)", help="Walk window duration in seconds."),
    Param("Initprompt", "prompt", "Init", "Str", "init",
          default="heavy dubstep, deathstep, afxdump, growl heavy bass distortion",
          order=100, multiline=True, label="Initial Prompt",
          help="Text prompt at session start (changeable later via Send Prompt). "
               "Default matches demon-public-demo."),
    Param("Initpromptb", "prompt_b", "Init", "Str", "init", default="",
          order=105, multiline=True, label="Initial Prompt B",
          help="Secondary prompt for A/B blending. The Prompt+LoRA page's "
               "Prompt Blend slider interpolates between Initial Prompt (A) "
               "and Initial Prompt B (B). Empty = no B, always A."),
    Param("Fixturename", "fixture_name", "Init", "Str", "init", default="",
          order=110, label="Fixture Name",
          help="Known fixture name for sidecar lookup (BPM/key/latents). Optional."),
]


# -----------------------------------------------------------------------------
# Page 3: Prompt + LoRA
# -----------------------------------------------------------------------------

# Generate the 70-keyscale menu: {A..G} × {natural, #, b} × {major, minor}
_NOTES = ["A", "B", "C", "D", "E", "F", "G"]
_ACCIDENTALS = [("", ""), ("#", "♯"), ("b", "♭")]
_QUALITIES = ["major", "minor"]


def _build_keyscale_menu() -> tuple[tuple[str, ...], tuple[str, ...]]:
    names: list[str] = ["auto"]
    labels: list[str] = ["Auto (detect)"]
    for note in _NOTES:
        for acc_wire, acc_label in _ACCIDENTALS:
            for qual in _QUALITIES:
                names.append(f"{note}{acc_wire} {qual}")
                labels.append(f"{note}{acc_label} {qual}")
    return tuple(names), tuple(labels)


_KEYSCALE_NAMES, _KEYSCALE_LABELS = _build_keyscale_menu()


PROMPT_LORA_PARAMS: list[Param] = [
    Param("Sendprompt", None, "Prompt+LoRA", "Pulse", "discrete", order=10,
          label="Send Prompt",
          help="Send the current Prompt / Key / Time Signature to the server."),
    Param("Prompt", None, "Prompt+LoRA", "Str", "session", default="",
          order=20, multiline=True,
          help="Tags or freeform text to apply on Send Prompt."),
    Param("Key", None, "Prompt+LoRA", "Menu", "session", default="auto", order=30,
          menu_names=_KEYSCALE_NAMES, menu_labels=_KEYSCALE_LABELS,
          help="Musical key. 'Auto' lets the server detect."),
    Param("Timesignature", None, "Prompt+LoRA", "Menu", "session",
          default="auto", order=40, label="Time Signature",
          menu_names=("auto", "2", "3", "4", "6"),
          menu_labels=("Auto", "2", "3", "4", "6"),
          help="Time signature numerator."),
    Param("Setpromptblend", None, "Prompt+LoRA", "Pulse", "discrete", order=50,
          label="Apply Prompt Blend",
          help="Send the current Prompt Blend value to the server."),
    Param("Promptblend", "prompt_blend", "Prompt+LoRA", "Float", "continuous",
          default=0.4, min=0.0, max=1.0, clamp_min=True, clamp_max=True,
          order=60, label="Prompt Blend",
          help="Prompt A vs B blend (0=A, 1=B). Streamed continuously."),
    Param("Loraheader", None, "Prompt+LoRA", "Header", "session",
          order=70, section_header=True, label="LoRAs"),
    Param("Lorablend", "lora_blend", "Prompt+LoRA", "Float", "continuous",
          default=0.5, min=0.0, max=1.0, clamp_min=True, clamp_max=True,
          order=80, label="LoRA Blend",
          help="UI-level A/B LoRA blend. Edge LoRA binding fans this to "
               "paired lora_str_<id> values."),
    # Dynamic per-LoRA rows are appended at runtime by DemonExt
    # once the server's lora_catalog is received.
]


# -----------------------------------------------------------------------------
# Page 4: Synthesis (the hot continuous params)
# -----------------------------------------------------------------------------
SYNTHESIS_PARAMS: list[Param] = [
    Param("Denoise", "denoise", "Synthesis", "Float", "continuous", default=0.85,
          min=0.0, max=1.0, clamp_min=True, clamp_max=True, order=10,
          help="Denoising strength."),
    Param("Seed", "seed", "Synthesis", "Float", "continuous", default=0.0,
          min=0.0, max=1.0, clamp_min=True, clamp_max=True, order=20,
          help="Random seed (normalized 0..1)."),
    Param("Feedback", "feedback", "Synthesis", "Float", "continuous", default=0.0,
          min=0.0, max=1.0, clamp_min=True, clamp_max=True, order=30,
          help="Feedback loop (pro). Use with caution."),
    Param("Shift", "shift", "Synthesis", "Float", "continuous", default=0.5,
          min=0.0, max=1.0, clamp_min=True, clamp_max=True, order=40,
          help="Temporal phase alignment (pro)."),
    Param("Hintstrength", "hint_strength", "Synthesis", "Float", "continuous",
          default=1.4, min=0.0, max=2.0, clamp_min=True, clamp_max=True,
          order=50, label="Hint Strength",
          help="Reference latent hint strength."),
    Param("Timbrestrength", "timbre_strength", "Synthesis", "Float", "continuous",
          default=1.0, min=0.0, max=1.0, clamp_min=True, clamp_max=True,
          order=60, label="Timbre Strength",
          help="Source vs generation timbre blend (rides own WS message)."),
    Param("Guidancescale", "guidance_scale", "Synthesis", "Float", "continuous",
          default=7.0, min=0.0, max=15.0, clamp_min=True, clamp_max=True,
          order=70, label="Guidance Scale",
          help="RCFG guidance (pro)."),
    Param("Cfgrescale", "cfg_rescale", "Synthesis", "Float", "continuous",
          default=0.0, min=0.0, max=1.0, clamp_min=True, clamp_max=True,
          order=80, label="CFG Rescale",
          help="CFG saturation taming (pro)."),
    Param("Odenoise", "ode_noise", "Synthesis", "Float", "continuous",
          default=0.0, min=0.0, max=0.5, clamp_min=True, clamp_max=True,
          order=90, label="ODE Noise",
          help="ODE noise injection (pro)."),
    Param("Periodicity", "periodicity", "Synthesis", "Float", "continuous",
          default=0.0, min=0.0, max=12.5, clamp_min=True, clamp_max=True,
          order=100, help="Beat-grid periodicity for SDE (pro)."),

    Param("Channelsheader", None, "Synthesis", "Header", "session",
          order=110, section_header=True, label="Channels"),
] + [
    Param(f"Chg{i}", f"ch_g{i}", "Synthesis", "Float", "continuous", default=1.0,
          min=0.0, max=3.0, clamp_min=True, clamp_max=True,
          order=120 + i, label=f"ch_g{i}",
          help=f"Channel guidance group {i}.")
    for i in range(8)
] + [
    Param("Keystoneheader", None, "Synthesis", "Header", "session",
          order=140, section_header=True, label="Keystone Channels"),
] + [
    Param(f"Ch{n}", f"ch{n}", "Synthesis", "Float", "continuous", default=1.0,
          min=0.0, max=3.0, clamp_min=True, clamp_max=True,
          order=150 + i, label=f"ch{n}",
          help=f"Keystone channel {n} guidance.")
    for i, n in enumerate([13, 14, 19, 23, 29, 56])
]


# -----------------------------------------------------------------------------
# Page 5: RCFG + DCW
# -----------------------------------------------------------------------------
RCFG_DCW_PARAMS: list[Param] = [
    Param("Rcfgmode", "rcfg_mode", "RCFG+DCW", "Menu", "continuous",
          default="off", order=10, label="RCFG Mode",
          menu_names=("off", "initialize", "self"),
          menu_labels=("Off", "Initialize", "Self"),
          help="RCFG mode selection."),
    Param("Dcwheader", None, "RCFG+DCW", "Header", "session",
          order=20, section_header=True, label="DCW (Wavelet Domain Correction)"),
    Param("Dcwenabled", "dcw_enabled", "RCFG+DCW", "Toggle", "continuous",
          default=True, order=30, label="DCW Enabled",
          help="Enable wavelet domain correction."),
    Param("Dcwmode", "dcw_mode", "RCFG+DCW", "Menu", "continuous",
          default="double", order=40, label="DCW Mode",
          menu_names=("low", "high", "double", "pix"),
          menu_labels=("Low", "High", "Double", "Pix"),
          help="Correction bands (pro)."),
    Param("Dcwscaler", "dcw_scaler", "RCFG+DCW", "Float", "continuous",
          default=0.05, min=0.0, max=0.5, clamp_min=True, clamp_max=True,
          order=50, label="DCW Scaler"),
    Param("Dcwhighscaler", "dcw_high_scaler", "RCFG+DCW", "Float", "continuous",
          default=0.02, min=0.0, max=0.5, clamp_min=True, clamp_max=True,
          order=60, label="DCW High Scaler"),
    Param("Dcwwavelet", "dcw_wavelet", "RCFG+DCW", "Menu", "continuous",
          default="haar", order=70, label="DCW Wavelet",
          menu_names=("haar", "db4", "sym8", "db8"),
          menu_labels=("Haar", "DB4", "Sym8", "DB8"),
          help="Wavelet family (pro)."),
    Param("Dcwmultblend", "dcw_mult_blend", "RCFG+DCW", "Float", "continuous",
          default=0.0, min=0.0, max=1.0, clamp_min=True, clamp_max=True,
          order=80, label="DCW Mult Blend"),
    Param("Dcwmagphase", "dcw_mag_phase", "RCFG+DCW", "Float", "continuous",
          default=0.0, min=0.0, max=1.0, clamp_min=True, clamp_max=True,
          order=90, label="DCW Mag/Phase"),
    Param("Dcwsoftthresh", "dcw_soft_thresh", "RCFG+DCW", "Float", "continuous",
          default=0.0, min=0.0, max=0.3, clamp_min=True, clamp_max=True,
          order=100, label="DCW Soft Thresh"),
]


# -----------------------------------------------------------------------------
# Page 6: Curves (advanced; accept JSON spec strings)
# -----------------------------------------------------------------------------
CURVE_PARAMS: list[Param] = [
    Param("Sdedenoisecurve", "sde_denoise_curve", "Curves", "Str", "continuous",
          default='{"type":"constant","value":0.7}', order=10,
          label="SDE Denoise Curve", multiline=True,
          help='JSON curve spec, e.g. {"type":"constant","value":0.7} or '
               '{"type":"raw","values":[...]}'),
    Param("Odenoisecurve", "ode_noise_curve", "Curves", "Str", "continuous",
          default='{"type":"constant","value":0.0}', order=20,
          label="ODE Noise Curve", multiline=True),
    Param("X0targetcurve", "x0_target_curve", "Curves", "Str", "continuous",
          default='', order=30, label="x0 Target Curve", multiline=True,
          help="Empty disables. JSON curve spec for x0 target guidance."),
    Param("Velocityscalecurve", "velocity_scale_curve", "Curves", "Str", "continuous",
          default='', order=40, label="Velocity Scale Curve", multiline=True),
    Param("Initialnoisecurve", "initial_noise_curve", "Curves", "Str", "continuous",
          default='', order=50, label="Initial Noise Curve", multiline=True),
]


# -----------------------------------------------------------------------------
# Page 7: Sources
# -----------------------------------------------------------------------------
SOURCES_PARAMS: list[Param] = [
    # ---------- Swap (replace the source track mid-session) ----------
    Param("Swapheader", None, "Sources", "Header", "session",
          order=5, section_header=True, label="Swap Source Track"),
    Param("Swapsourcefile", None, "Sources", "File", "session", default="",
          order=10, label="Swap Source File",
          help="WAV/MP3/M4A. If set, Swap Source uploads this file. "
               "Otherwise it uses the wired CHOP input."),
    Param("Swapsource", None, "Sources", "Pulse", "discrete", order=15,
          label="Swap Source",
          help="Swap to a new source track. Uses Swap Source File if set, "
               "else the wired CHOP input. Honors Swap Tags."),
    Param("Swaptags", None, "Sources", "Str", "session", default="", order=20,
          label="Swap Tags", multiline=True,
          help="Optional prompt tags override for Swap Source."),

    # ---------- Timbre reference ----------
    Param("Timbreheader", None, "Sources", "Header", "session",
          order=25, section_header=True, label="Timbre Reference"),
    Param("Timbresourcefile", None, "Sources", "File", "session", default="",
          order=30, label="Timbre Source File",
          help="WAV/MP3/M4A used as a timbre reference. If empty, the wired "
               "CHOP input is snapshotted instead."),
    Param("Settimbresource", None, "Sources", "Pulse", "discrete", order=35,
          label="Set Timbre Source",
          help="Upload the Timbre Source File (or wired CHOP) as a timbre reference."),
    Param("Cleartimbresource", None, "Sources", "Pulse", "discrete", order=40,
          label="Clear Timbre Source"),
    Param("Timbrefixture", None, "Sources", "Str", "session", default="", order=50,
          label="Timbre Fixture",
          help="Name of a server-side fixture to use as timbre reference."),
    Param("Settimbrefixture", None, "Sources", "Pulse", "discrete", order=55,
          label="Apply Timbre Fixture"),

    # ---------- Structure reference ----------
    Param("Structureheader", None, "Sources", "Header", "session",
          order=58, section_header=True, label="Structure Reference"),
    Param("Structuresourcefile", None, "Sources", "File", "session", default="",
          order=60, label="Structure Source File",
          help="WAV/MP3/M4A used as a structure reference. If empty, the wired "
               "CHOP input is snapshotted instead."),
    Param("Setstructuresource", None, "Sources", "Pulse", "discrete", order=65,
          label="Set Structure Source",
          help="Upload the Structure Source File (or wired CHOP) as a structure reference."),
    Param("Clearstructuresource", None, "Sources", "Pulse", "discrete", order=70,
          label="Clear Structure Source"),
    Param("Structurefixture", None, "Sources", "Str", "session", default="",
          order=80, label="Structure Fixture",
          help="Name of a server-side fixture to use as structure reference."),
    Param("Setstructurefixture", None, "Sources", "Pulse", "discrete", order=85,
          label="Apply Structure Fixture"),
]


# -----------------------------------------------------------------------------
# Aggregate
# -----------------------------------------------------------------------------
PARAMS: list[Param] = (
    SESSION_PARAMS
    + INIT_PARAMS
    + PROMPT_LORA_PARAMS
    + SYNTHESIS_PARAMS
    + RCFG_DCW_PARAMS
    + CURVE_PARAMS
    + SOURCES_PARAMS
)


# Pages, in display order
PAGES: list[str] = [
    "Session", "Init", "Prompt+LoRA", "Synthesis", "RCFG+DCW", "Curves", "Sources"
]


# -----------------------------------------------------------------------------
# Lookups
# -----------------------------------------------------------------------------

PARAM_BY_NAME: dict[str, Param] = {p.name: p for p in PARAMS}
PARAM_BY_WIRE: dict[str, Param] = {p.wire_name: p for p in PARAMS if p.wire_name}
INIT_PARAM_NAMES: frozenset[str] = frozenset(
    p.name for p in PARAMS if p.category == "init"
)
CONTINUOUS_PARAM_NAMES: frozenset[str] = frozenset(
    p.name for p in PARAMS if p.category == "continuous"
)


def session_config_defaults() -> dict[str, Any]:
    """Build the initial SessionConfig dict from default Init param values.

    The extension overrides these with actual param values at Connect() time.
    """
    cfg: dict[str, Any] = {}
    for p in PARAMS:
        if p.category == "init" and p.wire_name:
            cfg[p.wire_name] = p.default
    return cfg


def continuous_defaults() -> dict[str, Any]:
    """All continuous-param default values keyed by wire name."""
    return {p.wire_name: p.default for p in PARAMS if p.category == "continuous" and p.wire_name}


# -----------------------------------------------------------------------------
# Discrete message routing
# -----------------------------------------------------------------------------
# Maps pulse-par name → wire message kind, for OnParChange to dispatch on.

DISCRETE_PULSE_TO_KIND: dict[str, str] = {
    "Sendprompt": "prompt",
    "Setpromptblend": "set_prompt_blend",
    "Swapsource": "swap_source",
    "Settimbresource": "set_timbre_source",
    "Cleartimbresource": "clear_timbre_source",
    "Settimbrefixture": "set_timbre_fixture",
    "Setstructuresource": "set_structure_source",
    "Clearstructuresource": "clear_structure_source",
    "Setstructurefixture": "set_structure_fixture",
}
