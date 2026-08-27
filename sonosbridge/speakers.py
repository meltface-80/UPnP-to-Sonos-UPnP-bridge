"""Minimalist line-art glyphs for the Sonos models the bridge can bridge.

Every model is described once, as a handful of rounded polygons in a 100x100
box.  Both renderers work from that single description: :mod:`sonosbridge.icon`
rasterises it into the PNG that UPnP control points ask for, and :func:`svg`
emits the same outline for the status page or any other UI.  Nothing here needs
a drawing library, and there are no binary assets to keep in sync.

The shapes are deliberately schematic - a Five is a wide box, a Beam is a
rounded bar, a Sub is a slab with a hole - because at 48 pixels the silhouette
is the only thing that survives.  A stereo pair is drawn as two smaller copies
side by side.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Sequence
from functools import lru_cache

Point = tuple[float, float]
Path = tuple[Point, ...]

# Everything is drawn in this box and scaled at render time.
VIEWBOX = 100.0


# ----------------------------------------------------------------------
# Geometry helpers
# ----------------------------------------------------------------------
def _arc_points(cx: float, cy: float, r: float, start: float, end: float, steps: int) -> list[Point]:
    span = end - start
    return [
        (cx + r * math.cos(start + span * i / steps), cy + r * math.sin(start + span * i / steps))
        for i in range(steps + 1)
    ]


def circle(cx: float, cy: float, r: float, steps: int = 40) -> Path:
    return tuple(_arc_points(cx, cy, r, 0.0, math.tau, steps))


def line(x1: float, y1: float, x2: float, y2: float) -> Path:
    return ((x1, y1), (x2, y2))


def poly(points: Sequence[Point], radii: float | Sequence[float], steps: int = 6) -> Path:
    """A closed polygon with rounded corners, sampled into a polyline.

    Concave corners round just as happily as convex ones, which is what gives
    the Era 300 its waist and the Era 100 its dipped top.
    """
    count = len(points)
    if isinstance(radii, int | float):
        radii = [float(radii)] * count
    out: list[Point] = []
    for index, point in enumerate(points):
        before = points[index - 1]
        after = points[(index + 1) % count]
        to_before = (before[0] - point[0], before[1] - point[1])
        to_after = (after[0] - point[0], after[1] - point[1])
        len_before = math.hypot(*to_before)
        len_after = math.hypot(*to_after)
        if len_before < 1e-9 or len_after < 1e-9:
            out.append(point)
            continue
        u1 = (to_before[0] / len_before, to_before[1] / len_before)
        u2 = (to_after[0] / len_after, to_after[1] / len_after)
        angle = math.acos(max(-1.0, min(1.0, u1[0] * u2[0] + u1[1] * u2[1])))
        if angle < 1e-6 or abs(angle - math.pi) < 1e-6:
            out.append(point)  # a straight-through vertex needs no arc
            continue
        half = math.tan(angle / 2.0)
        radius = min(radii[index], len_before / 2.0 * half, len_after / 2.0 * half)
        tangent = radius / half
        start_point = (point[0] + u1[0] * tangent, point[1] + u1[1] * tangent)
        end_point = (point[0] + u2[0] * tangent, point[1] + u2[1] * tangent)
        bisector = (u1[0] + u2[0], u1[1] + u2[1])
        length = math.hypot(*bisector)
        if length < 1e-9 or radius < 1e-9:
            out.append(point)
            continue
        distance = radius / math.sin(angle / 2.0)
        centre = (point[0] + bisector[0] / length * distance,
                  point[1] + bisector[1] / length * distance)
        a1 = math.atan2(start_point[1] - centre[1], start_point[0] - centre[0])
        a2 = math.atan2(end_point[1] - centre[1], end_point[0] - centre[0])
        sweep = (a2 - a1 + math.pi) % math.tau - math.pi  # the short way round
        out.extend(_arc_points(centre[0], centre[1], radius, a1, a1 + sweep, steps))
    out.append(out[0])
    return tuple(out)


def rrect(x: float, y: float, w: float, h: float, r: float) -> Path:
    return poly(((x, y), (x + w, y), (x + w, y + h), (x, y + h)), r)


# ----------------------------------------------------------------------
# The models
# ----------------------------------------------------------------------
def _generic() -> list[Path]:
    return [rrect(25, 17, 50, 66, 10), circle(50, 41, 12), circle(50, 67, 5)]


def _five() -> list[Path]:
    return [rrect(9, 31, 82, 38, 8)]


def _play3() -> list[Path]:
    return [rrect(19, 33, 62, 34, 7)]


def _one() -> list[Path]:
    return [rrect(33, 16, 34, 68, 9)]


def _play1() -> list[Path]:
    # The same body as a One, with the Play:1's slight taper towards the top.
    return [poly(((36, 16), (64, 16), (67, 84), (33, 84)), (8, 8, 7, 7))]


def _era100() -> list[Path]:
    # Taller and rounder than a One - the body is an oval cylinder.
    return [rrect(35, 15, 30, 70, 14)]


def _era300() -> list[Path]:
    # The cinched, hourglass body - the most recognisable shape in the range.
    return [poly(((14, 28), (86, 28), (78, 51), (86, 74), (14, 74), (22, 51)),
                 (12, 12, 10, 12, 12, 10))]


def _beam() -> list[Path]:
    return [rrect(14, 40, 72, 20, 10)]


def _arc_bar() -> list[Path]:
    return [rrect(5, 43, 90, 13, 6.5)]


def _ray() -> list[Path]:
    return [rrect(17, 41, 66, 18, 5)]


def _playbar() -> list[Path]:
    return [rrect(8, 39, 84, 22, 4), line(20, 44, 20, 56), line(80, 44, 80, 56)]


def _playbase() -> list[Path]:
    # A plinth for the television to stand on: flat, and flared towards the floor.
    return [poly(((12, 41), (88, 41), (94, 60), (6, 60)), 5)]


def _move() -> list[Path]:
    return [poly(((35, 14), (65, 14), (69, 86), (31, 86)), (13, 13, 7, 7))]


def _roam() -> list[Path]:
    return [poly(((41, 21), (59, 21), (63, 83), (37, 83)), (8, 8, 6, 6))]


def _sub() -> list[Path]:
    return [rrect(19, 15, 62, 70, 14), rrect(41, 33, 18, 34, 9)]


def _submini() -> list[Path]:
    return [rrect(31, 15, 38, 70, 19), circle(50, 50, 11)]


def _amp() -> list[Path]:
    return [rrect(11, 33, 78, 34, 5), circle(72, 50, 8)]


def _port() -> list[Path]:
    return [rrect(24, 38, 52, 24, 4), circle(63, 50, 3.6)]


def _bookshelf() -> list[Path]:
    return [rrect(23, 22, 54, 56, 4)]


def _lamp() -> list[Path]:
    return [poly(((38, 13), (62, 13), (73, 41), (27, 41)), 5), rrect(37, 41, 26, 45, 7)]


def _frame() -> list[Path]:
    return [rrect(22, 18, 56, 64, 3), rrect(29, 25, 42, 50, 2)]


_BUILDERS: dict[str, Callable[[], list[Path]]] = {
    "five": _five,
    "play3": _play3,
    "one": _one,
    "play1": _play1,
    "era100": _era100,
    "era300": _era300,
    "beam": _beam,
    "arc": _arc_bar,
    "ray": _ray,
    "playbar": _playbar,
    "playbase": _playbase,
    "move": _move,
    "roam": _roam,
    "sub": _sub,
    "submini": _submini,
    "amp": _amp,
    "port": _port,
    "bookshelf": _bookshelf,
    "lamp": _lamp,
    "frame": _frame,
    "generic": _generic,
}

#: Human-readable name for each glyph, used by the status page and the docs.
LABELS: dict[str, str] = {
    "five": "Five / Play:5",
    "play3": "Play:3",
    "one": "One / One SL",
    "play1": "Play:1",
    "era100": "Era 100",
    "era300": "Era 300",
    "beam": "Beam",
    "arc": "Arc / Arc Ultra",
    "ray": "Ray",
    "playbar": "Playbar",
    "playbase": "Playbase",
    "move": "Move / Move 2",
    "roam": "Roam / Roam 2",
    "sub": "Sub",
    "submini": "Sub Mini",
    "amp": "Amp / Connect:Amp",
    "port": "Port / Connect",
    "bookshelf": "Symfonisk bookshelf",
    "lamp": "Symfonisk lamp",
    "frame": "Symfonisk picture frame",
    "generic": "Sonos player",
}

KINDS: tuple[str, ...] = tuple(_BUILDERS)

DEFAULT_KIND = "generic"


# ----------------------------------------------------------------------
# Matching a model name to a glyph
# ----------------------------------------------------------------------
# Ordered longest-phrase-first: "Era 300" must win over "Era", "Sub Mini" over
# "Sub", "Connect:Amp" over both "Connect" and "Amp".
_RULES: tuple[tuple[str, str], ...] = (
    ("era 300", "era300"),
    ("era 100", "era100"),
    ("arc ultra", "arc"),
    ("arc", "arc"),
    ("beam", "beam"),
    ("ray", "ray"),
    ("playbar", "playbar"),
    ("play bar", "playbar"),
    ("playbase", "playbase"),
    ("play base", "playbase"),
    ("sub mini", "submini"),
    ("submini", "submini"),
    ("sub", "sub"),
    ("move", "move"),
    ("roam", "roam"),
    ("five", "five"),
    ("play 5", "five"),
    ("play 3", "play3"),
    ("play 1", "play1"),
    ("one", "one"),
    ("picture frame", "frame"),
    ("frame", "frame"),
    ("table lamp", "lamp"),
    ("lamp", "lamp"),
    ("bookshelf", "bookshelf"),
    ("connect amp", "amp"),
    ("zp120", "amp"),
    ("zp100", "amp"),
    ("amp", "amp"),
    ("port", "port"),
    ("connect", "port"),
    ("zp90", "port"),
    ("zp80", "port"),
    ("symfonisk", "bookshelf"),
)


def _normalise(model: str) -> str:
    """Fold a model name down to lowercase words: ``Sonos PLAY:5`` -> ``sonos play 5``."""
    return " ".join(re.sub(r"[^0-9a-z]+", " ", (model or "").lower()).split())


def classify(model: str) -> str:
    """Pick the glyph that best matches a Sonos ``modelName``.

    Unknown or missing models fall back to a generic speaker, so a firmware that
    invents a new name still gets a sensible icon.
    """
    text = _normalise(model)
    if not text:
        return DEFAULT_KIND
    for phrase, kind in _RULES:
        if re.search(rf"(?<![0-9a-z]){re.escape(phrase)}(?![0-9a-z])", text):
            return kind
    return DEFAULT_KIND


# ----------------------------------------------------------------------
# Assembling a drawing
# ----------------------------------------------------------------------
def _bbox(paths: Iterable[Path]) -> tuple[float, float, float, float]:
    xs = [x for path in paths for x, _ in path]
    ys = [y for path in paths for _, y in path]
    return min(xs), min(ys), max(xs), max(ys)


def _scaled(paths: Sequence[Path], scale: float, cx: float, cy: float,
            to_x: float, to_y: float) -> list[Path]:
    return [
        tuple(((x - cx) * scale + to_x, (y - cy) * scale + to_y) for x, y in path)
        for path in paths
    ]


PAIR_GAP = 7.0
PAIR_MARGIN = 6.0
PAIR_MAX_SCALE = 0.78


def _as_pair(paths: Sequence[Path]) -> list[Path]:
    """Two copies of a glyph, side by side, sized to fit the same box."""
    x0, y0, x1, y1 = _bbox(paths)
    width = max(x1 - x0, 1e-6)
    height = max(y1 - y0, 1e-6)
    available = (VIEWBOX - 2 * PAIR_MARGIN - PAIR_GAP) / 2
    scale = min(available / width, (VIEWBOX - 2 * PAIR_MARGIN) / height, PAIR_MAX_SCALE)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    offset = (width * scale + PAIR_GAP) / 2
    centre = VIEWBOX / 2
    return (
        _scaled(paths, scale, cx, cy, centre - offset, centre)
        + _scaled(paths, scale, cx, cy, centre + offset, centre)
    )


@lru_cache(maxsize=128)
def glyph(kind: str = DEFAULT_KIND, pair: bool = False) -> tuple[Path, ...]:
    """The outline for *kind*, in a 100x100 box.  ``pair=True`` draws two."""
    paths = _BUILDERS.get(kind, _BUILDERS[DEFAULT_KIND])()
    return tuple(_as_pair(paths) if pair else paths)


def glyph_for_model(model: str, pair: bool = False) -> tuple[Path, ...]:
    return glyph(classify(model), pair)


def label(kind: str) -> str:
    return LABELS.get(kind, LABELS[DEFAULT_KIND])


# ----------------------------------------------------------------------
# SVG
# ----------------------------------------------------------------------
def svg(
    kind: str = DEFAULT_KIND,
    pair: bool = False,
    size: int = 64,
    colour: str = "currentColor",
    stroke: float = 5.0,
    title: str = "",
    auto_theme: bool = False,
) -> str:
    """The same glyph as a standalone SVG document.

    Strokes default to ``currentColor`` so an inline icon follows whatever text
    colour the surrounding page is using.  ``auto_theme`` adds the light/dark
    rule a standalone file needs when there is no page to inherit from.
    """
    paths = []
    for path in glyph(kind, pair):
        points = " ".join(f"{x:.2f} {y:.2f}" for x, y in path)
        closed = len(path) > 2 and path[0] == path[-1]
        paths.append(f'<path d="M {points}{" Z" if closed else ""}"/>')
    heading = f"<title>{title}</title>" if title else ""
    if auto_theme:
        heading += (
            "<style>svg{color:#16181d}"
            "@media (prefers-color-scheme:dark){svg{color:#f0f2f5}}</style>"
        )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" '
        f'width="{size}" height="{size}" fill="none" stroke="{colour}" '
        f'stroke-width="{stroke:g}" stroke-linecap="round" stroke-linejoin="round" '
        f'role="img" aria-label="{title or label(kind)}">'
        f"{heading}{''.join(paths)}</svg>"
    )
