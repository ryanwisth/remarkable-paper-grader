"""Parse MathWriting symbol InkML, screen for quality, render a contact sheet."""
import sys, math, glob, os
import xml.etree.ElementTree as ET
from PIL import Image, ImageDraw

NS = "{http://www.w3.org/2003/InkML}"
SYM = os.environ.get("MATHWRITING_SYMBOLS",
                     os.path.expanduser("~/data/mathwriting-2024/symbols"))


def parse(path):
    """-> (label, [stroke,...]) with stroke = [(x, y, t), ...]; InkML y is DOWN."""
    root = ET.parse(path).getroot()
    label = None
    for a in root.findall(NS + "annotation"):
        if a.get("type") == "label":
            label = (a.text or "").strip()
    strokes = []
    for tr in root.findall(NS + "trace"):
        pts = []
        for chunk in (tr.text or "").split(","):
            f = chunk.split()
            if len(f) >= 2:
                pts.append((float(f[0]), float(f[1]),
                            float(f[2]) if len(f) > 2 else 0.0))
        if len(pts) >= 2:
            strokes.append(pts)
    return label, strokes


def quality(strokes):
    """Cheap screen. Returns (ok, reason, stats)."""
    pts = [p for s in strokes for p in s]
    n = len(pts)
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    w, h = max(xs) - min(xs), max(ys) - min(ys)
    if n < 12:
        return False, f"too few points ({n})", {}
    if w < 1 or h < 1:
        return False, "degenerate bbox", {}
    diag = math.hypot(w, h)
    # path length relative to size — catches scribbles and retraced messes
    plen = sum(math.hypot(s[i][0] - s[i-1][0], s[i][1] - s[i-1][1])
               for s in strokes for i in range(1, len(s)))
    # mean turning angle: high = jagged (mouse/finger judder)
    turns = []
    for s in strokes:
        for i in range(1, len(s) - 1):
            ax, ay = s[i][0] - s[i-1][0], s[i][1] - s[i-1][1]
            bx, by = s[i+1][0] - s[i][0], s[i+1][1] - s[i][1]
            na, nb = math.hypot(ax, ay), math.hypot(bx, by)
            if na < 1e-6 or nb < 1e-6:
                continue
            c = max(-1.0, min(1.0, (ax*bx + ay*by) / (na*nb)))
            turns.append(math.degrees(math.acos(c)))
    jag = sum(turns) / len(turns) if turns else 0.0
    st = {"n": n, "w": w, "h": h, "strokes": len(strokes),
          "plen_ratio": plen / diag, "jag": jag}
    if jag > 42:
        return False, f"jagged (mean turn {jag:.0f}deg)", st
    if plen / diag > 9:
        return False, f"scribbled (path {plen/diag:.1f}x diag)", st
    return True, "", st


def to_font(strokes, target_h, baseline_y, x0=70.0):
    """Normalize into EMS glyph space: 1000/em, y-UP, baseline at y=0."""
    pts = [p for s in strokes for p in s]
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    x_lo, x_hi, y_lo, y_hi = min(xs), max(xs), min(ys), max(ys)
    h = max(y_hi - y_lo, 1e-6)
    k = target_h / h
    out = []
    for s in strokes:
        # InkML y grows DOWN; font y grows UP -> negate
        out.append([((p[0] - x_lo) * k + x0,
                     (y_hi - p[1]) * k + baseline_y) for p in s])
    return out


def load_class(label, limit=None):
    got = []
    for f in sorted(glob.glob(os.path.join(SYM, "*.inkml"))):
        try:
            lab, st = parse(f)
        except Exception:
            continue
        if lab != label or not st:
            continue
        ok, why, stats = quality(st)
        got.append({"file": os.path.basename(f), "strokes": st,
                    "ok": ok, "why": why, "stats": stats})
        if limit and len(got) >= limit:
            break
    return got


def sheet(cands, out_path, cols=8, cell=190, title=""):
    rows = (len(cands) + cols - 1) // cols
    im = Image.new("RGB", (cols * cell, rows * cell + 26), "white")
    d = ImageDraw.Draw(im)
    d.text((6, 6), title, fill="black")
    for i, c in enumerate(cands):
        cx, cy = (i % cols) * cell, (i // cols) * cell + 26
        pts = [p for s in c["strokes"] for p in s]
        xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
        w = max(max(xs) - min(xs), 1e-6); h = max(max(ys) - min(ys), 1e-6)
        k = (cell - 46) / max(w, h)
        ox = cx + (cell - w * k) / 2 - min(xs) * k
        oy = cy + (cell - 30 - h * k) / 2 - min(ys) * k + 12
        col = (0, 0, 0) if c["ok"] else (215, 215, 215)
        for s in c["strokes"]:
            q = [(p[0] * k + ox, p[1] * k + oy) for p in s]
            if len(q) >= 2:
                d.line(q, fill=col, width=3, joint="curve")
        d.rectangle([cx + 1, cy + 1, cx + cell - 2, cy + cell - 2],
                    outline=(230, 230, 230))
        d.text((cx + 6, cy + 4), f"{i}", fill=(190, 60, 60))
        if not c["ok"]:
            d.text((cx + 6, cy + cell - 16), c["why"][:22], fill=(200, 120, 120))
    im.save(out_path)
    return out_path


if __name__ == "__main__":
    label = sys.argv[1]
    out = sys.argv[2]
    cands = load_class(label)
    good = sum(1 for c in cands if c["ok"])
    print(f"{label}: {len(cands)} instances, {good} pass the screen")
    sheet(cands, out, title=f"{label}  —  {good}/{len(cands)} pass")
    print("wrote", out)
