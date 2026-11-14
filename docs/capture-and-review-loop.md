# The capture and review loop

This page follows one press of the Grade button from the tablet to ink on the page.

## Pieces

| Where | Piece | Role |
|---|---|---|
| Tablet | `tablet/gradebridge.qmd` | QML patch, applied by qmldiff through XOVI, that adds the Grade button to the toolbar and answers the daemon's verbs |
| Tablet | XOVI message broker | two shared FIFOs (`/run/xovi-mb`, `/run/xovi-mb-out`) the daemon writes verbs into over SSH |
| Tablet | scene-assistant extension | the stroke and layer API the bridge draws through |
| Computer | `rmreview.py` | the daemon: poll, capture, review, place, write back |
| Computer | `rmrender.py`, `strokes.py` | render a page from its stroke file; extract ordered ink events |
| Computer | `board.py`, `mathcheck.py`, `ems.py` | lay out feedback, check every claim, turn text into strokes |
| Computer | `readback.py` | read the student's pen ticks in the feedback boxes; fingerprint student ink |

## Step by step

1. **Press.** The Grade button's menu offers "Grade page" and batch options. A press
   sets a request string in the bridge (`one <timestamp>`, `n:<count>`, `all`).
2. **Poll.** The daemon polls the bridge with `aiPoll`. Only a new request starts a
   pass. A press younger than 90 seconds at daemon startup is honored rather than
   swallowed.
3. **Snapshot.** A full copy of the notebook store is taken on the device before any
   ink is written.
4. **Capture.** The daemon finds the open notebook and page, pulls the page's `.rm`
   stroke file and the background PDF over `scp`, and renders a canonical
   1620 x 2160 image with `rmrender.py`. Pages not yet flushed to disk fall back to an
   on-device screenshot, normalized to canonical space (see
   [ink-rendering-and-scale.md](ink-rendering-and-scale.md)).
5. **Stroke order.** The v6 `.rm` format has no timestamps, but file order is creation
   order. `strokes.py` clusters strokes into ordered "ink events" (a new event when
   the pen jumps more than 45 px down or 110 px across) and the list goes into the
   prompt. On one early page this showed the student committed an answer first and
   wrote the check afterwards; without the order, every reviewer design had invented
   an arithmetic story instead.
6. **Skip guards.** A page with no student strokes is skipped. A page whose student
   ink fingerprint (`readback.guard_sha`) matches the last graded version is skipped,
   because the bridge cannot erase and double ink would be permanent. The fingerprint
   covers only the student's strokes, not the file, since the tutor's own ink changes
   the file.
7. **Review.** The prompt asks for strict JSON: verdict, a headline of 24 characters
   or fewer, an ordered trace of what the student did, the first break, a worked route,
   a footer instruction and question, and at most four annotations anchored to the
   student's ink in pixel coordinates. Review is tiered: a cheaper model handles clean,
   correct pages and must reply `{"escalate": true}` on any doubt, which sends the page
   to the full model. Batches capture all pages first, review up to six in parallel,
   then apply.
8. **Check.** `mathcheck.py` gates every inked claim. Any `a = b` in a route step, check
   or disproof is evaluated and dropped if false. A banned-word filter removes
   statements about motives or feelings. A leak guard keeps the answer off correct
   pages. Over-length text is rejected, never truncated.
9. **Place.** `ClearSpace` builds an ink mask of the page (anything darker than 220
   counts as occupied, so printed furniture is protected) and finds free space for
   each note: home position, then shifted, then elsewhere on the page, then a
   continuation page. Skipping a mark is preferred to writing over the student.
10. **Lay out.** Below the student's last stroke a blue fold rule opens "the board":
    a "you did" column replaying the trace, a spine of tick, slash and open marks, a
    "do this" column with the worked steps, and a red break row. It degrades in steps
    (full board, trace only, no board) when space or validation runs out; the verdict,
    headline and footer always survive.
11. **Write back.** Text becomes strokes via `ems.py`, coordinates go through
    `page_transform` to page space, and batches of at most 1,900 lines go to the
    bridge's `aiLines` and `aiItems` verbs. The bridge adds them to a layer named
    "AI Feedback". Pages are changed with absolute `aiGoto` calls and read back with
    `aiPage`.
12. **Log.** Each page appends a row to `work/ledger.jsonl`, including the rectangles
    of the tick boxes, so the next pass can read the student's answers to the footer
    question.

## Safety design

The constraint behind every choice: the student's handwriting is never touched,
overprinted or deleted.

- **No pen injection.** Simulated pen events could end a stroke the user is drawing.
  All ink goes through the scene API into the AI Feedback layer.
- **No delete or erase verbs** exist in the bridge. Removing feedback is a manual
  layer delete by the user; the spike checklist in `tablet/SPIKE-CHECKLIST.md`
  verified that deleting the layer leaves the student's strokes intact.
- **One writer.** The broker pipes are shared FIFOs; two clients cross-deliver
  replies. The daemon holds an `fcntl` lock so only one instance runs.
- **Tethered extension stack.** XOVI is started by hand and never autostarted; a
  reboot returns the tablet to stock.
- **Queue-committed scene changes.** Layer and stroke mutations land only after the
  QML handler returns, so the bridge is a timer-driven state machine, one mutation per
  event-loop turn. The page has a five-layer cap; adding a layer at the cap fails
  closed.

## Device lessons

- QML `XMLHttpRequest` file writes fail silently on firmware 3.27.3 (reads work). Do
  not build a transport on them.
- qmldiff failures are silent. After starting XOVI, confirm the journal shows
  `Processing file .../Toolbar.qml`.
- Relative page flips double-step on a stale property; use absolute flips with a
  read-back. Flipping past the end of a PDF notebook is a no-op.
- Do not pipe large payloads through an SSH heredoc; BusyBox mangles them. Copy a file.
