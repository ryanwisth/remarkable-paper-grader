# tools/: building the optional human glyph bank

These scripts are offline and never touch the tablet. They build `human_glyphs.json`,
an optional add-on for the tutor hand, from Google's MathWriting-2024 dataset.
`ink_digits.py` and `ink_select.py` are run-once scripts that work in the current
directory (`digit_bank.json`, `digit_bank_final.json`).

## Harvesting real human glyphs (MathWriting → human_glyphs.json)

Adds glyphs EMS Casual Hand lacks, using real pen strokes instead of drawing them.
Ran once on 2026-08-26 to add `√ ≤ ≥ → ≠`. Re-run to add more.

### 1. Get the data (one time)

The full archive is 2.95 GB, but `symbols/` is the FIRST member in the tar, so
stream it and stop early — that costs ~40 MB, not 2.95 GB:

```bash
curl -sL https://storage.googleapis.com/mathwriting_data/mathwriting-2024.tgz \
  | tar xz -C <dir> 'mathwriting-2024/symbols/*'
```

Kill it once `<dir>/mathwriting-2024/symbols` stops growing (6,423 files).
Point the tools at it: `export MATHWRITING_SYMBOLS=<dir>/mathwriting-2024/symbols`
(default `~/data/mathwriting-2024/symbols`).

**License: CC BY-NC-SA 4.0 (Google LLC).** The dataset is non-commercial and
ShareAlike, so the derived `human_glyphs.json` is NOT shipped in this repository
and must not be merged into `EMSCasualHand.svg` (that font is SIL OFL and has to
stay unmixed with NC data). `ems.py` loads the two separately for this reason,
and runs fine without the bank: missing glyphs fall back to ASCII spellings.

### 2. Curate by eye — do not skip this

```bash
python tools/ink_harvest.py '\sqrt' /tmp/sheet-sqrt.png
```

Renders every instance as a numbered contact sheet. The automatic screen in
`quality()` is deliberately lenient and passed 100% of all five classes, so it
catches only garbage — the real selection is visual. Look for a clean shape with
proportions close to the shipped font's equivalent operator.

### 3. Pick, then check the aspect ratio

Put the chosen indices in `PICKS` in `ink_extract.py`. Match aspect to the font:
`<` is 419x463 (0.90), `>` is 387x513 (0.75), `=` is 381x255 (1.49). Two glyphs
that pair on the page (`≤`/`≥`) must be given the SAME aspect or they render
visibly asymmetric — that is why `\le[14]` and `\ge[9]` were chosen, both 0.72.

### 4. Extract

```bash
python tools/ink_extract.py     # writes ../human_glyphs.json
```

Advances are derived from real ink width + side bearing. The one exception is
`√`, whose advance is forced to 300 so its overbar OVERHANGS the next character
by ~470 units — a bit more than a digit's 403 advance, so `√7` puts the bar over
the 7. That only covers ONE character; the reviewer prompt says so.

### 5. Wire up

`ems.py:_load()` merges the bank add-only (an OFL glyph is never overridden).
If a glyph becomes renderable, also fix `rmreview.py`:
- `_NO_GLYPH` — flip its ASCII spelling from a downgrade into an upgrade
- the `ROUTE LAW` prompt — tell the model it may now write the character

## Allograph variants (digits) — added 2026-08-26

`ink_digits.py` normalizes every human instance of a digit into the EMS digit's
box (deslant by image moments, fit height, **clamp width to the EMS ink width** —
variants keep the EMS advance, so a wider one collides with the next character).
`ink_select.py` then screens and selects.

Three things that took a few tries and are worth not rediscovering:

1. **Use chamfer distance, not IoU.** Two people's identically-shaped `4` barely
   overlap pixel-wise. An IoU>=0.42 filter rejected nearly every candidate; the
   same threshold on chamfer keeps the right ones.
2. **Safety filter first, diversity second, and diversify only INSIDE the safe
   set.** Maximising diversity across the whole pool selects the weird tail —
   crossed European 7s that read as `F`, 9s that read as `g`, 6s that read as `b`.
3. **Hand-curate the final set anyway.** The automated pass still let a `9`
   through that rendered as `]`. Render a contact sheet and look. Digit `1` was
   dropped entirely: every human `1` is a bare vertical, so there is no variety to
   gain and it reads as `l`/`|`.

Selection policy lives in `ems.text_to_polylines`: ONE alternate per note per
digit, used ~half the time against the house form. Do NOT draw a fresh variant per
occurrence — that samples a different writer each time and reads as several people
sharing a page. Real variation is one person re-forming their own letter.
