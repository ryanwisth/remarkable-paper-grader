"""Screen the digit bank for RECOGNISABILITY, then select for DIVERSITY.

Two competing goods: a variant must still obviously be that digit (or it is a
legibility bug), but the variants must differ from each other (or there is no
point). So: reject anything too far from the EMS reference shape, then greedily
pick the most mutually-different survivors.
"""
import os, sys, json, math
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PIL import Image, ImageDraw
import ems

def raster(strokes, step=0.035):
    """Dense point cloud in a unit box. Chamfer — not IoU — is the right measure
    for thin strokes: two people's identically-shaped 4 barely overlap pixel-wise,
    so IoU rejects nearly everything while chamfer tolerates the offset."""
    pts = [p for s in strokes for p in s]
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    w = max(x1 - x0, 1e-6); h = max(y1 - y0, 1e-6)
    sc = 1.0 / max(w, h)
    out = []
    for s in strokes:
        q = [((p[0]-x0)*sc, (p[1]-y0)*sc) for p in s]
        for i in range(1, len(q)):
            ax, ay = q[i-1]; bx, by = q[i]
            d = math.hypot(bx-ax, by-ay)
            n = max(1, int(d/step))
            for t in range(n+1):
                out.append((ax + (bx-ax)*t/n, ay + (by-ay)*t/n))
    return out


def _dir(a, b):
    tot = 0.0
    for p in a:
        m = min((p[0]-q[0])**2 + (p[1]-q[1])**2 for q in b)
        tot += math.sqrt(m)
    return tot / len(a)


def iou(a, b):
    """Similarity in [0,1]: 1 - symmetric chamfer distance, clamped."""
    if not a or not b:
        return 0.0
    return max(0.0, 1.0 - 0.5 * (_dir(a, b) + _dir(b, a)) / 0.22)


bank = json.load(open("digit_bank.json"))
raw = json.load(open("digit_bank_all.json")) if False else None

# rebuild from the full candidate pool, not the pre-trimmed six
import ink_digits as D  # reuses its parse/normalize/deslant pipeline
pool = D.cands

MIN_IOU = 0.63
KEEP = 5
out, report = {}, []
for d in "0123456789":
    ref = raster([[list(p) for p in s] for s in ems._GLYPHS[ord(d)][1]])
    ok = []
    for c in pool[d]:
        r = raster(c["g"])
        s = iou(ref, r)
        if s >= MIN_IOU:
            ok.append((s, r, c))
    ok.sort(key=lambda t: -t[0])
    # Two stages, and the order matters. Maximising diversity over the whole pool
    # walks straight into the weird tail — crossed 7s that read as F, 9s that read
    # as g. So first keep only the safest half (closest to the EMS reference), and
    # only then diversify inside it. Variety must be bought from the safe set.
    ok = ok[:max(5, len(ok) // 3)]
    sel = []
    if ok:
        sel.append(ok[0])
        while len(sel) < KEEP and len(sel) < len(ok):
            best, bd = None, -1
            for cand in ok:
                if any(cand[2]["file"] == s[2]["file"] for s in sel):
                    continue
                dmin = min(iou(cand[1], s[1]) for s in sel)
                if (1 - dmin) > bd:
                    bd, best = (1 - dmin), cand
            if best is None:
                break
            sel.append(best)
    out[d] = [s[2]["g"] for s in sel]
    report.append(f"  {d}: {len(pool[d]):2d} candidates -> {len(ok):2d} recognisable "
                  f"(IoU>={MIN_IOU}) -> keeping {len(sel)}"
                  + ("   *** NO VARIANTS, falls back to EMS" if not sel else ""))

print("\n".join(report))
json.dump(out, open("digit_bank_final.json", "w"))

cell = 130
im = Image.new("RGB", (cell*(KEEP+1)+10, cell*10+20), "white")
dr = ImageDraw.Draw(im)
for row, d in enumerate("0123456789"):
    y = row*cell + 14
    sets = [[[list(p) for p in s] for s in ems._GLYPHS[ord(d)][1]]] + out[d]
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
    dr.text((30, y+cell-20), "EMS", fill=(170,40,40))
im.save("digits2.png")
print("\nwrote digits2.png")
