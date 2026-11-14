# tablet/: the device side

Requires a reMarkable Paper Pro in **developer mode** with SSH access. Everything here
was built and tested on firmware **3.27.3** only.

| File | What |
|---|---|
| `gradebridge.qmd` | The bridge: a qmldiff patch that adds the Grade button to the toolbar and answers the daemon's broker verbs (`aiPing`, `aiPoll`, `aiInfo`, `aiStatus`, `aiLines`, `aiItems`, `aiView`, `aiGoto`, `aiPage`, `aiNext`, `aiFwd`, ...). It has no delete or erase verb, by design. |
| `ai-test.qmd` | The first spike: proves feedback ink can go into its own layer. |
| `SPIKE-CHECKLIST.md` | The five-check protocol that showed the AI layer can be hidden and deleted without touching the user's strokes. |
| `deploy.sh` | Stages the extension stack on a dev-mode tablet: installs an SSH key, backs up the notebook store first, pushes the stack, rebuilds the hashtable. It does not start anything and never touches systemd. |

## Third-party pieces (not included)

The bridge runs on an open-source extension stack. Get these from their authors and
place the built files in `tablet/dist/` (ignored by git) before running `deploy.sh`:

- XOVI loader and the extension collection (message broker, framebuffer spy, rm-shot):
  https://github.com/asivery/rm-xovi-extensions
- qmldiff, used to hash `.qmd` patches against the firmware's QML hashtable:
  https://github.com/asivery/qmldiff
- xovi-scene-assistant, the stroke and layer API the bridge draws through:
  https://github.com/ingatellent/xovi-scene-assistant
- Optional: xovi-tripletap, to start XOVI with a triple power-button press:
  https://github.com/rmitchellscott/xovi-tripletap

`deploy.sh` expects `dist/xovi-aarch64.tar.gz`, `dist/rm-shot-aarch64.so` and
`dist/scene-assistant-aarch64.so`. The scene-assistant build and the hashtable are
tied to the firmware version.

## Installing the bridge

`qmldiff hash-diffs` rewrites its input in place, so hash a copy:

```bash
cp gradebridge.qmd gradebridge-hashed.qmd
qmldiff hash-diffs dist/hashtab-<firmware> gradebridge-hashed.qmd
scp gradebridge-hashed.qmd $RM_HOST:/home/root/xovi/exthome/qt-resource-rebuilder/
ssh $RM_HOST '/home/root/xovi/start'
ssh $RM_HOST 'journalctl -u xochitl -n 200 | grep qmldiff'
```

qmldiff fails silently. The journal must show
`Processing file /qt/qml/xofm/libs/toolbar/qml/Toolbar.qml`, or the patch did not
apply.

## Cautions

- **Never autostart XOVI** through systemd. On an encrypted Paper Pro this can
  bootloop the device into manual recovery. A reboot always returns to stock.
- A firmware update invalidates the hashtable and the scene-assistant build.
- Only one client may use the broker pipes at a time.
