"""Build an allograph bank of real human DIGITS, normalized into EMS glyph space.

The ransom-note risk in mixing writers comes from inconsistent SIZE, SLANT and
WEIGHT — not from letterform shape. So normalize those three away hard and keep
only the shape: same cap height, same baseline, deslanted by image moments, same
advance as the EMS digit it replaces. What is left varying is exactly the cue we
want — a person re-forms a digit rather than perturbing one outline.
"""
import sys, math, glob, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import xml.etree.ElementTree as ET
from PIL import Image, ImageDraw
import ems

NS = "{http://www.w3.org/2003/InkML}"
SYM = os.environ.get("MATHWRITING_SYMBOLS",
                     os.path.expanduser("~/data/mathwriting-2024/symbols"))


def parse(path):
    root = ET.parse(path).getroot()
    lab = None
    for a in root.findall(NS + "annotation"):
        if a.get("type") == "label":
            lab = (a.text or "").strip()
    strokes = []
    for tr in root.findall(NS + "trace"):
        pts = []
        for c in (tr.text or "").split(","):
            f = c.split()
            if len(f) >= 2:
                pts.append((float(f[0]), float(f[1])))
        if len(pts) >= 2:
            strokes.append(pts)
    return lab, strokes


def deslant(strokes):
    """Moment-based shear so the ink's vertical axis is upright."""
    pts = [p for s in strokes for p in s]
    n = len(pts)
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    vyy = sum((p[1] - my) ** 2 for p in pts) / n
    cxy = sum((p[0] - mx) * (p[1] - my) for p in pts) / n
    if vyy < 1e-9:
        return strokes
    s = -cxy / vyy
    s = max(-0.6, min(0.6, s))
    return [[(p[0] + s * (p[1] - my), p[1]) for p in st] for st in strokes]


def rdp(st, tol):
    if len(st) < 3:
        return st
    a, b = st[0], st[-1]
    dx, dy = b[0] - a[0], b[1] - a[1]
    n = math.hypot(dx, dy)
    worst, wi = -1.0, 0
    for i in range(1, len(st) - 1):
        p = st[i]
        d = (math.hypot(p[0]-a[0], p[1]-a[1]) if n < 1e-9 else
             abs(dy*p[0] - dx*p[1] + b[0]*a[1] - b[1]*a[0]) / n)
        if d > worst:
            worst, wi = d, i
    if worst <= tol:
        return [a, b]
    return rdp(st[:wi+1], tol)[:-1] + rdp(st[wi:], tol)


def normalize(strokes, tgt_h, tgt_w, y0, x0):
    """Fit the target box exactly in height; clamp width to the EMS digit's."""
    pts = [p for s in strokes for p in s]
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    xl, xh, yl, yh = min(xs), max(xs), min(ys), max(ys)
    h = max(yh - yl, 1e-6); w = max(xh - xl, 1e-6)
    k = tgt_h / h
    # A variant keeps the EMS ADVANCE, so it must not be wider than the EMS ink or
    # it crashes into the next character ("16525" collided at 1.18x).
    if w * k > tgt_w:
        k_x = tgt_w / w
    else:
        k_x = k
    out = []
    for s in strokes:
        q = [[(p[0]-xl)*k_x + x0, (yh-p[1])*k + y0] for p in s]   # InkML y is DOWN
        q = rdp(q, 3.0)
        if len(q) >= 2:
            out.append([[round(a,1), round(b,1)] for a, b in q])
    return out


def jag(strokes):
    t = []
    for s in strokes:
        for i in range(1, len(s)-1):
            ax, ay = s[i][0]-s[i-1][0], s[i][1]-s[i-1][1]
            bx, by = s[i+1][0]-s[i][0], s[i+1][1]-s[i][1]
            na, nb = math.hypot(ax,ay), math.hypot(bx,by)
            if na < 1e-6 or nb < 1e-6: continue
            c = max(-1.0, min(1.0, (ax*bx+ay*by)/(na*nb)))
            t.append(math.degrees(math.acos(c)))
    return sum(t)/len(t) if t else 99.0


# EMS reference metrics per digit
REF = {}
for dch in "0123456789":
    adv, st = ems._GLYPHS[ord(dch)]
    p = [q for s in st for q in s]
    REF[dch] = {"adv": adv, "w": max(x for x,_ in p) - min(x for x,_ in p),
                "h": max(y for _,y in p) - min(y for _,y in p),
                "y0": min(y for _,y in p), "x0": min(x for x,_ in p),
                "ns": len(st)}

cands = {d: [] for d in "0123456789"}
for f in sorted(glob.glob(os.path.join(SYM, "*.inkml"))):
    try:
        lab, st = parse(f)
    except Exception:
        continue
    if lab not in cands or not st:
        continue
    if sum(len(s) for s in st) < 14:
        continue
    j = jag(st)
    if j > 40:
        continue
    r = REF[lab]
    g = normalize(deslant(st), r["h"], r["w"], r["y0"], r["x0"])
    if not g:
        continue
    cands[lab].append({"g": g, "jag": j, "ns": len(g), "file": os.path.basename(f)})

# keep the cleanest few per digit, preferring the EMS stroke count
KEEP = 6
bank = {}
for d, lst in cands.items():
    lst.sort(key=lambda c: (abs(c["ns"] - REF[d]["ns"]), c["jag"]))
    bank[d] = lst[:KEEP]
    print(f"{d}: {len(lst):2d} usable -> keeping {len(bank[d])} "
          f"(EMS has {REF[d]['ns']} stroke(s); jag {min(c['jag'] for c in lst):.0f}-"
          f"{max(c['jag'] for c in lst):.0f})")

# contact sheet: EMS reference in the left column, human variants beside it
cell = 130
im = Image.new("RGB", (cell*(KEEP+1)+10, cell*10+20), "white")
dr = ImageDraw.Draw(im)
for row, d in enumerate("0123456789"):
    y = row*cell + 14
    dr.text((6, y+4), d, fill=(200,60,60))
    sets = [[[list(p) for p in s] for s in ems._GLYPHS[ord(d)][1]]] + [c["g"] for c in bank[d]]
    for col, g in enumerate(sets):
        ox = col*cell + 26
        pts = [p for s in g for p in s]
        if not pts: continue
        xs=[p[0] for p in pts]; ys=[p[1] for p in pts]
        w_=max(max(xs)-min(xs),1e-6); h_=max(max(ys)-min(ys),1e-6)
        k=(cell-46)/max(w_,h_)
        for s in g:
            q=[(p[0]*k+ox-min(xs)*k+14, (cell-24)-(p[1]-min(ys))*k+y) for p in s]
            if len(q)>=2: dr.line(q, fill=(0,0,0) if col else (170,40,40), width=3, joint="curve")
        dr.rectangle([ox, y, ox+cell-8, y+cell-8], outline=(235,235,235))
    dr.text((26+4, y+cell-20), "EMS", fill=(170,40,40))
im.save(sys.argv[1] if len(sys.argv)>1 else "digits.png")
print("\nwrote contact sheet")
json.dump({d: [c["g"] for c in bank[d]] for d in bank},
          open("digit_bank.json","w"))
