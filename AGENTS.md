# AGENTS.md — engineering handoff

This is the first file to read if you are an agent or maintainer picking up this
project. It records the architecture, the non-obvious decisions, the gotchas that
already cost time, and the rules for publishing.

## 1. What this project is

A local, cross-platform desktop tool that burns a speedometer HUD onto DJI Osmo
Action 4/5/6 footage using the camera's own `djmd` GPS telemetry. The GUI is
Tkinter; the pipeline is `speedpipe.py`; the overlays are rendered with Pillow in
`hud.py`.

Public repository: <https://github.com/WalkTheEarth/dji-speed-hud>

The app makes no network requests. It reads a local video, writes a local MP4,
and keeps intermediates in a sibling work directory.

The original development clip is a 28-minute HEVC dashcam video. It is **not** in
the repository. Local-only project notes (`RESUME.md`) are intentionally ignored
by git because they contain personal context. The source clip lives on an external
SD card that was removed; the full burn of that clip was never produced. The GUI
and pipeline are complete and tested on other real DJI clips and synthetic data.

## 2. Repository layout

| path | purpose |
|---|---|
| `gui.py` | Tkinter app: preview/editor, themes, controls, progress/log, settings |
| `speedpipe.py` | probe → packet index → telemetry parse → speed → frames → burn |
| `hud.py` | five 1920×1080 RGBA HUD renderers + free transform + fonts |
| `assets/fonts/` | bundled Hack and Lato TTFs + their licenses |
| `docs/gui.png` | GUI screenshot (synthetic demo background) |
| `docs/styles.png` | five HUD styles contact sheet (synthetic background) |
| `run_gui.sh` | Linux/macOS launcher |
| `run_gui.command` | macOS double-click launcher |
| `run_gui.bat` | Windows launcher (CRLF) |
| `requirements.txt` | Pillow, numpy, pyosmogps |
| `README.md` | user-facing documentation |
| `LICENSE` | MIT for the code |

Local, gitignored development assets (do **not** publish): `telemetry.csv`,
`speed_1hz.csv`, `djmd_pkts.csv`, `DJI_*_drive.gpx`, `frames/`, `checks/`,
`speed_overlay_alpha.mov`, `venv/`, `RESUME.md`, legacy one-off scripts.

## 3. Install and run

Requires Python 3.10+, Tkinter, and FFmpeg/ffprobe on `PATH`.

```bash
# Linux
sudo apt install python3 python3-venv python3-tk ffmpeg
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./run_gui.sh
```

Windows: install Python from python.org (includes Tk), `winget install
Gyan.FFmpeg`, then `py -m venv venv`, `venv\Scripts\pip install -r
requirements.txt`, `run_gui.bat`.

macOS: `brew install ffmpeg python-tk`, or use python.org Python; create the venv,
install requirements, run `./run_gui.command`.

`pyosmogps` is a real PyPI package and is cross-platform. It supplies the DJI
protobuf definitions (`pyosmogps.dji_pb2.GenericMessage`). Do not vendor it
unless the license is checked.

## 4. Pipeline (`speedpipe.py`)

Public entry point:

```python
run_pipeline(
    input_path, output_path, work_dir=None,
    unit="mph",                  # "mph" | "kmh"
    hud_style="classic",         # hud.styles() ids
    hud_scale=1.0,               # 0.5 .. 1.5
    hud_opacity=1.0,             # 0.3 .. 1.0
    hud_position="bottom-right", # legacy anchor; ignored if hud_center is set
    hud_center=None,             # (cx, cy) normalized 0..1, free placement
    keep_intermediates=True,
    progress=None, cancelled=None, log=None,
) -> PipelineResult
```

Stages:

1. **Probe** (`probe_video`): `ffprobe -show_format -show_streams -print_format
   json`. Picks the first video stream, applies rotation side data to display
   width/height, finds the audio stream, and selects the DJI telemetry data
   stream. The timecode track (`codec_tag_string=tmcd`) is excluded. Prefer tags
   containing `djmd`, `dvtm`, or `mebx`; otherwise take the remaining data stream
   with the largest `nb_frames`/`nb_read_packets`. Do **not** use
   `-count_packets` for probing — it is an extra full pass and is slow on SD
   cards.
2. **Packet index** (`extract_packet_index`): `ffprobe -select_streams N
   -show_entries packet=pts_time,dts_time,size,pos -of csv=p=0`. Writes
   `djmd_pkts.csv`. This is the original working approach.
3. **Parse** (`parse_telemetry`): each packet is parsed as a standalone
   `GenericMessage`. Packets are read sequentially by `pos` (seek only when the
   offset jumps), because random seeks on an SD card are slow. Timestamps are
   normalized by the first packet time (`t0`) so telemetry aligns with video
   starting at 0.
4. **Speed** (`compute_speed`): use `hypot(derivatives.x, derivatives.y)` from
   `remote_gps_info` — the camera's Doppler ground velocity in m/s. Do **not**
   difference lat/lon; it produces ±5–9 m/s noise and spikes when fixes drop.
   Validity mask: `abs(lat) > 0.001 and abs(lon) > 0.001`. Bin by `round(t)`,
   take the middle sample per second, fill only isolated one-second dropouts from
   neighbours. Compute running min/max. `nsec = max(round(duration),
   round(max(t))) + 1`. Write `speed_1hz.csv`.
5. **Frames** (`render_frames`): one 1920×1080 RGBA PNG per second in
   `frames/f_%05d.png`, using the selected style, unit, scale, opacity, and free
   center (or anchor). Supports progress/cancel.
6. **Burn** (`burn`): overlay the 1 fps PNG sequence directly on the source with
   `[1:v]scale=VIDEO_WIDTH:-2,fps=24[o];[0:v][o]overlay=W-w:H-h:shortest=1,
   format=yuv420p[v]`. H.264 `-preset veryfast -crf 20 -profile:v high`,
   `-movflags +faststart`, audio copied when the codec is MP4-friendly
   (`aac/mp3/ac3/eac3/alac`), otherwise AAC 192k. Output is written as
   `<name>.part.mp4` and renamed on success. Cancellation and failures delete the
   part file; the ffmpeg log is kept on failure and deleted on success.

Gotchas already handled — do not undo:

- `djmd` packets are not self-delimiting across packet boundaries. Parse **per
  packet**; concatenating them and parsing once fails with `Wire format was
  corrupt` (this is what `pyosmogps.OsmoGps` does, and it assumes the track is
  `trak` #3).
- `ffprobe -select_streams 0:2` is invalid; use the numeric index.
- `-shortest` with stream-copied audio trimmed unpredictably (we saw 33 s become
  23 s, not the video's 11 s). It was removed. The overlay covers the full clip in
  normal use, so audio and video durations match.
- The alpha `.mov` from the original one-off workflow is not needed by the GUI.
  The GUI burns the PNG sequence directly. The old `speed_overlay_alpha.mov` and
  `frames/` are classic-style only.
- `-movflags +faststart` makes a second pass but is worth it for playback.
- Windows subprocesses use `CREATE_NO_WINDOW` so ffmpeg doesn't pop consoles.
- FFmpeg discovery checks `FFMPEG`/`FFPROBE` env overrides, `PATH`, and
  `/opt/homebrew/bin` + `/usr/local/bin` on macOS.

## 5. Speed semantics / display

- The HUD shows **current**, **running min** and **running max** speed.
- On a GPS dropout, only the current value blanks (`--.-`) and `no gps` appears;
  min/max stay visible.
- Isolated one-second dropouts are interpolated from neighbours; multi-second
  gaps remain blank. This is intentional and matches the original frames.
- Important historical correction: earlier notes claimed a specific 0.7/84.9
  MIN/MAX expectation at the GPS gap around t=1074. That was wrong. Use the
  current renderer and the local-only RESUME notes for the expected values. The
  reference image `checks/chk_1074.jpg` was from an older HUD that blanked
  min/max entirely; do not use it as the target.
- The local regression assets (`telemetry.csv`, `frames/`) are not in the repo.
  On the original machine, the classic renderer at t=1500 must be pixel-identical
  to `frames/f_01500.png`, and the classic renderer at defaults must also be
  identical when `center=hud.DEFAULT_CENTER`.

## 6. HUD rendering (`hud.py`)

Entry point:

```python
hud.render(now_mps, min_mps, max_mps, sec, nofix=False, big_only=False,
           unit="mph", style="classic", scale=1.0, opacity=1.0,
           position="bottom-right", center=None) -> PIL.Image (1920x1080 RGBA)
```

Styles (`hud.styles()`): `classic`, `gauge`, `strip` (label "Bar"), `minimal`,
`racing`.

- **classic**: original 460×304 bottom-right panel. Must stay pixel-identical at
  its native placement. This is the pre-rendered frames/alpha overlay.
- **gauge**: circular dial, 0–120 mph / 0–200 km/h, needle, digital readout,
  min/max at top.
- **strip**: wide bottom-right bar with linear scale and marker.
- **minimal**: large floating number + compact soft scrim (the scrim was
  deliberately compacted so its visible bbox centers predictably under free
  placement).
- **racing**: cut-corner yellow digital panel, segmented bar, checkered strip.

Fonts: bundled `assets/fonts/Hack-Bold.ttf`, `Hack-Regular.ttf`,
`Lato-Bold.ttf`, `Lato-Regular.ttf` are resolved first; system fallbacks cover
Linux/Windows/macOS if the assets are missing. Hack is MIT/Expat + Bitstream Vera;
Lato is OFL-1.1. License texts are in `assets/fonts/`. Fonts are `lru_cache`d.

Transform behavior:

- Each style renders natively into a 1920×1080 transparent canvas. `_apply_transform`
  then optionally crops to the non-zero bbox, scales, applies global alpha, and
  places it.
- **Anchors** (`position`) are for CLI/backward compatibility: nine values with a
  56 px margin. The GUI no longer uses them.
- **Free center** (`center=(cx, cy)`, normalized 0..1) centers the cropped
  content at that point, clamped to the frame. This is what the GUI drag editor
  uses.
- `hud.DEFAULT_CENTER = (1634/1920, 872/1080)` is the center of the native
  classic panel. When `scale=1`, `opacity=1`, `position="bottom-right"` and
  either no center or exactly `DEFAULT_CENTER`, the untouched native image is
  returned, preserving pixel identity.
- Free placement uses a full-frame fit; anchored modes keep the 56 px margin.
- Opacity is applied by multiplying the alpha channel. Do not premultiply colors.
- All five styles handle `nofix` and both primary units.

If you add a style: add it to `STYLES` (id + label) and the dispatcher, ensure it
returns 1920×1080 RGBA, support `nofix`, and test all scales/centers.

## 7. GUI (`gui.py`)

Main layout:

- Header: logo, title, four window-theme buttons (Carbon, Midnight, Paper,
  Rally). This is only the app skin; it is *not* the burned HUD.
- Left card: input/output paths + Browse/Save as, metadata chips, unit toggle,
  keep-intermediates toggle, tip, burn settings chips.
- Preview card: synthetic or real still with the selected HUD composited, style
  buttons (Classic/Gauge/Bar/Minimal/Racing), live drag editor.
- HUD controls card: size slider (50–150%), opacity slider (30–100%), Position
  hint + Reset HUD.
- Action bar, progress card, scrollable log, footer with status, encoding note,
  UI scale `- UI n% +`.
- Keyboard: `Ctrl+O` open, `Ctrl+Enter` start, `Ctrl+=` / `Ctrl+-` UI scale.

### Free HUD editor (new, intentional)

The old 9-way anchor picker was removed at the user's request because fixed
anchors were not what they wanted. Do not reintroduce it. The editor works on the
preview `tk.Label`:

- `_compose_preview` renders the HUD overlay with `center=(hud_cx, hud_cy)`,
  pastes it onto the preview frame, then stores:
  - `_hud_overlay_scale = inner.width / 1920`
  - `_hud_overlay_origin = (ox, oy)` where the overlay was pasted
  - `_hud_bbox_overlay = hud_img.getbbox()`
  - `_hud_bbox_preview` = the bbox mapped into preview-label coordinates
- `_draw_editor` draws a dashed accent box + bottom-right handle. It is drawn only
  for a real input preview; the placeholder and output previews show no editor.
- Drag inside the box to move. Drag the handle to resize. The size slider and
  the handle stay in sync. `Reset HUD` restores scale=1, opacity=1,
  center=DEFAULT_CENTER.
- Moving converts preview pixels to normalized center deltas by dividing by
  `_hud_overlay_scale * 1920/1080` and clamps using the content half-size so the
  box stays inside the frame.
- Resizing uses the ratio of the pointer distance from the visible HUD center to
  the distance at press, times the starting scale, clamped 0.5–1.5.
- Rendering during drag is coalesced through `_editor_after` / `_editor_dirty`
  (~16 ms). Release does a final render and saves settings.
- `preview_token` increments on input changes and HUD control changes to
  invalidate stale output-preview frames.

### Settings

Default Linux path: `~/.config/speedometer/settings.json`. macOS:
`~/Library/Application Support/SpeedHUD/settings.json`. Windows:
`%APPDATA%\SpeedHUD\settings.json`. Override with `SPEEDOMETER_SETTINGS` (used by
tests).

Schema:

```json
{
  "theme": "carbon",
  "unit": "mph",
  "hud_style": "classic",
  "hud_scale": 1.0,
  "hud_opacity": 1.0,
  "hud_cx": 0.8510416667,
  "hud_cy": 0.8074074074,
  "keep": true,
  "ui_scale": 1.8
}
```

Migration: if `hud_cx`/`hud_cy` are absent but the old `hud_position` exists, map
it through `OLD_POSITION_CENTERS` (top-left 0.25/0.25, bottom-left 0.20/0.80,
etc.). `hud_position` is no longer written. The 9-way picker is gone.

### UI scaling

The original app was unreadable on a 3408×2130 display where Tk reported 96 DPI.
The fix has two parts:

1. `root.tk.call("tk", "scaling", (96/72)*ui_scale)` — fonts are specified in
   **points**, so they scale automatically.
2. Every pixel dimension in the layout goes through `px(v) = round(v*ui_scale)`:
   window size, card sizes/paddings, button sizes/radii, chip metrics, preview
   dimensions, slider/editor geometry, PIL font sizes in the previews, image
   margins, etc. If you add pixels, use `px()`.

Auto scale: saved `ui_scale` wins; otherwise macOS 1.0, Windows from
`winfo_fpixels('1i')/96`, Linux from `xrdb -query` `Xft.dpi: 192` (→2.0) with a
screen-size fallback. It is always clamped to fit the screen (`_screen_fit`) and
to 0.55–2.0, rounded to 0.05. On the development display (3408×2130, Xft 192) it
chooses 1.8 and the window is 2124×1944. The footer `- UI n% +` and
`Ctrl+=`/`Ctrl+-` change it; changes save and rebuild via `after_idle`.

If you rebuild the UI from a widget callback (theme, UI scale), use
`after_idle`; destroying the widget that is currently handling the event can
throw Tcl errors.

### Threading / state

Workers (probe, pipeline, output-preview grab) post messages to
`self.queue`; `_poll` drains it on the Tk thread every 80 ms. Never touch Tk
widgets from a worker. Probe results are guarded by `probe_token`; stale results
are ignored. `_preview_press` refuses while `self.running`.

## 8. Cross-platform-specific bits

- Launchers are separate: `run_gui.sh`, `run_gui.command`, `run_gui.bat` (CRLF).
- Settings paths use `APPDATA` / `Library/Application Support` / `.config`.
- `_open_path` uses `os.startfile` (Windows), `open` (macOS), `xdg-open` (Linux).
- `_pick_fonts` chooses native UI/mono families (Cantarell/Segoe UI/Helvetica
  Neue; Hack/Cascadia/Consolas/Menlo).
- `main()` sets Windows DPI awareness with `SetProcessDpiAwareness(1)` before
  creating Tk.
- FFmpeg lookup and `CREATE_NO_WINDOW` are in `speedpipe.py`.
- HUD fonts are bundled so burned output is identical everywhere.
- Linux is the only physically tested platform. Windows/macOS code paths are
  implemented but have not been run on those OSes in this development session.

## 9. Privacy and publishing rules

**Never commit** any of: source video files, `telemetry.csv`, `speed_1hz.csv`,
`djmd_pkts.csv`, GPX route files, `frames/`, `checks/`, `speed_overlay_alpha.mov`,
`venv/`, `RESUME.md`, logs, or local settings. The repo is public.

The public repo intentionally contains only code, launchers, README, demo images
built from a synthetic road background, bundled fonts/licenses, requirements, and
the MIT license. The original drive's telemetry and route must stay out.

- Git identity used for the published commits: `WalkTheEarth
  <152687463+WalkTheEarth@users.noreply.github.com>`.
- Remote: `origin` → `https://github.com/WalkTheEarth/dji-speed-hud.git`,
  branch `main`.
- Commit trailer used here: `Co-Authored-By: Claude Code <noreply@anthropic.com>`.
- If you add demo imagery, use synthetic/generated frames, not the user's
  footage.

## 10. Tests / verification

```bash
cd speedometer
./venv/bin/python -m py_compile gui.py speedpipe.py hud.py
./venv/bin/python -c "import hud; print(hud.styles())"
./venv/bin/python -c "import speedpipe; speedpipe.check_tools(); print('ffmpeg ok')"
```

Style/transform matrix (135 combinations):

```bash
./venv/bin/python - <<'PY'
import hud
for sid, _ in hud.styles():
    for scale in (0.5, 1.0, 1.5):
        for pos in hud.POSITIONS:
            img = hud.render(18, 3, 34, 0, style=sid, scale=scale,
                             position=pos, opacity=0.8)
            assert img.size == (1920, 1080) and img.mode == "RGBA"
print("ok")
PY
```

Local regression (only on the original machine, assets are gitignored):

```bash
./venv/bin/python - <<'PY'
import numpy as np
from PIL import Image
import hud, speedpipe
filled, rmin, rmax, _ = speedpipe.compute_speed(
    "telemetry.csv", "/tmp/speed.csv", 1693.98)
native = hud.render(float(filled[1500]), float(rmin[1500]),
                    float(rmax[1500]), 1500)
old = Image.open("frames/f_01500.png").convert("RGBA")
assert np.array_equal(np.asarray(native), np.asarray(old))
centered = hud.render(float(filled[1500]), float(rmin[1500]),
                      float(rmax[1500]), 1500, center=hud.DEFAULT_CENTER)
assert np.array_equal(np.asarray(centered), np.asarray(old))
print("classic pixel identity ok")
PY
```

GUI smoke test under Xvfb (uses a temp settings file so you don't touch the
user's settings):

```bash
printf '%s\n' '{"ui_scale":1.0,"theme":"carbon","unit":"mph","hud_style":"gauge","hud_scale":1.0,"hud_opacity":1.0,"hud_cx":0.8510416667,"hud_cy":0.8074074074,"keep":true}' > /tmp/settings.json
SPEEDOMETER_SETTINGS=/tmp/settings.json \
  xvfb-run -a -s "-screen 0 2200x1700x24" timeout 3 ./run_gui.sh
# exit 124 means it stayed up; check stderr for tracebacks
```

Free editor test: instantiate the app, set a fake `VideoInfo` and a synthetic
preview image, call `_render_preview()`, then feed simple objects with `.x`/`.y`
to `_preview_press`, `_preview_drag`, `_preview_release`. Verify `hud_cx/cy`
changes when dragging inside `_hud_bbox_preview`, and `hud_scale` changes when
dragging the bottom-right handle. The settings file must then contain
`hud_cx`/`hud_cy` and no `hud_position`.

Burn smoke test: generate a 3-second testsrc with audio, render 4 frames via
`speedpipe.render_frames(... hud_center=(0.3, 0.3), hud_scale=1.2,
hud_opacity=0.8)`, burn, and confirm a frame shows the HUD at that position.

## 11. Known limitations / TODO

- The original 28-minute development clip is unavailable and its final burn is
  still outstanding. If the SD card returns, run the full burn and verify the
  classical output before declaring the original task done.
- HUD geometry assumes a 16:9-ish 1920×1080 overlay. For portrait/non-16:9 video
  the overlay is scaled to the source width and bottom-anchored, as in the burn
  filter; free positions map within that overlay band, not the full portrait
  frame.
- HUD scale is limited to 50–150%. The transform clamps to fit if a style would
  overflow.
- The free editor clamps the HUD fully inside the frame; it does not allow
  partially off-screen placement.
- Windows/macOS have not been physically tested. If you can, run the launchers on
  both and fix any platform-specific issues (DPI, fonts, ffmpeg PATH).
- FFmpeg is an external dependency. A packaged `.exe`/`.app` (PyInstaller or
  similar) is not included; the user was told packaging can be added if wanted.
- Do not add personal media or telemetry to the repo, even in test fixtures.

## 12. Working rules for successors

1. Read this file and `README.md`; keep `README.md` user-facing and this file
   engineering-facing.
2. Keep the classic HUD pixel-identical; it is the existing exported frames/alpha
   overlay. Test it if you touch `hud.py`.
3. If you touch layout, use `px()` for every pixel value and verify at 0.8, 1.0,
   and 1.8 UI scale. Check for card-body `reqheight` overflow.
4. Don't reintroduce fixed 9-way placement. The user explicitly asked for free
   drag/resize.
5. Don't publish personal data. Review `git status` and the staged diff before
   every push.
6. Run the compile/style-matrix tests and a GUI smoke test before pushing.
7. Use `gh` for GitHub operations and keep `main` clean.
