"""Device drawings for every Sonos model the bridge knows about.

Each speaker is described by the size of its cabinet - width across the front,
depth back, height - and flattened through one shared oblique projection, so
the whole set is drawn from the same viewpoint with the same horizon.  The
result is a handful of outlines on a 32 x 32 grid, which :mod:`sonosbridge.icon`
rasterises and ``tools/gen_device_icons.py`` writes out as an SVG sprite.
"""

from __future__ import annotations

import math
from functools import cache

# Depth recedes up and to the left, foreshortened - a three-quarter view from
# slightly above, the way hardware is drawn in a device picker.
KX, KY = -0.30, -0.23
VIEW = 32.0          # the drawings sit on a VIEW x VIEW grid
MARGIN = 2.0
K = 0.5523           # circle-to-bezier constant

YAW = 0.0            # per-device turn about the vertical axis, in radians


def set_yaw(degrees):
    """Turn a device on the spot. Long bars need it or they collapse to a line."""
    global YAW
    YAW = math.radians(degrees)


def P(x, y, z):
    """Project an object-space point to the drawing plane."""
    if YAW:
        c, s = math.cos(YAW), math.sin(YAW)
        x, y = x * c - y * s, x * s + y * c
    return (x + KX * y, -z + KY * y)


class Path:
    def __init__(self):
        self.segs = []

    def M(self, p):
        self.segs.append(("M", [p]))
        return self

    def L(self, p):
        self.segs.append(("L", [p]))
        return self

    def C(self, a, b, c):
        self.segs.append(("C", [a, b, c]))
        return self

    def Z(self):
        self.segs.append(("Z", []))
        return self

    def points(self):
        return [p for _, pts in self.segs for p in pts]

    def render(self):
        out = []
        for kind, pts in self.segs:
            if kind == "Z":
                out.append("Z")
                continue
            coords = " ".join(f"{a:.2f} {b:.2f}" for a, b in pts)
            out.append(f"{kind}{coords}")
        return "".join(out)


def seg(p1, p2):
    return Path().M(p1).L(p2)


def poly(*pts, close=True):
    p = Path().M(pts[0])
    for q in pts[1:]:
        p.L(q)
    return p.Z() if close else p


def box(w, d, h, org=(0.0, 0.0, 0.0)):
    """Silhouette plus the three interior edges of a rectangular volume."""
    ox, oy, oz = org

    def q(x, y, z):
        return P(ox + x, oy + y, oz + z)

    A, B, C = q(0, 0, 0), q(w, 0, 0), q(w, 0, h)
    D, E, F, G = q(0, 0, h), q(0, d, h), q(w, d, h), q(0, d, 0)
    return [poly(A, B, C, F, E, G), seg(D, A), seg(D, C), seg(D, E)]


def ring(cx, cy, z, rx, ry, t0=0.0, t1=2 * math.pi):
    """Bezier arc of an axis-aligned ellipse lying flat at height *z*."""
    p = Path()
    steps = max(1, int(math.ceil(abs(t1 - t0) / (math.pi / 2) - 1e-9)))
    step = (t1 - t0) / steps
    a = K * 4 / 3 * math.tan(step / 4)

    def pos(t):
        return P(cx + rx * math.cos(t), cy + ry * math.sin(t), z)

    def tan(t):
        return (-rx * math.sin(t), ry * math.cos(t))

    p.M(pos(t0))
    for i in range(steps):
        start, end = t0 + i * step, t0 + (i + 1) * step
        tsx, tsy = tan(start)
        tex, tey = tan(end)
        c1 = P(cx + rx * math.cos(start) + a * tsx, cy + ry * math.sin(start) + a * tsy, z)
        c2 = P(cx + rx * math.cos(end) - a * tex, cy + ry * math.sin(end) - a * tey, z)
        p.C(c1, c2, pos(end))
    return p


def silhouette_angle(rx, ry):
    """Parameter angle where the projected ellipse reaches its extreme x."""
    return math.atan2(KX * ry, rx)


def cylinder(rx, ry, h, cx=0.0, cy=0.0, lid=None):
    """Upright cylinder: full top ellipse, two silhouette edges, front floor."""
    t = silhouette_angle(rx, ry)
    left, right = t + math.pi, t
    parts = [ring(cx, cy, h, rx, ry)]
    for ang in (left, right):
        top = P(cx + rx * math.cos(ang), cy + ry * math.sin(ang), h)
        bot = P(cx + rx * math.cos(ang), cy + ry * math.sin(ang), 0)
        parts.append(seg(top, bot))
    # Floor: the near half only, swept the short way round the front.
    parts.append(ring(cx, cy, 0, rx, ry, left, right + 2 * math.pi))
    if lid:
        parts.append(ring(cx, cy, h, rx * lid, ry * lid))
    return parts


def taper(rx0, ry0, rx1, ry1, h, z0=0.0, lid=None, floor=True):
    """Cylinder whose footprint shrinks with height (Move)."""
    t0, t1 = silhouette_angle(rx0, ry0), silhouette_angle(rx1, ry1)
    parts = [ring(0, 0, z0 + h, rx1, ry1)]
    for a0, a1 in ((t0 + math.pi, t1 + math.pi), (t0, t1)):
        parts.append(seg(P(rx0 * math.cos(a0), ry0 * math.sin(a0), z0),
                         P(rx1 * math.cos(a1), ry1 * math.sin(a1), z0 + h)))
    if floor:
        parts.append(ring(0, 0, z0, rx0, ry0, t0 + math.pi, t0 + 2 * math.pi))
    if lid:
        parts.append(ring(0, 0, z0 + h, rx1 * lid, ry1 * lid))
    return parts


def face_rect(x0, x1, z0, z1, y=0.0, r=0.0):
    """Rectangle drawn on the front plane, optionally with rounded corners."""
    if not r:
        return poly(P(x0, y, z0), P(x1, y, z0), P(x1, y, z1), P(x0, y, z1))
    p = Path().M(P(x0 + r, y, z0))
    corners = [((x1 - r, z0), (x1, z0), (x1, z0 + r)),
               ((x1, z1 - r), (x1, z1), (x1 - r, z1)),
               ((x0 + r, z1), (x0, z1), (x0, z1 - r)),
               ((x0, z0 + r), (x0, z0), (x0 + r, z0))]
    for (lx, lz), (cxp, czp), (nx, nz) in corners:
        p.L(P(lx, y, lz))
        c1 = P(lx + (cxp - lx) * K, y, lz + (czp - lz) * K)
        c2 = P(nx + (cxp - nx) * K, y, nz + (czp - nz) * K)
        p.C(c1, c2, P(nx, y, nz))
    return p.Z()


def face_circle(cx, cz, r, y=0.0):
    p = Path()
    pts = [(cx + r, cz), (cx, cz + r), (cx - r, cz), (cx, cz - r)]
    p.M(P(pts[0][0], y, pts[0][1]))
    for i in range(4):
        (ax, az), (bx, bz) = pts[i], pts[(i + 1) % 4]
        # Tangents run vertical at the sides, horizontal at top and bottom.
        if i % 2 == 0:
            c1 = P(ax, y, az + K * r * (1 if i == 0 else -1))
            c2 = P(bx + K * r * (1 if i == 0 else -1), y, bz)
        else:
            c1 = P(ax + K * r * (-1 if i == 1 else 1), y, az)
            c2 = P(bx, y, bz + K * r * (1 if i == 1 else -1))
        p.C(c1, c2, P(bx, y, bz))
    return p.Z()



def tri_bar(length, half_depth, height):
    """Triangular-section bar lying on its side (Roam)."""
    def sec(x):
        return P(x, -half_depth, 0), P(x, half_depth, 0), P(x, 0, height)
    a0, b0, c0 = sec(0.0)
    a1, b1, c1 = sec(length)
    return [poly(a0, b0, c0), poly(a1, c1, b1, close=False),
            seg(a0, a1), seg(b0, b1), seg(c0, c1)]


def feet(w, d, h=0.0, drop=0.9):
    """The little legs Sonos components stand on."""
    return [seg(P(x, y, h), P(x, y, h - drop))
            for x, y in ((0, 0), (w, 0), (0, d))]


# ----------------------------------------------------------------------
# The devices, in centimetres: width across the front, depth back, height.
# ----------------------------------------------------------------------
def d_one():
    return cylinder(6.2, 6.2, 14.6, lid=0.70)


def d_era100():
    parts = cylinder(6.0, 6.6, 20.0, lid=0.66)
    parts.append(ring(0, 0, 19.0, 6.0 * 0.44, 6.6 * 0.44))   # dished top
    return parts


def d_era300():
    """The pinched waist, built as three stacked footprints."""
    rx_t, ry_t, rx_w, ry_w, rx_b, ry_b, h = 13.0, 9.0, 8.2, 5.7, 11.0, 7.6, 16.0
    tt, tw, tb = (silhouette_angle(rx_t, ry_t), silhouette_angle(rx_w, ry_w),
                  silhouette_angle(rx_b, ry_b))
    parts = [ring(0, 0, h, rx_t, ry_t)]
    for off in (math.pi, 0.0):
        top = P(rx_t * math.cos(tt + off), ry_t * math.sin(tt + off), h)
        mid = P(rx_w * math.cos(tw + off), ry_w * math.sin(tw + off), h * 0.5)
        bot = P(rx_b * math.cos(tb + off), ry_b * math.sin(tb + off), 0)
        p = Path().M(top)
        p.C((top[0] + (mid[0] - top[0]) * 0.9, top[1] + (mid[1] - top[1]) * 0.55),
            (mid[0], mid[1] - (mid[1] - top[1]) * 0.45), mid)
        p.C((mid[0], mid[1] + (bot[1] - mid[1]) * 0.45),
            (bot[0] + (mid[0] - bot[0]) * 0.9, bot[1] + (mid[1] - bot[1]) * 0.55), bot)
        parts.append(p)
    parts.append(ring(0, 0, 0, rx_b, ry_b, tb + math.pi, tb + 2 * math.pi))
    return parts


def d_five():
    w, d, h = 36.4, 15.4, 20.3
    return box(w, d, h) + [seg(P(w * 0.32, d * 0.26, h), P(w * 0.68, d * 0.26, h))]


def d_play3():
    w, d, h = 26.8, 13.2, 13.2
    return box(w, d, h) + [seg(P(w * 0.42, 0, h * 0.22), P(w * 0.58, 0, h * 0.22))]


def _bar(w, d, h, bevel=0.30):
    return box(w, d, h) + [seg(P(0, 0, h * (1 - bevel)), P(w, 0, h * (1 - bevel)))]


def d_arc():
    set_yaw(34)
    return _bar(114.0, 11.6, 8.7, 0.34)


def d_beam():
    set_yaw(30)
    return _bar(65.0, 10.0, 6.9, 0.32)


def d_ray():
    set_yaw(26)
    return box(56.0, 7.1, 9.5)


def d_playbar():
    set_yaw(32)
    w, d, h = 90.0, 14.0, 8.5
    return _bar(w, d, h, 0.38) + [seg(P(w * 0.5, 0, 0), P(w * 0.5, 0, h * 0.62))]


def d_playbase():
    set_yaw(16)
    w, d, h = 72.0, 38.0, 5.8
    inset = [P(w * 0.08, d * 0.14, h), P(w * 0.92, d * 0.14, h),
             P(w * 0.92, d * 0.86, h), P(w * 0.08, d * 0.86, h)]
    return box(w, d, h) + [poly(*inset)]


def d_sub():
    w, d, h = 15.8, 38.9, 40.2
    hx0, hx1, hz0, hz1 = w * 0.20, w * 0.80, h * 0.28, h * 0.72
    r = (hx1 - hx0) / 2
    return box(w, d, h) + [face_rect(hx0, hx1, hz0, hz1, r=r)]


def d_submini():
    r, h = 11.5, 30.5
    return cylinder(r, r, h) + [face_circle(0.0, h * 0.52, r * 0.42, y=-r * 0.55)]


def d_move():
    """Body on its charging base - the cue that says portable."""
    return (cylinder(8.4, 6.9, 1.4)
            + taper(7.5, 6.2, 6.8, 5.6, 22.0, z0=1.4, lid=0.74, floor=False))


def d_roam():
    set_yaw(30)
    return tri_bar(16.8, 3.1, 6.2)


def d_amp():
    w, d, h = 21.7, 21.7, 6.4
    return (box(w, d, h) + feet(w, d)
            + [face_circle(w * 0.74, h * 0.5, h * 0.28)])


def d_port():
    w, d, h = 13.8, 13.8, 4.4
    return (box(w, d, h) + feet(w, d, drop=0.7)
            + [face_rect(w * 0.44, w * 0.56, h * 0.24, h * 0.76, r=w * 0.05)])


def d_play1():
    """Same body as a One, with the Play:1's slight taper towards the top."""
    return taper(6.4, 6.4, 6.0, 6.0, 14.6, lid=0.70)


def d_bookshelf():
    """SYMFONISK bookshelf: a plain box, fabric front, no controls to speak of."""
    return box(31.0, 10.0, 20.0)


def d_lamp():
    """SYMFONISK lamp: a shade sitting on the glass cylinder that holds the driver."""
    return (cylinder(6.4, 6.4, 17.0)
            + taper(10.4, 10.4, 8.6, 8.6, 12.0, z0=17.0, floor=True))


def d_frame():
    """SYMFONISK picture frame: a thin panel, hung flat against the wall."""
    w, d, h = 41.0, 6.0, 27.0
    return box(w, d, h) + [face_rect(w * 0.10, w * 0.90, h * 0.11, h * 0.89)]


def d_generic():
    w, d, h = 20.0, 14.0, 15.0
    return box(w, d, h) + [face_circle(w * 0.5, h * 0.5, h * 0.28)]


DEVICES = [
    ("one", d_one), ("play1", d_play1), ("era100", d_era100), ("era300", d_era300),
    ("five", d_five), ("play3", d_play3),
    ("arc", d_arc), ("beam", d_beam), ("ray", d_ray),
    ("playbar", d_playbar), ("playbase", d_playbase),
    ("sub", d_sub), ("submini", d_submini),
    ("move", d_move), ("roam", d_roam),
    ("amp", d_amp), ("port", d_port),
    ("bookshelf", d_bookshelf), ("lamp", d_lamp), ("frame", d_frame),
    ("generic", d_generic),
]


# ----------------------------------------------------------------------
# Fitting, flattening and model matching
# ----------------------------------------------------------------------
BUILDERS = dict(DEVICES)
ICON_NAMES = tuple(name for name, _ in DEVICES)
FALLBACK = "generic"


def _bezier(p0, p1, p2, p3, steps):
    for i in range(1, steps + 1):
        t = i / steps
        u = 1 - t
        yield (u * u * u * p0[0] + 3 * u * u * t * p1[0]
               + 3 * u * t * t * p2[0] + t * t * t * p3[0],
               u * u * u * p0[1] + 3 * u * u * t * p1[1]
               + 3 * u * t * t * p2[1] + t * t * t * p3[1])


@cache
def fitted(name: str) -> tuple[Path, ...]:
    """Build a device and scale it to sit centred in the 32-unit grid.

    The bounds come from the flattened curves rather than their control points,
    so a cylinder ends up exactly as large as a box - measuring the hull would
    leave every curved device a little short.
    """
    set_yaw(0)
    parts = BUILDERS.get(name, BUILDERS[FALLBACK])()
    pts = [p for run in _runs(parts) for p in run]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    span = max(max(xs) - min(xs), max(ys) - min(ys))
    scale = (VIEW - 2 * MARGIN) / span
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2

    def place(p):
        return (VIEW / 2 + (p[0] - cx) * scale, VIEW / 2 + (p[1] - cy) * scale)

    for path in parts:
        path.segs = [(kind, [place(p) for p in pts]) for kind, pts in path.segs]
    return tuple(parts)


def svg_paths(name: str) -> list[str]:
    """The ``d`` attribute of every stroke in a device, ready for an SVG."""
    return [path.render() for path in fitted(name)]


def _runs(paths, steps: int = 8) -> list[tuple[tuple[float, float], ...]]:
    """Flatten paths to plain point runs, sampling every curve into segments."""
    runs = []
    for path in paths:
        run: list[tuple[float, float]] = []
        start = None
        for kind, pts in path.segs:
            if kind == "M":
                if len(run) > 1:
                    runs.append(tuple(run))
                start = pts[0]
                run = [start]
            elif kind == "L":
                run.append(pts[0])
            elif kind == "C":
                run.extend(_bezier(run[-1], pts[0], pts[1], pts[2], steps))
            elif kind == "Z" and start is not None:
                run.append(start)
        if len(run) > 1:
            runs.append(tuple(run))
    return runs


@cache
def polylines(name: str) -> tuple[tuple[tuple[float, float], ...], ...]:
    """A device as plain point runs, for anything that cannot draw curves."""
    return tuple(_runs(fitted(name)))
