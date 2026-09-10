"""Draw the Curbside app icon, once, into every size the web needs.

PRODUCT.md says no logo and no wordmark, and that still holds for the interface
itself: the top bar says "Curbside" in the same face as everything else. An
installed app is the exception it did not anticipate. Android puts a tile on a
home screen next to thirty others and gives you no words to tell them apart, so
the choice is a mark or a grey default square.

The mark is the thing the bot is named after: a box left at the edge of a curb,
where the pavement steps down to the street. Two shapes, no detail that dies at
32px, one accent blue.

Geometry lives here in Python and is emitted as BOTH the SVG and the PNGs, so
the crisp favicon and the raster tiles cannot drift apart.

    .venv/bin/python tools/make_icons.py
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "dealbot" / "web" / "static"

BLUE = (43, 100, 214)          # --accent, light theme
WHITE = (255, 255, 255)
G = 512                        # design grid
SS = 4                         # supersample: draw big, shrink with LANCZOS

STROKE = 34
# The curb: pavement on the left, a step down, then the street. Drawn as one
# polyline so the corner is a single round join rather than two shapes meeting.
CURB = [(72, 330), (352, 330), (352, 396), (452, 396)]
# Sits ON the pavement: the bottom edge's ink stops exactly where the curb's
# begins. A gap reads as floating and an overlap reads as one blob.
BOX = (128, 146, 300, 296)     # left, top, right, bottom
BOX_R = 24
# There is no tape line across the box. Three versions had one and every one of
# them closed up into a smudge by 32px, which is the size that has to work --
# it is the browser tab, and it is the home screen icon at arm's length.


def _ink_bbox() -> tuple[float, float, float, float]:
    """Everything drawn, stroke included, so the mark can be optically centred.
    Composed by eye it sits low and left; centring is arithmetic, not taste."""
    h = STROKE / 2
    xs = [x for x, _ in CURB] + [BOX[0], BOX[2]]
    ys = [y for _, y in CURB] + [BOX[1], BOX[3]]
    return min(xs) - h, min(ys) - h, max(xs) + h, max(ys) + h


def _offset(scale: float = 1.0) -> tuple[float, float]:
    x0, y0, x1, y1 = _ink_bbox()
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    return G / 2 - cx * scale, G / 2 - cy * scale


def _shapes(scale: float = 1.0):
    """The mark at `scale` about the grid centre, as (kind, args) tuples."""
    dx, dy = _offset(scale)

    def t(p):
        return (p[0] * scale + dx, p[1] * scale + dy)

    stroke = STROKE * scale
    yield "line", ([t(p) for p in CURB], stroke)
    yield "rect", (t((BOX[0], BOX[1])) + t((BOX[2], BOX[3])), BOX_R * scale, stroke)


def draw_png(size: int, *, bleed: bool, scale: float = 1.0) -> Image.Image:
    """One tile. `bleed` fills the square (Android masks it itself, iOS rounds
    it); otherwise the corners are rounded here and left transparent."""
    px = size * SS
    img = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    k = px / G

    if bleed:
        d.rectangle((0, 0, px, px), fill=BLUE)
    else:
        d.rounded_rectangle((0, 0, px - 1, px - 1), radius=0.22 * px, fill=BLUE)

    for kind, args in _shapes(scale):
        if kind == "line":
            pts, w = args
            pts = [(x * k, y * k) for x, y in pts]
            w *= k
            # Pillow rounds joins but not caps, so the ends get their own dots.
            d.line(pts, fill=WHITE, width=int(round(w)), joint="curve")
            for x, y in (pts[0], pts[-1]):
                d.ellipse((x - w / 2, y - w / 2, x + w / 2, y + w / 2), fill=WHITE)
        else:
            (x0, y0, x1, y1), r, w = args
            d.rounded_rectangle((x0 * k, y0 * k, x1 * k, y1 * k), radius=r * k,
                                outline=WHITE, width=int(round(w * k)))
    return img.resize((size, size), Image.LANCZOS)


def svg() -> str:
    parts = [f'<rect width="{G}" height="{G}" rx="112" fill="#2b64d6"/>']
    for kind, args in _shapes():
        if kind == "line":
            pts, w = args
            pts = " ".join(f"{x:.0f},{y:.0f}" for x, y in pts)
            parts.append(f'<polyline points="{pts}"/>')
        else:
            (x0, y0, x1, y1), r, w = args
            parts.append(f'<rect x="{x0:.0f}" y="{y0:.0f}" width="{x1 - x0:.0f}" '
                         f'height="{y1 - y0:.0f}" rx="{r:.0f}"/>')
    body = "\n  ".join(parts)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {G} {G}">\n'
            f'  <g fill="none" stroke="#fff" stroke-width="{STROKE}" '
            f'stroke-linecap="round" stroke-linejoin="round">\n  {body}\n  </g>\n'
            f'</svg>\n').replace(
        f'<g fill="none" stroke="#fff" stroke-width="{STROKE}" '
        f'stroke-linecap="round" stroke-linejoin="round">\n  <rect '
        f'width="{G}" height="{G}" rx="112" fill="#2b64d6"/>',
        f'<rect width="{G}" height="{G}" rx="112" fill="#2b64d6"/>\n'
        f'  <g fill="none" stroke="#fff" stroke-width="{STROKE}" '
        f'stroke-linecap="round" stroke-linejoin="round">')


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    made = []
    for name, size, bleed, scale in (
            ("icon-192.png", 192, False, 1.06),
            ("icon-512.png", 512, False, 1.06),
            # Maskable: Android crops to a circle at worst, so the mark shrinks
            # into the safe zone and the blue runs to the edge.
            ("icon-maskable-512.png", 512, True, 0.66),
            ("apple-touch-icon.png", 180, True, 0.88),
            ("favicon-32.png", 32, False, 1.06),
            ("favicon-64.png", 64, False, 1.06),
    ):
        img = draw_png(size, bleed=bleed, scale=scale)
        img.save(OUT / name)
        made.append(f"{name} {size}px")
    (OUT / "icon.svg").write_text(svg())
    made.append("icon.svg")
    print("wrote " + ", ".join(made))


if __name__ == "__main__":
    main()
