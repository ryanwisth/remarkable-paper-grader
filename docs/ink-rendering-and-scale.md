# Ink rendering, coordinate spaces and scale calibration

Everything the grader writes is a real ink stroke inside the tablet's own notebook
format. Getting those strokes to land exactly where the reviewer meant them took
three separate fixes. This page records the coordinate model that came out of them.

Device: reMarkable Paper Pro, firmware 3.27.3. Screen and canonical page image:
1620 x 2160 px.

## Three spaces

| Space | Origin | Used by |
|---|---|---|
| Canonical page pixels | top-left of a 1620 x 2160 image | every image the reviewer sees, ClearSpace placement, bounding boxes in the ledger |
| Scene units | x centered on the page, y from the top | the strokes the bridge injects (`aiLines` / `aiItems`) |
| PDF points | the background PDF page | imported workbooks and worksheets |

Canonical pixels and scene units differ only by a fixed offset, measured on the device
and stored in `calibration.json`:

```
scene_x = px_x - 809.5      px_x = scene_x + 809.5
scene_y = px_y + 0.5        px_y = scene_y - 0.5
```

Scale is exactly 1:1 on both axes. `rmreview.canon_to_scene()` is the only
pixel-to-scene transform in the code.

## Ink lives in page space at 3.128 px per point

The tablet stores ink in page space, at 1620 / 517.9 = 3.128 ink pixels per PDF
point (`rmrender.DEVICE_PX_PER_PT`). That equals screen space only when the
background PDF is screen-shaped (8.5 x 11.33 in, aspect 0.75). A US Letter PDF is
not, so its ink must be rescaled by `k = zoom / DEVICE_PX_PER_PT`, where `zoom` is
the factor that fits the PDF page into the 1620 x 2160 render.

Before this was fixed, ink on Letter-size worksheets rendered about 18% too large:
work was pushed past the page edge and filled answer bubbles landed about 150 px
below the answer strip. The reviewer then saw a page that looked unfinished and gave
confident wrong verdicts.

Three rules came out of that bug:

1. **One source of truth.** `rmrender.page_transform(pdf, page)` returns `(k, cx, top)`
   for a page. The renderer, the stroke-event extractor (`strokes.py`), the bounding
   boxes cited to the reviewer, and the write-back path (`rmreview.INK_TF`,
   `_to_ink()`) all use it. Fixing only the render would have silently broken the
   write-back.
2. **Proportional versus constant error.** A proportional offset is a scale bug; a
   constant offset is a translation bug. Check that ratio first when ink lands in the
   wrong place.
3. **Never hide clipping.** PIL drops ink outside the canvas without complaint, so
   every render records how many strokes fell off the page (`rmrender.LAST_CLIP`).
   When that number is non-zero the prompt gets a `PARTIAL PAGE` caution that forbids
   "stopped early" or "never committed" verdicts.

A corollary for anyone generating worksheets: never rebuild a PDF at a different page
size while someone is writing in it. Ink is anchored in page space, so changing the
page height misaligns every page already written.

## Zoom-proof capture

All ink is drawn in scene coordinates, which the view cannot distort. The risk was in
capture: which pixel space each page image lived in, and which transform interpreted
the reviewer's coordinates.

An earlier version probed a live view transform at apply time. That is wrong in two
cases: the user zooms between capture and apply, or a file-pulled render (always
canonical) is mapped through a zoomed live view.

The fix normalizes every capture to canonical page space before the reviewer or the
placement code sees it:

- **File-pull renders** (the `.rm` stroke file drawn over the PDF page by
  `rmrender.py`) are canonical by construction.
- **Screenshot fallbacks** (used only for pages not yet flushed to disk) read the view
  rectangle at capture time (`view_rect()`, bridge verb `aiView`). If the view is
  zoomed or panned, `normalize_shot()` warps the screenshot into canonical space.
  Areas that were off-screen or under the tablet UI become mid-grey (128): dark
  enough that placement treats them as occupied, light enough that blank-page
  detection ignores them. The prompt gets a note forbidding marks or claims inside
  grey areas.

Synthetic test (a real captured page, with the framebuffer simulated from the exact
view rectangle the warp would read):

| View state | mean dx | mean dy | max abs |
|---|---|---|---|
| zoom in 1.5x, panned | +0.28 px | 0.00 px | 2.0 px |
| zoom in 2.0x, corner | 0.00 px | 0.00 px | 0.0 px |
| zoom out 0.75x | +0.31 px | -0.15 px | 2.6 px |

A real pinch-zoom capture on the device has not been run yet.

## What the firmware actually honors

Each injected point is `[x, y, speed, width, direction, pressure]`, byte-identical to
the on-disk v6 point. Stored width is px x 4 and pressure is a fraction x 255 (per
`rmscene`). A grid of test strokes on the device, read back from the framebuffer,
established which fields do anything on screen:

| Tool (id) | per-point width | pressure | speed | direction |
|---|---|---|---|---|
| 19 solid pen | honored | no effect | no effect | no effect |
| 15 ballpoint 2 | honored | slight | minor | no effect |
| 14 pencil 2 | honored | strong (dust to solid) | subtle | no effect |
| 13 mechanical pencil 2 | honored | strong | not tested | no effect |
| 12 paintbrush 2 | honored | strong | live (high speed = dry sputter) | no effect |
| 16 marker 2 | honored | mild | no effect | no effect |
| 21 calligraphy | honored | mild | no effect | no effect (width follows travel direction) |
| 23 shader | honored | mild | not tested | no effect |
| 17 fineliner 2 | honored | no effect | no effect | no effect |
| 18 highlighter 2 | translucent band drawn under existing ink | n/a | n/a | n/a |

Stroke-level `thickness`, `maskScale` and `rgba` have no visible effect. The color
enum alone decides the pigment.

Practical rules:

- **Per-point width is the one universal channel.** All texture in the tutor hand is
  carried by it.
- **Always set the tool explicitly.** When it is omitted the loader defaults to 23
  (shader), not the solid pen.
- **Never send width 0.** Clamp stored width to at least 4 (1 px).
- **Never inject tool ids 6, 8, 9, 10 or 11.** They map to eraser and selection
  behavior.
- **Bounds ignore stroke width.** Inflate item bounds by half the maximum width, or
  wide strokes (the 44-wide highlighter band) risk clipped redraws.

## Color

Framebuffer sampling showed the palette renders vivid: red (enum 7) about
(254, 32, 32), blue (enum 6) about (50, 26, 231), green (enum 10) pure lime, not the
pale export color in the scene API's table. The grader's color rules:

- **Blue** carries the lettering voice: notes, worked steps, the fold rule.
- **Red** letters only in the one-line "break" row; otherwise it strikes and circles.
- **Green** never letters (it is the faintest of the three at note size); it is used for
  ticks and the translucent highlight band.
