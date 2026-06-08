# Equipment Automation Setup

This installs the dependencies for `tools/equipment_ocr_submitter.py`.

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

If `exec-out screencap` fails with `error: closed`, fix/restart the BlueStacks ADB bridge before running OCR capture or ADB movement snapshots. Also verify that ADB is enabled on Bluestacks.

## Useful Run Commands

Notes: 
- All commands can be run with a "--dry-run" flag to just step through what will be performed without making changes on MIRPG (some dry runs work better than others at this point).
- Some object titling may fail as OCR is flaky. A piece of gear may lack lvl in the name, but the script jut skips it.

## Submit Gear List

Use this when you are entering items from the game’s right-side Manage Equipment list. This flow lets you choose where each visible page starts, then the script clicks and submits that item plus the remaining items to the right and below it. Manually scrolls for now, as scrolling programmatically is difficult with the momentum scrolling.

.venv/bin/python tools/equipment_ocr_submitter.py --config tools/equipment_ocr_config.example.json --submit-gear-list

How to use it:

1. Open MIRPG Optimizer to the Equipment page and leave it logged in.
2. In MapleIdle, open Preset -> Edit Preset (Ideally basic preset)
3. Run the command.
4. When prompted, press Enter.
5. Click the first item in the "Manage Equipment" scrollable table you want the script to process on the current visible page.
6. Wait while it submits that item and every later visible item.
7. When prompted again, manually scroll the game list to the next page.
8. Repeat until done, or type q at the prompt to stop.

## Submit Equipped Slots

Use this when you want to save the currently equipped items shown on the character/equipment screen’s left-side equipped slots.

.venv/bin/python tools/equipment_ocr_submitter.py --config tools/equipment_ocr_config.example.json --submit-equipped-slots

How to use it:

1. Open MIRPG Optimizer to the Equipment page and leave it logged in.
2. In MapleIdle, open Preset -> Edit Preset (Ideally basic preset)
3. Run the command.
4. When prompted, press Enter.
5. Click the top-left equipped item in the game (Helm).
6. The script uses that click to calibrate the slot positions, submits that first item, then taps/submits the remaining equipped slots.

## Unequip Website Items

Use this when you want MIRPG Optimizer to clear the currently equipped comparison item for every equipment category. This is website-only and does not interact with the game.

.venv/bin/python tools/equipment_ocr_submitter.py --config tools/equipment_ocr_config.example.json --unequip-website

How to use it:

1. Open MIRPG Optimizer to the Equipment page.
2. Keep that Chrome tab active while the command runs.
3. Run the command.
4. The script clicks each equipment category on the website and clicks Unequip on the left/equipped comparison item if one exists.

## Dismantle Website Items

Use this when you want to remove saved items from MIRPG Optimizer’s Manage Equipment list. This is website-only and does not interact with the game.

.venv/bin/python tools/equipment_ocr_submitter.py --config tools/equipment_ocr_config.example.json --dismantle-website

How to use it:

1. Open MIRPG Optimizer to the Equipment page.
2. Keep that Chrome tab active while the command runs.
3. Run the command.
4. The script selects each equipment category on the website.
5. For each category, it clicks the first visible item in Manage Equipment, clicks Dismantle in the comparison panel, then repeats until that category is empty.
6. The script stops if it cannot find Dismantle or if the item list does not visibly change after a dismantle click.
