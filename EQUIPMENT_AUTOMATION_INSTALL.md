# Equipment Automation Setup

This installs the dependencies for `tools/equipment_ocr_submitter.py`.

## macOS Install

```bash
cd /Users/Stephen.Cassaro/Documents/Playground
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements-equipment-automation.txt
brew install tesseract android-platform-tools
.venv/bin/python -m playwright install chromium
```

## Verify

```bash
cd /Users/Stephen.Cassaro/Documents/Playground
.venv/bin/python -c "import PIL, mss, playwright, pyautogui, pytesseract; print('python deps ok')"
tesseract --version
adb version
.venv/bin/python -m py_compile tools/equipment_ocr_submitter.py
.venv/bin/python -m unittest tools/test_equipment_ocr_submitter.py
```

## Chrome Setup

For MIRPG Optimizer entry with the current config, open Chrome manually, log in to:

```text
https://mirpg-optimizer.netlify.app/equipment
```

Then enable:

```text
Chrome > View > Developer > Allow JavaScript from Apple Events
```

## BlueStacks ADB Check

```bash
adb kill-server
adb start-server
adb connect 127.0.0.1:5555
adb -s 127.0.0.1:5555 exec-out screencap -p >/tmp/mirpg_adb_test.png
```

If `exec-out screencap` fails with `error: closed`, fix/restart the BlueStacks ADB bridge before running OCR capture or ADB movement snapshots.

## Useful Run Commands

Dry-run OCR only:

```bash
.venv/bin/python tools/equipment_ocr_submitter.py --config tools/equipment_ocr_config.example.json --limit 1 --dry-run
```

Test one advance action:

```bash
.venv/bin/python tools/equipment_ocr_submitter.py --config tools/equipment_ocr_config.example.json --advance-only 0
```

Tap a full visible page, scroll once, then tap the next page:

```bash
.venv/bin/python tools/equipment_ocr_submitter.py --config tools/equipment_ocr_config.example.json --tap-page-test 2
```

Detect visible item cards from ADB screenshots, tap those detected centers, scroll once, then detect/tap the next page:

```bash
.venv/bin/python tools/equipment_ocr_submitter.py --config tools/equipment_ocr_config.example.json --tap-detected-page-test 2
```

Detect and tap every visible/new item, scrolling until no unseen cards remain:

```bash
.venv/bin/python tools/equipment_ocr_submitter.py --config tools/equipment_ocr_config.example.json --tap-detected-all-test
```

Add `--movement-snapshots` only when debugging click/scroll behavior; it captures extra ADB screenshots and slows the run down.

With the example config, game-side automation uses ADB for capture, taps, scrolls, and detected-card centers. No hover calibration is needed. If you switch `advance.input_backend` or `advance.detect_items.input_backend` back to `desktop`, the command asks for calibration hovers because it must map ADB screenshot coordinates to Mac screen coordinates.

The detected-page test also fingerprints each visible card. If a scroll lands with overlap, already-clicked cards are skipped only in the first `advance.detect_items.skip_seen_rows_after_scroll` visible rows; matching cards below those rows are still clicked and logged as `seen_match_ignored`. If a scroll shows no new cards, the script will try another scroll up to `advance.detect_items.max_extra_scrolls_per_page`.

The all-items test stops when scrolling no longer reveals unseen cards. `advance.detect_items.max_all_pages` is a safety cap to prevent an accidental infinite run.

The all-items test uses `advance.detect_items.all_items_scroll_actions`, a smaller overlapping crawl scroll, instead of the larger fixed-page scroll. This is slower than a full-page jump but much less likely to miss cards over a long inventory.

Row snapping is available but disabled by default for speed now that ADB taps use exact screenshot centers. Set `advance.detect_items.row_snap_after_scroll` to `true` only if later pages start landing badly enough to need corrective drags.

Analyze a movement snapshot run:

```bash
.venv/bin/python tools/equipment_ocr_submitter.py --config tools/equipment_ocr_config.example.json --analyze-movement-snapshots equipment_ocr_debug/latest/movement_snapshots/<timestamp>
```

Submit one item to MIRPG Optimizer without advancing:

```bash
.venv/bin/python tools/equipment_ocr_submitter.py --config tools/equipment_ocr_config.example.json --limit 1 --no-advance
```

During OCR/submit runs, `skip_duplicate_parsed_equipment` compares the parsed `equipment_name`, slot, and ordered on-equip effects. If overlap brings the same item back, the duplicate is logged to CSV, website submit is skipped, and the script still advances to the next item.

Run multiple items:

```bash
.venv/bin/python tools/equipment_ocr_submitter.py --config tools/equipment_ocr_config.example.json --limit 10
```
