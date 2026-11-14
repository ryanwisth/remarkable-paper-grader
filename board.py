"""The Ordered Board — the depth surface below the fold.

Layout (canonical px; spec v3 §board):

  FOLD ------------------------------------------------ blue rule x200-1490
    you did                    |        do this            labels, blue
  (1) <trace row, black>       o  1. <route step, blue>
  (2) <trace row, black>       /  2. <route step, blue>
  (3) ...                      ?  3. <... = [empty box]>
                                  [  <gate: a 2s self-check>
  break  <the fact, red, full width>

Left column = the writer's attempt in stroke order (black body, provenance by ordinal
weight + words). Central spine x620 = per-row state glyph. Right column = the
WORKED route (show-the-work law: real values line by line, ending at the
answer, on wrong/unanswered pages). Everything lettered here has already
passed mathcheck (equations true, lexicon clean; leak guard on correct pages).
"""
from __future__ import annotations

from ems import text_to_polylines, naturalize, measure
import mathcheck

_NO_GLYPH = [("->", " to "), ("→", " to "), ("<-", " from "), ("≠", " differs "),
             ("✓", "yes"), ("≥", ">="), ("≤", "<="), ("√", "sqrt"), ("☐", ""),
             ("—", "-"), ("×", "x"), ("·", "."), ("±", "+-")]


def _clean(text: str) -> str:
    for bad, good in _NO_GLYPH:
        text = text.replace(bad, good)
    return " ".join(text.split())

# canonical px -> scene (same constants as rmreview.canon_to_scene)
def _s(x: float, y: float) -> tuple[float, float]:
    return (x - 809.5, y + 0.5)


BLUE = (6, 0xFF0062CC)
RED = (7, 0xFFD90707)
GREEN = (10, 0xFF91DA71)
BLACK = (0, 0xFF000000)

BX = 200                 # board left edge
SPINE_X = 700
RX_ORD, RX_TEXT = 774, 820
LX_DISC, LX_TEXT = 196, 230
SIZE = 36                # bumped 24->30->36 after user feedback: bigger
PITCH = 70
BOARD_BOTTOM = 1935
CAP_L, CAP_R, CAP_BREAK = 50, 84, 56
LCOL_W = SPINE_X - 78 - LX_TEXT      # 392px trace column
RCOL_W = 1486 - RX_TEXT              # 666px route column


def _text(text, x, y, size, color, w, natural=True, max_width=1500):
    polys, y_end = text_to_polylines(text, *_s(x, y), size, max_width=max_width)
    out = []
    if natural:
        for pts, ws in naturalize(polys, base_w=w):
            out.append({"pts": pts, "_w": ws, "c": color[0], "rgba": color[1]})
    else:
        out = [{"pts": p, "_w": w, "c": color[0], "rgba": color[1]} for p in polys]
    # width of the drawn text in px (scene units are 1:1)
    tw = 0.0
    for item in out:
        for pt in item["pts"]:
            tw = max(tw, pt[0] - _s(x, y)[0])
    return out, tw


def _line(p0, p1, color, w):
    return {"pts": [list(_s(*p0)), list(_s(*p1))], "_w": w, "c": color[0], "rgba": color[1]}


def _tick(x, y, color, w=22):
    return _line((x - 14, y), (x - 4, y), color, w)


def _spine_glyph(state, y):
    cy = y + 13
    x0 = SPINE_X - 66            # glyph band sits just left of the spine seam
    if state == "tick":
        return [{"pts": [list(_s(x0 - 6, cy + 3)), list(_s(x0 + 2, cy + 13)), list(_s(x0 + 20, cy - 13))],
                 "_w": 26, "c": GREEN[0], "rgba": GREEN[1]}]
    if state == "slash":
        return [_line((x0 - 4, cy - 15), (x0 + 20, cy + 15), RED, 26)]
    if state == "open":
        import math
        out = []
        for k in range(8):
            t0 = k * math.pi / 4
            t1 = t0 + math.pi / 6
            out.append({"pts": [list(_s(x0 + 8 + 13 * math.cos(t0), cy + 13 * math.sin(t0))),
                                list(_s(x0 + 8 + 13 * math.cos(t1), cy + 13 * math.sin(t1)))],
                        "_w": 22, "c": BLUE[0], "rgba": BLUE[1]})
        return out
    # "?" — unsure
    q, _ = _text("?", x0 - 2, y - 4, 24, BLUE, 18, natural=False)
    return q


def _disc(n, y):
    import math
    cx, cy, r = LX_DISC, y + 11, 20
    pts = [[*_s(cx + r * math.cos(t * math.pi / 10), cy + r * math.sin(t * math.pi / 10))]
           for t in range(21)]
    out = [{"pts": pts, "_w": 14, "c": BLACK[0], "rgba": BLACK[1]}]
    num, _ = _text(str(n), cx - 7 * len(str(n)), y - 3, 20, BLACK, 14, natural=False)
    return out + num


def _empty_box(x, y, color=BLUE, w=18, bw=76, bh=44):
    return [_line((x, y), (x + bw, y), color, w),
            _line((x + bw, y), (x + bw, y + bh), color, w),
            _line((x + bw, y + bh), (x, y + bh), color, w),
            _line((x, y + bh), (x, y), color, w)]


def validate(result: dict, events: list[dict], answer: str | None) -> dict:
    """Gate the board content. Returns {mode: full|trace|none, trace, route,
    break, gate, reasons}. Every rejection is logged, never silent.

    Show-the-work law (user's standing instruction): on wrong/open pages AND
    correct-but-flawed-method pages the route carries the REAL worked solution
    ending at the answer; the answer-leak guard applies only to pages that are
    correct with a sound method (which get no route anyway)."""
    reasons = []
    guard_ans = (answer if result.get("verdict") == "correct"
                 and not result.get("method_flawed") else None)
    ev_ids = {e["event"] for e in events}
    trace = []
    unsure = 0
    for row in (result.get("trace") or []):
        t = _clean(str(row.get("text", "")))[:CAP_L + 20]
        if len(t) > CAP_L:
            reasons.append(f"trace row over {CAP_L}ch: {t[:30]}"); continue
        if row.get("event") not in ev_ids:
            reasons.append(f"trace row cites unknown event {row.get('event')}"); continue
        if not mathcheck.lexicon_ok(t):
            reasons.append(f"trace row lexicon: {t[:30]}"); continue
        # NO leak_guard here: trace rows transcribe THE WRITER'S ink — quoting what
        # the writer wrote is never a leak. The guard applies to the tutor's own
        # voice only (route / gate / break / disproof).
        if row.get("read") == "unsure":
            unsure += 1
        trace.append(dict(row, text=t))
    if unsure >= 2:
        return {"mode": "none", "reasons": reasons + ["2+ unsure rows — reconstruction too weak to ink"]}
    if not trace:
        return {"mode": "none", "reasons": reasons + ["no valid trace rows"]}

    brk = result.get("break") or {}
    bfact = _clean(str(brk.get("fact", "")))[:CAP_BREAK + 20]
    if bfact and (len(bfact) > CAP_BREAK or not mathcheck.lexicon_ok(bfact)
                  or not mathcheck.leak_guard(bfact, guard_ans)):
        reasons.append(f"break row rejected: {bfact[:30]}")
        bfact = ""
    disproof = _clean(str(brk.get("disproof", "")))
    if disproof and (len(disproof) > CAP_BREAK
                     or not mathcheck.equation_ok(disproof)
                     or not mathcheck.leak_guard(disproof, guard_ans)):
        reasons.append(f"disproof rejected: {disproof[:30]}")
        disproof = ""

    route = result.get("route") or {}
    steps = []
    route_ok = route.get("source") in ("self_evident", "bank")
    if not route_ok and route.get("steps"):
        reasons.append(f"route source {route.get('source')!r} not permitted")
    if route_ok:
        expanded = []
        for st in (route.get("steps") or [])[:8]:
            t = _clean(str(st.get("text", "") if isinstance(st, dict) else st))
            if len(t) > CAP_R and "=" not in t:
                # prose splits safely at spaces; only equations are unsliceable
                while len(t) > CAP_R and len(expanded) < 9:
                    cut = t.rfind(" ", 1, CAP_R + 1)
                    cut = cut if cut > 0 else CAP_R
                    expanded.append(dict(st, text=t[:cut]) if isinstance(st, dict) else t[:cut])
                    t = t[cut:].strip()
                reasons.append("long prose step split")
            expanded.append(dict(st, text=t) if isinstance(st, dict) else t)
        for st in expanded[:10]:
            t = str(st.get("text", "") if isinstance(st, dict) else st)
            if len(t) > CAP_R:
                reasons.append(f"route step over {CAP_R}ch: {t[:30]}"); route_ok = False; break
            if not (mathcheck.lexicon_ok(t) and mathcheck.leak_guard(t, guard_ans)
                    and mathcheck.equation_ok(t)):
                reasons.append(f"route step failed gates: {t[:30]}"); route_ok = False; break
            chk = st.get("check") if isinstance(st, dict) else None
            if chk:
                import re as _re
                _lettered = _re.search(r"[a-wyzA-WYZ√]", _re.sub(r"\b(and|or|but|so|is|not|gives|then|if)\b", " ", chk))
                # 'x' is ambiguous (times vs variable); x together with ²/³ is algebra
                if _lettered or (_re.search(r"[xX]", chk) and _re.search(r"[²³]", chk)):
                    reasons.append(f"route check symbolic, skipped: {chk[:40]}")
                elif not mathcheck.check_claim(chk):
                    reasons.append(f"route check false: {chk[:40]}"); route_ok = False; break
            steps.append(t)
    # SHOW-THE-WORK landing gate: on broken pages the route must END at the
    # verified answer; a route that lands elsewhere is wrong and never inks.
    if route_ok and steps and answer and guard_ans is None:
        landed = any(mathcheck.lands_at(s, answer) for s in steps[-2:])
        if not landed:
            reasons.append(f"route does not land at the verified answer ({answer[:20]})")
            route_ok = False
            steps = []
    gate = _clean(str(route.get("gate", "")))
    if len(gate) > CAP_R:
        # over-cap is REJECTED, never truncated — a sliced equation can turn
        # a true claim into an inkable falsehood (or a false gate failure)
        reasons.append(f"gate over {CAP_R}ch, dropped: {gate[:30]}")
        gate = ""
    if gate and not (mathcheck.equation_ok(gate) and mathcheck.leak_guard(gate, guard_ans)):
        reasons.append(f"gate rejected: {gate[:30]}")
        gate = ""
    mode = "full" if (route_ok and steps) else "trace"
    return {"mode": mode, "trace": trace, "steps": steps, "gate": gate,
            "break_fact": bfact, "disproof": disproof, "reasons": reasons}


def height(v: dict, compact: bool = False) -> int:
    if v["mode"] == "none":
        return 0
    p = 58 if compact else PITCH
    sz = 30 if compact else SIZE
    n_l = sum(measure(str(r.get("text", "")), sz, LCOL_W)[1] for r in v["trace"])
    n_r = ((sum(measure(s, sz, RCOL_W)[1] for s in (v.get("steps") or []))
            + (1 if v.get("gate") else 0)) if v["mode"] == "full" else 0)
    rows = max(n_l, n_r)
    msg = ((v.get("break_fact") or "") + "  " + (v.get("disproof") or "")).strip()
    extra = measure(msg, sz, 1160)[1] if msg else 0
    return 40 + p + rows * p + extra * p + 16


def render(v: dict, fold_y: float, compact: bool = False) -> tuple[list[dict], dict]:
    """Draw the board with its fold rule; content already validated.
    compact squeezes size/pitch ~20% so full worked solutions fit dense pages."""
    meta: dict = {"terminal_box": None}
    if v["mode"] == "none":
        return [], meta
    SZ = 30 if compact else SIZE
    P = 58 if compact else PITCH
    out = [ _line((BX, fold_y), (1490, fold_y), BLUE, 14) ]
    by = fold_y + 40
    t, _ = _text("you did", 246, by, SZ, BLUE, 22); out += t
    out.append(_tick(246, by + SZ * 0.6, BLUE))
    if v["mode"] == "full":
        t, _ = _text("do this", RX_TEXT, by, SZ, BLUE, 22); out += t
        out.append(_tick(RX_TEXT, by + SZ * 0.6, BLUE))
    y = by + P
    y_r = y
    for i, row in enumerate(v["trace"]):
        w_ord = 22 if row.get("read") != "unsure" else 14
        out += _disc(i + 1, y)
        t, _ = _text(str(row["text"]), LX_TEXT, y, SZ, BLACK, 14,
                     max_width=LCOL_W); out += t
        state = row.get("state") or ("slash" if row.get("read") == "unsure" else "")
        if row.get("read") == "unsure":
            out += _spine_glyph("?", y)
        elif state in ("tick", "slash", "open"):
            out += _spine_glyph(state, y)
        _ = w_ord
        y += P * measure(str(row.get("text", "")), SZ, LCOL_W)[1]
    if v["mode"] == "full":
        for j, step in enumerate(v["steps"]):
            n, _ = _text(f"{j + 1}.", RX_ORD, y_r, SZ, BLUE, 18, natural=False); out += n
            t, tw = _text(step, RX_TEXT, y_r, SZ, BLUE, 16,
                          max_width=RCOL_W); out += t
            y_r += P * measure(step, SZ, RCOL_W)[1]
        if v.get("gate"):
            out.append(_line((RX_ORD + 8, y_r - 2), (RX_ORD, y_r - 2), BLUE, 16))
            out.append(_line((RX_ORD, y_r - 2), (RX_ORD, y_r + 34), BLUE, 16))
            out.append(_line((RX_ORD, y_r + 34), (RX_ORD + 8, y_r + 34), BLUE, 16))
            t, _ = _text(v["gate"], RX_TEXT, y_r, 22 if not compact else 18, BLUE, 14); out += t
            y_r += P
    yb = max(y, y_r) + 4
    if v.get("break_fact") or v.get("disproof"):
        out.append(_tick(BX, yb + SZ * 0.6, RED))
        t, tw = _text("break", BX, yb, SZ, RED, 22); out += t
        # both parts individually capped at validation — never slice ink
        msg = v.get("break_fact") or ""
        if v.get("disproof"):
            msg = (msg + "  " + v["disproof"]).strip()
        t, _ = _text(msg, BX + tw + 26, yb, SZ, RED, 14,
                     max_width=1486 - (BX + tw + 26)); out += t
    return out, meta
