"""A tiny procedurally generated device icon.

Control points (Audirvana included) show the renderer's icon next to its name.
Rather than ship binary assets, the icon is drawn with plain arithmetic and
encoded as a PNG at start-up - no image library, no files to keep in sync.
"""

from __future__ import annotations

import math
import struct
import zlib
from functools import lru_cache

BACKGROUND = (24, 26, 31)
FOREGROUND = (240, 242, 245)
ACCENT = (110, 190, 255)


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


@lru_cache(maxsize=8)
def render(size: int = 48) -> bytes:
    """Draw a speaker-with-soundwaves glyph at *size* x *size* pixels."""
    radius = size * 0.22          # corner rounding of the tile
    cx, cy = size / 2.0, size / 2.0
    rows = bytearray()

    # Soundwave arcs, expressed as (radius, thickness) in units of `size`.
    arcs = [(0.20, 0.052), (0.30, 0.052), (0.40, 0.052)]

    for y in range(size):
        rows.append(0)  # PNG filter type 0 (None) for this scanline
        for x in range(size):
            px, py = x + 0.5, y + 0.5

            # Rounded-square mask.
            dx = max(radius - px, px - (size - radius), 0.0)
            dy = max(radius - py, py - (size - radius), 0.0)
            corner = math.hypot(dx, dy)
            tile_alpha = max(0.0, min(1.0, radius - corner + 0.5)) if corner > 0 else 1.0

            colour = BACKGROUND
            # The speaker body: a small rounded block left of centre.
            bx0, bx1 = size * 0.24, size * 0.40
            by0, by1 = size * 0.38, size * 0.62
            if bx0 <= px <= bx1 and by0 <= py <= by1:
                colour = FOREGROUND
            # ...and its cone, a triangle opening to the right.
            elif bx1 <= px <= size * 0.52:
                spread = (px - bx1) / (size * 0.12) * (size * 0.20)
                if abs(py - cy) <= size * 0.12 + spread:
                    colour = FOREGROUND

            # Concentric arcs to the right of the cone.
            dist = math.hypot(px - cx * 1.02, py - cy) / size
            if px > size * 0.55:
                for arc_r, thickness in arcs:
                    if abs(dist - arc_r) < thickness / 2:
                        colour = ACCENT
                        break

            r, g, b = _blend(BACKGROUND, colour, 1.0) if colour != BACKGROUND else BACKGROUND
            alpha = round(255 * tile_alpha)
            rows.extend((r, g, b, alpha))

    return _encode_png(size, size, rows)


ICON_SIZES = (48, 120)


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
