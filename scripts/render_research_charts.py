#!/usr/bin/env python3
"""Render research charts (SVG) from the measured open-model results.

Two charts, generated from the data table below:
  1. quality-vs-cost scatter (colour = platform speed)
  2. score leaderboard with min/max dispersion whiskers

Charts follow the articles/ house style: transparent canvas, a light "card"
panel so dark text/axes read on both light and dark blog themes, IBM Plex Sans.
Output is SVG (crisp, diff-able, generated from code — never hand-edited).

Usage: uv run python scripts/render_research_charts.py [ru|en]
"""

from __future__ import annotations

import math
import shutil
import subprocess
import sys
from pathlib import Path

# name, score_avg, score_min, score_max, cost_usd, time_min, n_runs, tier
# tier: base | top | mid | weak
DATA = [
    ("OpenAI mini/nano", 0.907, 0.896, 0.934, 1.18, 56, 4, "base"),
    ("Kimi K2.7", 0.868, 0.840, 0.898, 2.78, 100, 3, "top"),
    ("GLM 5.1", 0.854, 0.838, 0.881, 10.8, 215, 3, "top"),
    ("MiniMax M3", 0.825, 0.818, 0.837, 1.19, 114, 3, "top"),
    ("Qwen 3.6 27B", 0.809, 0.798, 0.819, 5.65, 151, 3, "mid"),
    ("Nemotron 3 Super", 0.747, 0.738, 0.757, 1.95, 140, 3, "mid"),
    ("Mistral Large 3", 0.729, 0.694, 0.767, 1.85, 58, 3, "mid"),
    ("Qwen 3.6", 0.717, 0.662, 0.755, 2.29, 179, 4, "mid"),
    ("Gemma 4", 0.693, 0.686, 0.700, 0.76, 268, 2, "weak"),
    ("DeepSeek V4 Pro", 0.615, 0.608, 0.621, 3.06, 218, 3, "weak"),
    ("Llama 4 Maverick", 0.553, 0.526, 0.574, 0.82, 50, 3, "weak"),
]

TIER_COLOR = {"base": "#7c3aed", "top": "#3b82f6", "mid": "#f59e0b", "weak": "#ef4444"}

TXT = {
    "ru": {
        "scatter_title": "Open weights модели. Качество / цена / время прогона",
        "x": "Стоимость прогона, $ (лог. шкала)",
        "y": "Средний score",
        "fast": "быстро, 50 мин",
        "slow": "медленно, 270 мин",
        "lb_title": "Open weights модели. Качество / цена прогона",
    },
    "en": {
        "scatter_title": "Open-weight models. Quality / cost / time per run",
        "x": "Cost per run, $ (log scale)",
        "y": "Average score",
        "fast": "fast, 50 min",
        "slow": "slow, 270 min",
        "lb_title": "Open-weight models. Quality / cost per run",
    },
}

# per-point label tweaks for the scatter: (anchor, dx, dy)
LABELS = {
    "OpenAI mini/nano": ("start", 11, -8),
    "Kimi K2.7": ("start", 11, 4),
    "GLM 5.1": ("end", -12, 4),
    "MiniMax M3": ("start", 11, 4),
    "Qwen 3.6 27B": ("end", -12, -8),
    "Nemotron 3 Super": ("start", 11, -8),
    "Mistral Large 3": ("end", -12, 16),
    "Qwen 3.6": ("start", 11, 12),
    "Gemma 4": ("start", 11, 4),
    "DeepSeek V4 Pro": ("start", 11, 4),
    "Llama 4 Maverick": ("start", 11, 4),
}

FONT = "IBM Plex Sans, system-ui, -apple-system, Segoe UI, Roboto, sans-serif"
CARD = 'fill="#f8fafc" stroke="#e2e8f0" stroke-width="1.5" rx="14"'
INK = "#0f172a"
MUTED = "#475569"
GRID = "#e2e8f0"


def _lerp(a: int, b: int, t: float) -> int:
    return round(a + (b - a) * t)


def speed_color(minutes: float) -> str:
    """Green (fast) -> amber (mid) -> red (slow)."""
    lo, mid, hi = 50.0, 150.0, 270.0
    g, a, r = (0x16, 0xA3, 0x4A), (0xF5, 0x9E, 0x0B), (0xEF, 0x44, 0x44)
    m = max(lo, min(hi, minutes))
    if m <= mid:
        t = (m - lo) / (mid - lo)
        c = tuple(_lerp(g[i], a[i], t) for i in range(3))
    else:
        t = (m - mid) / (hi - mid)
        c = tuple(_lerp(a[i], r[i], t) for i in range(3))
    return f"#{c[0]:02x}{c[1]:02x}{c[2]:02x}"


def svg_scatter(lang: str) -> str:
    t = TXT[lang]
    w, h = 820, 520
    left, right, top, bot = 92, 64, 82, 84
    pw, ph = w - left - right, h - top - bot
    xmin, xmax = 0.7, 12.0
    ymin, ymax = 0.50, 0.95

    def sx(cost: float) -> float:
        return left + (math.log10(cost) - math.log10(xmin)) / (
            math.log10(xmax) - math.log10(xmin)
        ) * pw

    def sy(score: float) -> float:
        return top + (ymax - score) / (ymax - ymin) * ph

    s = [
        f'<svg viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg" '
        f'font-family="{FONT}">'
    ]
    s.append(f'<rect x="20" y="20" width="{w - 40}" height="{h - 40}" {CARD}/>')
    s.append(
        f'<text x="{w / 2}" y="54" text-anchor="middle" font-size="18.5" '
        f'font-weight="700" fill="{INK}">{t["scatter_title"]}</text>'
    )
    # y gridlines + ticks
    for sc in (0.5, 0.6, 0.7, 0.8, 0.9):
        y = sy(sc)
        s.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{left + pw}" y2="{y:.1f}" '
            f'stroke="{GRID}" stroke-width="1"/>'
        )
        s.append(
            f'<text x="{left - 10}" y="{y + 4:.1f}" text-anchor="end" '
            f'font-size="11.5" fill="{MUTED}">{sc:.1f}</text>'
        )
    # x ticks (log)
    for cost in (1, 2, 5, 10):
        x = sx(cost)
        s.append(
            f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{top + ph}" '
            f'stroke="{GRID}" stroke-width="1"/>'
        )
        s.append(
            f'<text x="{x:.1f}" y="{top + ph + 22}" text-anchor="middle" '
            f'font-size="11.5" fill="{MUTED}">${cost}</text>'
        )
    # axis titles
    s.append(
        f'<text x="{left + pw / 2}" y="{h - 30}" text-anchor="middle" '
        f'font-size="12.5" fill="{INK}">{t["x"]}</text>'
    )
    s.append(
        f'<text x="28" y="{top + ph / 2}" text-anchor="middle" font-size="12.5" '
        f'fill="{INK}" transform="rotate(-90 28 {top + ph / 2:.0f})">{t["y"]}</text>'
    )
    # speed legend (bottom-right, clear of all points)
    leg_w = 212
    lx, ly = left + pw - leg_w - 4, top + ph - 48
    s.append(
        f'<rect x="{lx}" y="{ly}" width="{leg_w}" height="9" rx="4" '
        f'fill="url(#spd)"/>'
    )
    s.append(
        '<defs><linearGradient id="spd" x1="0" x2="1">'
        '<stop offset="0" stop-color="#16a34a"/>'
        '<stop offset="0.5" stop-color="#f59e0b"/>'
        '<stop offset="1" stop-color="#ef4444"/></linearGradient></defs>'
    )
    s.append(
        f'<text x="{lx}" y="{ly + 24}" font-size="10.5" fill="{MUTED}">'
        f'{t["fast"]}</text>'
    )
    s.append(
        f'<text x="{lx + leg_w}" y="{ly + 24}" text-anchor="end" font-size="10.5" '
        f'fill="{MUTED}">{t["slow"]}</text>'
    )
    # points
    for name, sc, _, _, cost, tmin, _, tier in DATA:
        x, y = sx(cost), sy(sc)
        col = speed_color(tmin)
        base = tier == "base"
        r = 9 if base else 7
        stroke = "#1e293b" if base else "#0f172a"
        sw = 2.4 if base else 1.2
        s.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="{col}" '
            f'stroke="{stroke}" stroke-width="{sw}"/>'
        )
        anchor, dx, dy = LABELS[name]
        weight = "700" if base else "600"
        s.append(
            f'<text x="{x + dx:.1f}" y="{y + dy:.1f}" text-anchor="{anchor}" '
            f'font-size="12" font-weight="{weight}" fill="{INK}">{name}</text>'
        )
        s.append(
            f'<text x="{x + dx:.1f}" y="{y + dy + 13:.1f}" text-anchor="{anchor}" '
            f'font-size="10.5" fill="{MUTED}">{sc:.3f} · ${cost:g}</text>'
        )
    s.append("</svg>")
    return "\n".join(s)


def svg_leaderboard(lang: str) -> str:
    t = TXT[lang]
    w = 820
    row_h, top, bot, left, right = 34, 82, 40, 168, 150
    h = top + row_h * len(DATA) + bot
    x0, x1 = left, w - right
    smin, smax = 0.50, 0.95

    def sx(score: float) -> float:
        return x0 + (score - smin) / (smax - smin) * (x1 - x0)

    s = [
        f'<svg viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg" '
        f'font-family="{FONT}">'
    ]
    s.append(f'<rect x="20" y="20" width="{w - 40}" height="{h - 40}" {CARD}/>')
    s.append(
        f'<text x="{w / 2}" y="52" text-anchor="middle" font-size="18.5" '
        f'font-weight="700" fill="{INK}">{t["lb_title"]}</text>'
    )
    # x gridlines
    for sc in (0.5, 0.6, 0.7, 0.8, 0.9):
        x = sx(sc)
        s.append(
            f'<line x1="{x:.1f}" y1="{top - 6}" x2="{x:.1f}" '
            f'y2="{top + row_h * len(DATA)}" stroke="{GRID}" stroke-width="1"/>'
        )
        s.append(
            f'<text x="{x:.1f}" y="{top - 12}" text-anchor="middle" '
            f'font-size="11" fill="{MUTED}">{sc:.1f}</text>'
        )
    for i, (name, sc, lo, hi, cost, _, _, tier) in enumerate(DATA):
        cy = top + i * row_h + row_h / 2
        col = TIER_COLOR[tier]
        bw = sx(sc) - x0
        s.append(
            f'<rect x="{x0}" y="{cy - 9:.1f}" width="{bw:.1f}" height="18" '
            f'rx="4" fill="{col}" opacity="0.88"/>'
        )
        # min-max whisker
        s.append(
            f'<line x1="{sx(lo):.1f}" y1="{cy:.1f}" x2="{sx(hi):.1f}" '
            f'y2="{cy:.1f}" stroke="{INK}" stroke-width="1.4"/>'
        )
        for xx in (sx(lo), sx(hi)):
            s.append(
                f'<line x1="{xx:.1f}" y1="{cy - 5:.1f}" x2="{xx:.1f}" '
                f'y2="{cy + 5:.1f}" stroke="{INK}" stroke-width="1.4"/>'
            )
        weight = "700" if tier == "base" else "600"
        s.append(
            f'<text x="{x0 - 12}" y="{cy + 4:.1f}" text-anchor="end" '
            f'font-size="12.5" font-weight="{weight}" fill="{INK}">{name}</text>'
        )
        s.append(
            f'<text x="{sx(hi) + 10:.1f}" y="{cy + 4:.1f}" font-size="11.5" '
            f'fill="{INK}">{sc:.3f} · ${cost:g}</text>'
        )
    s.append("</svg>")
    return "\n".join(s)


def _to_png(svg_path: Path, scale: int = 3) -> None:
    """Rasterize an SVG to a same-named PNG via the cairosvg CLI, if present.

    cairosvg reads the viewBox and renders the exact size, so there is no
    viewport/clipping ambiguity. Chrome headless --window-size was unreliable here.
    """
    png_path = svg_path.with_suffix(".png")
    cairosvg = shutil.which("cairosvg")
    if cairosvg is None:
        print(f"  cairosvg not found; skipped {png_path.name} (install: uv tool install cairosvg)")
        return
    subprocess.run([cairosvg, str(svg_path), "-o", str(png_path), "-s", str(scale)], check=True)
    print(f"  rendered {png_path.name}")


def main() -> None:
    lang = sys.argv[1] if len(sys.argv) > 1 else "ru"
    out = Path(__file__).resolve().parents[1] / "articles" / "images" / lang
    out.mkdir(parents=True, exist_ok=True)
    charts = {
        "research-01-quality-cost.svg": svg_scatter(lang),
        "research-02-leaderboard.svg": svg_leaderboard(lang),
    }
    for name, svg in charts.items():
        path = out / name
        path.write_text(svg)
        _to_png(path)
    print(f"wrote {len(charts)} charts (svg + png) to {out}")


if __name__ == "__main__":
    main()
