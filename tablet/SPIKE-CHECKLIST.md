# Day-1 spike — pass/fail protocol

**The one question:** does `addDrawingLine` into a named layer give AI ink that is
separately hideable and deletable, with the user's own strokes provably untouched?
If yes → build the live path. If no → Remarque-style side panel, document never written.

## Order of operations

1. `./deploy.sh` (stages everything; asks for the root password once)
2. Open a **scratch notebook** (not a real one) and handwrite a few lines. Count the strokes.
3. Start the environment: `ssh root@10.11.99.1 '/home/root/xovi/start'`
4. **Before installing the qmd**: dump the device's real QML and diff the patch targets —
   `Toolbar.qml` (toolbar button) and `DeviceSceneView.qml` (stroke hook, later).
   The shipped draft was written against the sudoku extension's targets (OS 3.20–3.27);
   reMarkable reshuffles the toolbar between releases. Fix the qmd against ground truth first.
5. Install `ai-test.qmd` → `/home/root/xovi/exthome/qt-resource-rebuilder/`, restart env.
6. Open the scratch notebook, press the new toolbar button.

## Pass/fail checks (in order — stop at first failure)

| # | Check | Pass means |
|---|-------|-----------|
| 1 | A line appears at a predictable spot, page doesn't flash horribly | coordinates + render path work |
| 2 | Layers panel shows a layer named `AI-TEST` containing exactly that stroke | layer creation + naming work |
| 3 | **Hiding `AI-TEST` hides only the AI line; the handwriting stays** | separability (soft) |
| 4 | **Deleting `AI-TEST` removes it; handwriting intact — count the strokes** | separability (hard) — THE test |
| 5 | Close notebook, reopen, let it cloud-sync, check web app: still correct everywhere | persistence + sync safety |

## Also harvest while in there (probe answers, from `journalctl | grep AI-SPIKE`)

- `sceneController` key dump → **is there a `setCurrentLayer`-equivalent?**
  (Research found `addLayer`/`currentLayer`/`setLayerName` but no setter. If none:
  one fresh AI layer per grading pass is the fallback — survivable, decided in advance.)
- `currentLayer` before/after `addLayer()` → does addLayer switch to the new layer?
- After the button press, handwrite one more stroke → **which layer does it land on?**
  (If it lands on AI-TEST, the daemon must draw + immediately hand focus back, or
  accept that the user re-selects their layer in the layers panel.)

## Abort/rollback at any point

- Return to stock instantly: `ssh root@10.11.99.1 '/home/root/xovi/stock'` — or just reboot.
- Notebook store backup from before anything started: `tablet/backups/xochitl-<ts>.tar.gz`
- Nothing is installed outside `/home/root`; no systemd units; no autostart.
