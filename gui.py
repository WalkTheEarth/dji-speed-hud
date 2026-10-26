#!/usr/bin/env python3
"""Speed HUD — a desktop GUI for DJI video -> burned speed video.

Cross-platform Tkinter app with:
  * four window themes
  * five burned-HUD styles
  * HUD size / opacity / placement controls
  * live input/output preview
  * automatic HiDPI UI scaling plus a manual UI-scale control
"""

from __future__ import annotations

import io
import json
import math
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, ttk

from PIL import (Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps,
                 ImageTk)

import hud
import speedpipe

APP_TITLE = "Speed HUD — DJI telemetry overlay"
IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

BASE_W, BASE_H = 1180, 1080
PREVIEW_W, PREVIEW_H = 620, 349
SAMPLE_NOW, SAMPLE_MIN, SAMPLE_MAX = 18.0, 3.0, 34.0
DEFAULT_CENTER = hud.DEFAULT_CENTER

# Centers used only when migrating settings from the old 9-way picker.
OLD_POSITION_CENTERS = {
    "top-left": (0.25, 0.25), "top-center": (0.50, 0.20),
    "top-right": (0.75, 0.25), "middle-left": (0.20, 0.50),
    "center": (0.50, 0.50), "middle-right": (0.80, 0.50),
    "bottom-left": (0.20, 0.80), "bottom-center": (0.50, 0.85),
    "bottom-right": DEFAULT_CENTER,
}


def _settings_path() -> Path:
    override = os.environ.get("SPEEDOMETER_SETTINGS")
    if override:
        return Path(override).expanduser()
    if IS_WIN:
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
        return base / "SpeedHUD" / "settings.json"
    if IS_MAC:
        return Path.home() / "Library" / "Application Support" / "SpeedHUD" / "settings.json"
    return Path.home() / ".config" / "speedometer" / "settings.json"


SETTINGS_PATH = _settings_path()

THEMES = {
    "carbon": dict(
        label="Carbon",
        bg="#0e1014", card="#171b21", card_alt="#21262e", border="#2b323d",
        fg="#e9edf3", muted="#8b95a3",
        accent="#ff8a3d", accent2="#ffc46b", accent_text="#16110b",
        ok="#5ad18a", err="#ff6b6b", log_fg="#b9c2cf",
        progress_trough="#0a0c0f", preview_border="#323a46",
        tag_bg=(13, 16, 20, 220),
        card_radius=14, button_radius=10,
        font_ui="Cantarell", font_mono="Hack",
        strip_style="gradient",
    ),
    "midnight": dict(
        label="Midnight",
        bg="#070b16", card="#101a30", card_alt="#182542", border="#263a60",
        fg="#e8f1ff", muted="#7f93b8",
        accent="#4cc9f0", accent2="#7a5cff", accent_text="#061019",
        ok="#5ee6b5", err="#ff7a90", log_fg="#a9bcd8",
        progress_trough="#050812", preview_border="#2b4370",
        tag_bg=(6, 11, 22, 220),
        card_radius=20, button_radius=16,
        font_ui="Cantarell", font_mono="Hack",
        strip_style="gradient",
    ),
    "paper": dict(
        label="Paper",
        bg="#efe9df", card="#fffdf8", card_alt="#f0e8db", border="#d8cdbb",
        fg="#241f19", muted="#7d7264",
        accent="#e4572e", accent2="#f2a65a", accent_text="#ffffff",
        ok="#2f8f5b", err="#c0392b", log_fg="#4a4238",
        progress_trough="#e3d8c8", preview_border="#d8cdbb",
        tag_bg=(255, 253, 248, 235),
        card_radius=10, button_radius=8,
        font_ui="Cantarell", font_mono="Hack",
        strip_style="gradient",
    ),
    "rally": dict(
        label="Rally",
        bg="#080808", card="#141414", card_alt="#1e1e1e", border="#2e2e2e",
        fg="#f4f4f4", muted="#949494",
        accent="#ffd400", accent2="#ff5d00", accent_text="#0b0b0b",
        ok="#6ee07a", err="#ff5252", log_fg="#c2c2c2",
        progress_trough="#050505", preview_border="#343434",
        tag_bg=(8, 8, 8, 225),
        card_radius=6, button_radius=4,
        font_ui="Cantarell", font_mono="Hack",
        strip_style="stripes",
    ),
}


# ---------------------------------------------------------------- utilities

def _rgb(color) -> tuple[int, int, int]:
    if isinstance(color, tuple):
        return color[:3]
    h = color.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _hex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(int(v) for v in rgb[:3])


def _mix(c1, c2, t: float):
    a, b = _rgb(c1), _rgb(c2)
    return tuple(round(a[i] * (1 - t) + b[i] * t) for i in range(3))


def _hover(color) -> str:
    r, g, b = _rgb(color)
    lum = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0
    return _hex(_mix(color, (255, 255, 255) if lum < 0.55 else (0, 0, 0), 0.08))


def _pil_font(path: str | None, size: int):
    return hud.font(path, max(1, int(round(size))))


def _load_settings() -> dict:
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_settings(data: dict) -> None:
    try:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass


def _as_float(value, default, lo, hi):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, value))


def _round_points(x1, y1, x2, y2, r):
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    return [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]


def draw_round_rect(canvas: tk.Canvas, x1, y1, x2, y2, r, **kwargs):
    return canvas.create_polygon(_round_points(x1, y1, x2, y2, r),
                                 smooth=True, splinesteps=36, **kwargs)


def _open_path(folder: Path) -> None:
    if IS_WIN:
        os.startfile(str(folder))  # type: ignore[attr-defined]
    elif IS_MAC:
        subprocess.Popen(["open", str(folder)])
    else:
        subprocess.Popen(["xdg-open", str(folder)])


def _pick_fonts(root: tk.Tk):
    try:
        families = set(tkfont.families(root))
    except tk.TclError:
        families = set()
    ui = next((name for name in
               ("Cantarell", "Segoe UI", "SF Pro Text", "Helvetica Neue",
                "Noto Sans", "DejaVu Sans", "Arial")
               if name in families), "TkDefaultFont")
    mono = next((name for name in
                 ("Hack", "Cascadia Mono", "Consolas", "Menlo",
                  "DejaVu Sans Mono", "Courier New")
                 if name in families), "TkFixedFont")
    return ui, mono


# ------------------------------------------------------------------ widgets

class RoundButton(tk.Canvas):
    def __init__(self, parent, text, command=None, *, theme, variant="ghost",
                 width=120, height=36, radius=None, font=None, bg=None,
                 selected=False):
        try:
            bg = bg or parent.cget("bg")
        except tk.TclError:
            bg = theme["bg"]
        super().__init__(parent, width=max(1, int(width)), height=max(1, int(height)),
                         bg=bg, highlightthickness=0, bd=0, cursor="hand2")
        self.theme = theme
        self.text = text
        self.command = command
        self.variant = variant
        self.selected = selected
        self.radius = radius if radius is not None else theme["button_radius"]
        self.font = font or (theme["font_ui"], 10)
        self.w, self.h = max(1, int(width)), max(1, int(height))
        self._enabled = True
        self._hover = False
        self._draw()
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)

    def _colors(self):
        t = self.theme
        if not self._enabled:
            return t["card_alt"], t["muted"], t["border"]
        if self.variant == "accent" or self.selected:
            return t["accent"], t["accent_text"], t["accent"]
        return t["card_alt"], t["fg"], t["border"]

    def _draw(self):
        fill, fg, border = self._colors()
        if self._hover and self._enabled:
            fill = _hover(fill)
        self.delete("all")
        draw_round_rect(self, 1, 1, self.w - 1, self.h - 1, self.radius,
                        fill=fill, outline=border, width=1)
        self.create_text(self.w / 2, self.h / 2 + 1, text=self.text,
                         fill=fg, font=self.font)

    def _on_enter(self, _e):
        if self._enabled:
            self._hover = True
            self._draw()

    def _on_leave(self, _e):
        self._hover = False
        self._draw()

    def _on_click(self, _e):
        if self._enabled and self.command:
            self.command()

    def set_enabled(self, enabled: bool):
        self._enabled = enabled
        self.configure(cursor="hand2" if enabled else "arrow")
        self._draw()

    def set_selected(self, selected: bool):
        self.selected = selected
        self._draw()

    def set_text(self, text: str):
        self.text = text
        self._draw()


class Chip(tk.Canvas):
    def __init__(self, parent, text, *, theme, fill=None, fg=None,
                 font=None, height=24, padx=10, dot=None, scale=1.0):
        try:
            bg = parent.cget("bg")
        except tk.TclError:
            bg = theme["card"]
        font = font or (theme["font_mono"], 8)
        font_obj = tkfont.Font(font=font)
        height = max(1, int(round(height * scale)))
        padx = max(1, int(round(padx * scale)))
        dot_space = int(round(12 * scale)) if dot else 0
        width = font_obj.measure(text) + 2 * padx + dot_space
        super().__init__(parent, width=width, height=height, bg=bg,
                         highlightthickness=0, bd=0)
        draw_round_rect(self, 0.5, 0.5, width - 0.5, height - 0.5, height / 2,
                        fill=fill or theme["card_alt"],
                        outline=theme["border"], width=1)
        x = padx
        if dot:
            r = 3 * scale
            self.create_oval(x, height / 2 - r, x + 2 * r, height / 2 + r,
                             fill=dot, outline="")
            x += dot_space
        self.create_text(x, height / 2 + 1, text=text, anchor="w",
                         fill=fg or theme["muted"], font=font)


class Toggle(tk.Canvas):
    def __init__(self, parent, value: bool, command, *, theme, scale=1.0):
        try:
            bg = parent.cget("bg")
        except tk.TclError:
            bg = theme["card"]
        self.scale = scale
        self.w = max(1, int(round(44 * scale)))
        self.h = max(1, int(round(24 * scale)))
        super().__init__(parent, width=self.w, height=self.h, bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self.theme = theme
        self.value = bool(value)
        self.command = command
        self._enabled = True
        self._draw()
        self.bind("<Button-1>", self._click)

    def _draw(self):
        t = self.theme
        s = self.scale
        self.delete("all")
        track = t["accent"] if self.value and self._enabled else t["card_alt"]
        outline = t["accent"] if self.value and self._enabled else t["border"]
        draw_round_rect(self, 1, 2 * s, self.w - 1, self.h - 2 * s,
                        10 * s, fill=track, outline=outline, width=1)
        cx = self.w - 12 * s if self.value else 12 * s
        r = 7 * s
        knob = "#ffffff" if self._enabled else t["muted"]
        self.create_oval(cx - r, self.h / 2 - r, cx + r, self.h / 2 + r,
                         fill=knob, outline="")

    def _click(self, _e):
        if not self._enabled:
            return
        self.value = not self.value
        self._draw()
        if self.command:
            self.command(self.value)

    def set_value(self, value: bool):
        self.value = bool(value)
        self._draw()

    def set_enabled(self, enabled: bool):
        self._enabled = enabled
        self.configure(cursor="hand2" if enabled else "arrow")
        self._draw()


class Card(tk.Canvas):
    """Canvas-drawn rounded card with an embedded frame body."""

    def __init__(self, parent, theme, *, padding=14, width=400, height=300):
        super().__init__(parent, width=max(1, int(width)), height=max(1, int(height)),
                         bg=theme["bg"], highlightthickness=0, bd=0)
        self.theme = theme
        self.pad = max(1, int(padding))
        self.radius = theme["card_radius"]
        self.body = tk.Frame(self, bg=theme["card"])
        self._win = self.create_window(self.pad, self.pad, anchor="nw",
                                       window=self.body)
        self.bind("<Configure>", self._on_configure)

    def _on_configure(self, event):
        w, h = event.width, event.height
        if w < 2 or h < 2:
            return
        self.delete("cardbg")
        draw_round_rect(self, 1, 1, w - 1, h - 1, self.radius,
                        fill=self.theme["card"], outline=self.theme["border"],
                        width=1, tags="cardbg")
        self.tag_lower("cardbg")
        self.coords(self._win, self.pad, self.pad)
        self.itemconfigure(self._win, width=max(1, w - 2 * self.pad),
                           height=max(1, h - 2 * self.pad))


class GradientStrip(tk.Canvas):
    def __init__(self, parent, *, theme, height=3, style="gradient", scale=1.0):
        super().__init__(parent, height=max(1, int(height)), bg=theme["bg"],
                         highlightthickness=0, bd=0)
        self.theme = theme
        self.style = style
        self.scale = scale
        self.bind("<Configure>", lambda _e: self._draw())

    def _draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 2:
            return
        t = self.theme
        s = self.scale
        if self.style == "stripes":
            for x in range(-int(h * 2), w + int(h), max(1, int(14 * s))):
                self.create_line(x, h, x + h, 0, fill=t["accent"],
                                 width=max(1, int(6 * s)))
                self.create_line(x + 7 * s, h, x + 7 * s + h, 0,
                                 fill=t["bg"], width=max(1, int(6 * s)))
        else:
            steps = 180
            for i in range(steps):
                x0 = w * i / steps
                x1 = w * (i + 1) / steps
                color = _hex(_mix(t["accent"], t["accent2"], i / (steps - 1)))
                self.create_rectangle(x0, 0, x1, h, fill=color, outline=color)


class RoundProgress(tk.Canvas):
    def __init__(self, parent, *, theme, height=12, scale=1.0):
        try:
            bg = parent.cget("bg")
        except tk.TclError:
            bg = theme["card"]
        super().__init__(parent, height=max(1, int(height)), bg=bg,
                         highlightthickness=0, bd=0)
        self.theme = theme
        self.scale = scale
        self.frac = 0.0
        self.bind("<Configure>", lambda _e: self._draw())

    def set(self, fraction: float):
        self.frac = max(0.0, min(1.0, fraction))
        self._draw()

    def _draw(self):
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4:
            return
        t = self.theme
        s = self.scale
        self.delete("all")
        draw_round_rect(self, 0, 0, w, h, h / 2,
                        fill=t["progress_trough"], outline=t["progress_trough"])
        fill_w = w * self.frac
        if fill_w > 2:
            r = min(h / 2, fill_w / 2)
            draw_round_rect(self, 0, 0, max(fill_w, h), h, r,
                            fill=t["accent"], outline=t["accent"])
            if fill_w > 10 * s:
                self.create_line(4 * s, 2.5 * s, max(fill_w, h) - 4 * s, 2.5 * s,
                                 fill=_hex(_mix(t["accent"], (255, 255, 255), 0.35)),
                                 width=max(1, int(s)))


class StatusDot(tk.Canvas):
    def __init__(self, parent, *, theme, size=9):
        try:
            bg = parent.cget("bg")
        except tk.TclError:
            bg = theme["bg"]
        size = max(2, int(size))
        super().__init__(parent, width=size, height=size, bg=bg,
                         highlightthickness=0, bd=0)
        self.dot = self.create_oval(1, 1, size - 1, size - 1,
                                    fill=theme["muted"], outline="")

    def set_color(self, color: str):
        self.itemconfig(self.dot, fill=color)


class RoundSlider(tk.Canvas):
    def __init__(self, parent, value, minimum, maximum, command, *,
                 theme, scale=1.0, width=280, height=24, step=0.05):
        try:
            bg = parent.cget("bg")
        except tk.TclError:
            bg = theme["card"]
        self.scale = scale
        self.w = max(1, int(round(width * scale)))
        self.h = max(1, int(round(height * scale)))
        super().__init__(parent, width=self.w, height=self.h, bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self.theme = theme
        self.minimum = float(minimum)
        self.maximum = float(maximum)
        self.step = float(step)
        self.command = command
        self.value = self._clamp(value)
        self._enabled = True
        self.bind("<Button-1>", self._press)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", self._release)
        self._draw()

    def _clamp(self, value):
        return max(self.minimum, min(self.maximum, float(value)))

    def _frac(self):
        span = self.maximum - self.minimum
        return (self.value - self.minimum) / span if span else 0.0

    def _draw(self):
        s = self.scale
        t = self.theme
        self.delete("all")
        x1 = 10 * s
        x2 = self.w - 10 * s
        y = self.h / 2
        th = max(3, int(6 * s))
        draw_round_rect(self, x1, y - th / 2, x2, y + th / 2, th / 2,
                        fill=t["progress_trough"], outline=t["progress_trough"])
        fw = (x2 - x1) * self._frac()
        if fw > 0:
            draw_round_rect(self, x1, y - th / 2, x1 + max(fw, th), y + th / 2,
                            th / 2, fill=t["accent"], outline=t["accent"])
        hx = x1 + fw
        r = 8 * s
        fill = t["accent"] if self._enabled else t["muted"]
        self.create_oval(hx - r, y - r, hx + r, y + r, fill=fill,
                         outline=t["card"], width=max(1, int(2 * s)))

    def _value_at(self, x):
        x1 = 10 * self.scale
        x2 = self.w - 10 * self.scale
        frac = (x - x1) / max(1, x2 - x1)
        frac = max(0.0, min(1.0, frac))
        value = self.minimum + frac * (self.maximum - self.minimum)
        if self.step > 0:
            value = round(value / self.step) * self.step
        return self._clamp(value)

    def _notify(self, final):
        if self.command:
            self.command(self.value, final)

    def _press(self, event):
        if not self._enabled:
            return
        self.value = self._value_at(event.x)
        self._draw()
        self._notify(False)

    def _drag(self, event):
        if not self._enabled:
            return
        value = self._value_at(event.x)
        if value != self.value:
            self.value = value
            self._draw()
            self._notify(False)

    def _release(self, _event):
        if self._enabled:
            self._notify(True)

    def set_value(self, value):
        self.value = self._clamp(value)
        self._draw()

    def set_enabled(self, enabled: bool):
        self._enabled = enabled
        self.configure(cursor="hand2" if enabled else "arrow")
        self._draw()


# ---------------------------------------------------------------- the app

class SpeedHudApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_TITLE)

        settings = _load_settings()
        self.theme_name = settings.get("theme", "carbon")
        if self.theme_name not in THEMES:
            self.theme_name = "carbon"

        self.ui_scale = self._resolve_ui_scale(settings.get("ui_scale"))
        self.root.tk.call("tk", "scaling", (96 / 72.0) * self.ui_scale)
        self.ui_font, self.mono_font = _pick_fonts(self.root)
        self.theme = self._scaled_theme(self.theme_name)

        self.root.geometry(f"{self.px(BASE_W)}x{self.px(BASE_H)}")
        self.root.minsize(self.px(1160), self.px(920))

        self.queue: queue.Queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.running = False
        self.video_info: speedpipe.VideoInfo | None = None
        self.probe_token = 0
        self.probe_after_id: str | None = None
        self.preview_token = 0
        self._closing = False
        self._setting_output = False
        self._output_touched = False
        self._last_stage = ""
        self._last_output: Path | None = None
        self._log_lines: list[str] = []
        self._progress_frac = 0.0
        self._status_kind = "idle"
        self.preview_base: Image.Image | None = None
        self.input_preview_base: Image.Image | None = None
        self.preview_kind = "none"
        self.preview_w = self.px(PREVIEW_W)
        self.preview_h = self.px(PREVIEW_H)
        self._preview_photo = None
        self._logo_photo = None
        self.probe_message = "Waiting for a file…"
        self.probe_kind = "muted"

        self.input_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.unit_var = tk.StringVar(value=settings.get("unit", "mph"))
        if self.unit_var.get() not in ("mph", "kmh"):
            self.unit_var.set("mph")
        valid_styles = {sid for sid, _ in hud.styles()}
        saved_style = settings.get("hud_style", "classic")
        self.hud_style_var = tk.StringVar(
            value=saved_style if saved_style in valid_styles else "classic")
        self.hud_scale = _as_float(settings.get("hud_scale", 1.0), 1.0, 0.5, 1.5)
        self.hud_opacity = _as_float(settings.get("hud_opacity", 1.0), 1.0, 0.3, 1.0)
        if "hud_cx" in settings and "hud_cy" in settings:
            self.hud_cx = _as_float(settings.get("hud_cx"), DEFAULT_CENTER[0], 0.0, 1.0)
            self.hud_cy = _as_float(settings.get("hud_cy"), DEFAULT_CENTER[1], 0.0, 1.0)
        else:
            legacy = settings.get("hud_position", "bottom-right")
            self.hud_cx, self.hud_cy = OLD_POSITION_CENTERS.get(legacy, DEFAULT_CENTER)
        self._hud_bbox_preview = None
        self._hud_bbox_overlay = None
        self._hud_overlay_scale = None
        self._hud_overlay_origin = None
        self._drag_mode = None
        self._drag_start = None
        self._drag_center_start = None
        self._drag_scale_start = 1.0
        self._drag_dist_start = 1.0
        self._drag_half_norm = (0.0, 0.0)
        self._editor_after = None
        self._editor_dirty = False
        self._cursor = "arrow"
        self.keep_var = tk.BooleanVar(value=bool(settings.get("keep", True)))
        self.status_var = tk.StringVar(value="Choose a DJI video to begin.")
        self.stage_var = tk.StringVar(value="Ready")
        self.detail_var = tk.StringVar(value="")
        self.pct_var = tk.StringVar(value="0%")
        self.preview_name_var = tk.StringVar(value="No video selected")
        self.preview_caption_var = tk.StringVar(value="Choose a DJI clip to see the HUD preview.")

        self._build_icon()
        self._build_ui()

        self.input_var.trace_add("write", self._on_input_changed)
        self.output_var.trace_add("write", self._on_output_changed)
        self.root.bind("<Control-o>", lambda _e: self._browse_input())
        self.root.bind("<Control-Return>", lambda _e: self._start())
        self.root.bind("<Control-plus>", lambda _e: self._change_ui_scale(0.05))
        self.root.bind("<Control-equal>", lambda _e: self._change_ui_scale(0.05))
        self.root.bind("<Control-minus>", lambda _e: self._change_ui_scale(-0.05))
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(80, self._poll)

    # -------------------------------------------------------------- scaling

    def px(self, value) -> int:
        return max(1, int(round(float(value) * self.ui_scale)))

    def _screen_fit(self) -> float:
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        return min((sw - 70) / BASE_W, (sh - 130) / BASE_H)

    def _resolve_ui_scale(self, saved):
        fit = self._screen_fit()
        if saved is not None:
            try:
                scale = float(saved)
            except (TypeError, ValueError):
                scale = None
        else:
            scale = None

        if scale is None:
            scale = 1.0
            if IS_MAC:
                scale = 1.0
            elif IS_WIN:
                try:
                    scale = max(1.0, self.root.winfo_fpixels("1i") / 96.0)
                except tk.TclError:
                    scale = 1.0
            else:
                try:
                    out = subprocess.run(["xrdb", "-query"], capture_output=True,
                                         text=True, timeout=2).stdout
                    for line in out.splitlines():
                        if line.lower().startswith("xft.dpi:"):
                            scale = float(line.split(":", 1)[1].strip()) / 96.0
                            break
                except Exception:
                    pass
                if scale <= 1.0:
                    sw = self.root.winfo_screenwidth()
                    sh = self.root.winfo_screenheight()
                    scale = max(1.0, max(sw / 1920.0, sh / 1080.0) * 0.9)

        scale = min(scale, 2.0, fit * 0.97)
        scale = max(0.55, scale)
        return round(scale / 0.05) * 0.05

    def _change_ui_scale(self, delta: float) -> None:
        if self.running:
            return
        fit = self._screen_fit()
        new = min(self.ui_scale + delta, 2.0, fit * 0.99)
        new = max(0.55, new)
        new = round(new / 0.05) * 0.05
        if abs(new - self.ui_scale) < 1e-9:
            return
        self.ui_scale = new
        self.root.tk.call("tk", "scaling", (96 / 72.0) * new)
        self.root.geometry(f"{self.px(BASE_W)}x{self.px(BASE_H)}")
        self.root.minsize(self.px(1160), self.px(920))
        self._save_settings()
        self.root.after_idle(self._build_ui)

    def _scaled_theme(self, name: str) -> dict:
        t = dict(THEMES[name])
        t["card_radius"] = self.px(t["card_radius"])
        t["button_radius"] = self.px(t["button_radius"])
        t["font_ui"] = self.ui_font
        t["font_mono"] = self.mono_font
        return t

    # -------------------------------------------------------------- theme

    def _save_settings(self) -> None:
        _save_settings({
            "theme": self.theme_name,
            "unit": self.unit_var.get(),
            "hud_style": self.hud_style_var.get(),
            "hud_scale": round(self.hud_scale, 2),
            "hud_opacity": round(self.hud_opacity, 2),
            "hud_cx": round(self.hud_cx, 4),
            "hud_cy": round(self.hud_cy, 4),
            "keep": bool(self.keep_var.get()),
            "ui_scale": round(self.ui_scale, 2),
        })

    def _select_theme(self, name: str) -> None:
        if self.running or name == self.theme_name:
            return
        self.theme_name = name
        self._save_settings()
        self.root.after_idle(self._build_ui)

    def _build_icon(self) -> None:
        try:
            icon_path = Path(__file__).with_name("speedometer.png")
            if icon_path.exists():
                size = self.px(64)
                icon = Image.open(icon_path).convert("RGBA").resize(
                    (size, size), Image.LANCZOS)
                self._icon = ImageTk.PhotoImage(icon)
            else:
                self._icon = None
            if self._icon is not None:
                self.root.iconphoto(True, self._icon)
        except Exception:
            self._icon = None

    def _font(self, size: int, weight: str = "normal", mono: bool = False):
        return (self.theme["font_mono" if mono else "font_ui"], size, weight)

    # --------------------------------------------------------------- build

    def _build_styles(self) -> None:
        t = self.theme
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=t["bg"], foreground=t["fg"],
                        font=(t["font_ui"], 10))
        style.configure("Path.TEntry", fieldbackground=t["card_alt"],
                        foreground=t["fg"], insertcolor=t["fg"],
                        bordercolor=t["border"], lightcolor=t["card_alt"],
                        darkcolor=t["card_alt"], padding=self.px(8))
        style.map("Path.TEntry",
                  bordercolor=[("focus", t["accent"])],
                  lightcolor=[("focus", t["accent"])],
                  darkcolor=[("focus", t["accent"])])
        style.configure("Vertical.TScrollbar", background=t["card_alt"],
                        troughcolor=t["card"], bordercolor=t["card"],
                        arrowcolor=t["muted"], darkcolor=t["card_alt"],
                        lightcolor=t["card_alt"], gripcount=0,
                        arrowsize=self.px(12))
        style.map("Vertical.TScrollbar",
                  background=[("active", t["border"]), ("pressed", t["accent"])])

    def _section(self, parent, text: str) -> tk.Frame:
        t = self.theme
        frame = tk.Frame(parent, bg=t["card"])
        bar = tk.Canvas(frame, width=self.px(3), height=self.px(11),
                        bg=t["card"], highlightthickness=0, bd=0)
        bar.create_rectangle(0, 0, self.px(3), self.px(11),
                             fill=t["accent"], outline=t["accent"])
        bar.pack(side="left", padx=(0, self.px(7)))
        tk.Label(frame, text=text.upper(), bg=t["card"], fg=t["muted"],
                 font=(t["font_ui"], 8, "bold")).pack(side="left")
        return frame

    def _build_ui(self) -> None:
        t = self._scaled_theme(self.theme_name)
        self.theme = t
        px = self.px
        self.preview_w = px(PREVIEW_W)
        self.preview_h = px(PREVIEW_H)

        self.root.configure(bg=t["bg"])
        for child in self.root.winfo_children():
            child.destroy()

        self._build_styles()
        outer = tk.Frame(self.root, bg=t["bg"])
        outer.pack(fill="both", expand=True, padx=px(22), pady=px(16))
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(6, weight=1)

        # ---- header
        header = tk.Frame(outer, bg=t["bg"])
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)

        logo_path = Path(__file__).with_name("speedometer.png")
        try:
            logo = Image.open(logo_path).convert("RGBA").resize(
                (px(40), px(40)), Image.LANCZOS)
            self._logo_photo = ImageTk.PhotoImage(logo)
            tk.Label(header, image=self._logo_photo, bg=t["bg"]).grid(
                row=0, column=0, rowspan=2, padx=(0, px(12)))
        except Exception:
            self._logo_photo = None

        title = tk.Frame(header, bg=t["bg"])
        title.grid(row=0, column=1, sticky="w")
        tk.Label(title, text="SPEED", bg=t["bg"], fg=t["fg"],
                 font=(t["font_ui"], 21, "bold")).pack(side="left")
        tk.Label(title, text=" HUD", bg=t["bg"], fg=t["accent"],
                 font=(t["font_ui"], 21, "bold")).pack(side="left")
        tk.Label(header, text="DJI telemetry → burned video", bg=t["bg"],
                 fg=t["muted"], font=(t["font_ui"], 9)).grid(
            row=1, column=1, sticky="w")

        picker = tk.Frame(header, bg=t["bg"])
        picker.grid(row=0, column=2, rowspan=2, sticky="e")
        tk.Label(picker, text="DESIGN", bg=t["bg"], fg=t["muted"],
                 font=(t["font_ui"], 8, "bold")).pack(side="left", padx=(0, px(8)))
        self.theme_buttons: list[RoundButton] = []
        for name, data in THEMES.items():
            btn = RoundButton(picker, data["label"],
                              lambda n=name: self._select_theme(n),
                              theme=t, width=px(76), height=px(28),
                              radius=px(8), font=(t["font_ui"], 8, "bold"),
                              bg=t["bg"], selected=(name == self.theme_name))
            btn.pack(side="left", padx=(0, px(5)))
            self.theme_buttons.append(btn)

        GradientStrip(outer, theme=t, height=px(3), scale=self.ui_scale,
                      style=t.get("strip_style", "gradient")).grid(
            row=1, column=0, sticky="ew", pady=(px(10), px(14)))

        # ---- main row
        main = tk.Frame(outer, bg=t["bg"])
        main.grid(row=2, column=0, sticky="ew")
        main.columnconfigure(0, weight=1, minsize=px(430))
        main.rowconfigure(0, weight=1)

        left_card = Card(main, t, padding=px(18), width=px(460), height=px(476))
        left_card.grid(row=0, column=0, sticky="nsew", padx=(0, px(18)))
        left = left_card.body
        left.columnconfigure(0, weight=1)
        left.rowconfigure(9, weight=1)

        self._section(left, "Input video").grid(row=0, column=0, sticky="w")
        in_row = tk.Frame(left, bg=t["card"])
        in_row.grid(row=1, column=0, sticky="ew", pady=(px(6), 0))
        in_row.columnconfigure(0, weight=1)
        self.input_entry = ttk.Entry(in_row, textvariable=self.input_var,
                                     style="Path.TEntry")
        self.input_entry.grid(row=0, column=0, sticky="ew", padx=(0, px(8)))
        self.input_browse = RoundButton(in_row, "Browse", self._browse_input,
                                        theme=t, width=px(88), height=px(34),
                                        radius=px(9), font=(t["font_ui"], 9))
        self.input_browse.grid(row=0, column=1)

        self.chips_frame = tk.Frame(left, bg=t["card"], height=px(24))
        self.chips_frame.grid(row=2, column=0, sticky="ew", pady=(px(7), 0))
        self.chips_frame.pack_propagate(False)

        self._section(left, "Output video").grid(row=3, column=0, sticky="w",
                                                 pady=(px(16), 0))
        out_row = tk.Frame(left, bg=t["card"])
        out_row.grid(row=4, column=0, sticky="ew", pady=(px(6), 0))
        out_row.columnconfigure(0, weight=1)
        self.output_entry = ttk.Entry(out_row, textvariable=self.output_var,
                                      style="Path.TEntry")
        self.output_entry.grid(row=0, column=0, sticky="ew", padx=(0, px(8)))
        self.output_browse = RoundButton(out_row, "Save as", self._browse_output,
                                         theme=t, width=px(88), height=px(34),
                                         radius=px(9), font=(t["font_ui"], 9))
        self.output_browse.grid(row=0, column=1)

        tk.Label(left, text="Defaults next to the source; read-only media goes to Videos.",
                 bg=t["card"], fg=t["muted"], font=(t["font_ui"], 8)).grid(
            row=5, column=0, sticky="w", pady=(px(4), 0))

        tk.Frame(left, bg=t["border"], height=px(1)).grid(
            row=6, column=0, sticky="ew", pady=(px(14), px(12)))

        unit_row = tk.Frame(left, bg=t["card"])
        unit_row.grid(row=7, column=0, sticky="ew")
        tk.Label(unit_row, text="Primary unit", bg=t["card"], fg=t["fg"],
                 font=(t["font_ui"], 9)).pack(side="left", padx=(0, px(10)))
        self.unit_mph = RoundButton(unit_row, "MPH",
                                    lambda: self._set_unit("mph"), theme=t,
                                    width=px(62), height=px(28), radius=px(8),
                                    font=(t["font_ui"], 8, "bold"),
                                    selected=self.unit_var.get() == "mph")
        self.unit_mph.pack(side="left", padx=(0, px(5)))
        self.unit_kmh = RoundButton(unit_row, "KM/H",
                                    lambda: self._set_unit("kmh"), theme=t,
                                    width=px(62), height=px(28), radius=px(8),
                                    font=(t["font_ui"], 8, "bold"),
                                    selected=self.unit_var.get() == "kmh")
        self.unit_kmh.pack(side="left")

        keep_row = tk.Frame(left, bg=t["card"])
        keep_row.grid(row=8, column=0, sticky="ew", pady=(px(10), 0))
        tk.Label(keep_row, text="Keep intermediate files", bg=t["card"],
                 fg=t["fg"], font=(t["font_ui"], 9)).pack(side="left")
        self.keep_toggle = Toggle(keep_row, self.keep_var.get(),
                                  self._toggle_keep, theme=t,
                                  scale=self.ui_scale)
        self.keep_toggle.pack(side="right")

        tip = tk.Frame(left, bg=t["card_alt"], highlightbackground=t["border"],
                       highlightthickness=1)
        tip.grid(row=9, column=0, sticky="sew", pady=(px(14), 0))
        tip_bar = tk.Canvas(tip, width=px(3), height=px(34), bg=t["card_alt"],
                            highlightthickness=0, bd=0)
        tip_bar.create_rectangle(0, 0, px(3), px(34),
                                 fill=t["accent"], outline=t["accent"])
        tip_bar.pack(side="left", padx=(px(10), px(8)), pady=px(8))
        tk.Label(tip, text="Speed is the camera's Doppler ground velocity — "
                           "clips without a GPS fix can't be burned.",
                 bg=t["card_alt"], fg=t["muted"], font=(t["font_ui"], 8),
                 wraplength=px(330), justify="left", anchor="w").pack(
            side="left", fill="x", expand=True, padx=(0, px(10)), pady=px(8))

        recipe = tk.Frame(left, bg=t["card"])
        recipe.grid(row=10, column=0, sticky="ew", pady=(px(10), 0))
        self._section(recipe, "Burn settings").pack(anchor="w")
        recipe_chips = tk.Frame(recipe, bg=t["card"])
        recipe_chips.pack(anchor="w", pady=(px(7), 0), fill="x")
        for text in ("H.264", "CRF 20", "Audio copy", "MP4"):
            Chip(recipe_chips, text, theme=t, font=(t["font_mono"], 8),
                 scale=self.ui_scale).pack(side="left", padx=(0, px(5)))
        tk.Label(recipe, text="1 Hz values · no interpolation",
                 bg=t["card"], fg=t["muted"], font=(t["font_ui"], 8)).pack(
            anchor="w", pady=(px(6), 0))

        # preview card
        preview_card = Card(main, t, padding=px(12),
                            width=self.preview_w + px(24), height=px(476))
        preview_card.grid(row=0, column=1, sticky="nsew")
        pv = preview_card.body
        pv.columnconfigure(0, weight=1)
        pv_head = tk.Frame(pv, bg=t["card"])
        pv_head.grid(row=0, column=0, sticky="ew")
        self._section(pv_head, "Preview").pack(side="left")
        tk.Label(pv_head, textvariable=self.preview_name_var, bg=t["card"],
                 fg=t["muted"], font=(t["font_mono"], 8), anchor="e").pack(
            side="right", fill="x", expand=True)
        self.preview_label = tk.Label(pv, bg=t["card"], bd=0,
                                      highlightthickness=0, cursor="arrow")
        self.preview_label.grid(row=1, column=0, pady=(px(8), 0))
        self.preview_label.bind("<Button-1>", self._preview_press)
        self.preview_label.bind("<B1-Motion>", self._preview_drag)
        self.preview_label.bind("<ButtonRelease-1>", self._preview_release)
        self.preview_label.bind("<Motion>", self._preview_hover)
        self.preview_label.bind("<Leave>", lambda _e: self._set_preview_cursor("arrow"))
        tk.Label(pv, textvariable=self.preview_caption_var, bg=t["card"],
                 fg=t["muted"], font=(t["font_ui"], 8), anchor="w").grid(
            row=2, column=0, sticky="ew", pady=(px(8), 0))

        style_row = tk.Frame(pv, bg=t["card"])
        style_row.grid(row=3, column=0, sticky="ew", pady=(px(10), 0))
        self._section(style_row, "HUD style").pack(side="left")
        self.hud_style_buttons: dict[str, RoundButton] = {}
        style_widths = {"classic": 60, "gauge": 54, "strip": 46,
                        "minimal": 58, "racing": 52}
        for sid, label in hud.styles():
            btn = RoundButton(style_row, label,
                              lambda s=sid: self._set_hud_style(s),
                              theme=t, width=px(style_widths.get(sid, 56)),
                              height=px(26), radius=px(8),
                              font=(t["font_ui"], 8, "bold"),
                              selected=(sid == self.hud_style_var.get()))
            btn.pack(side="left", padx=(px(5), 0))
            self.hud_style_buttons[sid] = btn

        # ---- HUD geometry controls
        controls = Card(outer, t, padding=px(14), width=px(1130), height=px(124))
        controls.grid(row=3, column=0, sticky="ew", pady=(px(12), 0))
        cb = controls.body
        cb.columnconfigure(0, weight=1)
        crow = tk.Frame(cb, bg=t["card"])
        crow.pack(fill="x")
        crow.columnconfigure(0, weight=1)
        crow.columnconfigure(1, weight=1)

        size_box = tk.Frame(crow, bg=t["card"])
        size_box.grid(row=0, column=0, sticky="w")
        self._section(size_box, "HUD size").pack(anchor="w")
        srow = tk.Frame(size_box, bg=t["card"])
        srow.pack(anchor="w", pady=(px(6), 0))
        self.size_slider = RoundSlider(
            srow, self.hud_scale, 0.5, 1.5, self._on_hud_scale,
            theme=t, scale=self.ui_scale, width=280, height=24)
        self.size_slider.pack(side="left")
        self.size_value = tk.Label(
            srow, text=f"{int(round(self.hud_scale * 100))}%", bg=t["card"],
            fg=t["fg"], font=(t["font_mono"], 10, "bold"))
        self.size_value.pack(side="left", padx=(px(10), 0))

        op_box = tk.Frame(crow, bg=t["card"])
        op_box.grid(row=0, column=1, sticky="w")
        self._section(op_box, "Opacity").pack(anchor="w")
        orow = tk.Frame(op_box, bg=t["card"])
        orow.pack(anchor="w", pady=(px(6), 0))
        self.opacity_slider = RoundSlider(
            orow, self.hud_opacity, 0.3, 1.0, self._on_hud_opacity,
            theme=t, scale=self.ui_scale, width=280, height=24)
        self.opacity_slider.pack(side="left")
        self.opacity_value = tk.Label(
            orow, text=f"{int(round(self.hud_opacity * 100))}%", bg=t["card"],
            fg=t["fg"], font=(t["font_mono"], 10, "bold"))
        self.opacity_value.pack(side="left", padx=(px(10), 0))

        pos_box = tk.Frame(crow, bg=t["card"])
        pos_box.grid(row=0, column=2, sticky="w", padx=(px(20), 0))
        self._section(pos_box, "Position").pack(anchor="w")
        tk.Label(pos_box, text="Drag the HUD in the preview to move it; "
                               "drag the corner handle to resize.",
                 bg=t["card"], fg=t["muted"], font=(t["font_ui"], 8),
                 wraplength=px(250), justify="left", anchor="w").pack(
            anchor="w", pady=(px(6), 0))
        self.reset_btn = RoundButton(pos_box, "Reset HUD", self._reset_hud,
                                     theme=t, width=px(100), height=px(26),
                                     radius=px(8), font=(t["font_ui"], 8, "bold"))
        self.reset_btn.pack(anchor="w", pady=(px(6), 0))

        # ---- action bar
        actions = tk.Frame(outer, bg=t["bg"])
        actions.grid(row=4, column=0, sticky="ew", pady=(px(12), 0))
        self.start_btn = RoundButton(actions, "Start burn", self._start,
                                     theme=t, variant="accent", width=px(190),
                                     height=px(44), radius=t["button_radius"] + px(2),
                                     font=(t["font_ui"], 11, "bold"))
        self.start_btn.pack(side="left")
        self.cancel_btn = RoundButton(actions, "Cancel", self._cancel,
                                      theme=t, width=px(112), height=px(44),
                                      radius=t["button_radius"] + px(2),
                                      font=(t["font_ui"], 10))
        self.cancel_btn.pack(side="left", padx=(px(8), 0))
        self.open_btn = RoundButton(actions, "Open output folder", self._open_folder,
                                    theme=t, width=px(190), height=px(44),
                                    radius=t["button_radius"] + px(2),
                                    font=(t["font_ui"], 10))
        self.open_btn.pack(side="right")

        # ---- progress
        pc = Card(outer, t, padding=px(14), width=px(1130), height=px(76))
        pc.grid(row=5, column=0, sticky="ew", pady=(px(12), 0))
        body = pc.body
        body.columnconfigure(0, weight=1)
        prow = tk.Frame(body, bg=t["card"])
        prow.grid(row=0, column=0, sticky="ew")
        tk.Label(prow, textvariable=self.stage_var, bg=t["card"], fg=t["fg"],
                 font=(t["font_ui"], 11, "bold")).pack(side="left")
        tk.Label(prow, textvariable=self.detail_var, bg=t["card"], fg=t["muted"],
                 font=(t["font_ui"], 8)).pack(side="left", padx=(px(10), 0))
        tk.Label(prow, textvariable=self.pct_var, bg=t["card"], fg=t["accent"],
                 font=(t["font_mono"], 10, "bold")).pack(side="right")
        self.progress = RoundProgress(body, theme=t, height=px(12),
                                      scale=self.ui_scale)
        self.progress.grid(row=1, column=0, sticky="ew", pady=(px(8), 0))
        self.progress.set(self._progress_frac)

        # ---- log
        log_card = Card(outer, t, padding=px(12), width=px(1130), height=px(160))
        log_card.grid(row=6, column=0, sticky="nsew", pady=(px(12), 0))
        lb = log_card.body
        lb.columnconfigure(0, weight=1)
        lb.rowconfigure(1, weight=1)
        lhead = tk.Frame(lb, bg=t["card"])
        lhead.grid(row=0, column=0, columnspan=2, sticky="ew")
        self._section(lhead, "Log").pack(side="left")
        self.clear_btn = RoundButton(lhead, "Clear", self._clear_log, theme=t,
                                     width=px(62), height=px(24), radius=px(6),
                                     font=(t["font_ui"], 8))
        self.clear_btn.pack(side="right")

        self.log = tk.Text(lb, bg=t["card"], fg=t["log_fg"],
                           insertbackground=t["fg"], font=(t["font_mono"], 9),
                           relief="flat", bd=0, highlightthickness=0,
                           wrap="word", padx=px(8), pady=px(6),
                           selectbackground=t["accent"],
                           selectforeground=t["accent_text"])
        self.log.grid(row=1, column=0, sticky="nsew", pady=(px(8), 0))
        scroll = ttk.Scrollbar(lb, orient="vertical", command=self.log.yview)
        scroll.grid(row=1, column=1, sticky="ns", pady=(px(8), 0))
        self.log.configure(yscrollcommand=scroll.set)
        self.log.configure(state="disabled")

        # ---- footer
        foot = tk.Frame(outer, bg=t["bg"])
        foot.grid(row=7, column=0, sticky="ew", pady=(px(10), 0))
        self.status_dot = StatusDot(foot, theme=t, size=px(9))
        self.status_dot.pack(side="left", padx=(px(1), px(7)))
        self.status_label = tk.Label(foot, textvariable=self.status_var, bg=t["bg"],
                                     fg=t["muted"], font=(t["font_ui"], 8))
        self.status_label.pack(side="left")

        ui_box = tk.Frame(foot, bg=t["bg"])
        ui_box.pack(side="right")
        self.ui_plus = RoundButton(ui_box, "+", lambda: self._change_ui_scale(0.05),
                                   theme=t, width=px(26), height=px(20),
                                   radius=px(6), font=(t["font_ui"], 9, "bold"),
                                   bg=t["bg"])
        self.ui_plus.pack(side="right")
        tk.Label(ui_box, text=f"UI {int(round(self.ui_scale * 100))}%",
                 bg=t["bg"], fg=t["muted"], font=(t["font_mono"], 8)).pack(
            side="right", padx=(px(5), px(4)))
        self.ui_minus = RoundButton(ui_box, "-", lambda: self._change_ui_scale(-0.05),
                                    theme=t, width=px(26), height=px(20),
                                    radius=px(6), font=(t["font_ui"], 9, "bold"),
                                    bg=t["bg"])
        self.ui_minus.pack(side="right")
        tk.Label(foot, text="H.264 · CRF 20", bg=t["bg"], fg=t["muted"],
                 font=(t["font_ui"], 8)).pack(side="right", padx=(0, px(14)))

        if self.output_var.get().strip():
            self.output_entry.xview_moveto(1.0)

        self._refresh_meta()
        self._render_log()
        self._render_preview()
        self._sync_controls()
        self._render_status()

    # -------------------------------------------------------- HUD settings

    def _show_input_preview(self) -> None:
        if self.preview_kind != "output":
            return
        if self.input_preview_base is not None:
            self.preview_base = self.input_preview_base
            self.preview_kind = "input"
            self.preview_name_var.set(
                self.video_info.path.name if self.video_info else "No video selected")
        else:
            self.preview_base = None
            self.preview_kind = "unavailable" if self.video_info else "none"

    def _set_unit(self, unit: str) -> None:
        if unit not in ("mph", "kmh"):
            return
        self.preview_token += 1
        self.unit_var.set(unit)
        self.unit_mph.set_selected(unit == "mph")
        self.unit_kmh.set_selected(unit == "kmh")
        self._save_settings()
        self._show_input_preview()
        self._render_preview()

    def _set_hud_style(self, style_id: str) -> None:
        if self.running or style_id not in {sid for sid, _ in hud.styles()}:
            return
        self.preview_token += 1
        self.hud_style_var.set(style_id)
        for sid, btn in getattr(self, "hud_style_buttons", {}).items():
            btn.set_selected(sid == style_id)
        self._save_settings()
        self._show_input_preview()
        self._render_preview()

    def _on_hud_scale(self, value: float, final: bool) -> None:
        self.preview_token += 1
        self.hud_scale = _as_float(value, 1.0, 0.5, 1.5)
        if hasattr(self, "size_slider"):
            self.size_slider.set_value(self.hud_scale)
        self.size_value.configure(text=f"{int(round(self.hud_scale * 100))}%")
        self._show_input_preview()
        self._render_preview()
        if final:
            self._save_settings()

    def _on_hud_opacity(self, value: float, final: bool) -> None:
        self.preview_token += 1
        self.hud_opacity = _as_float(value, 1.0, 0.3, 1.0)
        if hasattr(self, "opacity_slider"):
            self.opacity_slider.set_value(self.hud_opacity)
        self.opacity_value.configure(text=f"{int(round(self.hud_opacity * 100))}%")
        self._show_input_preview()
        self._render_preview()
        if final:
            self._save_settings()

    def _reset_hud(self) -> None:
        if self.running:
            return
        self.preview_token += 1
        self.hud_scale = 1.0
        self.hud_opacity = 1.0
        self.hud_cx, self.hud_cy = DEFAULT_CENTER
        if hasattr(self, "size_slider"):
            self.size_slider.set_value(self.hud_scale)
            self.size_value.configure(text="100%")
        if hasattr(self, "opacity_slider"):
            self.opacity_slider.set_value(self.hud_opacity)
            self.opacity_value.configure(text="100%")
        self._show_input_preview()
        self._render_preview()
        self._save_settings()

    # --- preview drag/resize editor
    def _set_preview_cursor(self, cursor: str) -> None:
        if cursor != self._cursor:
            self._cursor = cursor
            try:
                self.preview_label.configure(cursor=cursor)
            except tk.TclError:
                pass

    def _preview_interactive(self) -> bool:
        return (not self.running and self.preview_kind == "input"
                and self.preview_base is not None
                and self._hud_bbox_preview is not None)

    def _hud_center_preview(self):
        if self._hud_overlay_origin is not None and self._hud_overlay_scale:
            ox, oy = self._hud_overlay_origin
            s = self._hud_overlay_scale
            return (ox + self.hud_cx * 1920 * s,
                    oy + self.hud_cy * 1080 * s)
        box = self._hud_bbox_preview
        if box:
            return ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)
        return (0.0, 0.0)

    def _near_handle(self, x, y) -> bool:
        box = self._hud_bbox_preview
        if not box:
            return False
        r = self.px(14)
        return (x - box[2]) ** 2 + (y - box[3]) ** 2 <= r * r

    def _inside_bbox(self, x, y) -> bool:
        box = self._hud_bbox_preview
        if not box:
            return False
        x1, y1, x2, y2 = box
        return x1 <= x <= x2 and y1 <= y <= y2

    def _preview_press(self, event) -> None:
        if self.running:
            return
        if self.preview_kind == "output":
            self._show_input_preview()
            self._render_preview()
        if not self._preview_interactive():
            return
        self.preview_token += 1
        if self._near_handle(event.x, event.y):
            self._drag_mode = "resize"
            self._drag_scale_start = self.hud_scale
            cx, cy = self._hud_center_preview()
            self._drag_dist_start = max(
                1.0, math.hypot(event.x - cx, event.y - cy))
            self._set_preview_cursor("sizing")
        elif self._inside_bbox(event.x, event.y):
            self._drag_mode = "move"
            self._drag_start = (event.x, event.y)
            self._drag_center_start = (self.hud_cx, self.hud_cy)
            bbox = self._hud_bbox_overlay
            if bbox:
                half_w = (bbox[2] - bbox[0]) / 2.0 / 1920.0
                half_h = (bbox[3] - bbox[1]) / 2.0 / 1080.0
            else:
                half_w = half_h = 0.0
            self._drag_half_norm = (min(0.5, half_w), min(0.5, half_h))
            self._set_preview_cursor("fleur")
        else:
            self._drag_mode = None

    def _preview_drag(self, event) -> None:
        if not self._drag_mode:
            return
        if self._drag_mode == "move":
            if not self._drag_start or not self._drag_center_start:
                return
            os_ = self._hud_overlay_scale or 1.0
            dx = (event.x - self._drag_start[0]) / (os_ * 1920.0)
            dy = (event.y - self._drag_start[1]) / (os_ * 1080.0)
            cx = self._drag_center_start[0] + dx
            cy = self._drag_center_start[1] + dy
            hw, hh = self._drag_half_norm
            self.hud_cx = 0.5 if hw >= 0.5 else max(hw, min(1.0 - hw, cx))
            self.hud_cy = 0.5 if hh >= 0.5 else max(hh, min(1.0 - hh, cy))
        elif self._drag_mode == "resize":
            cx, cy = self._hud_center_preview()
            dist = max(1.0, math.hypot(event.x - cx, event.y - cy))
            new_scale = self._drag_scale_start * dist / self._drag_dist_start
            self.hud_scale = max(0.5, min(1.5, new_scale))
            if hasattr(self, "size_slider"):
                self.size_slider.set_value(self.hud_scale)
                self.size_value.configure(
                    text=f"{int(round(self.hud_scale * 100))}%")
        self._schedule_editor_render()

    def _preview_release(self, event) -> None:
        if not self._drag_mode:
            return
        self._drag_mode = None
        if self._editor_after is not None:
            try:
                self.root.after_cancel(self._editor_after)
            except tk.TclError:
                pass
            self._editor_after = None
        self._editor_dirty = False
        self._render_preview()
        self._save_settings()
        self._preview_hover(event)

    def _preview_hover(self, event) -> None:
        if not self._preview_interactive():
            self._set_preview_cursor("arrow")
            return
        if self._near_handle(event.x, event.y):
            self._set_preview_cursor("sizing")
        elif self._inside_bbox(event.x, event.y):
            self._set_preview_cursor("fleur")
        else:
            self._set_preview_cursor("arrow")

    def _schedule_editor_render(self) -> None:
        self._editor_dirty = True
        if self._editor_after is None:
            self._editor_after = self.root.after(16, self._editor_tick)

    def _editor_tick(self) -> None:
        self._editor_after = None
        if not self._editor_dirty:
            return
        self._editor_dirty = False
        self._render_preview()

    def _dashed_rect(self, draw, box, color, dash, gap, width) -> None:
        x1, y1, x2, y2 = box
        step = max(2, dash + gap)
        for x in range(x1, x2 + 1, step):
            draw.line([(x, y1), (min(x + dash, x2), y1)], fill=color, width=width)
            draw.line([(x, y2), (min(x + dash, x2), y2)], fill=color, width=width)
        for y in range(y1, y2 + 1, step):
            draw.line([(x1, y), (x1, min(y + dash, y2))], fill=color, width=width)
            draw.line([(x2, y), (x2, min(y + dash, y2))], fill=color, width=width)

    def _draw_editor(self, canvas, box) -> None:
        px = self.px
        draw = ImageDraw.Draw(canvas)
        accent = _rgb(self.theme["accent"])
        dash, gap = max(3, px(8)), max(2, px(5))
        self._dashed_rect(draw, box, (0, 0, 0), dash, gap,
                          max(1, px(2) + 1))
        self._dashed_rect(draw, box, accent, dash, gap, max(1, px(2)))
        hx, hy = box[2], box[3]
        r = max(3, px(6))
        draw.ellipse([hx - r - 1, hy - r - 1, hx + r + 1, hy + r + 1],
                     fill=(0, 0, 0))
        draw.ellipse([hx - r, hy - r, hx + r, hy + r], fill=accent,
                     outline=(255, 255, 255), width=max(1, px(1)))

    def _toggle_keep(self, value: bool) -> None:
        self.keep_var.set(value)
        self._save_settings()

    # --------------------------------------------------------------- meta

    def _refresh_meta(self) -> None:
        if not hasattr(self, "chips_frame"):
            return
        t = self.theme
        for child in self.chips_frame.winfo_children():
            child.destroy()
        if self.video_info is not None:
            info = self.video_info
            chips = [f"{info.width}×{info.height}", f"{info.fps:.2f} fps",
                     info.duration_hms]
            if info.rotation:
                chips.append(f"rot {abs(info.rotation)}°")
            chips.append(f"{info.data_packets:,} packets" if info.data_packets
                         else "telemetry track")
            for text in chips:
                Chip(self.chips_frame, text, theme=t, font=(t["font_mono"], 8),
                     scale=self.ui_scale).pack(side="left", padx=(0, self.px(5)))
        elif self.probe_message:
            color = {"muted": t["muted"], "busy": t["accent"], "err": t["err"]}[
                self.probe_kind if self.probe_kind in ("muted", "busy", "err") else "muted"]
            tk.Label(self.chips_frame, text=self.probe_message, bg=t["card"],
                     fg=color, font=(t["font_ui"], 8), anchor="w",
                     justify="left").pack(side="left")

    def _render_log(self) -> None:
        if not hasattr(self, "log"):
            return
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        if self._log_lines:
            self.log.insert("end", "\n".join(self._log_lines) + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _render_status(self) -> None:
        if not hasattr(self, "status_dot"):
            return
        colors = {"idle": self.theme["muted"], "busy": self.theme["accent"],
                  "ok": self.theme["ok"], "err": self.theme["err"]}
        self.status_dot.set_color(colors.get(self._status_kind, self.theme["muted"]))
        self.status_label.configure(
            fg=self.theme["fg"] if self._status_kind in ("ok", "err") else self.theme["muted"])

    def _set_status(self, text: str, kind: str = "idle") -> None:
        self.status_var.set(text)
        self._status_kind = kind
        if hasattr(self, "status_dot"):
            self._render_status()

    # ------------------------------------------------------------- preview

    def _grab_frame(self, path: Path, at: float) -> Image.Image | None:
        width = max(900, self.px(900))
        cmd = [
            speedpipe.FFMPEG, "-v", "error", "-ss", f"{max(0.0, at):.3f}",
            "-i", str(path), "-frames:v", "1", "-vf", f"scale={width}:-2",
            "-f", "image2pipe", "-c:v", "png", "-",
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=45,
                                  creationflags=CREATE_NO_WINDOW if IS_WIN else 0)
        except Exception:
            return None
        if proc.returncode != 0 or not proc.stdout:
            return None
        try:
            return Image.open(io.BytesIO(proc.stdout)).convert("RGB")
        except Exception:
            return None

    def _rounded_preview(self, img: Image.Image) -> Image.Image:
        t = self.theme
        r = t["card_radius"]
        mask = Image.new("L", img.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            [0, 0, img.width - 1, img.height - 1], radius=r, fill=255)
        out = Image.new("RGBA", img.size, (0, 0, 0, 0))
        out.paste(img, (0, 0), mask)
        ImageDraw.Draw(out).rounded_rectangle(
            [0, 0, img.width - 1, img.height - 1], radius=r,
            outline=_rgb(t["preview_border"]), width=max(1, self.px(1)))
        return out

    def _draw_tag(self, img: Image.Image, text: str) -> Image.Image:
        t = self.theme
        px = self.px
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(overlay)
        f = _pil_font(hud.F_LBL, px(10))
        bbox = d.textbbox((0, 0), text, font=f)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        pad, box_h = px(8), px(22)
        x0, y0 = px(14), px(14)
        d.rounded_rectangle([x0, y0, x0 + tw + 2 * pad, y0 + box_h],
                            radius=px(7), fill=t["tag_bg"],
                            outline=_rgb(t["preview_border"]),
                            width=max(1, px(1)))
        d.text((x0 + pad, y0 + (box_h - th) / 2 - bbox[1]), text, font=f,
               fill=_rgb(t["accent"]))
        return Image.alpha_composite(img, overlay)

    def _compose_preview(self, base: Image.Image | None, unit: str,
                         kind: str) -> Image.Image:
        t = self.theme
        px = self.px
        W, H = self.preview_w, self.preview_h
        canvas = Image.new("RGB", (W, H), _rgb(t["card_alt"]))
        dark = (0.2126 * _rgb(t["bg"])[0] + 0.7152 * _rgb(t["bg"])[1]
                + 0.0722 * _rgb(t["bg"])[2]) / 255.0 < 0.5

        if base is not None:
            base = base.convert("RGB")
            cover = ImageOps.fit(base, (W, H), method=Image.LANCZOS)
            cover = cover.filter(ImageFilter.GaussianBlur(max(2, px(18))))
            cover = ImageEnhance.Brightness(cover).enhance(0.38 if dark else 0.78)
            canvas.paste(cover, (0, 0))
            scrim = Image.new("RGBA", (W, H),
                              (0, 0, 0, 55) if dark else (255, 255, 255, 30))
            canvas = Image.alpha_composite(canvas.convert("RGBA"), scrim).convert("RGB")

            inner = ImageOps.contain(base, (W - px(40), H - px(32)),
                                     method=Image.LANCZOS)
            x = (W - inner.width) // 2
            y = (H - inner.height) // 2
            canvas.paste(inner, (x, y))
            ImageDraw.Draw(canvas).rectangle(
                [x, y, x + inner.width - 1, y + inner.height - 1],
                outline=_rgb(t["preview_border"]), width=max(1, px(1)))

            if kind != "output":
                hud_img = hud.render(
                    SAMPLE_NOW, SAMPLE_MIN, SAMPLE_MAX, 0, unit=unit,
                    style=self.hud_style_var.get(), scale=self.hud_scale,
                    opacity=self.hud_opacity,
                    center=(self.hud_cx, self.hud_cy))
                os_ = inner.width / hud_img.width
                hh = max(1, int(round(hud_img.height * os_)))
                hud_scaled = hud_img.resize((inner.width, hh), Image.LANCZOS)
                ox, oy = x, y + inner.height - hh
                canvas.paste(hud_scaled, (ox, oy), hud_scaled)
                bbox = hud_img.getbbox()
                self._hud_overlay_scale = os_
                self._hud_overlay_origin = (ox, oy)
                self._hud_bbox_overlay = bbox
                if bbox and kind == "input":
                    bx1 = ox + bbox[0] * os_
                    by1 = oy + bbox[1] * os_
                    bx2 = ox + bbox[2] * os_
                    by2 = oy + bbox[3] * os_
                    self._hud_bbox_preview = (
                        int(round(bx1)), int(round(by1)),
                        int(round(bx2)), int(round(by2)))
                    self._draw_editor(canvas, self._hud_bbox_preview)
                else:
                    self._hud_bbox_preview = None
            else:
                self._hud_bbox_preview = None
                self._hud_bbox_overlay = None
                self._hud_overlay_scale = None
                self._hud_overlay_origin = None
            tag = "OUTPUT" if kind == "output" else "PREVIEW"
        else:
            d = ImageDraw.Draw(canvas)
            c1, c2 = _rgb(t["card"]), _rgb(t["bg"])
            for yy in range(H):
                d.line([(0, yy), (W, yy)], fill=_mix(c1, c2, yy / max(1, H - 1)))
            grid = _mix(_rgb(t["border"]), c1, 0.55)
            step = max(2, px(36))
            for xx in range(0, W, step):
                d.line([(xx, 0), (xx, H)], fill=grid)
            for yy in range(0, H, step):
                d.line([(0, yy), (W, yy)], fill=grid)
            if kind == "unavailable":
                title, sub, tag = ("PREVIEW UNAVAILABLE",
                                   "The clip is ready to burn.", "NO PREVIEW")
            else:
                title, sub, tag = ("SELECT A DJI CLIP",
                                   "The HUD is burned into the bottom-right corner.",
                                   "NO VIDEO")
            d.text((px(24), px(58)), title,
                   font=_pil_font(hud.F_LBL, px(22)), fill=_rgb(t["accent"]))
            d.text((px(24), px(92)), sub,
                   font=_pil_font(hud.F_LATO, px(12)), fill=_rgb(t["muted"]))
            d.text((px(24), px(112)), "Preview values are illustrative.",
                   font=_pil_font(hud.F_LATO, px(11)), fill=_rgb(t["muted"]))
            self._hud_bbox_preview = None
            self._hud_bbox_overlay = None
            self._hud_overlay_scale = None
            self._hud_overlay_origin = None
            hud_img = hud.render(
                SAMPLE_NOW, SAMPLE_MIN, SAMPLE_MAX, 0, unit=unit,
                style=self.hud_style_var.get(), scale=self.hud_scale,
                opacity=self.hud_opacity,
                center=(self.hud_cx, self.hud_cy))
            scale = (W - px(60)) / hud_img.width
            hud_scaled = hud_img.resize(
                (max(1, int(hud_img.width * scale)),
                 max(1, int(hud_img.height * scale))), Image.LANCZOS)
            canvas.paste(hud_scaled, (W - hud_scaled.width - px(16),
                                      H - hud_scaled.height - px(8)), hud_scaled)

        out = self._rounded_preview(canvas.convert("RGBA"))
        return self._draw_tag(out, tag)

    def _render_preview(self) -> None:
        if not hasattr(self, "preview_label"):
            return
        try:
            img = self._compose_preview(self.preview_base, self.unit_var.get(),
                                        self.preview_kind)
            self._preview_photo = ImageTk.PhotoImage(img)
            self.preview_label.configure(image=self._preview_photo)
        except Exception:
            self._preview_photo = None

        if self.preview_kind == "output":
            self.preview_caption_var.set("Actual frame from the burned video.")
        elif self.preview_kind == "unavailable":
            self.preview_caption_var.set("Preview unavailable for this clip.")
        elif self.preview_base is not None:
            self.preview_caption_var.set(
                "HUD preview · drag to move · corner handle to resize.")
        else:
            self.preview_caption_var.set("Choose a DJI clip to see the HUD preview.")

    def _output_preview_worker(self, path: Path, duration: float, token: int) -> None:
        frame = self._grab_frame(path, duration / 2.0)
        self.queue.put(("output_preview", token, frame))

    # -------------------------------------------------------------- helpers

    def _log(self, text: str) -> None:
        line = f"{time.strftime('%H:%M:%S')}  {text}"
        self._log_lines.append(line)
        if len(self._log_lines) > 1500:
            del self._log_lines[:500]
        if hasattr(self, "log"):
            self.log.configure(state="normal")
            self.log.insert("end", line + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")

    def _clear_log(self) -> None:
        self._log_lines.clear()
        if hasattr(self, "log"):
            self.log.configure(state="normal")
            self.log.delete("1.0", "end")
            self.log.configure(state="disabled")

    def _default_output(self, input_path: str) -> Path:
        source = Path(input_path)
        parent = source.parent
        if not os.access(parent, os.W_OK):
            videos = "Movies" if IS_MAC else "Videos"
            parent = Path.home() / videos
        return parent / (source.stem + "_speed_hud.mp4")

    def _apply_default_output(self) -> None:
        raw = self.input_var.get().strip()
        if not raw or self._output_touched:
            return
        self._setting_output = True
        self.output_var.set(str(self._default_output(raw)))
        self._setting_output = False
        if hasattr(self, "output_entry"):
            self.output_entry.xview_moveto(1.0)

    def _on_output_changed(self, *_args) -> None:
        if not self._setting_output:
            self._output_touched = True

    def _on_input_changed(self, *_args) -> None:
        self.probe_token += 1
        self.preview_token += 1
        if self.probe_after_id is not None:
            try:
                self.root.after_cancel(self.probe_after_id)
            except tk.TclError:
                pass
        self.probe_after_id = self.root.after(550, self._start_probe)
        self.video_info = None
        self.preview_base = None
        self.input_preview_base = None
        self.preview_kind = "none"
        self.preview_name_var.set("No video selected")
        self.probe_message = "Waiting for the file…"
        self.probe_kind = "muted"
        self.stage_var.set("Ready")
        self.detail_var.set("")
        self._set_progress(0.0)
        self._set_status("Choose a DJI video to begin.", "idle")
        self._apply_default_output()
        self._refresh_meta()
        self._render_preview()
        self._sync_controls()

    # ---------------------------------------------------------------- probe

    def _start_probe(self) -> None:
        self.probe_after_id = None
        raw = self.input_var.get().strip()
        if not raw:
            self.video_info = None
            self._sync_controls()
            return
        path = Path(raw).expanduser()
        if not path.is_file():
            self.video_info = None
            self.preview_base = None
            self.preview_kind = "none"
            self.probe_message = f"File not found: {path.name}"
            self.probe_kind = "err"
            self._refresh_meta()
            self._render_preview()
            self._set_status("File not found.", "err")
            self._sync_controls()
            return

        self.probe_token += 1
        token = self.probe_token
        self.video_info = None
        self.probe_message = "Reading video metadata…"
        self.probe_kind = "busy"
        self._refresh_meta()
        self._sync_controls()
        threading.Thread(target=self._probe_worker, args=(str(path), token),
                         daemon=True).start()

    def _probe_worker(self, path: str, token: int) -> None:
        try:
            info = speedpipe.probe_video(path)
        except Exception as exc:
            self.queue.put(("probe_err", token, str(exc)))
            return
        preview = None
        try:
            at = min(max(info.duration * 0.08, 0.3), max(0.3, info.duration - 0.2))
            preview = self._grab_frame(Path(path), at)
        except Exception:
            preview = None
        self.queue.put(("probe_ok", token, info, preview))

    def _probe_ok(self, token: int, info: speedpipe.VideoInfo,
                  preview: Image.Image | None) -> None:
        if token != self.probe_token:
            return
        self.video_info = info
        self.probe_message = ""
        self.probe_kind = "muted"
        self.input_preview_base = preview
        self.preview_base = preview
        self.preview_kind = "input" if preview is not None else "unavailable"
        self.preview_name_var.set(info.path.name)
        self._refresh_meta()
        self._render_preview()
        self._set_status("Ready to burn.", "ok")
        self._sync_controls()

    def _probe_err(self, token: int, message: str) -> None:
        if token != self.probe_token:
            return
        first, *rest = message.splitlines()
        self.video_info = None
        self.preview_base = None
        self.input_preview_base = None
        self.preview_kind = "none"
        self.preview_name_var.set("No video selected")
        self.probe_message = first
        self.probe_kind = "err"
        for line in rest:
            self._log(line)
        self._log(first)
        self._refresh_meta()
        self._render_preview()
        self._set_status("This file can't be processed.", "err")
        self._sync_controls()

    # ------------------------------------------------------------- browsing

    def _browse_input(self) -> None:
        path = filedialog.askopenfilename(
            title="Choose a DJI video",
            filetypes=[("Video files", "*.mp4 *.MP4 *.mov *.MOV"), ("All files", "*")],
        )
        if path:
            self.input_var.set(path)
            self.input_entry.icursor("end")

    def _browse_output(self) -> None:
        current = self.output_var.get().strip()
        initial_dir = str(Path(current).parent) if current else str(Path.home() / "Videos")
        initial_file = Path(current).name if current else ""
        path = filedialog.asksaveasfilename(
            title="Save burned video as",
            initialdir=initial_dir,
            initialfile=initial_file,
            defaultextension=".mp4",
            filetypes=[("MP4 video", "*.mp4")],
        )
        if path:
            self.output_var.set(path)
            self.output_entry.xview_moveto(1.0)

    # ------------------------------------------------------------- controls

    def _sync_controls(self) -> None:
        if not hasattr(self, "start_btn"):
            return
        busy = self.running
        has_info = self.video_info is not None
        self.start_btn.set_enabled(has_info and not busy)
        self.start_btn.set_text("Burning…" if busy else "Start burn")
        self.cancel_btn.set_enabled(busy)
        self.open_btn.set_enabled(self._last_output is not None)
        for btn in getattr(self, "theme_buttons", []):
            btn.set_enabled(not busy)
        self.input_entry.state(["disabled"] if busy else ["!disabled"])
        self.input_browse.set_enabled(not busy)
        self.output_entry.state(["disabled"] if busy else ["!disabled"])
        self.output_browse.set_enabled(not busy)
        self.unit_mph.set_enabled(not busy)
        self.unit_kmh.set_enabled(not busy)
        for btn in getattr(self, "hud_style_buttons", {}).values():
            btn.set_enabled(not busy)
        self.size_slider.set_enabled(not busy)
        self.opacity_slider.set_enabled(not busy)
        self.reset_btn.set_enabled(not busy)
        self.keep_toggle.set_enabled(not busy)
        self.clear_btn.set_enabled(not busy)
        self.ui_minus.set_enabled(not busy)
        self.ui_plus.set_enabled(not busy)

    def _set_progress(self, fraction: float) -> None:
        self._progress_frac = max(0.0, min(1.0, fraction))
        if hasattr(self, "progress"):
            self.progress.set(self._progress_frac)
        self.pct_var.set(f"{self._progress_frac * 100:.0f}%")

    def _progress(self, stage: str, fraction: float, detail: str) -> None:
        if stage != self._last_stage:
            self._last_stage = stage
            self.stage_var.set(stage)
            self.detail_var.set(detail)
            self._log(stage + (f" — {detail}" if detail else ""))
        else:
            self.detail_var.set(detail)
        self._set_progress(fraction)

    # --------------------------------------------------------------- start

    def _start(self) -> None:
        if self.running:
            return
        raw_input = self.input_var.get().strip()
        raw_output = self.output_var.get().strip()
        if not raw_input or not raw_output:
            messagebox.showwarning(APP_TITLE, "Choose an input video and an output file first.")
            return
        if self.video_info is None or \
                Path(self.video_info.path).resolve() != Path(raw_input).expanduser().resolve():
            messagebox.showwarning(APP_TITLE,
                                   "Still reading the input file — try again in a moment.")
            return
        output = Path(raw_output).expanduser()
        if output.suffix.lower() != ".mp4":
            output = output.with_name(output.name + ".mp4")
            self.output_var.set(str(output))
        if output.exists() and not messagebox.askyesno(
                APP_TITLE, f"{output.name} already exists.\n\nOverwrite it?"):
            return

        self._save_settings()
        self.cancel_event = threading.Event()
        self.running = True
        self._last_stage = ""
        self._last_output = None
        self._set_progress(0.0)
        self.stage_var.set("Reading video metadata")
        self.detail_var.set(self.video_info.path.name)
        self._set_status("Running — you can cancel at any time.", "busy")
        self._sync_controls()
        self._log("Starting: " + str(self.video_info.path))

        threading.Thread(
            target=self._pipeline_worker,
            args=(self.video_info, output, self.unit_var.get(),
                  self.hud_style_var.get(), self.hud_scale, self.hud_opacity,
                  (self.hud_cx, self.hud_cy), bool(self.keep_var.get()),
                  self.cancel_event),
            daemon=True,
        ).start()

    def _pipeline_worker(self, info, output: Path, unit: str, hud_style: str,
                         hud_scale: float, hud_opacity: float,
                         hud_center: tuple[float, float], keep: bool,
                         cancel_event: threading.Event) -> None:
        def progress(stage: str, fraction: float, detail: str) -> None:
            self.queue.put(("progress", stage, fraction, detail))

        def log(message: str) -> None:
            self.queue.put(("log", message))

        try:
            result = speedpipe.run_pipeline(
                info.path, output, unit=unit, hud_style=hud_style,
                hud_scale=hud_scale, hud_opacity=hud_opacity,
                hud_center=hud_center, keep_intermediates=keep,
                progress=progress, cancelled=cancel_event.is_set, log=log,
            )
            self.queue.put(("done", result))
        except speedpipe.Cancelled:
            self.queue.put(("cancelled",))
        except Exception as exc:
            self.queue.put(("failed", str(exc)))

    def _cancel(self) -> None:
        if not self.running:
            return
        self.cancel_event.set()
        self._set_status("Cancelling…", "busy")
        self.cancel_btn.set_enabled(False)

    def _finish_run(self) -> None:
        self.running = False
        self._sync_controls()

    def _done(self, result: speedpipe.PipelineResult) -> None:
        self._last_output = result.output
        self._finish_run()
        self.stage_var.set("Done")
        self.detail_var.set("")
        self._set_progress(1.0)
        stats = result.stats
        self._set_status(
            f"Done in {speedpipe.fmt_hms(result.elapsed)}  ·  "
            f"{stats.distance_km:.1f} km  ·  max {stats.max_mph:.1f} mph", "ok")
        self._log(f"Done in {speedpipe.fmt_hms(result.elapsed)}")
        self._log(f"Output: {result.output}")
        self._log(
            f"Max {stats.max_mph:.1f} mph · min {stats.min_mph:.1f} mph · "
            f"avg moving {stats.avg_moving_mph:.1f} mph · {stats.distance_km:.1f} km")
        if result.work_dir.exists():
            self._log(f"Intermediates: {result.work_dir}")
        threading.Thread(
            target=self._output_preview_worker,
            args=(result.output, result.info.duration, self.preview_token),
            daemon=True,
        ).start()

    def _failed(self, message: str) -> None:
        self._finish_run()
        self.stage_var.set("Failed")
        self.detail_var.set("")
        self._set_status("Failed — see the log.", "err")
        for line in message.splitlines():
            self._log(line)
        messagebox.showerror(APP_TITLE, message.splitlines()[0] if message else "Failed.")

    def _cancelled(self) -> None:
        self._finish_run()
        self.stage_var.set("Cancelled")
        self.detail_var.set("")
        self._set_status("Cancelled.", "idle")
        self._log("Cancelled by user.")

    def _open_folder(self) -> None:
        target = self._last_output
        folder = target.parent if target else Path(self.output_var.get()).expanduser().parent
        if folder.exists():
            try:
                _open_path(folder)
            except Exception:
                pass

    # ---------------------------------------------------------------- poll

    def _poll(self) -> None:
        try:
            while True:
                message = self.queue.get_nowait()
                kind = message[0]
                if kind == "probe_ok":
                    self._probe_ok(message[1], message[2], message[3])
                elif kind == "probe_err":
                    self._probe_err(message[1], message[2])
                elif kind == "progress":
                    _, stage, fraction, detail = message
                    self._progress(stage, fraction, detail)
                elif kind == "log":
                    self._log(message[1])
                elif kind == "done":
                    self._done(message[1])
                elif kind == "failed":
                    self._failed(message[1])
                elif kind == "cancelled":
                    self._cancelled()
                elif kind == "output_preview":
                    _, token, frame = message
                    if token == self.preview_token and frame is not None:
                        self.preview_base = frame
                        self.preview_kind = "output"
                        if self._last_output is not None:
                            self.preview_name_var.set(self._last_output.name)
                        self._sync_controls()
                        self._render_preview()
        except queue.Empty:
            pass
        self.root.after(80, self._poll)

    def _wait_for_close(self) -> None:
        if self.running:
            self.root.after(100, self._wait_for_close)
        else:
            self.root.destroy()

    def _on_close(self) -> None:
        if self._closing:
            return
        if self.running:
            if not messagebox.askyesno(APP_TITLE,
                                       "A burn is running.\n\nCancel it and quit?"):
                return
            self._closing = True
            self.cancel_event.set()
            self._set_status("Cancelling…", "busy")
            self.root.after(100, self._wait_for_close)
        else:
            self.root.destroy()


def main() -> int:
    if IS_WIN:
        try:
            import ctypes
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
            except Exception:
                ctypes.windll.user32.SetProcessDPIAware()  # type: ignore[attr-defined]
        except Exception:
            pass
    root = tk.Tk()
    SpeedHudApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
