"""Illustrative SVG "camera frames" for simulated detections.

These are drawings, not photos, and every frame says so. They carry the cues
a reviewer would use in a real photo, so the review workflow can be tried out:
leaf shape, gloss and margin, and whether flowers or fruit sit in racemes or
leaf axils. The true species is never printed on the image.
"""

from __future__ import annotations

import math
import random
from xml.sax.saxutils import escape

W, H = 480, 360

# leaf length, width, fill, glossy, margin dash (None = entire margin)
_LEAF = {
    "Prunus serotina": (62, 15, "#2f5d2a", True, "2 1.5"),
    "Prunus padus": (58, 22, "#6c9a4a", False, "1 1"),
    "Frangula alnus": (48, 24, "#5b8a3c", False, None),
    "Prunus avium": (74, 26, "#79a856", False, "4 2"),
}
_SEASON_BG = {"flowering": ("#4d6b3a", "#9fb57a"), "fruiting": ("#3e4f2c", "#a19a62"),
              "vegetative": ("#465f35", "#93a870"), "autumn_colour": ("#5a4a2c", "#b48a4a")}


def _leaf(x: float, y: float, angle: float, length: float, width: float, fill: str, glossy: bool, dash: str | None) -> str:
    d = f"M0,0 Q{width:.1f},{-length / 2:.1f} 0,{-length:.1f} Q{-width:.1f},{-length / 2:.1f} 0,0 Z"
    edge = f'stroke="#1c3316" stroke-width="1.6" stroke-dasharray="{dash}"' if dash else 'stroke="#1c3316" stroke-width="1"'
    out = [f'<g transform="translate({x:.1f},{y:.1f}) rotate({angle:.1f})">',
           f'<path d="{d}" fill="{fill}" {edge}/>',
           f'<path d="M0,-2 L0,{-length + 3:.1f}" stroke="#a9c48f" stroke-width="1"/>']
    if glossy:
        out.append(f'<path d="M{width * 0.25:.1f},{-length * 0.2:.1f} Q{width * 0.55:.1f},{-length / 2:.1f} '
                   f'{width * 0.2:.1f},{-length * 0.8:.1f}" stroke="#ffffff" stroke-opacity="0.75" stroke-width="3" fill="none"/>')
    out.append("</g>")
    return "".join(out)


def _raceme(x: float, y: float, n: int, colour: str, r: float, droop: float, calyx: bool, rng: random.Random) -> str:
    parts = [f'<path d="M{x},{y} q10,{droop / 2} 6,{droop + n * 5}" stroke="#4a3b22" stroke-width="2" fill="none"/>']
    for i in range(n):
        t = i / max(n - 1, 1)
        cx = x + 8 * math.sin(t * 2.2) + rng.uniform(-4, 4)
        cy = y + droop * t + i * 5
        parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r}" fill="{colour}" stroke="#222" stroke-width="0.5"/>')
        if calyx:
            parts.append(f'<circle cx="{cx:.1f}" cy="{cy - r:.1f}" r="1.6" fill="#7a8f3a"/>')
    return "".join(parts)


def render(taxon: str, visual: str, height_class: str | None, distance_m: float,
           uid: str, observed_at: str, seed: int) -> str:
    """`visual` is one of: raceme_white, raceme_black, raceme_green, axillary_flowers,
    axillary_berries, stalked_cherries, autumn, none."""
    rng = random.Random(seed)
    length, width, fill, glossy, dash = _LEAF[taxon]
    season = "autumn_colour" if visual == "autumn" else ("flowering" if observed_at[5:7] < "08" else "fruiting")
    bg0, bg1 = _SEASON_BG[season]
    small = height_class == "seedling"
    scale = 0.7 if small else 1.0
    body = [f'<defs><linearGradient id="bg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{bg1}"/>'
            f'<stop offset="1" stop-color="{bg0}"/></linearGradient></defs>',
            f'<rect width="{W}" height="{H}" fill="url(#bg)"/>']
    for _ in range(18):  # out-of-focus background foliage
        body.append(f'<circle cx="{rng.uniform(0, W):.0f}" cy="{rng.uniform(40, H):.0f}" r="{rng.uniform(10, 40):.0f}" '
                    f'fill="#2c4424" opacity="{rng.uniform(0.15, 0.35):.2f}"/>')
    body.append(f'<g transform="translate(60,{300 if not small else 320}) scale({scale})">')
    body.append('<path d="M0,0 C80,-40 180,-90 330,-150" stroke="#5a4127" stroke-width="7" fill="none"/>')
    n_leaves = 4 if small else 8
    for i in range(n_leaves):
        t = (i + 1) / (n_leaves + 1)
        bx, by = 330 * t, -150 * t - 20 * math.sin(t * math.pi)
        side = 1 if i % 2 else -1
        leaf_fill = "#b59a3a" if visual == "autumn" and rng.random() < 0.6 else fill
        body.append(_leaf(bx, by, side * rng.uniform(35, 70) + 20, length, width, leaf_fill, glossy, dash))
    if not small:
        if visual == "raceme_white":
            body.append(_raceme(200, -100, 16, "#fbfbf2", 3.2, 30, False, rng))
        elif visual == "raceme_black":
            body.append(_raceme(200, -100, 10, "#1a0f14", 4.5, 40, True, rng))
        elif visual == "raceme_green":
            body.append(_raceme(200, -100, 9, "#7fa04a", 3.5, 45, False, rng))
        elif visual in ("axillary_flowers", "axillary_berries"):
            colours = ["#b3261e", "#1a0f14", "#6b8f3a"] if visual == "axillary_berries" else ["#c9d9a0"]
            for _ in range(9):  # directly in the leaf axils, not in racemes
                t = rng.uniform(0.25, 0.9)
                body.append(f'<circle cx="{330 * t + rng.uniform(-6, 6):.1f}" cy="{-150 * t + rng.uniform(-4, 10):.1f}" '
                            f'r="4" fill="{rng.choice(colours)}" stroke="#222" stroke-width="0.5"/>')
        elif visual == "stalked_cherries":
            for i in range(3):
                cx, cy = 210 + i * 12, -95
                body.append(f'<path d="M{cx},{cy - 30} L{cx},{cy}" stroke="#4a3b22" stroke-width="1.5"/>'
                            f'<circle cx="{cx}" cy="{cy}" r="6" fill="#b3261e" stroke="#222" stroke-width="0.5"/>')
    body.append("</g>")
    for _ in range(int(distance_m / 3)):  # occluding foreground leaves grow with distance
        body.append(f'<ellipse cx="{rng.uniform(0, W):.0f}" cy="{rng.uniform(60, H):.0f}" rx="{rng.uniform(20, 50):.0f}" '
                    f'ry="{rng.uniform(8, 18):.0f}" fill="#1f3319" opacity="0.8" transform="rotate({rng.uniform(-40, 40):.0f})"/>')
    body.append(f'<rect width="{W}" height="{H}" fill="#d8e0d0" opacity="{min(0.5, distance_m / 22):.2f}"/>')
    body.append(f'<text x="{W / 2}" y="{H / 2 + 20}" text-anchor="middle" font-family="sans-serif" font-size="54" '
                f'font-weight="700" fill="#ffffff" opacity="0.28" transform="rotate(-18 {W / 2} {H / 2})">SIMULATED</text>')
    body.append(f'<rect width="{W}" height="26" fill="#000" opacity="0.72"/>'
                f'<text x="10" y="18" font-family="sans-serif" font-size="13" fill="#fff">SIMULATED IMAGE · illustrative drawing, not a photo</text>')
    body.append(f'<rect y="{H - 24}" width="{W}" height="24" fill="#000" opacity="0.6"/>'
                f'<text x="10" y="{H - 8}" font-family="monospace" font-size="11" fill="#fff">'
                f'{escape(uid)} · {escape(observed_at[:16])} · ~{distance_m:.0f} m</text>')
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}">{"".join(body)}</svg>'
