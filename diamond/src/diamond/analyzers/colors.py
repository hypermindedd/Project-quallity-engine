"""Colour math: WCAG contrast and CIE76 ΔE (pure functions, no dependencies)."""

from __future__ import annotations

import re

RGB = tuple[int, int, int]
_RGB_RE = re.compile(r"rgba?\(([^)]+)\)")


def parse_rgb(value: str | None) -> tuple[RGB, float] | None:
    """Parse ``rgb()/rgba()`` (as returned by getComputedStyle). Returns (rgb, alpha)."""
    if not value:
        return None
    m = _RGB_RE.search(value)
    if not m:
        return None
    parts = [p for p in re.split(r"[,\s/]+", m.group(1)) if p]
    try:
        r, g, b = (round(float(p)) for p in parts[:3])
        a = float(parts[3]) if len(parts) > 3 else 1.0
    except (ValueError, IndexError):
        return None
    return (r, g, b), a


def blend(fg: RGB, alpha: float, bg: RGB) -> RGB:
    return tuple(round(f * alpha + b * (1 - alpha)) for f, b in zip(fg, bg, strict=True))  # type: ignore[return-value]


def _linear(c: int) -> float:
    s = c / 255
    return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4


def luminance(rgb: RGB) -> float:
    r, g, b = (_linear(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a: RGB, b: RGB) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def to_lab(rgb: RGB) -> tuple[float, float, float]:
    r, g, b = (_linear(c) for c in rgb)
    x = (r * 0.4124 + g * 0.3576 + b * 0.1805) / 0.95047
    y = r * 0.2126 + g * 0.7152 + b * 0.0722
    z = (r * 0.0193 + g * 0.1192 + b * 0.9505) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116

    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def delta_e(a: RGB, b: RGB) -> float:
    la, lb = to_lab(a), to_lab(b)
    return sum((p - q) ** 2 for p, q in zip(la, lb, strict=True)) ** 0.5


def hex_of(rgb: RGB) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)
