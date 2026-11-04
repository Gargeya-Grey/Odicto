"""The colour theme: one `THEME_COLOR` setting for the HUD leaf and the setup page.

No Qt import here, so the setup server, the tests and the tools can use it too.

A theme is a hue and a chroma factor `k`. The page takes the two numbers as the CSS
variables `--h` and `--k`. The HUD takes a `Palette`: every themed colour of the Forest
leaf keeps its OKLCH lightness, gets the theme's hue and has its chroma scaled by `k`,
then it is clamped into the sRGB gamut by lowering chroma (never lightness). So a leaf
in any theme keeps the light and dark of the Forest leaf, and the label stays readable.

Forest is not computed. It is the constants below, exactly as the painter drew them
before themes existed. WARN, DANGER, INK and MOON never change with the theme: they carry
meaning (notice, error, text, AI).

The setup page repeats this maths in JavaScript (assets/setup_template.html, "THEME").
tests/test_theme.py keeps the two equal: the preset table, the k rule and the matrices.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

Rgb = tuple[int, int, int]

DEFAULT_THEME = "forest"

# name -> (hue in degrees, k). k scales every chroma of the leaf and of the page.
PRESETS: dict[str, tuple[float, float]] = {
    "forest": (165.0, 1.0),
    "ocean": (232.0, 1.0),
    "plum": (318.0, 0.95),
    "ember": (32.0, 1.0),
    "graphite": (230.0, 0.12),
}

# A custom colour's k is its own chroma over this, clamped. 0.13 is about the chroma of
# a fully coloured Forest accent, so a vivid pick looks as rich as a preset.
HEX_K_DIVISOR = 0.13
HEX_K_MIN = 0.08
HEX_K_MAX = 1.0

_HEX = re.compile(r"#[0-9a-f]{6}")


@dataclass(frozen=True)
class Palette:
    """Every colour of the leaf that a theme changes, then the four it never changes."""

    jade: Rgb            # the accent: voice pixels, stem, glow, seed
    surface_top: Rgb     # leaf body gradient, top
    surface_bottom: Rgb  # leaf body gradient, bottom
    cool: Rgb            # the darker, cooler tip
    vein: Rgb
    sheen: Rgb           # the light along the top of the body and the card
    rim: Rgb             # the 1 px edge light
    card_top: Rgb        # caption card gradient
    card_bottom: Rgb
    glint: Rgb           # the label shimmer and the seed core
    stem_hi: Rgb         # the stem's centre line
    bead: Rgb            # the Transcribing bead
    warn: Rgb = (242, 194, 122)
    danger: Rgb = (242, 163, 155)
    ink: Rgb = (231, 229, 223)
    moon: Rgb = (178, 214, 244)


# Forest: today's constants.
FOREST = Palette(
    jade=(142, 220, 187),
    surface_top=(27, 64, 46),
    surface_bottom=(9, 31, 22),
    cool=(3, 18, 24),
    vein=(205, 245, 222),
    sheen=(225, 255, 238),
    rim=(215, 250, 230),
    card_top=(24, 58, 42),
    card_bottom=(10, 32, 23),
    glint=(240, 255, 248),
    stem_hi=(244, 255, 250),
    bead=(238, 255, 248),
)

_THEMED = ("jade", "surface_top", "surface_bottom", "cool", "vein", "sheen", "rim",
           "card_top", "card_bottom", "glint", "stem_hi", "bead")


# --------------------------------------------------------------------------- colour maths
def _to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _from_linear(c: float) -> float:
    return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1.0 / 2.4) - 0.055


def _cbrt(x: float) -> float:
    return math.copysign(abs(x) ** (1.0 / 3.0), x)


def srgb_to_oklch(rgb: Rgb) -> tuple[float, float, float]:
    """sRGB 0-255 to (lightness 0-1, chroma, hue in degrees 0-360)."""
    r, g, b = (_to_linear(v / 255.0) for v in rgb)
    l_ = _cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b)
    m_ = _cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b)
    s_ = _cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b)
    lightness = 0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_
    a = 1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_
    bb = 0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_
    return lightness, math.hypot(a, bb), math.degrees(math.atan2(bb, a)) % 360.0


def _oklch_linear(lightness: float, chroma: float, hue: float) -> tuple[float, float, float]:
    a = chroma * math.cos(math.radians(hue))
    b = chroma * math.sin(math.radians(hue))
    l_ = lightness + 0.3963377774 * a + 0.2158037573 * b
    m_ = lightness - 0.1055613458 * a - 0.0638541728 * b
    s_ = lightness - 0.0894841775 * a - 1.2914855480 * b
    l3, m3, s3 = l_ ** 3, m_ ** 3, s_ ** 3
    return (4.0767416621 * l3 - 3.3077115913 * m3 + 0.2309699292 * s3,
            -1.2684380046 * l3 + 2.6097574011 * m3 - 0.3413193965 * s3,
            -0.0041960863 * l3 - 0.7034186147 * m3 + 1.7076147010 * s3)


def _in_gamut(rgb: tuple[float, float, float]) -> bool:
    return all(-1e-6 <= v <= 1.0 + 1e-6 for v in rgb)


def gamut_clamp(lightness: float, chroma: float, hue: float) -> float:
    """The largest chroma up to `chroma` that sRGB can show at this lightness and hue."""
    if _in_gamut(_oklch_linear(lightness, chroma, hue)):
        return chroma
    lo, hi = 0.0, chroma
    for _ in range(24):
        mid = (lo + hi) / 2.0
        if _in_gamut(_oklch_linear(lightness, mid, hue)):
            lo = mid
        else:
            hi = mid
    return lo


def oklch_to_srgb(lightness: float, chroma: float, hue: float) -> Rgb:
    """OKLCH to sRGB 0-255. Out-of-gamut chroma is lowered; lightness is kept."""
    chroma = gamut_clamp(lightness, max(0.0, chroma), hue)
    out = []
    for v in _oklch_linear(lightness, chroma, hue):
        v = min(1.0, max(0.0, v))
        out.append(int(math.floor(_from_linear(v) * 255.0 + 0.5)))
    return out[0], out[1], out[2]


# --------------------------------------------------------------------------- setting
def parse_theme(value: object) -> Optional[str]:
    """The canonical theme for a setting value, or None.

    A preset name (any case) gives the lower-case name. `#rrggbb` (any case) gives the
    lower-case hex. Blank, `#rgb`, a bare hex without `#` and anything else give None.
    """
    text = str(value if value is not None else "").strip().lower()
    if text in PRESETS or _HEX.fullmatch(text):
        return text
    return None


def theme_hue_k(theme: str) -> tuple[float, float]:
    """(hue, k) of a canonical theme."""
    if theme in PRESETS:
        return PRESETS[theme]
    if not _HEX.fullmatch(theme):
        raise ValueError(f"unknown theme {theme!r}")
    rgb = (int(theme[1:3], 16), int(theme[3:5], 16), int(theme[5:7], 16))
    _, chroma, hue = srgb_to_oklch(rgb)
    return hue, min(HEX_K_MAX, max(HEX_K_MIN, chroma / HEX_K_DIVISOR))


def page_theme(theme: str) -> dict[str, str]:
    """The CSS variables of the page: {"--h": ..., "--k": ...}, as short numbers."""
    hue, k = theme_hue_k(theme)
    return {"--h": f"{round(hue, 2):g}", "--k": f"{round(k, 3):g}"}


@lru_cache(maxsize=32)
def hud_palette(theme: str = DEFAULT_THEME) -> Palette:
    """The leaf palette of a canonical theme. Forest is the exact constants."""
    if theme == DEFAULT_THEME:
        return FOREST
    hue, k = theme_hue_k(theme)
    fields = {}
    for name in _THEMED:
        lightness, chroma, _ = srgb_to_oklch(getattr(FOREST, name))
        fields[name] = oklch_to_srgb(lightness, chroma * k, hue)
    return Palette(**fields)
