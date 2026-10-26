"""Speed HUD renderers.

Five looks, all returning a 1920x1080 RGBA overlay:

  classic  current bottom-right panel (pixel-identical to the original)
  gauge    circular speedometer with needle + digital readout
  strip    wide bottom-right speed bar with a linear scale
  minimal  large floating number, no panel
  racing   cut-corner digital panel with segmented bar and checkered strip

The current value blanks on a GPS dropout; session min/max stay visible.
"""

import math
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1920, 1080
PW, PH = 460, 304
MARGIN = 56
PX = W - MARGIN - PW      # bottom-right
PY = H - MARGIN - PH

FONT_DIR = Path(__file__).resolve().parent / "assets" / "fonts"


def _find_font(filename, *fallbacks):
    local = FONT_DIR / filename
    if local.exists():
        return str(local)
    for candidate in fallbacks:
        try:
            if Path(candidate).exists():
                return str(candidate)
        except OSError:
            pass
    return None


# Bundled so Windows/macOS/Linux render the burned HUD identically.
F_BLACK = _find_font(
    "Hack-Bold.ttf",
    "/usr/share/fonts/truetype/hack/Hack-Bold.ttf",
    "C:/Windows/Fonts/consolab.ttf", "C:/Windows/Fonts/arialbd.ttf",
    "/System/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
)
F_MONO = _find_font(
    "Hack-Regular.ttf",
    "/usr/share/fonts/truetype/hack/Hack-Regular.ttf",
    "C:/Windows/Fonts/consola.ttf", "C:/Windows/Fonts/arial.ttf",
    "/System/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
)
F_LBL = _find_font(
    "Lato-Bold.ttf",
    "/usr/share/fonts/truetype/lato/Lato-Bold.ttf",
    "C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)
F_LATO = _find_font(
    "Lato-Regular.ttf",
    "/usr/share/fonts/truetype/lato/Lato-Regular.ttf",
    "C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)
F_VAL = F_BLACK

ACCENT = (255, 138, 61, 255)
ACCENT_DIM = (255, 138, 61, 110)
WHITE = (255, 255, 255, 255)
MUTED = (142, 151, 163, 255)
SECOND = (168, 176, 187, 255)
VALUE = (226, 231, 236, 255)
DIM = (110, 118, 128, 255)

RACE_YELLOW = (255, 212, 0, 255)
RACE_RED = (255, 77, 61, 255)
RACE_DARK = (8, 8, 8, 225)
RACE_TRACK = (38, 38, 38, 255)

STYLES = [
    ("classic", "Classic"),
    ("gauge", "Gauge"),
    ("strip", "Bar"),
    ("minimal", "Minimal"),
    ("racing", "Racing"),
]

POSITIONS = [
    "top-left", "top-center", "top-right",
    "middle-left", "center", "middle-right",
    "bottom-left", "bottom-center", "bottom-right",
]

# Center of the native classic panel. Used as the default free-position center.
DEFAULT_CENTER = (1634.0 / W, 872.0 / H)


def _apply_transform(img, scale=1.0, opacity=1.0, position="bottom-right",
                     center=None):
    """Scale / fade / relocate the rendered HUD inside the 1920x1080 frame."""
    try:
        scale = max(0.05, float(scale))
    except (TypeError, ValueError):
        scale = 1.0
    try:
        opacity = max(0.0, min(1.0, float(opacity)))
    except (TypeError, ValueError):
        opacity = 1.0
    if position not in POSITIONS:
        position = "bottom-right"

    free = center is not None
    if free:
        try:
            cx, cy = float(center[0]), float(center[1])
        except (TypeError, ValueError, IndexError):
            free = False
            cx = cy = 0.0
        else:
            cx = max(0.0, min(1.0, cx))
            cy = max(0.0, min(1.0, cy))

    at_default = (not free) or (
        abs(cx - DEFAULT_CENTER[0]) < 1e-4 and
        abs(cy - DEFAULT_CENTER[1]) < 1e-4)
    native = abs(scale - 1.0) < 1e-3 and position == "bottom-right" and at_default
    if native:
        if opacity >= 0.999:
            return img
        out = img.copy()
        out.putalpha(out.getchannel("A").point(lambda a: int(round(a * opacity))))
        return out

    bbox = img.getbbox()
    if bbox is None:
        return img
    content = img.crop(bbox)
    if free:
        fit = min(W / content.width, H / content.height)
    else:
        fit = min((W - 2 * MARGIN) / content.width,
                  (H - 2 * MARGIN) / content.height)
    factor = min(scale, fit)
    if factor <= 0:
        return img
    if abs(factor - 1.0) > 1e-3:
        content = content.resize(
            (max(1, int(round(content.width * factor))),
             max(1, int(round(content.height * factor)))),
            Image.LANCZOS)
    if opacity < 0.999:
        content.putalpha(
            content.getchannel("A").point(lambda a: int(round(a * opacity))))

    if free:
        x = cx * W - content.width / 2.0
        y = cy * H - content.height / 2.0
        x = max(0.0, min(W - content.width, x))
        y = max(0.0, min(H - content.height, y))
    else:
        if position == "center":
            vertical, horizontal = "middle", "center"
        else:
            vertical, horizontal = position.split("-", 1)

        if horizontal == "left":
            x = MARGIN
        elif horizontal == "center":
            x = (W - content.width) // 2
        else:
            x = W - MARGIN - content.width
        if vertical == "top":
            y = MARGIN
        elif vertical == "middle":
            y = (H - content.height) // 2
        else:
            y = H - MARGIN - content.height

    out = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    out.paste(content, (int(round(x)), int(round(y))), content)
    return out


@lru_cache(maxsize=None)
def font(path, size):
    if path:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def styles():
    """Ordered (id, label) pairs for the GUI."""
    return list(STYLES)


def tracked(draw, xy, text, f, fill, spacing=2.6):
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=f, fill=fill)
        x += draw.textlength(ch, font=f) + spacing


def _tracked_width(draw, text, f, spacing=2.6):
    return sum(draw.textlength(ch, font=f) for ch in text) + spacing * (len(text) - 1)


def _tracked_right(draw, right_x, y, text, f, fill, spacing=2.6):
    tracked(draw, (right_x - _tracked_width(draw, text, f, spacing), y),
            text, f, fill, spacing)


def hms(sec):
    sec = int(sec)
    return "%d:%02d:%02d" % (sec // 3600, (sec % 3600) // 60, sec % 60)


def _unit_info(unit):
    if unit == "kmh":
        return dict(factor=3.6, label="KM/H", other=2.23694,
                    other_label="mph", scale=200, major=40, minor=20)
    return dict(factor=2.23694, label="MPH", other=3.6,
                other_label="km/h", scale=120, major=20, minor=10)


def _speed_text(now_mps, info, nofix):
    return "--.-" if nofix else "%.1f" % (now_mps * info["factor"])


def _secondary_text(now_mps, info, nofix):
    if nofix:
        return "no gps"
    return "%.1f %s" % (now_mps * info["other"], info["other_label"])


def _clamp01(v):
    return max(0.0, min(1.0, v))


def _bar(draw, box, radius, fill):
    x1, y1, x2, y2 = box
    if x2 - x1 < 1:
        return
    r = max(0, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    draw.rounded_rectangle([x1, y1, x2, y2], radius=r, fill=fill)


_MINIMAL_SCRIM = None


def _minimal_scrim():
    global _MINIMAL_SCRIM
    if _MINIMAL_SCRIM is None:
        # Compact soft scrim behind the number, so free drag/resize moves a
        # predictable box rather than a screen-wide gradient.
        sw, sh = 660, 430
        small = Image.new("L", (64, 64))
        pix = small.load()
        for y in range(64):
            ay = (y / 63.0) ** 1.5
            for x in range(64):
                ax = (x / 63.0) ** 1.6
                pix[x, y] = 2 + int(140 * ax * ay)
        alpha = small.resize((sw, sh), Image.BILINEAR)
        scrim = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
        scrim.putalpha(alpha)
        _MINIMAL_SCRIM = scrim
    return _MINIMAL_SCRIM


# ------------------------------------------------------------------ classic

def _render_classic(now_mps, min_mps, max_mps, sec, nofix, unit):
    if unit == "kmh":
        factor = 3.6
        primary_label = "KM/H"
        secondary = "%.1f mph" % (now_mps * 2.23694)
    else:
        factor = 2.23694
        primary_label = "MPH"
        secondary = "%.1f km/h" % (now_mps * 3.6)

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # panel
    d.rounded_rectangle([PX, PY, PX + PW, PY + PH], radius=24, fill=(10, 12, 16, 206))
    d.rounded_rectangle([PX, PY, PX + PW, PY + PH], radius=24, outline=(255, 255, 255, 34), width=1)
    # accent bar
    d.rounded_rectangle([PX + 1, PY + 26, PX + 5, PY + 286], radius=2, fill=ACCENT)

    pad = 34
    right = PX + PW - pad

    tracked(d, (PX + pad, PY + 26), "SPEED", font(F_LBL, 17), MUTED, 3.0)
    lbl_unit = font(F_LBL, 17)
    d.text((right - d.textlength(primary_label, font=lbl_unit), PY + 26), primary_label,
           font=lbl_unit, fill=ACCENT)

    # big number
    bigf = font(F_BLACK, 108)
    txt = "--.-" if nofix else "%.1f" % (now_mps * factor)
    bb = d.textbbox((0, 0), txt, font=bigf)
    d.text((right - (bb[2] - bb[0]), PY + 62 - bb[1]), txt, font=bigf,
           fill=(110, 118, 128, 255) if nofix else WHITE)

    # secondary unit
    second_line = "no gps" if nofix else secondary
    f2 = font(F_MONO, 23)
    d.text((right - d.textlength(second_line, font=f2), PY + 180), second_line, font=f2, fill=SECOND)

    d.line([PX + pad, PY + 216, right, PY + 216], fill=(255, 255, 255, 30), width=1)

    fl = font(F_LBL, 16)
    fv = font(F_VAL, 26)
    for i, (lab, val) in enumerate((("MIN", min_mps), ("MAX", max_mps))):
        y = PY + 230 + i * 32
        tracked(d, (PX + pad, y + 5), lab, fl, MUTED, 2.4)
        v = "%.1f" % (val * factor)
        d.text((right - d.textlength(v, font=fv), y), v, font=fv, fill=(226, 231, 236, 255))

    return img


# -------------------------------------------------------------------- gauge

def _render_gauge(now_mps, min_mps, max_mps, sec, nofix, unit):
    info = _unit_info(unit)
    factor = info["factor"]
    pw, ph = 560, 600
    px, py = W - MARGIN - pw, H - MARGIN - ph
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # panel
    d.rounded_rectangle([px, py, px + pw, py + ph], radius=34,
                        fill=(10, 12, 16, 210))
    d.rounded_rectangle([px, py, px + pw, py + ph], radius=34,
                        outline=(255, 255, 255, 36), width=1)

    cx, cy, R = px + pw / 2, py + 270, 175

    # top labels
    f_lbl = font(F_LBL, 14)
    label_w = _tracked_width(d, "SPEED", f_lbl, 2.4)
    tracked(d, (cx - label_w / 2, py + 20), "SPEED", f_lbl, MUTED, 2.4)

    fl = font(F_LBL, 13)
    fv = font(F_BLACK, 20)
    d.text((px + 28, py + 52), "MIN", font=fl, fill=MUTED)
    d.text((px + 58, py + 46), "%.1f" % (min_mps * factor), font=fv, fill=VALUE)
    max_txt = "%.1f" % (max_mps * factor)
    mw = d.textlength(max_txt, font=fv)
    d.text((px + pw - 28 - mw, py + 46), max_txt, font=fv, fill=VALUE)
    d.text((px + pw - 28 - mw - 34, py + 52), "MAX", font=fl, fill=MUTED)

    # arc background
    box = [cx - R, cy - R, cx + R, cy + R]
    d.arc(box, start=140, end=400, fill=(55, 62, 72, 255), width=14)

    frac = _clamp01((now_mps * factor) / info["scale"]) if not nofix else 0.0

    # ticks and scale labels
    for value in range(0, info["scale"] + 1, info["minor"]):
        major = (value % info["major"] == 0)
        deg = 140 + 260 * (value / info["scale"])
        rad = math.radians(deg)
        r1 = R - (30 if major else 23)
        r2 = R - 8
        d.line(
            [cx + r1 * math.cos(rad), cy + r1 * math.sin(rad),
             cx + r2 * math.cos(rad), cy + r2 * math.sin(rad)],
            fill=WHITE if major else (105, 113, 125, 255),
            width=3 if major else 1,
        )
        if major:
            lx = cx + (R - 48) * math.cos(rad)
            ly = cy + (R - 48) * math.sin(rad)
            d.text((lx, ly), str(value), font=font(F_MONO, 13),
                   fill=MUTED, anchor="mm")

    # value arc + needle
    if not nofix:
        end_deg = 140 + 260 * frac
        if end_deg > 140.5:
            d.arc(box, start=140, end=end_deg, fill=ACCENT, width=14)

        deg = end_deg
        rad = math.radians(deg)
        dx, dy = math.cos(rad), math.sin(rad)
        tip = (cx + (R - 34) * dx, cy + (R - 34) * dy)
        bx, by = -dy, dx
        base1 = (cx + 8 * bx, cy + 8 * by)
        base2 = (cx - 8 * bx, cy - 8 * by)
        d.polygon([(tip[0] + 2, tip[1] + 3), (base1[0] + 2, base1[1] + 3),
                   (base2[0] + 2, base2[1] + 3)], fill=(0, 0, 0, 130))
        d.polygon([tip, base1, base2], fill=WHITE)
        d.ellipse([cx - 11, cy - 11, cx + 11, cy + 11],
                  fill=ACCENT, outline=WHITE, width=2)
    else:
        d.ellipse([cx - 11, cy - 11, cx + 11, cy + 11],
                  fill=(82, 89, 99, 255), outline=(170, 176, 186, 255), width=2)

    # digital readout
    bigf = font(F_BLACK, 78)
    d.text((cx, py + 500), _speed_text(now_mps, info, nofix), font=bigf,
           fill=DIM if nofix else WHITE, anchor="mm")
    d.text((cx, py + 542), info["label"], font=font(F_LBL, 17),
           fill=ACCENT, anchor="mm")
    d.text((cx, py + 568), _secondary_text(now_mps, info, nofix),
           font=font(F_MONO, 15), fill=SECOND, anchor="mm")

    return img


# -------------------------------------------------------------------- strip

def _render_strip(now_mps, min_mps, max_mps, sec, nofix, unit):
    info = _unit_info(unit)
    factor = info["factor"]
    pw, ph = 1100, 180
    px, py = W - MARGIN - pw, H - MARGIN - ph
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    d.rounded_rectangle([px, py, px + pw, py + ph], radius=26,
                        fill=(10, 12, 16, 208))
    d.rounded_rectangle([px, py, px + pw, py + ph], radius=26,
                        outline=(255, 255, 255, 34), width=1)
    d.rounded_rectangle([px + 1, py + 24, px + 5, py + 156], radius=2, fill=ACCENT)

    tracked(d, (px + 36, py + 20), "SPEED", font(F_LBL, 14), MUTED, 2.8)

    bigf = font(F_BLACK, 84)
    d.text((px + 36, py + 36), _speed_text(now_mps, info, nofix),
           font=bigf, fill=DIM if nofix else WHITE)

    unit_f = font(F_LBL, 16)
    unit_w = d.textlength(info["label"], font=unit_f)
    d.text((px + 36, py + 132), info["label"], font=unit_f, fill=ACCENT)
    d.text((px + 36 + unit_w + 10, py + 134),
           _secondary_text(now_mps, info, nofix),
           font=font(F_MONO, 14), fill=SECOND)

    x1, x2 = px + 420, px + pw - 36
    yc, th = py + 62, 16
    _bar(d, [x1, yc - th / 2, x2, yc + th / 2], th / 2, (38, 44, 54, 255))
    d.rounded_rectangle([x1, yc - th / 2, x2, yc + th / 2], radius=th / 2,
                        outline=(255, 255, 255, 22), width=1)

    frac = _clamp01((now_mps * factor) / info["scale"]) if not nofix else 0.0
    if frac > 0:
        fill_w = (x2 - x1) * frac
        _bar(d, [x1, yc - th / 2, x1 + max(fill_w, th), yc + th / 2],
             th / 2, ACCENT)
        mx = x1 + fill_w
        d.polygon([(mx - 6, yc - th / 2 - 10), (mx + 6, yc - th / 2 - 10),
                   (mx, yc - th / 2 - 2)], fill=WHITE)

    # scale ticks
    for value in range(0, info["scale"] + 1, info["minor"]):
        major = (value % info["major"] == 0)
        tx = x1 + (x2 - x1) * (value / info["scale"])
        d.line([tx, yc + th / 2 + 2, tx, yc + th / 2 + (8 if major else 5)],
               fill=(150, 158, 170, 255) if major else (85, 92, 102, 255),
               width=1)
        if major:
            d.text((tx, yc + th / 2 + 12), str(value),
                   font=font(F_MONO, 13), fill=MUTED, anchor="ma")

    fl = font(F_LBL, 13)
    fv = font(F_BLACK, 19)
    d.text((x1, py + 128), "MIN", font=fl, fill=MUTED)
    d.text((x1 + 34, py + 122), "%.1f" % (min_mps * factor), font=fv, fill=VALUE)
    d.text((x1 + 190, py + 128), "MAX", font=fl, fill=MUTED)
    d.text((x1 + 224, py + 122), "%.1f" % (max_mps * factor), font=fv, fill=VALUE)
    if nofix:
        d.text((x2, py + 128), "NO GPS", font=font(F_LBL, 12),
               fill=ACCENT, anchor="ra")

    return img


# ------------------------------------------------------------------ minimal

def _render_minimal(now_mps, min_mps, max_mps, sec, nofix, unit):
    info = _unit_info(unit)
    factor = info["factor"]
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    scrim = _minimal_scrim()
    img.paste(scrim, (W - scrim.width, H - scrim.height), scrim)
    d = ImageDraw.Draw(img)

    right = W - 76
    _tracked_right(d, right, 696, "SPEED", font(F_LBL, 15), MUTED, 2.8)
    unit_txt = info["label"]
    unit_f = font(F_LBL, 26)
    d.text((right - d.textlength(unit_txt, font=unit_f), 720), unit_txt,
           font=unit_f, fill=ACCENT)

    txt = _speed_text(now_mps, info, nofix)
    bigf = font(F_BLACK, 190)
    tw = d.textlength(txt, font=bigf)
    d.text((right + 4, 786), txt, font=bigf, fill=(0, 0, 0, 170), anchor="ra")
    d.text((right, 782), txt, font=bigf,
           fill=DIM if nofix else WHITE, anchor="ra")

    # thin accent rule above the number
    d.rounded_rectangle([right - tw, 766, right, 770], radius=2, fill=ACCENT)

    d.text((right, 978), _secondary_text(now_mps, info, nofix),
           font=font(F_MONO, 20), fill=SECOND, anchor="ra")

    fl = font(F_LBL, 18)
    fv = font(F_BLACK, 24)
    parts = [
        ("MIN", fl, MUTED), (" %.1f" % (min_mps * factor), fv, VALUE),
        ("     MAX", fl, MUTED), (" %.1f" % (max_mps * factor), fv, VALUE),
    ]
    total = sum(d.textlength(t, font=f) for t, f, _ in parts)
    x = right - total
    for text, f, color in parts:
        d.text((x, 1008), text, font=f, fill=color)
        x += d.textlength(text, font=f)

    return img


# ------------------------------------------------------------------- racing

def _render_racing(now_mps, min_mps, max_mps, sec, nofix, unit):
    info = _unit_info(unit)
    factor = info["factor"]
    pw, ph, cut = 540, 300, 26
    px, py = W - MARGIN - pw, H - MARGIN - ph
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    points = [
        (px + cut, py), (px + pw, py), (px + pw, py + ph - cut),
        (px + pw - cut, py + ph), (px, py + ph), (px, py + cut),
    ]
    d.polygon(points, fill=RACE_DARK)
    d.line(points + [points[0]], fill=RACE_YELLOW, width=2)

    # checkered strip along the top
    sx, sy, cell = px + cut + 12, py + 12, 12
    for i in range(20):
        x = sx + i * cell
        if x + cell > px + pw - 12:
            break
        color = WHITE if i % 2 else (0, 0, 0, 255)
        d.rectangle([x, sy, x + cell - 1, sy + cell - 1], fill=color)

    tracked(d, (px + 26, py + 34), "SPEED", font(F_LBL, 13), RACE_YELLOW, 2.8)

    bigf = font(F_BLACK, 116)
    d.text((px + 26, py + 56), _speed_text(now_mps, info, nofix),
           font=bigf, fill=DIM if nofix else RACE_YELLOW)

    unit_f = font(F_LBL, 17)
    unit_w = d.textlength(info["label"], font=unit_f)
    d.text((px + 26, py + 188), info["label"], font=unit_f, fill=RACE_RED)
    d.text((px + 26 + unit_w + 10, py + 190),
           _secondary_text(now_mps, info, nofix),
           font=font(F_MONO, 14), fill=MUTED)

    # segmented speed bar
    n, gap = 24, 3
    bx1, bx2 = px + 26, px + pw - 26
    seg_w = (bx2 - bx1 - (n - 1) * gap) / n
    by, bh = py + 220, 18
    frac = _clamp01((now_mps * factor) / info["scale"]) if not nofix else 0.0
    for i in range(n):
        x = bx1 + i * (seg_w + gap)
        filled = (i + 1) / n <= frac
        if filled:
            color = RACE_RED if i >= n - 4 else RACE_YELLOW
        else:
            color = RACE_TRACK
        d.rectangle([x, by, x + seg_w, by + bh], fill=color)

    fl = font(F_LBL, 13)
    fv = font(F_BLACK, 19)
    d.text((px + 26, py + 258), "MIN", font=fl, fill=RACE_YELLOW)
    d.text((px + 60, py + 252), "%.1f" % (min_mps * factor), font=fv, fill=WHITE)
    d.text((px + 250, py + 258), "MAX", font=fl, fill=RACE_YELLOW)
    d.text((px + 284, py + 252), "%.1f" % (max_mps * factor), font=fv, fill=WHITE)

    return img


# --------------------------------------------------------------- dispatcher

def _render_native(now_mps, min_mps, max_mps, sec, nofix, unit, style):
    style = (style or "classic").lower()
    if style == "gauge":
        return _render_gauge(now_mps, min_mps, max_mps, sec, nofix, unit)
    if style == "strip":
        return _render_strip(now_mps, min_mps, max_mps, sec, nofix, unit)
    if style == "minimal":
        return _render_minimal(now_mps, min_mps, max_mps, sec, nofix, unit)
    if style == "racing":
        return _render_racing(now_mps, min_mps, max_mps, sec, nofix, unit)
    return _render_classic(now_mps, min_mps, max_mps, sec, nofix, unit)


@lru_cache(maxsize=16)
def _render_native_cached(now_mps, min_mps, max_mps, sec, nofix, unit, style):
    return _render_native(now_mps, min_mps, max_mps, sec, nofix, unit, style)


def render_native(now_mps, min_mps, max_mps, sec, nofix=False, unit="mph",
                  style="classic", cache=True):
    """Untransformed style render (1920x1080 RGBA).

    ``cache=True`` is for the GUI preview, whose sample values/style/unit are
    stable. The burn uses cache=False because every second has new values.
    """
    style = (style or "classic").lower()
    if cache:
        return _render_native_cached(now_mps, min_mps, max_mps, sec,
                                     bool(nofix), unit, style)
    return _render_native(now_mps, min_mps, max_mps, sec, nofix, unit, style)


def render(now_mps, min_mps, max_mps, sec, nofix=False, big_only=False,
           unit="mph", style="classic", scale=1.0, opacity=1.0,
           position="bottom-right", center=None, cache=False):
    img = render_native(now_mps, min_mps, max_mps, sec, nofix=nofix,
                        unit=unit, style=style, cache=cache)
    return _apply_transform(img, scale, opacity, position, center)
