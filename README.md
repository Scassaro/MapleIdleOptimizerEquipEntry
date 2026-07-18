# MIRPG Equipment OCR Runner

Condensed setup, file list, and run commands for `equipment_ocr_submitter.py`.

## Minimum Files

To run the automation on another machine, copy:

```text
equipment_ocr_submitter.py
equipment_ocr_config.example.json
requirements-equipment-automation.txt
README.md
```

Optional for validation:

```text
test_equipment_ocr_submitter.py
```

Do not copy generated files:

```text
.venv/
equipment_ocr_debug/
__pycache__/
*.sqlite
*.png
*.csv
```

## macOS Install

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements-equipment-automation.txt
brew install tesseract android-platform-tools
.venv/bin/python -m playwright install chromium
```

## Verify

```bash
.venv/bin/python -c "import PIL, mss, playwright, pyautogui, pytesseract; print('python deps ok')"
tesseract --version
adb version
.venv/bin/python -m py_compile equipment_ocr_submitter.py
.venv/bin/python -m unittest test_equipment_ocr_submitter.py
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

If `exec-out screencap` fails with `error: closed`, fix/restart the BlueStacks ADB bridge before running OCR capture or ADB movement snapshots. Also verify that ADB is enabled on Bluestacks.

## Running Commands

Run commands from this repo folder. The recommended flow is: scan gear into the local SQLite DB, then import pending DB rows into MIRPG with one JSON paste.

### Open Gear Menu

```bash
.venv/bin/python equipment_ocr_submitter.py --config equipment_ocr_config.example.json --open-gear-menu-test
```

Dry test for MapleIdle `Menu -> Preset -> Edit Preset`.

### Scan Gear List

Manual page-by-page:

```bash
.venv/bin/python equipment_ocr_submitter.py --config equipment_ocr_config.example.json --open-gear-menu-first --scan-gear-list
```

Auto-scroll:

```bash
.venv/bin/python equipment_ocr_submitter.py --config equipment_ocr_config.example.json --open-gear-menu-first --scan-gear-list-auto-scroll
```

Saves OCR'd right-side Manage Equipment items as pending DB rows.

### Scan Equipped Slots

```bash
.venv/bin/python equipment_ocr_submitter.py --config equipment_ocr_config.example.json --open-gear-menu-first --scan-equipped-slots
```

Saves equipped/left-side gear and marks those DB rows as equipped.

### Import DB Items Into MIRPG

```bash
.venv/bin/python equipment_ocr_submitter.py --config equipment_ocr_config.example.json --apply-mirpg-json
```

Reads current MIRPG JSON, backs it up, adds pending DB items, skips duplicates already in MIRPG, imports JSON, verifies, and marks exported rows submitted.

### Export JSON Without Importing

```bash
.venv/bin/python equipment_ocr_submitter.py \
  --config equipment_ocr_config.example.json \
  --mirpg-template-json ~/Downloads/mirpg-template.json \
  --export-mirpg-json equipment_ocr_debug/mirpg-equipment-export.json
```

Add `--copy-mirpg-json` to copy the generated JSON to the clipboard.

## Website Cleanup

### Unequip All Presets

Dry run:

```bash
.venv/bin/python equipment_ocr_submitter.py --config equipment_ocr_config.example.json --unequip-via-json --dry-run
```

Apply:

```bash
.venv/bin/python equipment_ocr_submitter.py --config equipment_ocr_config.example.json --unequip-via-json
```

Clears MIRPG equipment presets by JSON import and saves a profile snapshot first.

### Delete All Manage Equipment Items

```bash
.venv/bin/python equipment_ocr_submitter.py --config equipment_ocr_config.example.json --dismantle-website
```

Deletes saved MIRPG Manage Equipment items by JSON import and saves a profile snapshot first.

### Restore MIRPG Snapshot

```bash
.venv/bin/python equipment_ocr_submitter.py --config equipment_ocr_config.example.json --restore-profile-snapshot
```

Snapshot `1` is newest. To restore a specific snapshot:

```bash
.venv/bin/python equipment_ocr_submitter.py --config equipment_ocr_config.example.json --restore-profile-snapshot 2
```

## DB Reports

Best Max HP and Max MP by slot:

```bash
.venv/bin/python equipment_ocr_submitter.py --config equipment_ocr_config.example.json --db-hp-mp-report
```

Strictly worse duplicate candidates:

```bash
.venv/bin/python equipment_ocr_submitter.py --config equipment_ocr_config.example.json --db-dominated-report
```

No dominated-report limit:

```bash
.venv/bin/python equipment_ocr_submitter.py --config equipment_ocr_config.example.json --db-dominated-report --db-dominated-limit 0
```

## Useful Flags

`--dry-run`: Preview where supported without applying website changes.

`--save-debug-images`: Save screenshots and crops for OCR debugging.

`--debug-dir PATH`: Choose where debug files are written.

`--manual-page-limit N`: Stop manual gear-list scanning after N pages.

`--equipped-slot-limit N`: Scan only the first N equipped slots.

`--export-db-statuses pending|submitted|failed|all`: Choose DB rows for JSON export.

`--export-db-limit N`: Export only the first N matching DB rows.

`--db-rank-statuses pending|submitted|failed|all`: Choose DB rows for reports.

`--db-rank-limit N`: Show top N HP/MP rows per slot.
