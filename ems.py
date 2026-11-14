"""EMS single-stroke SVG font renderer (EMS Casual Hand — neat print handwriting).

SVG 1.1 font: glyphs are M/L polylines with occasional C cubics (flattened here).
Coordinate space: 1000 units/em, y-up. License: SIL OFL (oskay/svg-fonts).
API-compatible with hershey.text_to_polylines.
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path

_FONT = Path(__file__).parent / "EMSCasualHand.svg"
_NUM = re.compile(r"[-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?")
_CMD = re.compile(r"([MLCmlc])|(" + _NUM.pattern + r")")


def _flatten_cubic(p0, p1, p2, p3, steps=8):
    out = []
    for i in range(1, steps + 1):
        t = i / steps
        mt = 1 - t
        x = mt**3 * p0[0] + 3 * mt**2 * t * p1[0] + 3 * mt * t**2 * p2[0] + t**3 * p3[0]
        y = mt**3 * p0[1] + 3 * mt**2 * t * p1[1] + 3 * mt * t**2 * p2[1] + t**3 * p3[1]
        out.append((x, y))
    return out


def _parse_path(d: str) -> list[list[tuple[float, float]]]:
    tokens = []
    for m in _CMD.finditer(d):
        tokens.append(m.group(1) if m.group(1) else float(m.group(2)))
    strokes: list[list[tuple[float, float]]] = []
    cur: list[tuple[float, float]] = []
    i = 0
    cmd = None
    pos = (0.0, 0.0)
    while i < len(tokens):
        t = tokens[i]
        if isinstance(t, str):
            cmd = t
            i += 1
            continue
        if cmd in ("M", "m"):
            if cur:
                strokes.append(cur)
            x, y = tokens[i], tokens[i + 1]
            if cmd == "m":
                x, y = pos[0] + x, pos[1] + y
            pos = (x, y)
            cur = [pos]
            i += 2
            cmd = "L" if cmd == "M" else "l"   # implicit lineto after moveto
        elif cmd in ("L", "l"):
            x, y = tokens[i], tokens[i + 1]
            if cmd == "l":
                x, y = pos[0] + x, pos[1] + y
            pos = (x, y)
            cur.append(pos)
            i += 2
        elif cmd in ("C", "c"):
            c1 = (tokens[i], tokens[i + 1])
            c2 = (tokens[i + 2], tokens[i + 3])
            end = (tokens[i + 4], tokens[i + 5])
            if cmd == "c":
                c1 = (pos[0] + c1[0], pos[1] + c1[1])
                c2 = (pos[0] + c2[0], pos[1] + c2[1])
                end = (pos[0] + end[0], pos[1] + end[1])
            cur.extend(_flatten_cubic(pos, c1, c2, end))
            pos = end
            i += 6
        else:
            i += 1   # unknown command payload — skip conservatively
    if cur:
        strokes.append(cur)
    return strokes


def _load():
    tree = ET.parse(_FONT)
    root = tree.getroot()
    font = root.find(".//{http://www.w3.org/2000/svg}font")
    face = root.find(".//{http://www.w3.org/2000/svg}font-face")
    upm = float(face.get("units-per-em", 1000))
    default_adv = float(font.get("horiz-adv-x", upm * 0.5))
    glyphs = {}
    variants: dict = {}
    for g in root.iter("{http://www.w3.org/2000/svg}glyph"):
        u = g.get("unicode")
        if not u or len(u) != 1:
            continue
        adv = float(g.get("horiz-adv-x", default_adv))
        d = g.get("d")
        strokes = _parse_path(d) if d else []
        glyphs[ord(u)] = (adv, strokes)
    # Real human pen strokes for glyphs this font simply does not have
    # (sqrt, <=, >=, ->, !=). Harvested from MathWriting; see human_glyphs.json
    # for provenance and licence. ADD-ONLY: an OFL glyph is never overridden, so
    # the shipped hand stays authoritative wherever it has an opinion.
    hp = Path(__file__).parent / "human_glyphs.json"
    if hp.exists():
        try:
            import json
            bank = json.loads(hp.read_text())
            for ch, g in bank.get("glyphs", {}).items():
                if len(ch) == 1 and ord(ch) not in glyphs:
                    glyphs[ord(ch)] = (float(g["adv"]),
                                       [[(float(x), float(y)) for x, y in st]
                                        for st in g["strokes"]])
            # Allographs: alternate real-human forms for glyphs the font DOES have.
            # A person re-forms a digit rather than perturbing one outline, and that
            # is the single cue per-glyph jitter cannot fake — six 2s on a page are
            # otherwise six copies of one shape under noise.
            for ch, vs in bank.get("variants", {}).items():
                if len(ch) == 1 and vs:
                    variants[ord(ch)] = [[[(float(x), float(y)) for x, y in st]
                                          for st in v] for v in vs]
        except Exception:
            pass          # a malformed bank must never take the tutor's pen away
    # cap height estimate from 'H'
    cap = 700.0
    if ord("H") in glyphs and glyphs[ord("H")][1]:
        ys = [p[1] for st in glyphs[ord("H")][1] for p in st]
        cap = max(ys) - min(ys) or 700.0
    return glyphs, upm, default_adv, cap, variants


_GLYPHS, _UPM, _DEF_ADV, _CAP, _VARIANTS = _load()


def text_to_polylines(
    text: str,
    x: float,
    y: float,
    size: float = 28.0,
    max_width: float | None = None,
) -> tuple[list[list[list[float]]], float]:
    """Render text at (x, y) top-left-ish baseline anchor. Same contract as hershey."""
    import random
    import zlib
    # NOT hash(): Python salts str hashing per process, so the same note seeded a
    # different hand on every run and the documented "stable across re-renders"
    # property was false. That now also picks the allograph, so an unstable seed
    # would re-letter the page nightly. crc32 is stable across processes.
    rnd = random.Random(zlib.crc32(text.encode("utf-8")) & 0xFFFFFFFF)
    scale = size / _CAP
    line_h = size * 1.75
    track = 0.06 * _DEF_ADV          # SVG fonts have no kerning; add gentle tracking
    out: list[list[list[float]]] = []
    cx, cy = x, y + size             # y param is top; glyph baseline sits size below
    # line-level realism (2026-08-24, RESEARCH-PREMIUM-HAND follow-up): a slow baseline
    # wave along each line + a tiny per-line slope. Per-glyph drift alone reads as noise;
    # real lines undulate coherently. Amplitudes kept below ~2% of size — legibility first.
    wave_amp = size * 0.018
    wave_len = size * 26.0
    line_phase = rnd.uniform(0.0, 6.28)
    line_slope = rnd.uniform(-0.004, 0.004)

    def _line_dy(px_x: float) -> float:
        import math as _m
        return (wave_amp * _m.sin(line_phase + 6.2832 * (px_x - x) / wave_len)
                + line_slope * (px_x - x))

    # 2026-08-26: independent per-glyph noise -> correlated structure. Measured on
    # the old output, the lag-1 autocorrelation of glyph lean was -0.02: every
    # letter chose its tilt alone, so neighbours leaned opposite ways. No hand does
    # that; a real one has ONE slant that wanders. Same for size. AR(1) at rho=0.82
    # keeps the variance identical and only adds memory, so runs of letters lean and
    # grow together — one motor act instead of letter-stamping.
    RHO = 0.82
    _ar_k = _math.sqrt(1.0 - RHO * RHO)

    def _ar(state: list[float], sigma: float) -> float:
        state[0] = RHO * state[0] + _ar_k * rnd.gauss(0.0, sigma)
        return state[0]

    # Allograph policy. Drawing a fresh variant per occurrence would sample a
    # different WRITER each time and read as several people sharing a page — real
    # variation is one person re-forming their own letter. So each note picks ONE
    # alternate form per digit and alternates it against the house form: two shapes
    # per digit on a page, which is what a single hand actually produces.
    alt = {c: rnd.choice(v) for c, v in _VARIANTS.items()} if _VARIANTS else {}

    # the hand's own lean, one per note; per-glyph lean wanders around it.
    slant0 = rnd.uniform(-0.05, 0.05)
    slant_dev = [0.0]
    size_dev = [0.0]
    base_dev = [0.0]

    # NOTE: stacked fractions were built here and REMOVED after rendering them.
    # At the 0.62 scale a stacked fraction needs, the decimal points disappear —
    # "0.998/1.0002" read as "0998" over "10002", which changes the value, not just
    # the look. Inline was already legible. Do not rebuild this without solving the
    # decimal point first (a larger scale collides with the line above).
    for word in text.replace("\n", " \n ").split(" "):
        if word == "\n":
            cx, cy = x, cy + line_h
            line_phase = rnd.uniform(0.0, 6.28)
            line_slope = rnd.uniform(-0.004, 0.004)
            continue
        wlen = sum((_GLYPHS.get(ord(ch), (_DEF_ADV, []))[0] + track) * scale for ch in word + " ")
        if max_width and cx + wlen > x + max_width and cx > x:
            cx, cy = x, cy + line_h
            line_phase = rnd.uniform(0.0, 6.28)
            line_slope = rnd.uniform(-0.004, 0.004)
        chars = word + " "
        ci, sup = 0, 0
        while ci < len(chars):
            ch = chars[ci]
            # Exponents. "10^-3" used to render as a literal caret, minus and 3 at
            # full size — four character slots on a page whose width is rationed,
            # and it reads as a caret rather than as a power. Consume the '^' and
            # raise the run that follows instead.
            if ch == "^" and sup == 0:
                j = ci + 1
                if j < len(chars) and chars[j] == "-":
                    j += 1
                k = j
                while k < len(chars) and chars[k].isdigit():
                    k += 1
                if k == j and j < len(chars) and chars[j].isalpha():
                    k = j + 1                  # x^n as well as 10^-3
                if k > ci + 1:
                    sup = k - (ci + 1)
                    ci += 1
                    continue
            adv, strokes = _GLYPHS.get(ord(ch), (_DEF_ADV, []))
            if ord(ch) in alt and rnd.random() < 0.5:
                strokes = alt[ord(ch)]         # advance stays the font's: no reflow
            gs = scale * (1.0 + _ar(size_dev, 0.023)) * (0.62 if sup else 1.0)
            rise = 0.42 * size if sup else 0.0
            wob = _ar(base_dev, 0.020) * size
            # Lean is applied as SHEAR, not rotation. Rotation pivots the whole
            # glyph, so it lifts the feet of 1/4/7/t off the writing line by up to
            # ~2% of its width; shear leaves y untouched (gy = 0 at the baseline)
            # so the feet stay planted. It is also how type has always slanted.
            sh = slant0 + _ar(slant_dev, 0.018)
            gdy = _line_dy(cx)                                     # baseline wave at the anchor
            for st in strokes:
                pts = []
                for (px, py) in st:
                    gx, gy = px * gs, -py * gs                     # y-up -> y-down, glyph-local
                    pts.append([cx + gx - sh * gy + rnd.uniform(-0.25, 0.25),
                                cy + gy + wob + gdy - rise + rnd.uniform(-0.25, 0.25)])
                if len(pts) >= 2:
                    out.append(pts)
            cx += (adv + track) * gs
            if sup:
                sup -= 1
            ci += 1
    return out, cy + line_h - size


if __name__ == "__main__":
    polys, yy = text_to_polylines("Rule: flip to the smaller group. C(14,10)=C(14,4)=1001", 0, 0, 26)
    print(f"glyphs: {len(_GLYPHS)} | polylines: {len(polys)} | cap: {_CAP:.0f}")


# --- naturalization (Aug 24 2026 ink lab; see INK-LAB-FINDINGS.md) -----------

import math as _math
import random as _random
import zlib as _zlib


def _nat_resample(poly, step=3.0):
    """Even arc-length resampling; always keeps first and last points."""
    if len(poly) < 2:
        return poly
    out = [list(poly[0])]
    dist_to_next = step
    for i in range(len(poly) - 1):
        x0, y0 = poly[i]
        x1, y1 = poly[i + 1]
        seg = _math.hypot(x1 - x0, y1 - y0)
        if seg == 0:
            continue
        pos = 0.0
        while pos + dist_to_next <= seg:
            pos += dist_to_next
            t = pos / seg
            out.append([x0 + (x1 - x0) * t, y0 + (y1 - y0) * t])
            dist_to_next = step
        dist_to_next -= (seg - pos)
    if out[-1] != list(poly[-1]):
        out.append(list(poly[-1]))
    return out


def _nat_chaikin(poly):
    if len(poly) < 3:
        return poly
    out = [poly[0]]
    for i in range(len(poly) - 1):
        (x0, y0), (x1, y1) = poly[i], poly[i + 1]
        out.append([0.75 * x0 + 0.25 * x1, 0.75 * y0 + 0.25 * y1])
        out.append([0.25 * x0 + 0.75 * x1, 0.25 * y0 + 0.75 * y1])
    out.append(poly[-1])
    return out


def _nat_dwell(p, gain=0.30, k0=0.05, win=4):
    """Per-point width gain from curvature, via the 2/3 power law of motor control.

    A hand slows in tight curves — v ∝ r^(1/3), i.e. v ∝ κ^(-1/3) — while the pen
    lays ink at a roughly constant rate per unit TIME. Slower pen therefore means
    more ink per unit LENGTH: strokes thicken at the belly of a 3, the bottom of an
    s, the corner of a 4, and stay thin through fast straight sweeps.

    Returns additive gains in [0, gain]. Below k0 the pen is at full speed and the
    gain is EXACTLY zero, so a straight stroke ('=', '-', '/', '1') renders
    identically to a run with this disabled. Curvature can only ever add ink, which
    is what makes this legibility-safe by construction rather than by tuning.

    Supersedes the Aug-24 pooling pass, which was a step function: nothing at all
    below a 20 degree turn, then an abrupt jump. Real dwell is continuous.

    Gain calibrated 2026-08-26 by rendering a ladder at production size (30px):
    0.30 is invisible (mean +4% width), 0.70 reads as a pen slowing into curves
    (+10%, peak +7 on base 15) while the counters of 9/8/a/e stay open, and 1.20+
    starts filling them. Counters close first on e-ink, so 0.70 is the ceiling
    worth taking here — raise it only against a fresh on-device look.
    """
    n = len(p)
    g = [0.0] * n
    for i in range(1, n - 1):
        ax, ay = p[i][0] - p[i - 1][0], p[i][1] - p[i - 1][1]
        bx, by = p[i + 1][0] - p[i][0], p[i + 1][1] - p[i][1]
        la, lb = _math.hypot(ax, ay), _math.hypot(bx, by)
        if la < 1e-6 or lb < 1e-6:
            continue
        cosang = max(-1.0, min(1.0, (ax * bx + ay * by) / (la * lb)))
        ds = 0.5 * (la + lb)
        if ds < 1e-6:
            continue
        kappa = _math.acos(cosang) / ds                 # rad per unit arc length
        if kappa <= k0:
            continue                                    # full speed -> no extra ink
        v = (k0 / kappa) ** (1.0 / 3.0)                  # normalized speed, <= 1
        g[i] = gain * (1.0 - v)
    if win > 1 and n > win:                             # the hand has inertia:
        sm = [0.0] * n                                  # width cannot step instantly
        for i in range(n):
            lo, hi = max(0, i - win), min(n, i + win + 1)
            sm[i] = sum(g[lo:hi]) / (hi - lo)
        g = sm
    return g


def _block_seed(polys) -> int:
    """A stable seed derived from the geometry itself.

    seed=7 used to be a default that every call site took, so each note block
    restarted the pen-dynamics RNG at the same state and the first stroke of every
    block got the identical width phase — a twin-tell living in the width channel.
    Deriving from the strokes keeps renders reproducible without asking eleven call
    sites to invent a seed.
    """
    key = repr([[(round(x, 1), round(y, 1)) for x, y in p[:4]] for p in polys[:8]])
    return _zlib.crc32(key.encode()) & 0xFFFFFFFF


def naturalize(polys, base_w=16, taper=True, wobble=0.10, smooth=True,
               flick=True, seed=None, curve_gain=0.70, shade=0.13,
               cross=0.16, lift=0.07):
    """Turn font polylines into (points, per-point widths) that read as pen.

    Levers proven on-device: touchdown ramp + lift taper + exit flick + width
    wobble, all via the per-point width channel (the only width channel the
    firmware honors). Returns [(polyline, widths)] pairs.
    """
    rnd = _random.Random(_block_seed(polys) if seed is None else seed)
    # Where two strokes cross, real ink is laid twice and reads darker. The width
    # channel is the only way to say that here, so points near a crossing widen.
    # A spatial hash keeps it linear instead of comparing every pair of segments.
    grid: dict = {}
    CELL = 4.0
    if cross:
        # densify first: the font stores a straight stroke as two endpoints, so a
        # grid built from raw points has nothing near the actual intersection and
        # '+' and '8' register no crossings at all.
        for pi, poly in enumerate(polys):
            for gi, (px, py) in enumerate(_nat_resample([list(q) for q in poly], 2.0)):
                grid.setdefault((int(px // CELL), int(py // CELL)), []).append(
                    (pi, gi * 2.0, px, py))
    out = []
    for _pi, poly in enumerate(polys):
        if len(poly) < 2:
            continue
        p = [list(pt) for pt in poly]
        if smooth:
            p = _nat_chaikin(p)
        p = _nat_resample(p, 3.0)
        L = sum(_math.hypot(p[i + 1][0] - p[i][0], p[i + 1][1] - p[i][1])
                for i in range(len(p) - 1))
        if flick and L > 10 and len(p) >= 3:
            (xa, ya), (xb, yb) = p[-2], p[-1]
            seg = _math.hypot(xb - xa, yb - ya) or 1.0
            ext = min(2.5, 0.15 * L)
            p.append([xb + (xb - xa) / seg * ext, yb + (yb - ya) / seg * ext])
        n = len(p)
        if curve_gain is None:
            # legacy Aug-24 step-function pooling, kept only for A/B against the
            # dwell model. Drop this branch once the comparison is settled.
            pool = [0.0] * n
            for i in range(1, n - 1):
                ax, ay = p[i][0] - p[i - 1][0], p[i][1] - p[i - 1][1]
                bx, by = p[i + 1][0] - p[i][0], p[i + 1][1] - p[i][1]
                la, lb = _math.hypot(ax, ay), _math.hypot(bx, by)
                if la < 1e-6 or lb < 1e-6:
                    continue
                cosang = max(-1.0, min(1.0, (ax * bx + ay * by) / (la * lb)))
                turn = _math.degrees(_math.acos(cosang))
                if turn > 20:
                    pool[i] = max(pool[i], 0.15 * min(1.0, turn / 70.0))
            for i in range(n):                    # one-neighbor falloff
                if pool[i]:
                    if i > 0:
                        pool[i - 1] = max(pool[i - 1], pool[i] * 0.5)
                    if i < n - 1:
                        pool[i + 1] = max(pool[i + 1], pool[i] * 0.5)
        else:
            pool = _nat_dwell(p, gain=curve_gain)
        ws = []
        phase = rnd.uniform(0, 6.28)
        for i in range(n):
            t = i / max(1, n - 1)
            w = float(base_w)
            if taper and L > 6:
                if t < 0.15:
                    w *= 0.55 + 0.45 * (t / 0.15)
                elif t > 0.80:
                    w *= 1.0 - 0.65 * ((t - 0.80) / 0.20)
            w *= 1.0 + pool[i]
            # Downstroke shading. A pen is pulled DOWN and pushed UP, so it lays
            # more ink descending. Measured on the old output the ratio was 0.973
            # — not merely absent, backwards. Additive on descents only, so the
            # "can only add ink" property of this whole pass survives.
            if shade and i > 0:
                dx_, dy_ = p[i][0] - p[i - 1][0], p[i][1] - p[i - 1][1]
                seg_ = _math.hypot(dx_, dy_)
                if seg_ > 1e-6:
                    w *= 1.0 + shade * max(0.0, dy_ / seg_)   # +y is downward here
            if cross:                                  # ink laid twice at crossings
                # true distance, not cell adjacency: neighbouring LETTERS sit well
                # inside a coarse cell, and testing adjacency bolded the whole page.
                gx0, gy0 = int(p[i][0] // CELL), int(p[i][1] // CELL)
                hit = False
                for a in (-1, 0, 1):
                    for b in (-1, 0, 1):
                        for (o, oarc, ox, oy) in grid.get((gx0 + a, gy0 + b), ()):
                            if (ox - p[i][0]) ** 2 + (oy - p[i][1]) ** 2 >= 6.25:
                                continue
                            # a stroke may cross ITSELF (8, t, 0) — accept that,
                            # but only far along the path, or every neighbouring
                            # point on the stroke would count as its own crossing.
                            if o != _pi or abs(oarc - i * 3.0) > 26.0:
                                hit = True
                                break
                        if hit:
                            break
                    if hit:
                        break
                if hit:
                    w *= 1.0 + cross
            if wobble:
                w *= 1.0 + wobble * _math.sin(phase + t * 5.1) * rnd.uniform(0.6, 1.0)
            ws.append(max(2, int(round(w))))
        # A hand occasionally lifts mid-stroke where the font draws straight
        # through. Rare, and only on long strokes, or it reads as a rendering
        # defect rather than as writing; the cut is kept to the middle third so a
        # letter never loses its head or its tail.
        if lift and L > 26 and len(p) >= 10 and rnd.random() < lift:
            c = rnd.randrange(len(p) // 3, 2 * len(p) // 3)
            if c >= 3 and len(p) - (c + 2) >= 3:
                out.append((p[:c], ws[:c]))
                out.append((p[c + 2:], ws[c + 2:]))
                continue
        out.append((p, ws))
    return out


def measure(text: str, size: float = 28.0, max_width: float | None = None):
    """Exact (width_px, n_lines) as text_to_polylines will render it.
    n_lines derives from y_end, which advances in whole line_h steps."""
    polys, y_end = text_to_polylines(text, 0.0, 0.0, size, max_width=max_width)
    line_h = size * 1.75
    n_lines = max(1, round(y_end / line_h))
    w = max((pt[0] for p in polys for pt in p), default=0.0)
    return w, n_lines
