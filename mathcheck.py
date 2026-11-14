"""Mac-side validation gates for board ink.

Everything the tutor letters onto paper passes through here first:
  * safe_eval     — arithmetic-only expression evaluator (no names, no calls)
  * check_claim   — a route step's `check` expression must evaluate True
  * equation_ok   — any literal `a = b` inside inked text must actually hold
  * lexicon_ok    — the banned-mind lexicon (motive/feeling words) as a regex,
                    enforced mechanically, not by prompt discipline
  * leak_guard    — the verified answer (bank handshake) may never be inked;
                    numerals within tolerance of it are a leak
"""
from __future__ import annotations

import ast
import operator
import re

_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Pow: operator.pow, ast.USub: operator.neg,
    ast.UAdd: operator.pos, ast.Mod: operator.mod, ast.FloorDiv: operator.floordiv,
}
_CMP = {
    ast.Eq: lambda a, b: abs(a - b) <= max(1e-9, abs(b) * 0.005, 0.0005),
    ast.NotEq: lambda a, b: abs(a - b) > max(1e-9, abs(b) * 0.005, 0.0005),
    ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
}


def _ev(node):
    if isinstance(node, ast.Expression):
        return _ev(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_ev(node.left), _ev(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_ev(node.operand))
    if isinstance(node, ast.Compare):
        left = _ev(node.left)
        for op, comp in zip(node.ops, node.comparators):
            if type(op) not in _CMP:
                raise ValueError("op")
            right = _ev(comp)
            if not _CMP[type(op)](left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.BoolOp):
        vals = [_ev(v) for v in node.values]
        return all(vals) if isinstance(node.op, ast.And) else any(vals)
    raise ValueError(f"disallowed node {type(node).__name__}")


_UNI = [("\u2212", "-"), ("\u2013", "-"), ("\u00d7", "x"), ("\u00b7", "*"),
        ("\u2044", "/"), ("\u221a", "sqrt")]


def _norm_uni(s: str) -> str:
    for a, b in _UNI:
        s = s.replace(a, b)
    return s


def safe_eval(expr: str):
    """Evaluate a pure-arithmetic expression; raises on anything else."""
    return _ev(ast.parse(expr.strip(), mode="eval"))


def check_claim(expr: str) -> bool:
    """A route/trace `check` must be a True arithmetic statement. Math-style
    input is normalized: 'x' = times, single '=' = equality, 'n%' = n/100."""
    e = re.sub(r"\b(but|then|so)\b", " and ", _norm_uni(expr))
    e = re.sub(r"(\d+(?:\.\d+)?)\s*%", r"(\1/100)", e)
    e = e.replace("²", "**2").replace("³", "**3")
    e = e.replace("≤", "<=").replace("≥", ">=").replace("≠", "!=")
    e = e.replace(";", " and ")
    e = e.replace("^", "**").replace("x", "*").replace("X", "*")
    e = re.sub(r"(?<![<>=!])=(?!=)", "==", e)
    # drop trailing commentary after a comma ("= 60, not 100" keeps the claim)
    e = e.split(",")[0]
    try:
        return bool(safe_eval(e))
    except (ValueError, SyntaxError, ZeroDivisionError, OverflowError):
        return False


_PCT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_ARITH = re.compile(r"[\d.()+\-x*^ /%]+")


def _side_value(s: str) -> float | None:
    """Leading arithmetic run of s; % = /100, x = times, ^ = power."""
    m = _ARITH.match(s.strip())
    if not m:
        return None
    expr = m.group(0).rstrip(" +-x*/(.,^")
    expr = _PCT.sub(r"(\1/100)", expr).replace("^", "**").replace("x", "*")
    if not re.search(r"\d", expr):
        return None
    try:
        v = safe_eval(expr)
    except (ValueError, SyntaxError, ZeroDivisionError, OverflowError):
        return None
    return None if isinstance(v, bool) else float(v)


def equation_ok(text: str) -> bool:
    """Every literal numeric `a = b` in inked text must hold ('x' = times,
    'n%' = n/100, commentary after a comma ignored). Words around '=' are
    skipped. A false numeric equation kills the line."""
    text = _norm_uni(text)
    for m in re.finditer(r"=", text):
        left = text[max(0, m.start() - 48):m.start()]
        right = text[m.end():m.end() + 48]
        lm = re.search(r"[\d.()+\-x*^ /%]+$", left)
        if not lm:
            continue
        # a numeric run bordered by a letter/²/√/) is a fragment of a VARIABLE
        # expression ("(u+v)^2", "8√7") — not a checkable numeric claim
        if lm.start() > 0 and (left[lm.start() - 1].isalpha()
                               or left[lm.start() - 1] in ")²√"):
            continue
        rm_ = _ARITH.match(right.strip())
        if rm_:
            tail_at = len(right) - len(right.strip()) + rm_.end()
            if tail_at < len(right) and (right[tail_at].isalpha()
                                         or right[tail_at] in "²√("):
                continue
        lv = _side_value(lm.group(0))
        rv = _side_value(right)
        if lv is None or rv is None:
            continue
        # a% = b: humans write "1/25 = 4%" — normalize the percent-free side
        tol = max(0.02 if abs(rv) >= 1 else 0.0012, abs(rv) * 0.025)
        if abs(lv - rv) > tol:
            if not (abs(lv - rv / 100.0) <= max(0.0006, abs(rv / 100.0) * 0.005)
                    or abs(lv / 100.0 - rv) <= max(0.0006, abs(rv) * 0.005)):
                return False
    return True


# Unconditional bans: tutor-voice motive/feeling/consequence words.
_BANNED_HARD = re.compile(
    r"\b(rush(ed|ing)?|panick?(ed|ing)?|careless(ly)?|lazy|sloppy|"
    r"obviously|clearly|you (thought|assumed|meant|wanted|tried to|forgot)|"
    r"distract(ed|ion)?|anxi(ous|ety)|test day)\b", re.IGNORECASE)
# Context-gated: these words are legitimate CONTENT on reading pages ("aptitude
# scores", a trap map saying a choice "feels relevant") — they are banned only
# when aimed at the student (near you/your).
_MINDY = r"(scor(e|ing)|points?|attention|focus(ed)?|confiden(t|ce)|feel(ing)?s?|felt|forgot)"
_BANNED_AT_YOU = re.compile(
    r"\b(you|your)\b[^.;]{0,28}\b" + _MINDY + r"\b"
    r"|\b" + _MINDY + r"\b[^.;]{0,28}\b(you|your)\b", re.IGNORECASE)


def lexicon_ok(text: str) -> bool:
    return not (_BANNED_HARD.search(text) or _BANNED_AT_YOU.search(text))


_NUM = re.compile(r"\d+(?:\.\d+)?")


def _answer_values(answer: str) -> list[float]:
    return [float(v) for v in _NUM.findall(answer or "")]


def leak_guard(text: str, answer: str | None) -> bool:
    """False if the text hands over the verified answer. Two detectors:
    (1) the answer EXPRESSION, whitespace-normalized, as a substring
        ("6√5", "k = -6", "79.2%");
    (2) numerals within 0.6% of the answer's numerals — but only for values
        that are distinctive (abs >= 15, or non-integer). Small integers like
        5 or 6 appear in ordinary arithmetic everywhere; matching them made
        the guard reject honest transcriptions wholesale.
    NOTE: trace rows (transcriptions of the student's own ink) are exempt by
    the caller — quoting what the writer wrote is never a leak."""
    if not answer:
        return True
    text = _norm_uni(text)
    ans = _norm_uni(answer).strip()
    if re.fullmatch(r"[A-E]", ans.split()[0] if ans else ""):
        letter = ans.split()[0]
        if re.search(rf"(?:^|[^A-Za-z]){letter}(?:[^A-Za-z]|$)", text) and \
           re.search(r"answer|pick|choose|it is|= *" + letter, text, re.IGNORECASE):
            return False
        return True
    ans_expr = re.sub(r"\s+", "", ans)
    ans_core = ans_expr.lstrip("kxyn=").lstrip("=")     # "k = 6√5" -> "6√5"
    hay = re.sub(r"\s+", "", text)
    for needle in {ans_expr, ans_core}:
        if len(needle) >= 2 and not needle.isalpha() and needle in hay:
            return False
    for t in _answer_values(ans):
        if abs(t) < 15 and float(t).is_integer():
            continue
        for m in _NUM.finditer(text):
            v = float(m.group(0))
            if (t != 0 and abs(v - t) <= abs(t) * 0.006) or (t == 0 and v == 0):
                return False
    return True


def lands_at(text: str, answer: str | None) -> bool:
    """Presence detector (NOT the leak guard): does this line state the
    verified answer? Used to require broken-page routes to END at the answer.
    Errs permissive — an undetectable answer format never blocks a route."""
    if not answer:
        return True
    text = _norm_uni(text)
    ans = _norm_uni(answer).strip()
    hay = re.sub(r"\s+", "", text)
    ans_expr = re.sub(r"\s+", "", ans)
    ans_core = ans_expr.lstrip("kxyn=").lstrip("=")
    for needle in {ans_expr, ans_core}:
        if len(needle) >= 2 and needle in hay:
            return True
    m = re.match(r"([A-E])\b", ans)
    if m and re.search(rf"(?<![A-Za-z]){m.group(1)}(?![A-Za-z])", text):
        return True
    for t in _answer_values(ans):
        if float(t).is_integer() and abs(t) < 1000:
            if re.search(rf"(?<![\d.]){int(t)}(?![\d.])", text):
                return True
        else:
            for mm in _NUM.finditer(text):
                if t != 0 and abs(float(mm.group(0)) - t) <= abs(t) * 0.006:
                    return True
    return False
