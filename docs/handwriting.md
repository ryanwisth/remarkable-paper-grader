# The tutor's handwriting: a single-stroke font made to look written

Feedback is drawn as pen strokes, so the grader needs a "hand": a way to turn text
into polylines with per-point width. The goal was ink that reads as a different person
marking the page, legible first and natural second.

## Requirements that shaped the choice

- **Legibility beats beauty.** The content is math-dense: digits, `= % / ( )`, short
  notes, sometimes a full worked solution.
- **Single-stroke glyphs.** The device draws centerlines with a width; it cannot fill
  outlines. Plotter and engraving fonts are the natural source.
- **Never imitate the student.** The tutor's ink sits on top of the student's work and
  must read as someone else's. Letterforms come from other writers, one consistent
  writer per document.
- **Runs locally, deterministic.** The same note should render the same way every
  time it is drawn.

## What ships: EMS Casual Hand plus naturalization

`ems.py` parses the SVG font `EMSCasualHand.svg` (1000 units per em, single-stroke
paths; SIL Open Font License) and lays out text as polylines in scene units.
`ems.naturalize()` then turns those clean skeletons into pen-like strokes. The
pipeline, in order:

1. **Chaikin smoothing** (one pass) rounds polyline corners the way a pen turns.
2. **Arc-length resampling** at 3 units gives the width profile room to act.
3. **Touchdown ramp and lift taper**: width starts at about 55%, ends at about 35%.
4. **Exit flick**: the last segment extends a little and tapers out.
5. **Curvature-derived dwell width**: a bounded two-thirds power-law speed estimate
   adds width where a real pen slows (the belly of a 3 or 5, the bottom of a u).
   It is additive only, so straight strokes are unchanged and legibility cannot drop.
   `curve_gain=0.70` is the calibrated setting.
6. **Downstroke shading**: verticals and descending diagonals run a little heavier
   than return strokes.
7. **Persistent slant as shear, plus AR(1) drift**: a run of letters leans and sizes
   together and then drifts, instead of each letter being rotated independently.
8. **Mid-stroke lifts** and small width variation.

All of this travels through the one channel the firmware honors, per-point width
(see [ink-rendering-and-scale.md](ink-rendering-and-scale.md)).

**Stable seeding.** Randomness is seeded from `zlib.crc32` of the text. An earlier
version used Python's `hash()`, which is salted per process, so the "same" note came
out in a different hand on every run. A second bug restarted every note block from
the same default seed, which gave identical-twin strokes. Both were fixed.

**Adding more wobble does not help.** Past the current amplitude, geometric noise
makes the hand look shaky, not human. The remaining realism came from structure
(dwell, shear, drift), not more noise.

## Optional: real human glyphs and digit variants

EMS Casual Hand has no `√ ≤ ≥ → ≠`. The original deployment filled those with real
pen strokes from Google's MathWriting-2024 dataset and added alternate digit forms
("allographs"), so repeated digits differ in shape rather than only in noise. The
scripts that build that bank are in `tools/`.

MathWriting is licensed CC BY-NC-SA 4.0, so the built `human_glyphs.json` is **not
included here**. `ems.py` loads it add-only when present (a font glyph is never
overridden). Without it, `rmreview.py` writes those symbols in ASCII (`>=`, `sqrt`)
instead of drawing blank space.

Lessons from building the digit bank:

- **Use chamfer distance, not pixel overlap,** to judge whether two hand-drawn digits
  are the same shape.
- **Filter for safety first, then pick for diversity inside the safe set.** Maximizing
  diversity across the whole pool selects the odd tail (7s that read as F, 9s as g).
- **One alternate per note per digit.** A fresh variant on every occurrence reads as
  several people sharing one page.

## Options tried and rejected

| Option | Verdict | Reason |
|---|---|---|
| Hershey script faces (cursive) | rejected | illegible at note size |
| Hershey Futura (`futural.jhf`, via `hershey.py`) | kept as fallback, unused | clean but mechanical next to EMS |
| Neural handwriting synthesis (Graves-style RNNs) | rejected | trained on prose datasets with almost no `=`, `%` or digits; no usable license |
| Diffusion stroke synthesis (DSD port, prototyped) | parked | its own samples traded away legibility |
| Cursive joins between EMS glyphs | rejected | only 3.8% of letter pairs are geometrically joinable; joins drift the baseline |
| Stroke-order (ductus) variation | rejected | EMS glyphs are mostly single-stroke; reordering the rest changes no pixels |
| Variable-width plotter fonts | none exist | plotter fonts target pen-up/pen-down devices; synthesized width is the only source |
| Stacked fractions | rejected | decimal points vanish at note size |

## Attribution

- **EMS Casual Hand**: created by Sheldon B. Michaels, SVG font conversion by
  Windell H. Oskay, a derivative of Covered By Your Grace by Kimberly Geswein.
  SIL Open Font License 1.1; full text in `EMSCasualHand-FONT-NOTICE.txt`.
- **Hershey fonts** (`futural.jhf`): designed by Dr. A. V. Hershey; public domain.
- **MathWriting-2024** (not included): Google LLC, CC BY-NC-SA 4.0.
