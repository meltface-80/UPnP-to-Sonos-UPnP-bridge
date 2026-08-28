"""Device icons, drawn at start-up rather than shipped as files.

Control points (Audirvana included) show a renderer's icon next to its name.
Each Sonos model has its own line drawing in :mod:`sonosbridge.deviceicons`;
here those outlines are stroked into a PNG with plain arithmetic - no image
library, no binary assets to keep in sync with the code.
"""

from __future__ import annotations

import math
import struct
import zlib
from functools import lru_cache

from .deviceicons import VIEW, icon_for_model, polylines

BACKGROUND = (24, 26, 31)
FOREGROUND = (240, 242, 245)

STROKE_UNITS = 1.25      # line weight on the 32-unit grid the devices are drawn on
MIN_STROKE_PX = 1.5      # below this a hairline breaks up into dots
CORNER = 0.22            # tile rounding, as a fraction of the icon
INSET = 0.07             # keep the drawing clear of the tile's rounded corners

ICON_SIZES = (48, 120)

__all__ = ["ICON_SIZES", "icon_for_model", "icon_list_xml", "render"]


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _encode_png(width: int, height: int, pixels: bytearray) -> bytes:
    """Encode RGBA rows (already filter-prefixed) into a PNG byte string."""
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(bytes(pixels), 9))
        + _chunk(b"IEND", b"")
    )


def _blend(base: tuple[int, int, int], top: tuple[int, int, int], alpha: float):
    alpha = max(0.0, min(1.0, alpha))
    return tuple(round(b + (t - b) * alpha) for b, t in zip(base, top, strict=True))


def _segment_distance(px: float, py: float, x0: float, y0: float, x1: float, y1: float) -> float:
    dx, dy = x1 - x0, y1 - y0
    length = dx * dx + dy * dy
    if length <= 1e-12:
        return math.hypot(px - x0, py - y0)
    t = ((px - x0) * dx + (py - y0) * dy) / length
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return math.hypot(px - (x0 + t * dx), py - (y0 + t * dy))


def _coverage(name: str, size: int) -> list[float]:
    """Per-pixel ink coverage of the stroked outline, 0.0 to 1.0.

    Distance to the nearest segment gives round caps and joins for free, and
    the half-pixel ramp either side of the edge is the anti-aliasing.
    """
    scale = size * (1 - 2 * INSET) / VIEW
    offset = size * INSET
    half = max(STROKE_UNITS * scale, MIN_STROKE_PX) / 2.0
    reach = half + 0.5
    ink = [0.0] * (size * size)

    for run in polylines(name):
        points = [(x * scale + offset, y * scale + offset) for x, y in run]
        for (x0, y0), (x1, y1) in zip(points, points[1:], strict=False):
            # Only the pixels the segment can actually reach are worth testing.
            lo_x = max(0, int(math.floor(min(x0, x1) - reach)))
            hi_x = min(size - 1, int(math.ceil(max(x0, x1) + reach)))
            lo_y = max(0, int(math.floor(min(y0, y1) - reach)))
            hi_y = min(size - 1, int(math.ceil(max(y0, y1) + reach)))
            for y in range(lo_y, hi_y + 1):
                row = y * size
                for x in range(lo_x, hi_x + 1):
                    distance = _segment_distance(x + 0.5, y + 0.5, x0, y0, x1, y1)
                    alpha = reach - distance
                    if alpha <= 0.0:
                        continue
                    if alpha > 1.0:
                        alpha = 1.0
                    if alpha > ink[row + x]:
                        ink[row + x] = alpha
    return ink


@lru_cache(maxsize=64)
def render(name: str = "generic", size: int = 48) -> bytes:
    """Draw *name* at *size* x *size* pixels: a light outline on a dark tile."""
    ink = _coverage(name, size)
    radius = size * CORNER
    rows = bytearray()

    for y in range(size):
        rows.append(0)  # PNG filter type 0 (None) for this scanline
        row = y * size
        for x in range(size):
            px, py = x + 0.5, y + 0.5

            # Rounded-square mask, softened over the outermost half pixel.
            dx = max(radius - px, px - (size - radius), 0.0)
            dy = max(radius - py, py - (size - radius), 0.0)
            corner = math.hypot(dx, dy)
            tile = 1.0 if corner <= 0 else max(0.0, min(1.0, radius - corner + 0.5))

            r, g, b = _blend(BACKGROUND, FOREGROUND, ink[row + x])
            rows.extend((r, g, b, round(255 * tile)))

    return _encode_png(size, size, rows)


def icon_list_xml(base_url: str) -> str:
    """The ``<iconList>`` fragment for a device description document."""
    parts = ["<iconList>"]
    for size in ICON_SIZES:
        parts.append(
            "<icon><mimetype>image/png</mimetype>"
            f"<width>{size}</width><height>{size}</height><depth>24</depth>"
            f"<url>{base_url}/icon/{size}.png</url></icon>"
        )
    parts.append("</iconList>")
    return "".join(parts)
