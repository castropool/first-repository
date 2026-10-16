"""Render "Hi, I'm Claude" — a short self-introduction video.

Every frame is drawn with Pillow and piped straight into ffmpeg; the soundtrack
is a soft synth pad generated with numpy. No external assets are needed.

    python3 video/make_video.py            # writes video/claude_intro.mp4
"""

import math
import os
import subprocess
import sys
import tempfile
import wave

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H, FPS = 1280, 720, 30
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "claude_intro.mp4")

INTER = "/usr/share/fonts/opentype/inter"
MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"

BG = (24, 22, 20)
INK = (240, 236, 228)
MUTED = (160, 152, 140)
ACCENT = (217, 119, 87)
CARD = (38, 35, 32)


def font(weight, size):
    return ImageFont.truetype(f"{INTER}/InterDisplay-{weight}.otf", size)


def mono(size):
    return ImageFont.truetype(MONO, size)


# --- easing / timing helpers ---------------------------------------------

def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def ease_out(t):
    t = clamp(t)
    return 1 - (1 - t) ** 3


def ease_in_out(t):
    t = clamp(t)
    return 3 * t * t - 2 * t * t * t


def window(t, start, fade_in, end, fade_out):
    """Opacity that rises over fade_in after start and falls over fade_out before end."""
    return clamp((t - start) / fade_in) * clamp((end - t) / fade_out)


def mix(c1, c2, a):
    return tuple(int(c1[i] + (c2[i] - c1[i]) * a) for i in range(3))


# --- drawing primitives ---------------------------------------------------

def text(draw, xy, s, fnt, color, alpha=1.0, anchor="la"):
    if alpha <= 0:
        return
    draw.text(xy, s, font=fnt, fill=mix(BG, color, alpha), anchor=anchor)


def spark(img, cx, cy, r, alpha, rot=0.0, color=ACCENT):
    """A radial starburst mark: tapered rays around a centre."""
    if alpha <= 0 or r <= 0:
        return
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    rays = 12
    for i in range(rays):
        a = rot + i * 2 * math.pi / rays
        length = r * (1.0 if i % 2 == 0 else 0.72)
        w = r * 0.11
        tip = (cx + math.cos(a) * length, cy + math.sin(a) * length)
        side = a + math.pi / 2
        b1 = (cx + math.cos(side) * w, cy + math.sin(side) * w)
        b2 = (cx - math.cos(side) * w, cy - math.sin(side) * w)
        d.polygon([b1, tip, b2], fill=color + (int(255 * alpha),))
    d.ellipse([cx - r * 0.16, cy - r * 0.16, cx + r * 0.16, cy + r * 0.16],
              fill=color + (int(255 * alpha),))
    img.alpha_composite(layer)


def glow(img, cx, cy, r, alpha):
    if alpha <= 0:
        return
    layer = Image.new("RGBA", (W // 4, H // 4), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    q = r / 4
    d.ellipse([cx / 4 - q, cy / 4 - q, cx / 4 + q, cy / 4 + q],
              fill=ACCENT + (int(70 * alpha),))
    layer = layer.filter(ImageFilter.GaussianBlur(q / 2)).resize((W, H), Image.BILINEAR)
    img.alpha_composite(layer)


def rounded(draw, box, fill, radius=18, outline=None):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=2)


def typed(s, t, start, cps):
    n = int(clamp((t - start) * cps, 0, len(s)))
    return s[:n], n < len(s)


def cursor_on(t):
    return int(t * 2) % 2 == 0


# --- scenes ----------------------------------------------------------------
# Each scene: (duration_seconds, draw_fn(img, draw, t_local, dur))

def scene_hello(img, d, t, dur):
    a = window(t, 0, 0.6, dur, 0.5)
    glow(img, W / 2, H / 2 - 40, 380, a)
    spark(img, W / 2, H / 2 - 90, 70 * ease_out(t / 1.2), a, rot=t * 0.4)
    d = ImageDraw.Draw(img)
    s, typing = typed("Hi, I'm Claude.", t, 1.0, 14)
    f = font("Bold", 84)
    full_w = d.textlength("Hi, I'm Claude.", font=f)
    x = W / 2 - full_w / 2
    text(d, (x, H / 2 + 20), s, f, INK, a)
    if (typing or cursor_on(t)) and t > 0.9:
        cx = x + d.textlength(s, font=f) + 6
        d.rectangle([cx, H / 2 + 34, cx + 6, H / 2 + 112], fill=mix(BG, ACCENT, a))
    sub_a = a * clamp((t - 2.6) / 0.6)
    text(d, (W / 2, H / 2 + 150), "An AI assistant made by Anthropic.",
         font("Regular", 32), MUTED, sub_a, anchor="ma")


def scene_what(img, d, t, dur):
    a = window(t, 0, 0.4, dur, 0.5)
    text(d, (110, 110), "What I do", font("Bold", 56), INK, a * ease_out(t / 0.5))
    items = [
        ("Write & edit", "essays, emails, stories, docs"),
        ("Code", "build features, fix bugs, review PRs"),
        ("Analyze", "data, papers, long documents"),
        ("Explain", "patiently, at whatever level you need"),
    ]
    for i, (title, desc) in enumerate(items):
        st = 0.5 + i * 0.45
        p = ease_out((t - st) / 0.6)
        if p <= 0:
            continue
        col, row = i % 2, i // 2
        x0 = 110 + col * 540 + (1 - p) * 60
        y0 = 210 + row * 200
        ca = a * p
        rounded(d, [x0, y0, x0 + 500, y0 + 160], mix(BG, CARD, ca))
        d.rectangle([x0, y0 + 28, x0 + 6, y0 + 132], fill=mix(BG, ACCENT, ca))
        text(d, (x0 + 36, y0 + 34), title, font("SemiBold", 40), INK, ca)
        text(d, (x0 + 36, y0 + 96), desc, font("Regular", 26), MUTED, ca)


TERMINAL_LINES = [
    ("$ ", "python3 video/make_video.py"),
    ("", "rendering scenes with Pillow ..."),
    ("", "synthesizing soundtrack with numpy ..."),
    ("", "encoding with ffmpeg -> claude_intro.mp4"),
    ("", "done."),
]


def scene_meta(img, d, t, dur):
    a = window(t, 0, 0.4, dur, 0.5)
    text(d, (110, 90), "Right now, I'm making this video.",
         font("Bold", 50), INK, a * ease_out(t / 0.5))
    text(d, (110, 160), "Running as Claude Code in a cloud container — no camera, just code.",
         font("Regular", 28), MUTED, a * clamp((t - 0.4) / 0.5))
    box = [110, 230, W - 110, 620]
    rounded(d, box, mix(BG, (14, 13, 12), a), radius=14, outline=mix(BG, (60, 56, 52), a))
    for i, c in enumerate([(237, 106, 94), (245, 191, 79), (98, 197, 84)]):
        d.ellipse([box[0] + 22 + i * 26, box[1] + 18, box[0] + 38 + i * 26, box[1] + 34],
                  fill=mix(BG, c, a))
    f = mono(26)
    y = box[1] + 70
    clock = 1.0
    for prompt, body in TERMINAL_LINES:
        if prompt:
            s, typing = typed(body, t, clock, 22)
            text(d, (box[0] + 30, y), prompt, f, ACCENT, a * (t > clock))
            text(d, (box[0] + 30 + d.textlength(prompt, font=f), y), s, f, INK, a * (t > clock))
            clock += len(body) / 22 + 0.3
        else:
            show = t > clock
            col = (130, 200, 120) if body == "done." else MUTED
            text(d, (box[0] + 30, y), body, f, col, a * show)
            clock += 0.7
        y += 54
    if cursor_on(t) and t > clock:
        d.rectangle([box[0] + 30, y + 2, box[0] + 44, y + 30], fill=mix(BG, INK, a))


def scene_values(img, d, t, dur):
    a = window(t, 0, 0.4, dur, 0.5)
    text(d, (W / 2, 140), "What I try to be", font("Bold", 56), INK,
         a * ease_out(t / 0.5), anchor="ma")
    words = [("Helpful", "Actually useful, not just agreeable."),
             ("Honest", "Truthful — even when it's not what you hoped."),
             ("Careful", "Thoughtful about harm, respectful of your choices.")]
    for i, (w, line) in enumerate(words):
        st = 0.6 + i * 0.7
        p = ease_out((t - st) / 0.6)
        y = 270 + i * 120 + (1 - p) * 30
        text(d, (400, y), w, font("Bold", 48), ACCENT, a * p, anchor="ra")
        text(d, (440, y + 14), line, font("Regular", 30), INK, a * p, anchor="la")


def scene_limits(img, d, t, dur):
    a = window(t, 0, 0.4, dur, 0.5)
    text(d, (110, 110), "And, honestly…", font("Bold", 56), INK, a * ease_out(t / 0.5))
    lines = [
        "I can be wrong. Double-check things that matter.",
        "I don't remember past chats unless I'm given memory.",
        "I'm not a person — but I do care about getting this right.",
    ]
    for i, s in enumerate(lines):
        st = 0.7 + i * 0.9
        p = ease_out((t - st) / 0.6)
        y = 240 + i * 110
        d.ellipse([118, y + 14, 132, y + 28], fill=mix(BG, ACCENT, a * p))
        text(d, (160 + (1 - p) * 40, y), s, font("Medium", 36), INK, a * p)


def scene_outro(img, d, t, dur):
    a = window(t, 0, 0.6, dur, 1.0)
    glow(img, W / 2, H / 2 - 60, 420, a)
    spark(img, W / 2, H / 2 - 80, 80 * (0.85 + 0.15 * ease_in_out(t / 1.5)), a, rot=t * 0.25)
    d = ImageDraw.Draw(img)
    text(d, (W / 2, H / 2 + 50), "What should we make together?",
         font("Bold", 60), INK, a * ease_out((t - 0.4) / 0.8), anchor="ma")
    text(d, (W / 2, H / 2 + 140), "— Claude", font("Regular", 32), MUTED,
         a * clamp((t - 1.4) / 0.6), anchor="ma")


SCENES = [
    (5.0, scene_hello),
    (6.0, scene_what),
    (8.5, scene_meta),
    (6.0, scene_values),
    (6.5, scene_limits),
    (5.0, scene_outro),
]


def render_frame(t_global):
    img = Image.new("RGBA", (W, H), BG + (255,))
    start = 0.0
    for dur, fn in SCENES:
        if start <= t_global < start + dur:
            fn(img, ImageDraw.Draw(img), t_global - start, dur)
            break
        start += dur
    # thin progress bar
    total = sum(s[0] for s in SCENES)
    d = ImageDraw.Draw(img)
    d.rectangle([0, H - 4, W * t_global / total, H], fill=mix(BG, ACCENT, 0.6))
    return img.convert("RGB")


# --- soundtrack -------------------------------------------------------------

def soundtrack(path, seconds, sr=44100):
    t = np.arange(int(seconds * sr)) / sr
    # slow chord progression: Cmaj7 -> Am9 -> Fmaj7 -> G6
    chords = [[261.63, 329.63, 392.00, 493.88],
              [220.00, 261.63, 329.63, 493.88],
              [174.61, 220.00, 261.63, 329.63],
              [196.00, 246.94, 293.66, 329.63]]
    seg = seconds / len(chords) / 2  # each chord twice through
    audio = np.zeros_like(t)
    for k in range(len(chords) * 2):
        freqs = chords[k % len(chords)]
        s0 = k * seg
        env = np.clip((t - s0) / 1.2, 0, 1) * np.clip((s0 + seg + 1.2 - t) / 1.2, 0, 1)
        for f in freqs:
            audio += env * (np.sin(2 * np.pi * f * t) + 0.3 * np.sin(2 * np.pi * f * 2.003 * t))
    audio *= 0.8 + 0.2 * np.sin(2 * np.pi * 0.2 * t)  # gentle swell
    fade = np.clip(t / 1.5, 0, 1) * np.clip((seconds - t) / 2.5, 0, 1)
    audio = audio * fade
    audio = audio / np.max(np.abs(audio)) * 0.25
    pcm = (audio * 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def main():
    total = sum(s[0] for s in SCENES)
    nframes = int(total * FPS)
    with tempfile.TemporaryDirectory() as tmp:
        wav = os.path.join(tmp, "pad.wav")
        soundtrack(wav, total)
        cmd = ["ffmpeg", "-y", "-loglevel", "error",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
               "-i", wav,
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", "-preset", "medium",
               "-c:a", "aac", "-b:a", "128k", "-shortest", "-movflags", "+faststart", OUT]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        for i in range(nframes):
            proc.stdin.write(render_frame(i / FPS).tobytes())
            if i % FPS == 0:
                sys.stdout.write(f"\r{i / FPS:5.1f}s / {total:.1f}s")
                sys.stdout.flush()
        proc.stdin.close()
        proc.wait()
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
