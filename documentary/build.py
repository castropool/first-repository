"""AMERICA — A Short History.

A five-minute, documentary-style film rendered entirely from code:
narration from an offline text-to-speech voice (Festival HTS), an
original synthesized score, animated maps built from real geometry
(us-atlas / world-atlas, Natural Earth + US Census data), period
documents, census charts, flags, film grain and slow camera moves.

    python3 documentary/build.py                 # full render -> documentary/american_history.mp4
    python3 documentary/build.py --stills 12 40  # render single frames (seconds) to the build dir

Requires: python3, numpy, Pillow, ffmpeg, festival + festvox-us-slt-hts,
fonts-ebgaramond.
"""

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import wave
from multiprocessing import get_context

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
BUILD = os.path.join(HERE, "build")
OUT = os.path.join(HERE, "american_history.mp4")
SRT = os.path.join(HERE, "american_history.srt")

W, H, FPS = 1920, 1080, 24
SR = 44100
TARGET_SECONDS = 300.0

# --------------------------------------------------------------------------
# Look
# --------------------------------------------------------------------------

GAR_DIR = "/usr/share/fonts/opentype/ebgaramond"
_fonts = {}


def F(kind, size):
    key = (kind, size)
    if key not in _fonts:
        name = {
            "reg": "EBGaramond12-Regular.otf",
            "it": "EBGaramond12-Italic.otf",
            "bold": "EBGaramond12-Bold.otf",
            "sc": "EBGaramond12-AllSC.otf",
        }[kind]
        _fonts[key] = ImageFont.truetype(os.path.join(GAR_DIR, name), size)
    return _fonts[key]


PAPER = (229, 214, 182)
INK = (44, 33, 24)
CREAM = (238, 228, 208)
DIM = (150, 140, 124)
GOLD = (212, 168, 92)
RED = (176, 52, 44)
BLUE = (78, 108, 156)
OCEAN = (16, 22, 30)
LAND = (62, 60, 54)
FLAG_RED = (178, 34, 52)
FLAG_BLUE = (60, 59, 110)


def clamp(x, lo=0.0, hi=1.0):
    return lo if x < lo else hi if x > hi else x


def ease(t):
    t = clamp(t)
    return t * t * (3 - 2 * t)


def eout(t):
    t = clamp(t)
    return 1 - (1 - t) ** 3


def ap(t, t0, d=0.7):
    """Appearance amount for an element that starts at t0."""
    return eout((t - t0) / d)


def rgba(c, a):
    return tuple(c) + (int(255 * clamp(a)),)


def tx(d, xy, s, font, color, a=1.0, anchor="la"):
    if a > 0.003:
        d.text(xy, s, font=font, fill=rgba(color, a), anchor=anchor)


def spaced(d, xy, s, font, color, a=1.0, track=6, anchor="m"):
    """Letter-spaced single line. anchor: l / m / r (horizontal), vertically centred."""
    if a <= 0.003:
        return
    widths = [d.textlength(ch, font=font) for ch in s]
    total = sum(widths) + track * (len(s) - 1)
    x = xy[0] - {"l": 0, "m": total / 2, "r": total}[anchor]
    for ch, w in zip(s, widths):
        d.text((x, xy[1]), ch, font=font, fill=rgba(color, a), anchor="lm")
        x += w + track


def wrap(d, text, font, maxw):
    lines, cur = [], ""
    for word in text.split():
        trial = (cur + " " + word).strip()
        if d.textlength(trial, font=font) <= maxw or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def rule(d, x0, x1, y, color, a, width=2):
    if a > 0.003:
        d.line([(x0, y), (x1, y)], fill=rgba(color, a), width=width)


# --------------------------------------------------------------------------
# Cached textures
# --------------------------------------------------------------------------

_cache = {}


def cached(key, fn):
    if key not in _cache:
        _cache[key] = fn()
    return _cache[key]


def smooth_noise(rng, cells_x, cells_y, size):
    small = rng.random((cells_y, cells_x)).astype(np.float32)
    im = Image.fromarray((small * 255).astype(np.uint8)).resize(size, Image.BICUBIC)
    return np.asarray(im, dtype=np.float32) / 255.0


def make_paper():
    rng = np.random.default_rng(7)
    blot = smooth_noise(rng, 24, 14, (W, H)) - 0.5
    blot2 = smooth_noise(rng, 90, 50, (W, H)) - 0.5
    fiber = rng.normal(0, 1, (H, W)).astype(np.float32)
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    r = np.sqrt(((xx - W / 2) / (W / 2)) ** 2 + ((yy - H / 2) / (H / 2)) ** 2)
    edge = np.clip(r - 0.55, 0, None) ** 1.6
    base = np.array(PAPER, np.float32)
    img = base[None, None, :] * (1 + 0.10 * blot[..., None] + 0.05 * blot2[..., None])
    img += fiber[..., None] * 3.0
    img *= (1 - 0.55 * edge)[..., None]
    return Image.fromarray(np.clip(img, 0, 255).astype(np.uint8))


def make_dark():
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    r = np.sqrt(((xx - W / 2) / W) ** 2 + ((yy - H * 0.45) / H) ** 2)
    glow = np.clip(1 - r * 1.6, 0, 1) ** 2
    lo = np.array((12, 12, 14), np.float32)
    hi = np.array((44, 38, 32), np.float32)
    img = lo + (hi - lo) * glow[..., None]
    return Image.fromarray(img.astype(np.uint8))


def paper():
    return cached("paper", make_paper).copy()


def dark():
    return cached("dark", make_dark).copy()


def glow_sprite(radius, color):
    def mk():
        s = radius * 2
        yy, xx = np.mgrid[0:s, 0:s].astype(np.float32)
        r = np.sqrt((xx - radius) ** 2 + (yy - radius) ** 2) / radius
        a = np.clip(1 - r, 0, 1) ** 2.2
        arr = np.zeros((s, s, 4), np.uint8)
        arr[..., :3] = color
        arr[..., 3] = (a * 255).astype(np.uint8)
        return Image.fromarray(arr, "RGBA")
    return cached(("glow", radius, color), mk)


def paste_glow(img, x, y, radius, color, a):
    if a <= 0.01:
        return
    spr = glow_sprite(radius, color)
    if a < 0.99:
        spr = spr.copy()
        spr.putalpha(spr.getchannel("A").point(lambda v: int(v * a)))
    img.paste(spr, (int(x - radius), int(y - radius)), spr)


# --------------------------------------------------------------------------
# Geography
# --------------------------------------------------------------------------

def load_topo(name):
    with open(os.path.join(DATA, name)) as f:
        topo = json.load(f)
    sx, sy = topo["transform"]["scale"]
    tx_, ty_ = topo["transform"]["translate"]
    arcs = []
    for arc in topo["arcs"]:
        x = y = 0
        pts = []
        for dx, dy in arc:
            x += dx
            y += dy
            pts.append((x * sx + tx_, y * sy + ty_))
        arcs.append(pts)

    def ring(idxs):
        pts = []
        for i in idxs:
            a = arcs[i] if i >= 0 else arcs[~i][::-1]
            pts.extend(a if not pts else a[1:])
        return pts

    def polys(g):
        if g["type"] == "Polygon":
            return [ring(g["arcs"][0])]
        if g["type"] == "MultiPolygon":
            return [ring(p[0]) for p in g["arcs"]]
        return []

    return topo, polys


def us_states():
    def mk():
        topo, polys = load_topo("states-albers-10m.json")
        return {g["properties"]["name"]: polys(g) for g in topo["objects"]["states"]["geometries"]}
    return cached("states", mk)


def world_land():
    def mk():
        topo, polys = load_topo("land-50m.json")
        out = []
        for g in topo["objects"]["land"]["geometries"]:
            out.extend(polys(g))
        return out
    return cached("land", mk)


class USView:
    """Places the 975x610 Albers-USA plane on screen."""

    def __init__(self, cx=W / 2, cy=H / 2 + 20, scale=1.5):
        self.k, self.ox, self.oy = scale, cx - 487.5 * scale, cy - 305 * scale

    def pt(self, x, y):
        return (self.ox + x * self.k, self.oy + y * self.k)

    def lonlat(self, lon, lat):
        return self.pt(*albers_usa(lon, lat))

    def shapes(self):
        key = ("usview", self.k, self.ox, self.oy)
        return cached(key, lambda: {n: [[self.pt(*p) for p in ring] for ring in rings]
                                    for n, rings in us_states().items()})


def albers_usa(lon, lat):
    """d3.geoAlbersUsa().scale(1300).translate([487.5, 305]) for the lower 48."""
    p0, p1 = math.radians(29.5), math.radians(45.5)
    sy0 = math.sin(p0)
    n = (sy0 + math.sin(p1)) / 2
    c = 1 + sy0 * (2 * n - sy0)
    r0 = math.sqrt(c) / n

    def raw(lam, phi):
        r = math.sqrt(c - 2 * n * math.sin(phi)) / n
        return r * math.sin(lam * n), r0 - r * math.cos(lam * n)

    px, py = raw(math.radians(lon + 96), math.radians(lat))
    cx, cy = raw(math.radians(-0.6), math.radians(38.7))
    return 487.5 + 1300 * (px - cx), 305 - 1300 * (py - cy)


class GeoView:
    """Simple equirectangular view of a lon/lat window."""

    def __init__(self, lon0, lon1, lat0, lat1, box=(0, 0, W, H)):
        self.lon0, self.lat1 = lon0, lat1
        self.kx_ = math.cos(math.radians((lat0 + lat1) / 2))
        span_x = (lon1 - lon0) * self.kx_
        span_y = lat1 - lat0
        bw, bh = box[2] - box[0], box[3] - box[1]
        self.k = max(bw / span_x, bh / span_y)
        self.ox = box[0] + (bw - span_x * self.k) / 2
        self.oy = box[1] + (bh - span_y * self.k) / 2
        self.key = ("geo", lon0, lon1, lat0, lat1, box)
        self.bounds = (lon0 - 5, lon1 + 5, lat0 - 5, lat1 + 5)

    def pt(self, lon, lat):
        return (self.ox + (lon - self.lon0) * self.kx_ * self.k, self.oy + (self.lat1 - lat) * self.k)

    def base(self):
        def mk():
            img = Image.new("RGB", (W, H), OCEAN)
            d = ImageDraw.Draw(img, "RGBA")
            for lon in range(-180, 181, 10):
                d.line([self.pt(lon, -80), self.pt(lon, 85)], fill=(255, 255, 255, 10), width=1)
            for lat in range(-80, 81, 10):
                d.line([self.pt(-180, lat), self.pt(180, lat)], fill=(255, 255, 255, 10), width=1)
            b = self.bounds
            for ring in world_land():
                xs = [p[0] for p in ring]
                ys = [p[1] for p in ring]
                if max(xs) < b[0] or min(xs) > b[1] or max(ys) < b[2] or min(ys) > b[3]:
                    continue
                pts = [self.pt(*p) for p in ring]
                if len(pts) > 2:
                    d.polygon(pts, fill=LAND, outline=(96, 92, 82))
            return img
        return cached(self.key, mk)


def us_map(img, view, colors, outline=(20, 20, 22), a_outline=0.9):
    """colors: name -> (rgb, alpha) for fill; states missing from colors use LAND."""
    d = ImageDraw.Draw(img, "RGBA")
    for name, rings in view.shapes().items():
        for ring in rings:
            d.polygon(ring, fill=LAND)
    for name, (col, a) in colors.items():
        if a <= 0.003 or name not in view.shapes():
            continue
        for ring in view.shapes()[name]:
            d.polygon(ring, fill=rgba(col, a))
    for name, rings in view.shapes().items():
        for ring in rings:
            d.line(ring + [ring[0]], fill=rgba(outline, a_outline), width=2)
    return d


def us_bg():
    img = Image.new("RGB", (W, H), OCEAN)
    return img


def pin(img, d, x, y, label, a, sub=None, color=GOLD, side="r"):
    if a <= 0.01:
        return
    paste_glow(img, x, y, 46, color, 0.55 * a)
    d.ellipse([x - 7, y - 7, x + 7, y + 7], fill=rgba(color, a), outline=rgba((20, 18, 16), a), width=2)
    dx = 22 if side == "r" else -22
    anc = "lm" if side == "r" else "rm"
    spaced(d, (x + dx, y - (12 if sub else 0)), label, F("sc", 34), CREAM, a, track=3,
           anchor="l" if side == "r" else "r")
    if sub:
        tx(d, (x + dx, y + 22), sub, F("it", 28), DIM, a, anchor=anc)


def route(d, p0, p1, prog, color, a, bend=0.18, width=4, dash=True):
    """Curved route drawn progressively; returns head point."""
    if a <= 0.01 or prog <= 0:
        return None
    (x0, y0), (x1, y1) = p0, p1
    mx, my = (x0 + x1) / 2, (y0 + y1) / 2
    nx, ny = -(y1 - y0), (x1 - x0)
    cx, cy = mx + nx * bend, my + ny * bend
    n = 120
    pts = []
    for i in range(int(n * clamp(prog)) + 1):
        s = i / n
        pts.append(((1 - s) ** 2 * x0 + 2 * (1 - s) * s * cx + s * s * x1,
                    (1 - s) ** 2 * y0 + 2 * (1 - s) * s * cy + s * s * y1))
    if len(pts) < 2:
        return pts[0]
    if dash:
        for i in range(0, len(pts) - 1, 3):
            d.line(pts[i:i + 3], fill=rgba(color, a), width=width)
    else:
        d.line(pts, fill=rgba(color, a), width=width, joint="curve")
    return pts[-1]


# --------------------------------------------------------------------------
# Flags, moon
# --------------------------------------------------------------------------

def star_poly(cx, cy, r, rot=-math.pi / 2):
    pts = []
    for i in range(10):
        rr = r if i % 2 == 0 else r * 0.382
        a = rot + i * math.pi / 5
        pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
    return pts


def make_flag(hoist, stars=50):
    fly = int(hoist * 1.9)
    img = Image.new("RGB", (fly, hoist), (255, 255, 255))
    d = ImageDraw.Draw(img)
    sh = hoist / 13
    for i in range(0, 13, 2):
        d.rectangle([0, i * sh, fly, (i + 1) * sh], fill=FLAG_RED)
    cw, ch = hoist * 0.76, sh * 7
    d.rectangle([0, 0, cw, ch], fill=FLAG_BLUE)
    r = hoist * 0.0616 / 2
    if stars == 50:
        for row in range(9):
            for col in range(11):
                if (row + col) % 2:
                    continue
                d.polygon(star_poly(hoist * (0.063 + 0.063 * col), hoist * (0.054 + 0.054 * row), r),
                          fill=(255, 255, 255))
    else:
        for i in range(13):
            a = -math.pi / 2 + i * 2 * math.pi / 13
            d.polygon(star_poly(cw / 2 + math.cos(a) * ch * 0.36, ch / 2 + math.sin(a) * ch * 0.36,
                                r * 1.1, rot=a), fill=(255, 255, 255))
    return img


def wave_flag(flag, t, amp=0.035, strength=1.0):
    arr = np.asarray(flag, dtype=np.float32)
    h, w = arr.shape[:2]
    x = np.arange(w, dtype=np.float32)
    ph = x / w * 2 * math.pi * 1.6 - t * 3.2
    falloff = (x / w) ** 0.8 * strength
    dy = amp * h * np.sin(ph) * falloff
    shade = 1 + 0.16 * np.cos(ph) * falloff
    rows = (np.arange(h, dtype=np.float32)[:, None] - dy[None, :]).round().astype(int)
    valid = (rows >= 0) & (rows < h)
    rows = rows.clip(0, h - 1)
    out = arr[rows, np.arange(w)[None, :]] * shade[None, :, None]
    alpha = (valid * 255).astype(np.uint8)
    rgb = np.clip(out, 0, 255).astype(np.uint8)
    im = Image.fromarray(np.dstack([rgb, alpha]), "RGBA")
    return im


def make_moon(r):
    rng = np.random.default_rng(3)
    s = 2 * r
    yy, xx = np.mgrid[0:s, 0:s].astype(np.float32)
    dx, dy = (xx - r) / r, (yy - r) / r
    rr = np.sqrt(dx ** 2 + dy ** 2)
    inside = rr <= 1
    tone = 0.72 + 0.10 * (smooth_noise(rng, 6, 6, (s, s)) - 0.5) + 0.05 * (smooth_noise(rng, 30, 30, (s, s)) - 0.5)
    maria = smooth_noise(rng, 5, 5, (s, s))
    tone -= 0.26 * np.clip((maria - 0.5) * 3, 0, 1)
    tone -= 0.12 * np.clip((smooth_noise(rng, 9, 9, (s, s)) - 0.55) * 4, 0, 1)
    for _ in range(90):
        cx, cy = rng.uniform(0, s, 2)
        cr = r * 0.008 * rng.pareto(1.6) + r * 0.006
        cr = min(cr, r * 0.12)
        dd = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2) / cr
        tone -= 0.05 * np.clip(1 - dd, 0, 1) ** 0.5 * (dd < 1)
        tone += 0.035 * np.exp(-((dd - 1.0) ** 2) / 0.03)
    nz = np.sqrt(np.clip(1 - rr ** 2, 0, 1))
    light = np.clip(0.25 + 0.85 * (-0.55 * dx - 0.25 * dy + 0.8 * nz), 0, 1.1)
    val = np.clip(tone * light, 0, 1) * 235
    arr = np.zeros((s, s, 4), np.uint8)
    arr[..., 0] = val
    arr[..., 1] = val * 0.98
    arr[..., 2] = val * 0.94
    edge = np.clip((1 - rr) * r / 1.5, 0, 1)
    arr[..., 3] = (edge * 255).astype(np.uint8)
    return Image.fromarray(arr, "RGBA")


def starfield(img, seed=1, n=500, a=1.0):
    rng = np.random.default_rng(seed)
    d = ImageDraw.Draw(img, "RGBA")
    for _ in range(n):
        x, y = rng.uniform(0, W), rng.uniform(0, H)
        b = rng.uniform(0.2, 1.0) * a
        rad = 1.6 if rng.random() > 0.93 else 0.9
        d.ellipse([x - rad, y - rad, x + rad, y + rad], fill=(255, 255, 255, int(200 * b)))


# --------------------------------------------------------------------------
# Generic panels
# --------------------------------------------------------------------------

class Ctx:
    def __init__(self, t, dur, beats, scene):
        self.t, self.dur, self.b, self.scene = t, dur, beats, scene

    def end_of(self, i):
        return self.b[i + 1] if i + 1 < len(self.b) else self.dur - 1.0


def card(kicker, title, sub=None, bg="dark", beat=0, delay=0.0, title_size=118):
    def draw(img, c):
        t0 = c.b[beat] + delay
        base = dark() if bg == "dark" else paper()
        img.paste(base)
        d = ImageDraw.Draw(img, "RGBA")
        fg = CREAM if bg == "dark" else INK
        acc = GOLD if bg == "dark" else RED
        a1, a2, a3 = ap(c.t, t0 - 0.4), ap(c.t, t0 - 0.1, 0.9), ap(c.t, t0 + 0.6, 0.9)
        spaced(d, (W / 2, H / 2 - 130), kicker, F("sc", 40), acc, a1, track=8)
        w = 260 * a1
        rule(d, W / 2 - w, W / 2 + w, H / 2 - 88, acc, a1 * 0.8)
        tx(d, (W / 2, H / 2 + 6 + (1 - a2) * 16), title, F("reg", title_size), fg, a2, anchor="mm")
        if sub:
            for i, line in enumerate(wrap(d, sub, F("it", 44), 1300)):
                tx(d, (W / 2, H / 2 + 120 + i * 58), line, F("it", 44), fg, a3 * 0.85, anchor="mm")
    return draw


def quote(text, attrib, beat=0, bg="dark", delay=0.0, size=74):
    def draw(img, c):
        t0 = c.b[beat] + delay
        img.paste(dark() if bg == "dark" else paper())
        d = ImageDraw.Draw(img, "RGBA")
        fg = CREAM if bg == "dark" else INK
        acc = GOLD if bg == "dark" else RED
        lines = wrap(d, text, F("it", size), 1450)
        lh = size * 1.25
        y0 = H / 2 - lh * len(lines) / 2 - 30
        tx(d, (W / 2 - 760, y0 - 40), "“", F("reg", 220), acc, ap(c.t, t0 - 0.5) * 0.5, anchor="lm")
        words_total = sum(len(l.split()) for l in lines)
        span = max(1.2, (c.end_of(beat) - t0) * 0.55)
        k = 0
        for i, line in enumerate(lines):
            x = W / 2 - d.textlength(line, font=F("it", size)) / 2
            for wd in line.split():
                a = ap(c.t, t0 + span * k / words_total, 0.5)
                tx(d, (x, y0 + i * lh), wd, F("it", size), fg, a, anchor="lm")
                x += d.textlength(wd + " ", font=F("it", size))
                k += 1
        aa = ap(c.t, t0 + span + 0.2, 0.8)
        y = y0 + lh * len(lines) + 50
        rule(d, W / 2 - 60, W / 2 + 60, y, acc, aa)
        for i, line in enumerate(attrib.split("\n")):
            if i == 0:
                spaced(d, (W / 2, y + 50), line, F("sc", 36), acc, aa, track=5)
            else:
                tx(d, (W / 2, y + 50 + 48 * i), line, F("it", 32), DIM if bg == "dark" else INK, aa, anchor="mm")
    return draw


def bignum(value, caption, sub=None, beat=0, delay=0.0, count=True, color=GOLD):
    def draw(img, c):
        t0 = c.b[beat] + delay
        img.paste(dark())
        d = ImageDraw.Draw(img, "RGBA")
        p = eout((c.t - t0) / 2.2)
        num = int(value.replace(",", "").replace("+", "")) if count else None
        s = f"{int(num * p):,}" + ("+" if value.endswith("+") and p >= 1 else "") if count else value
        tx(d, (W / 2, H / 2 - 50), s, F("reg", 230), color, ap(c.t, t0 - 0.3), anchor="mm")
        rule(d, W / 2 - 300 * ap(c.t, t0), W / 2 + 300 * ap(c.t, t0), H / 2 + 90, color, 0.7)
        spaced(d, (W / 2, H / 2 + 150), caption, F("sc", 44), CREAM, ap(c.t, t0 + 0.4), track=6)
        if sub:
            tx(d, (W / 2, H / 2 + 220), sub, F("it", 40), DIM, ap(c.t, t0 + 0.8), anchor="mm")
    return draw


def timeline(items, beats_for, bg="dark", title=None):
    """items: [(year, label, sublabel)], beats_for: index of beat for each item (with fractional offsets)."""
    def draw(img, c):
        img.paste(dark() if bg == "dark" else paper())
        d = ImageDraw.Draw(img, "RGBA")
        fg = CREAM if bg == "dark" else INK
        acc = GOLD if bg == "dark" else RED
        x0, x1, y = 200, W - 200, H / 2 + 20
        first = c.b[beats_for[0][0]]
        if title:
            spaced(d, (W / 2, 210), title, F("sc", 44), acc, ap(c.t, first - 0.6), track=8)
        rule(d, x0, x0 + (x1 - x0) * ap(c.t, first - 0.6, 1.6), y, fg, 0.55, 3)
        n = len(items)
        for i, ((yr, lab, sub), (bi, frac)) in enumerate(zip(items, beats_for)):
            t0 = c.b[bi] + frac * (c.end_of(bi) - c.b[bi])
            a = ap(c.t, t0, 0.6)
            x = x0 + (x1 - x0) * (i + 0.5) / n
            up = i % 2 == 0
            paste_glow(img, x, y, 40, acc, a * 0.6)
            d.ellipse([x - 9, y - 9, x + 9, y + 9], fill=rgba(acc, a))
            ly = y - 70 if up else y + 70
            d.line([(x, y + (-12 if up else 12)), (x, ly + (25 if up else -25))], fill=rgba(fg, a * 0.5), width=2)
            tx(d, (x, ly - (40 if up else -10)), str(yr), F("reg", 66), acc, a, anchor="mm")
            for j, line in enumerate(wrap(d, lab, F("reg", 36), 330)):
                yy = (ly - 110 - 42 * (len(wrap(d, lab, F("reg", 36), 330)) - 1 - j)) if up else (ly + 70 + 42 * j)
                tx(d, (x, yy), line, F("reg", 36), fg, a, anchor="mm")
            if sub:
                yy = ly - 160 - 42 * (len(wrap(d, lab, F("reg", 36), 330)) - 1) if up else ly + 70 + 42 * len(wrap(d, lab, F("reg", 36), 330))
                tx(d, (x, yy), sub, F("it", 28), DIM, a, anchor="mm")
    return draw


def chapter_card(img, c):
    sc = c.scene
    img.paste(Image.new("RGB", (W, H), (8, 8, 9)))
    d = ImageDraw.Draw(img, "RGBA")
    a = ap(c.t, 0.2, 0.9)
    spaced(d, (W / 2, H / 2 - 120), f"CHAPTER {sc['chapter']}", F("sc", 40), GOLD, a, track=14)
    tx(d, (W / 2, H / 2 + (1 - ap(c.t, 0.35, 1.2)) * 20), sc["title"], F("reg", 140), CREAM,
       ap(c.t, 0.35, 1.2), anchor="mm")
    w = 340 * ap(c.t, 0.6, 1.4)
    rule(d, W / 2 - w, W / 2 + w, H / 2 + 100, GOLD, 0.7)
    tx(d, (W / 2, H / 2 + 160), sc["years"], F("it", 54), DIM, ap(c.t, 0.9, 1.0), anchor="mm")


# --------------------------------------------------------------------------
# Scene-specific panels
# --------------------------------------------------------------------------

def p_title(img, c):
    img.paste(Image.new("RGB", (W, H), (6, 6, 7)))
    rng = np.random.default_rng(11)
    d = ImageDraw.Draw(img, "RGBA")
    for _ in range(220):  # drifting dust in a beam of light
        x0, y0 = rng.uniform(0, W), rng.uniform(0, H)
        sp = rng.uniform(4, 16)
        x = (x0 + c.t * sp) % W
        y = (y0 - c.t * sp * 0.3) % H
        b = rng.uniform(0.1, 0.5) * ap(c.t, 0.3, 3)
        d.ellipse([x - 1.3, y - 1.3, x + 1.3, y + 1.3], fill=(255, 236, 200, int(200 * b)))
    paste_glow(img, W / 2, H / 2 - 20, 700, (70, 56, 40), ap(c.t, 0.5, 4))
    d = ImageDraw.Draw(img, "RGBA")
    ta = ap(c.t, c.b[1] - 0.6, 2.4)
    spaced(d, (W / 2, H / 2 - 30), "AMERICA", F("sc", 210), CREAM, ta, track=int(60 - 26 * ta))
    w = 420 * ap(c.t, c.b[1], 2.0)
    rule(d, W / 2 - w, W / 2 + w, H / 2 + 90, GOLD, 0.8)
    tx(d, (W / 2, H / 2 + 160), "A Short History", F("it", 64), GOLD, ap(c.t, c.b[1] + 0.9, 1.6), anchor="mm")


NA_VIEW = GeoView(-135, -55, 14, 60, box=(0, 0, W, H))


def p_first_peoples(img, c):
    img.paste(NA_VIEW.base())
    d = ImageDraw.Draw(img, "RGBA")
    sites = [
        ("CHACO CANYON", "Ancestral Puebloans", -107.96, 36.06, 1, 0.10, "r"),
        ("HAUDENOSAUNEE", "Iroquois Confederacy", -75.9, 43.0, 1, 0.35, "r"),
        ("PACIFIC NORTHWEST", "Coast Salish & others", -122.5, 47.8, 1, 0.55, "r"),
        ("CALUSA", "Southwest Florida", -81.9, 26.6, 1, 0.75, "r"),
        ("CAHOKIA", "c. 1100 CE", -90.06, 38.66, 2, 0.05, "l"),
    ]
    for name, sub, lon, lat, bi, frac, side in sites:
        t0 = c.b[bi] + frac * (c.end_of(bi) - c.b[bi])
        x, y = NA_VIEW.pt(lon, lat)
        pin(img, d, x, y, name, ap(c.t, t0), sub, side=side)
    a = ap(c.t, c.b[2] + 2.2, 1.0)
    if a > 0:
        x, y = NA_VIEW.pt(-90.06, 38.66)
        tx(d, (x + 40, y + 90), "~20,000", F("reg", 96), GOLD, a, anchor="lm")
        tx(d, (x + 44, y + 155), "people at its height", F("it", 32), CREAM, a, anchor="lm")


ATL_VIEW = GeoView(-100, 25, -18, 60, box=(0, 0, W, H))


def p_atlantic(img, c):
    img.paste(ATL_VIEW.base())
    d = ImageDraw.Draw(img, "RGBA")
    a = ap(c.t, c.b[1], 2.5) * (1 - ap(c.t, c.b[3], 1.5) * 0.5)
    if a > 0:
        paste_glow(img, *ATL_VIEW.pt(-80, 20), 520, (150, 30, 20), 0.35 * a)
        d = ImageDraw.Draw(img, "RGBA")
    routes = [
        (0, (-6.9, 37.2), (-74.5, 24.0), "1492", "Columbus", GOLD, 0.15),
        (2, (-1.4, 50.9), (-76.8, 37.2), "1607", "Jamestown", CREAM, -0.12),
        (3, (13.2, -8.8), (-76.3, 37.0), "1619", "Point Comfort, Virginia", RED, -0.2),
    ]
    for bi, a0, a1, yr, place, col, bend in routes:
        t0 = c.b[bi]
        prog = eout((c.t - t0) / 3.0)
        fade = 1 - 0.55 * ap(c.t, c.b[min(bi + 1, 4)], 1.0) if bi < 3 else 1.0
        head = route(d, ATL_VIEW.pt(*a0), ATL_VIEW.pt(*a1), prog, col, fade, bend=bend)
        if head:
            d.ellipse([head[0] - 7, head[1] - 7, head[0] + 7, head[1] + 7], fill=rgba(col, fade))
        la = ap(c.t, t0 + 2.4) * fade
        x, y = ATL_VIEW.pt(*a1)
        if la > 0:
            tx(d, (x + 26, y + (80 if bi == 3 else -30)), yr, F("reg", 72), col, la, anchor="lm")
            tx(d, (x + 30, y + (134 if bi == 3 else 24)), place, F("it", 34), CREAM, la, anchor="lm")


THIRTEEN = ["Delaware", "Pennsylvania", "New Jersey", "Georgia", "Connecticut", "Massachusetts",
            "Maryland", "South Carolina", "New Hampshire", "Virginia", "New York", "North Carolina",
            "Rhode Island"]
US_BIG = USView(scale=1.55, cy=H / 2 + 40)
US_EAST = USView(scale=2.6, cx=W / 2 - 1250, cy=H / 2 - 30)


def p_colonies(img, c):
    img.paste(us_bg())
    t0 = c.b[4]
    north_to_south = sorted(THIRTEEN + ["West Virginia", "Maine"], key=lambda n: min(p[1] for r in us_states()[n] for p in r))
    cols = {n: (GOLD, ap(c.t, t0 + 0.15 * i, 0.6)) for i, n in enumerate(north_to_south)}
    d = us_map(img, US_EAST, cols)
    a = ap(c.t, t0 + 2.2)
    spaced(d, (1120, 330), "THE THIRTEEN COLONIES", F("sc", 48), CREAM, a, track=5, anchor="l")
    tx(d, (1120, 395), "British North America, 1732", F("it", 40), DIM, a, anchor="lm")
    x, y = US_EAST.lonlat(-76.8, 37.2)
    pin(img, d, x, y, "JAMESTOWN", ap(c.t, t0 + 0.5), "1607")


def p_broadside(img, c):
    img.paste(paper())
    d = ImageDraw.Draw(img, "RGBA")
    t0 = c.b[0]
    lines = [("NO", 120), ("TAXATION", 190), ("WITHOUT", 150), ("REPRESENTATION", 128)]
    y = 230
    for i, (s, size) in enumerate(lines):
        a = ap(c.t, t0 + 0.35 * i, 0.5)
        spaced(d, (W / 2, y + (1 - a) * -20), s, F("bold", size), INK if i != 3 else RED, a, track=10)
        y += size * 1.05
    rule(d, 560, W - 560, y + 10, INK, ap(c.t, t0 + 1.6))
    tx(d, (W / 2, y + 70), "Stamp Act 1765  ·  Townshend Acts 1767  ·  Tea Act 1773",
       F("it", 42), INK, ap(c.t, t0 + 1.9), anchor="mm")


DECL_HEAD = "IN CONGRESS, July 4, 1776."
DECL_SUB = "The unanimous Declaration of the thirteen united States of America,"
DECL_BODY = ("When in the Course of human events, it becomes necessary for one people to dissolve the political "
             "bands which have connected them with another, and to assume among the powers of the earth, the "
             "separate and equal station to which the Laws of Nature and of Nature's God entitle them… "
             "We hold these truths to be self-evident, that all men are created equal, that they are endowed "
             "by their Creator with certain unalienable Rights, that among these are Life, Liberty and the "
             "pursuit of Happiness.")


def p_declaration(img, c):
    img.paste(paper())
    d = ImageDraw.Draw(img, "RGBA")
    t0, t1 = c.b[2], c.b[3]
    spaced(d, (W / 2, 150), DECL_HEAD, F("sc", 46), INK, ap(c.t, t0 - 0.5), track=5)
    tx(d, (W / 2, 240), DECL_SUB, F("it", 62), INK, ap(c.t, t0), anchor="mm")
    rule(d, 360, W - 360, 300, INK, ap(c.t, t0 + 0.3) * 0.6)
    fnt = F("reg", 50)
    words = DECL_BODY.split()
    x0, maxw, lh = 330, W - 660, 72
    x, y = x0, 380
    boxes = []
    for wd in words:
        wl = d.textlength(wd, font=fnt)
        if x + wl > x0 + maxw:
            x, y = x0, y + lh
        boxes.append((wd, x, y, wl))
        x += wl + d.textlength(" ", font=fnt)
    span = max(2.0, (t1 - t0) * 0.95)
    hi = list(range(words.index("self-evident,") + 2, words.index("self-evident,") + 7))  # all men are created equal,
    hl = ap(c.t, t1 + 0.2, 1.0)
    for i, (wd, x, y, wl) in enumerate(boxes):
        a = ap(c.t, t0 + 0.5 + span * i / len(boxes), 0.35)
        col = RED if i in hi and hl > 0 else INK
        dim = 1 - 0.45 * hl if i not in hi else 1.0
        tx(d, (x, y), wd, fnt, col, a * dim, anchor="ls")
    if hl > 0:
        xs = boxes[hi[0]][1]
        same_line = [b for b in (boxes[i] for i in hi) if b[2] == boxes[hi[0]][2]]
        xe = same_line[-1][1] + same_line[-1][3]
        d.line([(xs, boxes[hi[0]][2] + 14), (xs + (xe - xs) * hl, boxes[hi[0]][2] + 14)], fill=rgba(RED, 1), width=4)
        if len(same_line) < len(hi):
            rest = [boxes[i] for i in hi if boxes[i][2] != boxes[hi[0]][2]]
            xs2, xe2 = rest[0][1], rest[-1][1] + rest[-1][3]
            d.line([(xs2, rest[0][2] + 14), (xs2 + (xe2 - xs2) * hl, rest[0][2] + 14)], fill=rgba(RED, 1), width=4)


def p_flag13(img, c):
    img.paste(dark())
    d = ImageDraw.Draw(img, "RGBA")
    t0 = c.b[4]
    flag = cached("flag13", lambda: make_flag(430, stars=13))
    f = wave_flag(flag, c.t, strength=0.8)
    a = ap(c.t, t0 - 0.4, 1.2)
    f.putalpha(f.getchannel("A").point(lambda v: int(v * a)))
    img.paste(f, (int(W / 2 - f.width / 2), 140), f)
    d = ImageDraw.Draw(img, "RGBA")
    mid = t0 + (c.end_of(4) - t0) * 0.45
    spaced(d, (W / 2 - 60, 720), "YORKTOWN", F("sc", 50), GOLD, ap(c.t, t0 + 0.6), track=6, anchor="r")
    tx(d, (W / 2 - 60, 790), "October 1781", F("it", 42), CREAM, ap(c.t, t0 + 0.9), anchor="rm")
    spaced(d, (W / 2 + 60, 720), "TREATY OF PARIS", F("sc", 50), GOLD, ap(c.t, mid), track=6, anchor="l")
    tx(d, (W / 2 + 60, 790), "September 1783", F("it", 42), CREAM, ap(c.t, mid + 0.3), anchor="lm")
    d.line([(W / 2, 700), (W / 2, 810)], fill=rgba(GOLD, ap(c.t, t0 + 0.6) * 0.5), width=2)


def p_we_the_people(img, c):
    img.paste(paper())
    t0 = c.b[1]
    layer = cached("wtp", lambda: _wtp_layer())
    p = ease((c.t - t0 - 0.2) / 2.2)
    if p > 0:
        w = int(layer.width * p)
        img.paste(layer.crop((0, 0, max(1, w), layer.height)), (0, 0), layer.crop((0, 0, max(1, w), layer.height)))
    d = ImageDraw.Draw(img, "RGBA")
    a = ap(c.t, t0 + 2.2, 1.2)
    tx(d, (W / 2, 720), "of the United States, in Order to form a more perfect Union…", F("it", 52), INK, a, anchor="mm")
    spaced(d, (W / 2, 860), "THE CONSTITUTION  ·  SEPTEMBER 17, 1787", F("sc", 38), RED, ap(c.t, t0 + 3.0), track=6)


def _wtp_layer():
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.text((W / 2, 470), "We the People", font=F("it", 270), fill=INK + (255,), anchor="mm")
    return im


AMENDMENTS = [
    ("I", "Religion, speech, press, assembly, petition"),
    ("II", "The right to keep and bear arms"),
    ("III", "No quartering of soldiers"),
    ("IV", "No unreasonable searches and seizures"),
    ("V", "Due process; no self-incrimination"),
    ("VI", "A speedy and public trial"),
    ("VII", "Trial by jury in civil cases"),
    ("VIII", "No cruel and unusual punishment"),
    ("IX", "Rights retained by the people"),
    ("X", "Powers reserved to the states or the people"),
]


def p_bill_of_rights(img, c):
    img.paste(paper())
    d = ImageDraw.Draw(img, "RGBA")
    t0 = c.b[3]
    spaced(d, (W / 2, 150), "THE BILL OF RIGHTS", F("sc", 64), INK, ap(c.t, t0 - 0.4), track=10)
    tx(d, (W / 2, 220), "Ratified December 15, 1791", F("it", 42), RED, ap(c.t, t0), anchor="mm")
    rule(d, 400, W - 400, 270, INK, ap(c.t, t0) * 0.6)
    span = (c.end_of(3) - t0) * 0.8
    for i, (num, s) in enumerate(AMENDMENTS):
        col, row = i // 5, i % 5
        x = 150 + col * 860
        y = 360 + row * 120
        a = ap(c.t, t0 + 0.3 + span * i / 10, 0.5)
        tx(d, (x + 90, y), num, F("reg", 56), RED, a, anchor="rm")
        tx(d, (x + 120, y), s, F("reg", 42), INK, a, anchor="lm")


STATEHOOD = {
    "Delaware": 1787, "Pennsylvania": 1787, "New Jersey": 1787, "Georgia": 1788, "Connecticut": 1788,
    "Massachusetts": 1788, "Maryland": 1788, "South Carolina": 1788, "New Hampshire": 1788, "Virginia": 1788,
    "New York": 1788, "North Carolina": 1789, "Rhode Island": 1790, "Vermont": 1791, "Kentucky": 1792,
    "Tennessee": 1796, "Ohio": 1803, "Louisiana": 1812, "Indiana": 1816, "Mississippi": 1817, "Illinois": 1818,
    "Alabama": 1819, "Maine": 1820, "Missouri": 1821, "Arkansas": 1836, "Michigan": 1837, "Florida": 1845,
    "Texas": 1845, "Iowa": 1846, "Wisconsin": 1848, "California": 1850, "Minnesota": 1858, "Oregon": 1859,
    "Kansas": 1861, "West Virginia": 1863, "Nevada": 1864, "Nebraska": 1867, "Colorado": 1876,
    "North Dakota": 1889, "South Dakota": 1889, "Montana": 1889, "Washington": 1889, "Idaho": 1890,
    "Wyoming": 1890, "Utah": 1896, "Oklahoma": 1907, "New Mexico": 1912, "Arizona": 1912, "Alaska": 1959,
    "Hawaii": 1959,
}


PART_OF = {"West Virginia": 1788, "Maine": 1788}  # within Virginia / Massachusetts before statehood


def state_fill(year_f, color=GOLD, base=0.95):
    years = {**STATEHOOD, **PART_OF}
    return {n: (color, base * clamp(year_f - (y - 1))) for n, y in years.items()}


def p_west(img, c):
    img.paste(us_bg())
    b = c.b
    if c.t < b[1]:
        year = 1800 + 3 * ease((c.t - b[0]) / max(0.1, b[1] - b[0]))
    else:
        year = 1803 + 47 * ease((c.t - b[1]) / max(0.1, (c.dur - 1.6) - b[1]))
    d = us_map(img, US_BIG, state_fill(year))
    spaced(d, (W - 120, 120), "STATES OF THE UNION", F("sc", 34), DIM, 1, track=5, anchor="r")
    tx(d, (W - 120, 210), str(int(year)), F("reg", 140), CREAM, 1, anchor="rm")
    la = ap(c.t, b[1] + 0.5) * (1 - ap(c.t, b[2] + 0.5))
    if la > 0:
        x, y = US_BIG.lonlat(-98, 42.5)
        spaced(d, (x, y - 20), "LOUISIANA PURCHASE", F("sc", 46), CREAM, la, track=5)
        tx(d, (x, y + 30), "1803  ·  about 828,000 square miles", F("it", 36), CREAM, la, anchor="mm")
    # Trail of Tears
    t0 = b[3]
    dest = US_BIG.lonlat(-95.6, 35.6)
    starts = [(-84.4, 34.6), (-86.3, 32.4), (-89.4, 32.9), (-88.9, 34.4), (-82.0, 28.8)]
    for i, s in enumerate(starts):
        prog = eout((c.t - t0 - 0.35 * i) / 2.6)
        head = route(d, US_BIG.lonlat(*s), dest, prog, RED, 0.95, bend=-0.12, width=5, dash=False)
        if head and prog > 0.02:
            d.ellipse([head[0] - 6, head[1] - 6, head[0] + 6, head[1] + 6], fill=rgba(RED, 1))
    ta = ap(c.t, t0 + 1.5)
    if ta > 0:
        lx, ly = US_BIG.lonlat(-106.5, 37.2)
        spaced(d, (lx, ly), "TRAIL OF TEARS", F("sc", 46), (236, 128, 108), ta, track=5)
        tx(d, (lx, ly + 50), "Forced removals, 1830s", F("it", 34), CREAM, ta, anchor="mm")
    ga = ap(c.t, b[4] + 1.5)
    x, y = US_BIG.lonlat(-120.89, 38.8)
    pin(img, d, x, y, "GOLD", ga, "Sutter's Mill, 1848")


SLAVE_1860 = ["Delaware", "Maryland", "Virginia", "West Virginia", "North Carolina", "South Carolina",
              "Georgia", "Florida", "Alabama", "Mississippi", "Louisiana", "Texas", "Arkansas", "Tennessee",
              "Kentucky", "Missouri"]
FREE_1860 = ["Maine", "New Hampshire", "Vermont", "Massachusetts", "Rhode Island", "Connecticut", "New York",
             "New Jersey", "Pennsylvania", "Ohio", "Indiana", "Illinois", "Michigan", "Wisconsin", "Minnesota",
             "Iowa", "California", "Oregon", "Kansas"]
SECESSION = ["South Carolina", "Mississippi", "Florida", "Alabama", "Georgia", "Louisiana", "Texas",
             "Virginia", "Arkansas", "North Carolina", "Tennessee"]
BORDER = ["Delaware", "Maryland", "Kentucky", "Missouri", "West Virginia"]
CONF = (168, 62, 46)
UNION = (64, 96, 160)
BORDER_C = (126, 150, 196)


def legend(d, x, y, items, a):
    for i, (col, label) in enumerate(items):
        yy = y + i * 56
        d.rectangle([x, yy - 16, x + 32, yy + 16], fill=rgba(col, a))
        tx(d, (x + 50, yy), label, F("reg", 40), CREAM, a, anchor="lm")


def p_civil_map(img, c):
    img.paste(us_bg())
    b = c.b
    sec = ap(c.t, b[1] + 1.0, 1.0)
    cols = {}
    for n in FREE_1860:
        cols[n] = (UNION, 0.9 * ap(c.t, b[0], 1.0))
    for n in SLAVE_1860:
        cols[n] = ((196, 150, 80), 0.9 * ap(c.t, b[0] + 0.4, 1.0) * (1 - sec))
    if sec > 0:
        span = (c.end_of(1) - b[1] - 1.0) * 0.8
        for i, n in enumerate(SECESSION):
            cols[n] = (CONF, ap(c.t, b[1] + 1.0 + span * i / 11, 0.5))
        for n in BORDER:
            cols[n] = (BORDER_C, 0.9 * sec)
    d = us_map(img, US_BIG, cols)
    la = ap(c.t, b[0] + 0.6) * (1 - sec)
    legend(d, 110, 860, [(UNION, "Free states, 1860"), ((196, 150, 80), "Slave states, 1860")], la)
    legend(d, 110, 820, [(UNION, "Union"), (BORDER_C, "Border states"), (CONF, "Confederacy")], sec)
    ga = ap(c.t, b[2] if len(b) > 2 else 1e9)
    if ga > 0:
        dd = ImageDraw.Draw(img, "RGBA")
        dd.rectangle([0, 0, W, H], fill=(0, 0, 0, int(150 * ga)))
        p = eout((c.t - b[2] - 0.3) / 2.5)
        tx(dd, (W / 2, H / 2 - 40), f"{int(600000 * p):,}" + ("+" if p >= 1 else ""), F("reg", 220), CREAM, ga, anchor="mm")
        spaced(dd, (W / 2, H / 2 + 110), "SOLDIERS KILLED  ·  1861 – 1865", F("sc", 46), GOLD, ap(c.t, b[2] + 0.8), track=6)


def p_gettysburg(img, c):
    quote("…that this nation, under God, shall have a new birth of freedom…",
          "ABRAHAM LINCOLN\nGettysburg Address, November 19, 1863", beat=3, delay=0.7)(img, c)
    d = ImageDraw.Draw(img, "RGBA")
    a = ap(c.t, c.b[3] - 0.2) * (1 - 0.6 * ap(c.t, c.b[3] + 1.8))
    spaced(d, (W / 2, 150), "JANUARY 1, 1863  ·  THE EMANCIPATION PROCLAMATION", F("sc", 38), GOLD, a, track=5)


def p_thirteenth(img, c):
    img.paste(paper())
    d = ImageDraw.Draw(img, "RGBA")
    b4 = c.b[4]
    span = c.end_of(4) - b4
    spaced(d, (W / 2, 170), "APRIL 9, 1865  ·  LEE SURRENDERS AT APPOMATTOX", F("sc", 36), INK, ap(c.t, b4 - 0.3), track=4)
    spaced(d, (W / 2, 230), "APRIL 15, 1865  ·  LINCOLN DIES OF HIS WOUNDS", F("sc", 36), RED, ap(c.t, b4 + span * 0.22), track=4)
    t0 = b4 + span * 0.5
    rule(d, 500, W - 500, 300, INK, ap(c.t, t0 - 0.5) * 0.6)
    tx(d, (W / 2, 400), "Amendment XIII", F("reg", 110), INK, ap(c.t, t0 - 0.3), anchor="mm")
    tx(d, (W / 2, 485), "Ratified December 6, 1865", F("it", 44), RED, ap(c.t, t0), anchor="mm")
    text = ("“Neither slavery nor involuntary servitude, except as a punishment for crime whereof the party "
            "shall have been duly convicted, shall exist within the United States, or any place subject to "
            "their jurisdiction.”")
    for i, line in enumerate(wrap(d, text, F("it", 50), 1350)):
        tx(d, (W / 2, 610 + i * 70), line, F("it", 50), INK, ap(c.t, t0 + 0.6 + 0.25 * i), anchor="mm")


def p_reconstruction(img, c):
    img.paste(paper())
    d = ImageDraw.Draw(img, "RGBA")
    t0 = c.b[0]
    items = [("XIII", "1865", "Abolishes slavery"), ("XIV", "1868", "Citizenship and equal protection"),
             ("XV", "1870", "Voting rights regardless of race")]
    spaced(d, (W / 2, 170), "THE RECONSTRUCTION AMENDMENTS", F("sc", 52), INK, ap(c.t, t0 - 0.3), track=8)
    for i, (num, yr, s) in enumerate(items):
        a = ap(c.t, t0 + 0.4 + 0.5 * i)
        x = W / 2 + (i - 1) * 540
        d.rounded_rectangle([x - 230, 280, x + 230, 640], radius=10, outline=rgba(INK, a * 0.6), width=3)
        tx(d, (x, 380), num, F("reg", 120), RED, a, anchor="mm")
        tx(d, (x, 480), yr, F("it", 50), INK, a, anchor="mm")
        for j, line in enumerate(wrap(d, s, F("reg", 40), 380)):
            tx(d, (x, 560 + j * 48), line, F("reg", 40), INK, a, anchor="mm")
    span = c.end_of(0) - t0
    ja = ap(c.t, t0 + span * 0.55)
    if ja > 0:
        d.rectangle([0, 730, W, 920], fill=rgba((40, 28, 22), 0.92 * ja))
        spaced(d, (W / 2, 790), "JIM CROW", F("bold", 76), (230, 120, 100), ja, track=14)
        tx(d, (W / 2, 865), "Segregation and disenfranchisement, 1877 – 1965", F("it", 44), CREAM, ja, anchor="mm")


RAIL = [(-121.49, 38.58), (-119.8, 39.53), (-117.7, 40.97), (-114.9, 41.1), (-112.55, 41.62),
        (-111.97, 41.22), (-109.2, 41.6), (-104.82, 41.14), (-100.77, 41.12), (-98.3, 40.9), (-95.94, 41.26)]


def p_railroad(img, c):
    img.paste(us_bg())
    t0 = c.b[1]
    d = us_map(img, US_BIG, state_fill(1869, color=(150, 128, 92), base=0.7))
    pts = [US_BIG.lonlat(*p) for p in RAIL]
    prog = eout((c.t - t0) / 3.2)
    seg = []
    total = sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
    left = total * prog
    seg.append(pts[0])
    for i in range(len(pts) - 1):
        L = math.dist(pts[i], pts[i + 1])
        if left >= L:
            seg.append(pts[i + 1])
            left -= L
        else:
            f = left / L
            seg.append((pts[i][0] + (pts[i + 1][0] - pts[i][0]) * f, pts[i][1] + (pts[i + 1][1] - pts[i][1]) * f))
            break
    if len(seg) > 1:
        d.line(seg, fill=(20, 18, 16, 255), width=12, joint="curve")
        d.line(seg, fill=rgba(GOLD, 1), width=6, joint="curve")
    pin(img, d, *pts[0], "SACRAMENTO", ap(c.t, t0), side="l")
    pin(img, d, *pts[-1], "OMAHA", ap(c.t, t0 + 2.6))
    x, y = US_BIG.lonlat(-112.55, 41.62)
    a = ap(c.t, t0 + 3.0)
    paste_glow(img, x, y, 60, GOLD, a * 0.8)
    spaced(d, (W / 2, 130), "MAY 10, 1869  ·  PROMONTORY SUMMIT, UTAH", F("sc", 44), CREAM, a, track=6)
    tx(d, (W / 2, 190), "The golden spike completes the first transcontinental railroad", F("it", 40), DIM, a, anchor="mm")


def p_nineteenth(img, c):
    img.paste(paper())
    d = ImageDraw.Draw(img, "RGBA")
    t0 = c.b[3]
    tx(d, (W / 2, 320), "Amendment XIX", F("reg", 120), INK, ap(c.t, t0 - 0.2), anchor="mm")
    tx(d, (W / 2, 410), "Ratified August 18, 1920", F("it", 46), RED, ap(c.t, t0 + 0.2), anchor="mm")
    text = ("“The right of citizens of the United States to vote shall not be denied or abridged by the "
            "United States or by any State on account of sex.”")
    for i, line in enumerate(wrap(d, text, F("it", 54), 1350)):
        tx(d, (W / 2, 560 + i * 76), line, F("it", 54), INK, ap(c.t, t0 + 0.8 + 0.3 * i), anchor="mm")


UNEMP = [(1929, 3.2), (1930, 8.7), (1931, 15.9), (1932, 23.6), (1933, 24.9), (1934, 21.7), (1935, 20.1),
         (1936, 16.9), (1937, 14.3), (1938, 19.0), (1939, 17.2), (1940, 14.6), (1941, 9.9), (1942, 4.7),
         (1943, 1.9), (1944, 1.2), (1945, 1.9)]


def p_unemployment(img, c):
    img.paste(dark())
    d = ImageDraw.Draw(img, "RGBA")
    t0 = c.b[1]
    x0, x1, y0, y1 = 260, W - 200, 860, 260
    spaced(d, (x0, 160), "UNEMPLOYMENT RATE, 1929 – 1945", F("sc", 44), CREAM, ap(c.t, t0 - 0.5), track=6, anchor="l")
    for pct in (0, 10, 20, 30):
        y = y0 - (y0 - y1) * pct / 30
        d.line([(x0, y), (x1, y)], fill=(255, 255, 255, 30 if pct else 90), width=2)
        tx(d, (x0 - 24, y), f"{pct}%", F("reg", 34), DIM, ap(c.t, t0 - 0.5), anchor="rm")
    n = len(UNEMP)
    bw = (x1 - x0) / n
    for i, (yr, v) in enumerate(UNEMP):
        p = eout((c.t - t0 - 0.12 * i) / 0.8)
        h = (y0 - y1) * v / 30 * p
        col = RED if yr == 1933 else GOLD
        d.rectangle([x0 + i * bw + 8, y0 - h, x0 + (i + 1) * bw - 8, y0], fill=rgba(col, 0.9))
        if yr % 4 == 1 or yr == 1945:
            tx(d, (x0 + (i + 0.5) * bw, y0 + 36), str(yr), F("reg", 32), DIM, ap(c.t, t0), anchor="mm")
    a = ap(c.t, t0 + 4 * 0.12 + 0.9)
    x = x0 + 4.5 * bw
    y = y0 - (y0 - y1) * 24.9 / 30
    tx(d, (x, y - 50), "24.9%", F("reg", 72), (240, 130, 110), a, anchor="mm")
    tx(d, (x + 90, y + 10), "about one in four workers", F("it", 36), CREAM, a, anchor="lm")
    tx(d, (x1, y0 + 90), "Source: Historical Statistics of the United States (Lebergott series)", F("it", 26), DIM,
       ap(c.t, t0 + 1), anchor="rm")


def p_beams(img, c):
    img.paste(Image.new("RGB", (W, H), (6, 8, 14)))
    t0 = c.b[1]
    a = ap(c.t, t0 - 0.3, 2.0)
    arr = np.asarray(img, dtype=np.float32).copy()
    xs = np.arange(W, dtype=np.float32)
    ys = np.arange(H, dtype=np.float32)
    for bx in (W * 0.68 - 70, W * 0.68 + 70):
        width = 18 + (H - ys) * 0.03
        prof = np.exp(-((xs[None, :] - bx) ** 2) / (2 * width[:, None] ** 2))
        fade = (0.35 + 0.65 * ys / H)[:, None]
        arr += (prof * fade * a)[..., None] * np.array((150, 175, 230), np.float32)
    img.paste(Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)))
    d = ImageDraw.Draw(img, "RGBA")
    spaced(d, (200, H / 2 - 30), "SEPTEMBER 11, 2001", F("sc", 56), CREAM, ap(c.t, t0 + 0.6), track=10, anchor="l")
    tx(d, (200, H / 2 + 40), "2,977 lives lost", F("it", 48), DIM, ap(c.t, t0 + 1.2), anchor="lm")


def p_moon(img, c):
    img.paste(Image.new("RGB", (W, H), (2, 2, 4)))
    starfield(img, a=0.8)
    t0 = c.b[4]
    moon = cached("moon", lambda: make_moon(430))
    p = c.t - t0
    s = 1.0 + 0.04 * clamp(p / 8)
    m = moon.resize((int(moon.width * s), int(moon.height * s)), Image.BILINEAR)
    m.putalpha(m.getchannel("A").point(lambda v: int(v * ap(c.t, t0 - 0.6, 1.5))))
    img.paste(m, (int(W * 0.66 - m.width / 2), int(H / 2 - m.height / 2 + 40)), m)
    d = ImageDraw.Draw(img, "RGBA")
    spaced(d, (120, 230), "JULY 20, 1969", F("sc", 52), GOLD, ap(c.t, t0 + 0.3), track=8, anchor="l")
    tx(d, (120, 310), "Apollo 11", F("reg", 110), CREAM, ap(c.t, t0 + 0.6), anchor="lm")
    q = ["“That's one small step", "for man, one giant", "leap for mankind.”"]
    for i, line in enumerate(q):
        tx(d, (120, 500 + i * 76), line, F("it", 62), CREAM, ap(c.t, t0 + 1.6 + 0.5 * i), anchor="lm")
    spaced(d, (120, 770), "NEIL ARMSTRONG", F("sc", 36), DIM, ap(c.t, t0 + 3.2), track=6, anchor="l")


POP = [(1790, 3.93), (1800, 5.31), (1810, 7.24), (1820, 9.64), (1830, 12.87), (1840, 17.07), (1850, 23.19),
       (1860, 31.44), (1870, 38.56), (1880, 50.19), (1890, 62.98), (1900, 76.21), (1910, 92.23), (1920, 106.02),
       (1930, 123.20), (1940, 132.16), (1950, 151.33), (1960, 179.32), (1970, 203.21), (1980, 226.55),
       (1990, 248.71), (2000, 281.42), (2010, 308.75), (2020, 331.45)]


def p_population(img, c):
    img.paste(dark())
    d = ImageDraw.Draw(img, "RGBA")
    t0 = c.b[0]
    x0, x1, y0, y1 = 240, W - 300, 880, 250
    spaced(d, (x0, 160), "U.S. POPULATION BY CENSUS, 1790 – 2020", F("sc", 42), CREAM, ap(c.t, t0 - 0.6), track=6, anchor="l")
    for m in (0, 100, 200, 300):
        y = y0 - (y0 - y1) * m / 350
        d.line([(x0, y), (x1, y)], fill=(255, 255, 255, 90 if m == 0 else 28), width=2)
        tx(d, (x0 - 24, y), f"{m}M", F("reg", 34), DIM, 1, anchor="rm")
    for yr in (1800, 1850, 1900, 1950, 2000):
        x = x0 + (x1 - x0) * (yr - 1790) / 230
        tx(d, (x, y0 + 40), str(yr), F("reg", 34), DIM, 1, anchor="mm")
    prog = ease((c.t - t0) / 4.5)
    pts = [(x0 + (x1 - x0) * (yr - 1790) / 230, y0 - (y0 - y1) * v / 350) for yr, v in POP]
    n = 1 + prog * (len(pts) - 1)
    k = int(n)
    seg = pts[:k]
    if k < len(pts):
        f = n - k
        seg.append((pts[k - 1][0] + (pts[k][0] - pts[k - 1][0]) * f, pts[k - 1][1] + (pts[k][1] - pts[k - 1][1]) * f))
    if len(seg) > 1:
        d.polygon(seg + [(seg[-1][0], y0), (seg[0][0], y0)], fill=rgba(GOLD, 0.12))
        d.line(seg, fill=rgba(GOLD, 1), width=6, joint="curve")
        hx, hy = seg[-1]
        paste_glow(img, hx, hy, 50, GOLD, 0.8)
        d = ImageDraw.Draw(img, "RGBA")
        d.ellipse([hx - 9, hy - 9, hx + 9, hy + 9], fill=rgba(CREAM, 1))
    a = ap(c.t, t0 + 4.5)
    tx(d, (pts[-1][0] - 20, pts[-1][1] - 70), "331 million", F("reg", 76), CREAM, a, anchor="rm")
    tx(d, (pts[0][0] + 10, pts[0][1] - 50), "3.9 million", F("it", 40), DIM, ap(c.t, t0 + 0.3), anchor="lm")
    tx(d, (x1, y0 + 100), "Source: U.S. Census Bureau", F("it", 26), DIM, 1, anchor="rm")


def p_finale(img, c):
    img.paste(Image.new("RGB", (W, H), (8, 8, 10)))
    flag = cached("flag50", lambda: make_flag(560, stars=50))
    t1, t2 = c.b[1], c.b[2]
    f = wave_flag(flag, c.t)
    a = ap(c.t, t1 - 0.5, 1.6) * (1 - 0.75 * ap(c.t, t2 + 0.5, 2.0))
    f.putalpha(f.getchannel("A").point(lambda v: int(v * a)))
    img.paste(f, (int(W / 2 - f.width / 2), int(H / 2 - f.height / 2 - 20)), f)
    d = ImageDraw.Draw(img, "RGBA")
    e = ap(c.t, t2 + 1.0, 2.0)
    spaced(d, (W / 2, H / 2 - 40), "AMERICA", F("sc", 170), CREAM, e, track=40)
    tx(d, (W / 2, H / 2 + 90), "The story continues.", F("it", 60), GOLD, ap(c.t, t2 + 1.8, 1.5), anchor="mm")
    cr = ap(c.t, c.dur - 5.0, 1.5)
    credits = ["Written and produced by Claude", "Maps: Natural Earth & U.S. Census Bureau, via world-atlas and us-atlas",
               "Data: U.S. Census Bureau · Historical Statistics of the United States",
               "Voice: Festival HTS  ·  Built with Python, NumPy, Pillow & FFmpeg"]
    for i, line in enumerate(credits):
        tx(d, (W / 2, H - 230 + i * 46), line, F("it", 32), DIM, cr, anchor="mm")


# --------------------------------------------------------------------------
# The film
# --------------------------------------------------------------------------

SCENES = [
    dict(id="open", chapter=None, title="", years="", era=None, sepia=0.6, lead=1.6, tail=3.2,
         lines=["This is the story of a nation that began as an idea.",
                "And of the people who fought over what that idea should mean."],
         panels=[(0, p_title)]),
    dict(id="first", chapter="I", title="The First Peoples", years="Before 1492", era=(1450, 1492), sepia=0.75,
         lines=["Long before Europeans arrived, millions of people lived in North America.",
                "Hundreds of distinct nations built farms, trade networks, and towns.",
                "Around 1100, Cahokia, near present-day St. Louis, may have held as many as twenty thousand people."],
         panels=[(0, p_first_peoples)]),
    dict(id="colonies", chapter="II", title="Two Worlds Collide", years="1492 – 1732", era=(1492, 1732), sepia=0.75,
         lines=["In 1492, Columbus crossed the Atlantic, and two worlds collided.",
                "European diseases and warfare devastated Native communities.",
                "In 1607, the English founded Jamestown, in Virginia.",
                "In 1619, the first recorded enslaved Africans arrived nearby, the start of a system that would shape the nation for centuries.",
                "By 1732, there were thirteen British colonies."],
         panels=[(0, p_atlantic), (4, p_colonies)]),
    dict(id="revolution", chapter="III", title="Revolution", years="1765 – 1783", era=(1765, 1783), sepia=0.8,
         lines=["Anger over taxation without representation turned to rebellion.",
                "In April 1775, the first shots were fired at Lexington and Concord.",
                "On July 4th, 1776, Congress adopted the Declaration of Independence.",
                "Its promise that all men are created equal excluded many, but generations would fight to make it true.",
                "With help from France, the Americans won at Yorktown in 1781, and in 1783, Britain recognized their independence."],
         panels=[(0, p_broadside),
                 (1, card("APRIL 19, 1775", "Lexington & Concord", "The first battles of the American Revolution", beat=1)),
                 (2, p_declaration), (4, p_flag13)]),
    dict(id="constitution", chapter="IV", title="We the People", years="1787 – 1791", era=(1787, 1791), sepia=0.8,
         lines=["In 1787, delegates in Philadelphia wrote a new Constitution.",
                "It began with three words: We the People.",
                "In 1789, George Washington became the first president.",
                "And in 1791, the Bill of Rights guaranteed freedom of speech, religion, and the press."],
         panels=[(0, card("PHILADELPHIA · SUMMER 1787", "The Constitutional Convention",
                          "Fifty-five delegates from twelve states", bg="paper")),
                 (1, p_we_the_people),
                 (2, card("APRIL 30, 1789", "George Washington", "Inaugurated as the first President of the United States", beat=2)),
                 (3, p_bill_of_rights)]),
    dict(id="west", chapter="V", title="Westward", years="1800 – 1850", era=(1800, 1850), sepia=0.7,
         lines=["The young nation pushed west.",
                "The Louisiana Purchase of 1803 doubled its size.",
                "But expansion came at a terrible cost.",
                "After the Indian Removal Act of 1830, tens of thousands of Native people were forced from their homelands, on the Trail of Tears.",
                "War with Mexico brought in the Southwest, and in 1848, gold was found in California."],
         panels=[(0, p_west)]),
    dict(id="civilwar", chapter="VI", title="A House Divided", years="1860 – 1865", era=(1860, 1865), sepia=0.3,
         lines=["Expansion reopened the deepest question of all: would slavery spread?",
                "After Abraham Lincoln's election in 1860, eleven Southern states seceded to form the Confederacy.",
                "The war that began in 1861 killed more than six hundred thousand soldiers.",
                "In 1863, Lincoln issued the Emancipation Proclamation, and at Gettysburg, called for a new birth of freedom.",
                "The war ended in 1865. Days later, Lincoln was assassinated. That December, the Thirteenth Amendment abolished slavery."],
         panels=[(0, p_civil_map), (3, p_gettysburg), (4, p_thirteenth)]),
    dict(id="industry", chapter="VII", title="Rails, Steel & Ballots", years="1865 – 1920", era=(1865, 1920), sepia=0.6,
         lines=["Reconstruction promised citizenship and the vote, promises Jim Crow laws would betray for nearly a century.",
                "In 1869, the transcontinental railroad tied the continent together.",
                "Factories boomed, and millions of immigrants arrived, twelve million through Ellis Island alone.",
                "In 1920, the Nineteenth Amendment gave women the vote."],
         panels=[(0, p_reconstruction), (1, p_railroad),
                 (2, bignum("12,000,000", "IMMIGRANTS THROUGH ELLIS ISLAND", "New York Harbor, 1892 – 1954", beat=2)),
                 (3, p_nineteenth)]),
    dict(id="depression", chapter="VIII", title="Depression & War", years="1929 – 1945", era=(1929, 1945), sepia=0.5,
         lines=["In October 1929, the stock market crashed.",
                "By 1933, one in four workers was unemployed.",
                "Franklin Roosevelt answered with the New Deal, and a promise: the only thing we have to fear is fear itself.",
                "On December 7th, 1941, Japan attacked Pearl Harbor.",
                "Sixteen million Americans served in the Second World War, and the nation emerged a superpower."],
         panels=[(0, card("OCTOBER 29, 1929", "Black Tuesday", "The stock market crash ushers in the Great Depression")),
                 (1, p_unemployment),
                 (2, quote("The only thing we have to fear is fear itself.",
                           "FRANKLIN D. ROOSEVELT\nFirst Inaugural Address, March 4, 1933", beat=2, delay=1.6)),
                 (3, card("DECEMBER 7, 1941", "Pearl Harbor", "“a date which will live in infamy”", beat=3)),
                 (4, bignum("16,000,000", "AMERICANS IN UNIFORM", "World War II, 1941 – 1945", beat=4))]),
    dict(id="rights", chapter="IX", title="Freedom Now", years="1945 – 1969", era=(1945, 1969), sepia=0.3,
         lines=["Peace brought prosperity, and a long Cold War with the Soviet Union.",
                "At home, Black Americans led a movement for long-denied rights.",
                "In 1954, the Supreme Court struck down school segregation. In 1955, Rosa Parks refused to give up her seat.",
                "Marches and sacrifice won the Civil Rights Act of 1964, and the Voting Rights Act of 1965.",
                "And on July 20th, 1969, Neil Armstrong walked on the Moon."],
         panels=[(0, card("1947 – 1991", "The Cold War", "A nuclear-age rivalry between two superpowers")),
                 (1, timeline([(1954, "Brown v. Board of Education", None), (1955, "Montgomery Bus Boycott", None),
                               (1963, "March on Washington", None), (1964, "Civil Rights Act", None),
                               (1965, "Voting Rights Act", None)],
                              [(2, 0.0), (2, 0.5), (3, 0.0), (3, 0.35), (3, 0.65)],
                              title="THE CIVIL RIGHTS MOVEMENT")),
                 (4, p_moon)]),
    dict(id="modern", chapter="X", title="A New Century", years="1970 – Today", era=(1970, 2020), sepia=0.12,
         lines=["Then came the end of the Vietnam War, the collapse of the Soviet Union, and the rise of the computer and the internet.",
                "On September 11th, 2001, terrorist attacks killed nearly three thousand people.",
                "In 2008, Barack Obama was elected the first Black president.",
                "And in 2020, a global pandemic tested the nation once again."],
         panels=[(0, timeline([(1975, "Vietnam War ends", None), (1981, "The personal computer", None),
                               (1989, "Berlin Wall falls", None), (1991, "Soviet Union dissolves", None),
                               (1991, "World Wide Web opens", None)],
                              [(0, 0.0), (0, 0.2), (0, 0.25), (0, 0.35), (0, 0.6)])),
                 (1, p_beams),
                 (2, card("NOVEMBER 4, 2008", "Barack Obama", "Elected the 44th President, the first Black American to hold the office", beat=2)),
                 (3, card("2020", "A Global Pandemic", "COVID-19 transforms daily life across the nation", beat=3))]),
    dict(id="epilogue", chapter=None, title="Epilogue", years="", era=None, sepia=0.0, lead=1.0, tail=7.5,
         lines=["Today, more than three hundred thirty million people call the United States home.",
                "Its history is one of courage and cruelty, of promises made, broken, and fought for again.",
                "It is a story still being written."],
         panels=[(0, p_population), (1, p_finale)]),
]

ROMAN_SCENE_LEAD = 3.0
XFADE = 0.9

# --------------------------------------------------------------------------
# Narration
# --------------------------------------------------------------------------

ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()


def two(n):
    if n < 20:
        return ONES[n]
    return TENS[n // 10] + ("" if n % 10 == 0 else " " + ONES[n % 10])


def year_words(y):
    if 2000 <= y < 2010:
        return "two thousand" + ("" if y == 2000 else " " + ONES[y - 2000])
    hi, lo = divmod(y, 100)
    if lo == 0:
        return two(hi) + " hundred"
    return two(hi) + " " + (two(lo) if lo >= 10 else "oh " + ONES[lo])


def spoken(s):
    for a, b in {"4th": "fourth", "7th": "seventh", "11th": "eleventh", "20th": "twentieth",
                 "St. Louis": "Saint Louis", "Rosa Parks": "Rosa Parks,"}.items():
        s = s.replace(a, b)
    return re.sub(r"\b(1[0-9]{3}|20[0-2][0-9])\b", lambda m: year_words(int(m.group(1))), s)


def synth(text):
    os.makedirs(os.path.join(BUILD, "tts"), exist_ok=True)
    key = hashlib.sha1(text.encode()).hexdigest()[:16]
    out = os.path.join(BUILD, "tts", key + ".wav")
    if not os.path.exists(out):
        raw = out + ".raw.wav"
        txt = out + ".txt"
        with open(txt, "w") as f:
            f.write(spoken(text))
        subprocess.run(["text2wave", "-eval", "(voice_cmu_us_slt_arctic_hts)", txt, "-o", raw], check=True)
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", raw, "-ar", str(SR), "-ac", "1",
                        "-af", "atempo=1.04", out], check=True)
        os.remove(raw)
        os.remove(txt)
    with wave.open(out) as w:
        data = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
    # trim silence
    idx = np.where(np.abs(data) > 0.01)[0]
    return data[max(0, idx[0] - 400): idx[-1] + 2000] if len(idx) else data


def plan():
    """Synthesize narration and lay out the timeline; returns scenes with timing and the voice track."""
    clips = [[synth(s) for s in sc["lines"]] for sc in SCENES]
    speech = sum(len(c) for cl in clips for c in cl) / SR
    fixed = 0
    ngaps = 0
    for sc in SCENES:
        fixed += sc.get("lead", ROMAN_SCENE_LEAD) + sc.get("tail", 1.2)
        ngaps += len(sc["lines"]) - 1
    gap = max(0.4, (TARGET_SECONDS - speech - fixed) / ngaps)
    t = 0.0
    timeline_ = []
    voice = []
    for sc, cl in zip(SCENES, clips):
        beats = []
        cur = sc.get("lead", ROMAN_SCENE_LEAD)
        for i, clip in enumerate(cl):
            beats.append(cur)
            voice.append((t + cur, clip, sc["lines"][i]))
            cur += len(clip) / SR + (gap if i < len(cl) - 1 else 0)
        cur += sc.get("tail", 1.2)
        frames = int(math.ceil(cur * FPS))
        timeline_.append(dict(scene=sc, start=t, frames=frames, dur=frames / FPS, beats=beats))
        t += frames / FPS
    return timeline_, voice, t, gap


# --------------------------------------------------------------------------
# Frame rendering
# --------------------------------------------------------------------------

def grain_frames():
    def mk():
        rng = np.random.default_rng(5)
        out = []
        for _ in range(6):
            g = rng.normal(0, 1, (H // 2, W // 2)).astype(np.float32)
            im = Image.fromarray(np.clip(g * 40 + 128, 0, 255).astype(np.uint8)).resize((W, H), Image.BILINEAR)
            out.append(((np.asarray(im, np.float32) - 128) / 40)[..., None])
        return out
    return cached("grain", mk)


def vignette():
    def mk():
        yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
        r = np.sqrt(((xx - W / 2) / (W / 2)) ** 2 + ((yy - H / 2) / (H / 2)) ** 2) / math.sqrt(2)
        return (1 - 0.55 * np.clip(r, 0, 1) ** 2.4)[..., None]
    return cached("vig", mk)


TONE = np.array([1.08, 0.96, 0.78], np.float32)


def panel_at(entry, t):
    sc, beats = entry["scene"], entry["beats"]
    sched = []
    if sc["chapter"]:
        sched.append((0.0, chapter_card))
    for bi, fn in sc["panels"]:
        start = beats[bi] - 0.35 if not (bi == 0 and not sc["chapter"]) else 0.0
        sched.append((start, fn))
    cur = 0
    for i, (st, _) in enumerate(sched):
        if t >= st:
            cur = i
    return sched, cur


def render_panel(fn, ctx):
    img = Image.new("RGB", (W, H), (0, 0, 0))
    fn(img, ctx)
    return img


def overlay(img, entry, t):
    sc = entry["scene"]
    if not sc["era"] or t < ROMAN_SCENE_LEAD - 0.4:
        return
    a = ap(t, ROMAN_SCENE_LEAD - 0.4, 0.8) * (1 - ap(t, entry["dur"] - 0.9, 0.5))
    d = ImageDraw.Draw(img, "RGBA")
    spaced(d, (90, 70), f"{sc['chapter']}  ·  {sc['title'].upper()}", F("sc", 30), CREAM, a * 0.75, track=4, anchor="l")
    x0, x1, y = 300, W - 300, H - 56
    y_a, y_b = 1450, 2030
    d.line([(x0, y), (x1, y)], fill=rgba(CREAM, a * 0.3), width=2)
    for yr in range(1500, 2001, 100):
        x = x0 + (x1 - x0) * (yr - y_a) / (y_b - y_a)
        d.line([(x, y - 6), (x, y + 6)], fill=rgba(CREAM, a * 0.4), width=2)
        tx(d, (x, y + 26), str(yr), F("reg", 24), CREAM, a * 0.45, anchor="mm")
    e0, e1 = sc["era"]
    p = ease((t - ROMAN_SCENE_LEAD) / max(1, entry["dur"] - ROMAN_SCENE_LEAD - 1))
    yr = e0 + (e1 - e0) * p
    x = x0 + (x1 - x0) * (yr - y_a) / (y_b - y_a)
    xs = x0 + (x1 - x0) * (e0 - y_a) / (y_b - y_a)
    d.line([(xs, y), (x, y)], fill=rgba(GOLD, a), width=4)
    d.ellipse([x - 7, y - 7, x + 7, y + 7], fill=rgba(GOLD, a))


def render_frame(entry, fi):
    t = fi / FPS
    sc, dur = entry["scene"], entry["dur"]
    ctx = Ctx(t, dur, entry["beats"], sc)
    sched, cur = panel_at(entry, t)
    img = render_panel(sched[cur][1], ctx)
    st = sched[cur][0]
    if cur > 0 and t < st + XFADE:
        prev = render_panel(sched[cur - 1][1], ctx)
        img = Image.blend(prev, img, ease((t - st) / XFADE))
    # slow camera push
    z = 1.0 + 0.05 * ease(t / dur)
    cw, ch = W / z, H / z
    cx = W / 2 + (W - cw) / 2 * 0.3 * math.sin(len(sc["id"]) * 1.7)
    cx = min(max(cx, cw / 2), W - cw / 2)
    img = img.resize((W, H), Image.BICUBIC, box=(cx - cw / 2, H / 2 - ch / 2, cx + cw / 2, H / 2 + ch / 2))
    overlay(img, entry, t)
    # grade
    a = np.asarray(img, dtype=np.float32)
    s = sc["sepia"]
    if s > 0:
        g = a @ np.array([0.299, 0.587, 0.114], np.float32)
        a = a * (1 - s) + (g[..., None] * TONE) * s
    a = a * vignette()
    gf = grain_frames()
    a = a + gf[fi % len(gf)] * (3.0 + 5.0 * s)
    flick = 1 + 0.018 * s * math.sin(t * 23.0) * math.sin(t * 7.3)
    fade = clamp(t / 0.6) * clamp((dur - t) / 0.6)
    a = a * (flick * fade)
    return np.clip(a, 0, 255).astype(np.uint8)


ENTRIES = []


def render_scene(idx):
    entry = ENTRIES[idx]
    path = os.path.join(BUILD, f"seg_{idx:02d}.mp4")
    cmd = ["ffmpeg", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
           "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-preset", "medium", "-crf", "19",
           "-pix_fmt", "yuv420p", "-threads", "2", path]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for fi in range(entry["frames"]):
        proc.stdin.write(render_frame(entry, fi).tobytes())
    proc.stdin.close()
    proc.wait()
    print(f"  scene {idx:2d} {entry['scene']['id']:<12} {entry['dur']:6.1f}s done", flush=True)
    return path


# --------------------------------------------------------------------------
# Score
# --------------------------------------------------------------------------

def note(name):
    names = {"C": -9, "C#": -8, "D": -7, "Eb": -6, "E": -5, "F": -4, "F#": -3, "G": -2, "Ab": -1, "A": 0, "Bb": 1, "B": 2}
    n, octave = name[:-1], int(name[-1])
    return 440.0 * 2 ** ((names[n] + 12 * (octave - 4)) / 12)


def score(total, chapter_times, voice_env):
    t = np.arange(int(total * SR)) / SR
    out = np.zeros_like(t, dtype=np.float32)
    minor = [["D3", "A3", "D4", "F4"], ["Bb2", "F3", "Bb3", "D4"], ["F2", "C3", "F3", "A3"], ["C3", "G3", "C4", "E4"]]
    major = [["F2", "C3", "A3", "F4"], ["C3", "G3", "E4", "G4"], ["D3", "A3", "F4", "A4"], ["Bb2", "F3", "D4", "F4"],
             ["F2", "C3", "A3", "F4"]]
    chord_len = 7.5
    final_start = chapter_times[-1]
    seq = []
    tt = 0.0
    k = 0
    while tt < final_start:
        seq.append((tt, minor[k % 4]))
        tt += chord_len
        k += 1
    for i, ch in enumerate(major):
        seq.append((final_start + i * chord_len * 0.9, ch))
    rng = np.random.default_rng(2)
    for i, (st, ch) in enumerate(seq):
        en = seq[i + 1][0] if i + 1 < len(seq) else total
        a0, a1 = int(max(0, st - 1.5) * SR), int(min(total, en + 2.5) * SR)
        if a0 >= len(t):
            break
        seg = t[a0:a1]
        env = np.clip((seg - (st - 1.5)) / 2.5, 0, 1) * np.clip((en + 2.5 - seg) / 2.5, 0, 1)
        acc = np.zeros_like(seg, dtype=np.float32)
        for nm in ch:
            f0 = note(nm)
            for det in (-0.12, 0.0, 0.11):
                f = f0 * 2 ** (det / 12)
                ph = rng.uniform(0, 2 * np.pi)
                for h in range(1, 7):
                    acc += (np.sin(2 * np.pi * f * h * seg + ph * h) / h ** 1.6).astype(np.float32)
        out[a0:a1] += (acc * env).astype(np.float32)
    out *= (0.85 + 0.15 * np.sin(2 * np.pi * t / 11.0)).astype(np.float32)
    out /= np.max(np.abs(out)) + 1e-9
    # timpani rolls under each chapter card
    for ct in chapter_times:
        i0 = int(ct * SR)
        n = int(3.5 * SR)
        if i0 + n > len(out):
            n = len(out) - i0
        if n <= 0:
            continue
        tt = np.arange(n) / SR
        f = 55 * (1 + 0.15 * np.exp(-tt * 6))
        boom = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-tt * 1.6)
        noise = np.convolve(rng.normal(0, 1, n), np.ones(60) / 60, mode="same") * np.exp(-tt * 9) * 0.6
        out[i0:i0 + n] += (0.9 * (boom + noise)).astype(np.float32)
    duck = 1 - 0.55 * voice_env
    fade = np.clip(t / 2.0, 0, 1) * np.clip((total - t) / 4.0, 0, 1)
    return (out * duck * fade * 0.22).astype(np.float32)


def build_audio(entries, voice, total):
    n = int(total * SR)
    vt = np.zeros(n, np.float32)
    subs = []
    for start, clip, text in voice:
        i = int(start * SR)
        m = min(len(clip), n - i)
        vt[i:i + m] += clip[:m]
        subs.append((start, start + len(clip) / SR, text))
    raw = os.path.join(BUILD, "voice_raw.wav")
    proc = os.path.join(BUILD, "voice.wav")
    write_wav(raw, vt)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", raw, "-af",
                    "highpass=f=80,equalizer=f=180:t=q:w=1.2:g=3,equalizer=f=3200:t=q:w=1.5:g=2,"
                    "aecho=0.85:0.7:38|71:0.10|0.06,acompressor=threshold=0.2:ratio=3:attack=10:release=200",
                    "-ar", str(SR), proc], check=True)
    with wave.open(proc) as w:
        vp = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
    vp = np.pad(vp, (0, max(0, n - len(vp))))[:n]
    env = np.abs(vt)
    k = int(0.4 * SR)
    env = np.convolve(env, np.ones(k) / k, mode="same")
    env = np.clip(env / (env.max() * 0.25 + 1e-9), 0, 1)
    chapter_times = [e["start"] for e in entries if e["scene"]["chapter"]] + [entries[-1]["start"]]
    music = score(total, chapter_times, env)
    mix = vp / (np.max(np.abs(vp)) + 1e-9) * 0.82 + music
    mix /= max(1.0, np.max(np.abs(mix)) / 0.97)
    out = os.path.join(BUILD, "mix.wav")
    write_wav(out, mix)
    return out, subs


def write_wav(path, data):
    pcm = (np.clip(data, -1, 1) * 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def srt_time(s):
    ms = int(round(s * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    sec, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"


def write_srt(subs):
    with open(SRT, "w") as f:
        for i, (a, b, text) in enumerate(subs, 1):
            f.write(f"{i}\n{srt_time(a)} --> {srt_time(b + 0.3)}\n{text}\n\n")


# --------------------------------------------------------------------------

def main():
    ap_ = argparse.ArgumentParser()
    ap_.add_argument("--stills", nargs="*", type=float, help="render frames at these global times (s) and exit")
    ap_.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    args = ap_.parse_args()
    os.makedirs(BUILD, exist_ok=True)

    entries, voice, total, gap = plan()
    print(f"film length {total:.1f}s ({int(total // 60)}:{total % 60:04.1f}), sentence gap {gap:.2f}s")
    for e in entries:
        print(f"  {e['scene']['id']:<12} start {e['start']:6.1f}  dur {e['dur']:5.1f}")

    if args.stills is not None:
        for gt in args.stills:
            for e in entries:
                if e["start"] <= gt < e["start"] + e["dur"]:
                    fi = int((gt - e["start"]) * FPS)
                    Image.fromarray(render_frame(e, fi)).save(os.path.join(BUILD, f"still_{gt:06.1f}.png"))
        return

    mix, subs = build_audio(entries, voice, total)
    write_srt(subs)
    ENTRIES[:] = entries
    order = sorted(range(len(entries)), key=lambda i: -entries[i]["frames"])
    with get_context("fork").Pool(args.jobs) as pool:
        segs = dict(zip(order, pool.map(render_scene, order, chunksize=1)))
    lst = os.path.join(BUILD, "segments.txt")
    with open(lst, "w") as f:
        for i in range(len(entries)):
            f.write(f"file '{segs[i]}'\n")
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst,
                    "-i", mix, "-i", SRT, "-map", "0:v", "-map", "1:a", "-map", "2:s",
                    "-c:v", "libx264", "-preset", "slow", "-crf", "25", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "192k", "-c:s", "mov_text",
                    "-metadata:s:s:0", "language=eng", "-metadata", "title=America: A Short History",
                    "-movflags", "+faststart", "-t", f"{total:.3f}", OUT], check=True)
    print("wrote", OUT)


if __name__ == "__main__":
    main()
