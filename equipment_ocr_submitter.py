#!/usr/bin/env python3
"""OCR equipment details from a game screen and submit them to a website.

This is a configurable automation scaffold:

1. Capture a screenshot from either an Android/BlueStacks device through adb,
   a desktop screen region through mss, or a saved image.
2. Crop/preprocess the equipment-details area.
3. Optionally OCR named comparison-popover regions, such as the right/new
   candidate item and the left/currently equipped item.
4. OCR it with Tesseract.
5. Parse fields using regex patterns from a JSON config file.
6. Fill the primary/candidate item into a website with Playwright.
7. Advance to the next item by tapping/clicking/scrolling according to config.

Install notes:

    python3 -m venv .venv
    .venv/bin/python -m pip install -r requirements-equipment-automation.txt
    brew install tesseract android-platform-tools
    .venv/bin/python -m playwright install chromium

See EQUIPMENT_AUTOMATION_INSTALL.md for the full setup and run-command list.

Start with --dry-run and a low --limit until the crop/regex/selector config is
right. Automation like this may violate a game's or site's terms, so only use it
where you are allowed to automate your own data entry.
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import datetime as dt
import hashlib
import importlib
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable


Rect = tuple[int, int, int, int]

EQUIPMENT_SLOTS = [
    "Hat",
    "Top",
    "Bottom",
    "Gloves",
    "Ring",
    "Eye",
    "Earring",
    "Cape",
    "Shoulder",
    "Belt",
    "Shoes",
    "Necklace",
    "Face",
]

EFFECT_LABEL_ALIASES = {
    "attack": "Attack",
    "atk": "Attack",
    "defense": "Defense",
    "defence": "Defense",
    "def": "Defense",
    "max hp": "Max HP",
    "hp": "Max HP",
    "max mp": "Max MP",
    "mp": "Max MP",
    "critical rate": "Critical Rate",
    "crit rate": "Critical Rate",
    "critical chance": "Critical Rate",
    "critical damage": "Critical Damage",
    "critical dmg": "Critical Damage",
    "critical power": "Critical Damage",
    "boss damage": "Boss Monster Damage",
    "boss monster damage": "Boss Monster Damage",
    "normal monster damage": "Normal Monster Damage",
    "final damage": "Damage",
    "damage": "Damage",
    "accuracy": "Accuracy",
    "hit chance": "Accuracy",
    "avoidance": "Evasion",
    "avoid chance": "Evasion",
    "evasion": "Evasion",
    "1st job skill lv": "1st Job Skill Lv.",
    "1st job skill lv.": "1st Job Skill Lv.",
    "2nd job skill lv": "2nd Job Skill Lv.",
    "2nd job skill lv.": "2nd Job Skill Lv.",
    "3rd job skill lv": "3rd Job Skill Lv.",
    "3rd job skill lv.": "3rd Job Skill Lv.",
    "4th job skill lv": "4th Job Skill Lv.",
    "4th job skill lv.": "4th Job Skill Lv.",
    "all skill level": "All Skill Lv.",
    "skill level": "All Skill Lv.",
}

MIRPG_EFFECT_OPTION_IDS = {
    "Attack": "attack",
    "Defense": "defense",
    "Critical Rate": "crit-rate",
    "Critical Damage": "crit-damage",
    "Damage": "damage",
    "Boss Monster Damage": "boss-damage",
    "Normal Monster Damage": "normal-damage",
    "Accuracy": "accuracy",
    "Avoidance": "evasion",
    "Evasion": "evasion",
    "Final Damage": "final-damage",
    "All Skill Levels": "all-skill-levels",
    "All Skill Lv.": "all-skill-levels",
    "1st Job Skill Lv.": "skill-level-1",
    "2nd Job Skill Lv.": "skill-level-2",
    "3rd Job Skill Lv.": "skill-level-3",
    "4th Job Skill Lv.": "skill-level-4",
}

MIRPG_KNOWN_UNSUPPORTED_EFFECTS = {
    "Max HP": "MIRPG Optimizer does not expose Max HP as an equipment sub-option.",
    "Max MP": "MIRPG Optimizer does not expose Max MP as an equipment sub-option.",
}


@dataclasses.dataclass
class EquipmentResult:
    index: int
    image_path: Path | None
    ocr_text_path: Path
    raw_text: str
    parsed: dict[str, Any]
    image_hash: str
    target_region: str = "equipment"
    region_image_paths: dict[str, Path | None] = dataclasses.field(default_factory=dict)
    region_ocr_text_paths: dict[str, Path] = dataclasses.field(default_factory=dict)
    full_image_path: Path | None = None


class AutomationError(RuntimeError):
    """Raised for config or runtime errors that should be shown cleanly."""


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AutomationError(f"Config file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise AutomationError(f"Invalid JSON in {path}: {exc}") from exc


def require_package(package: str, install_name: str | None = None) -> Any:
    try:
        return importlib.import_module(package)
    except ImportError as exc:
        pip_name = install_name or package
        raise AutomationError(
            f"Missing Python package '{package}'. Install it with: "
            f"python3 -m pip install {pip_name}"
        ) from exc


def parse_rect(value: Any, name: str) -> Rect | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) != 4:
        raise AutomationError(f"{name} must be [left, top, width, height].")
    try:
        left, top, width, height = (int(part) for part in value)
    except (TypeError, ValueError) as exc:
        raise AutomationError(f"{name} must contain integers.") from exc
    if width <= 0 or height <= 0:
        raise AutomationError(f"{name} width and height must be positive.")
    return left, top, width, height


def now_stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def run_command(args: list[str], *, timeout: float = 20.0, input_bytes: bytes | None = None) -> bytes:
    try:
        result = subprocess.run(
            args,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise AutomationError(f"Command not found: {args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise AutomationError(f"Command timed out: {shlex.join(args)}") from exc

    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise AutomationError(f"Command failed: {shlex.join(args)}\n{stderr}")
    return result.stdout


def website_driver(config: dict[str, Any]) -> str:
    return str(config.get("website", {}).get("driver", "playwright"))


def image_hash(image: Any) -> str:
    with io.BytesIO() as buf:
        image.save(buf, format="PNG")
        return hashlib.sha256(buf.getvalue()).hexdigest()


def crop_image(image: Any, rect: Rect | None) -> Any:
    if rect is None:
        return image
    left, top, width, height = rect
    return image.crop((left, top, left + width, top + height))


def capture_from_adb(config: dict[str, Any]) -> Any:
    Image = require_package("PIL.Image", "Pillow")
    adb_path = config.get("adb_path", "adb")
    connect = config.get("connect")
    if connect:
        run_command([adb_path, "connect", str(connect)], timeout=float(config.get("timeout_seconds", 20)))

    serial = config.get("serial")
    args = [adb_path]
    if serial:
        args.extend(["-s", str(serial)])
    args.extend(["exec-out", "screencap", "-p"])
    png = run_command(args, timeout=float(config.get("timeout_seconds", 20)))
    return Image.open(io.BytesIO(png)).convert("RGB")


def screen_monitor_bounds(config: dict[str, Any]) -> dict[str, int]:
    mss = require_package("mss")
    monitor_index = int(config.get("monitor", 1))
    with mss.mss() as sct:
        monitors = sct.monitors
        if monitor_index >= len(monitors):
            raise AutomationError(
                f"Screen monitor {monitor_index} does not exist. "
                f"Available monitor indexes: 1..{len(monitors) - 1}"
            )
        return dict(monitors[monitor_index])


def capture_from_screen(config: dict[str, Any]) -> Any:
    Image = require_package("PIL.Image", "Pillow")
    mss = require_package("mss")

    bounds = screen_monitor_bounds(config)
    with mss.mss() as sct:
        shot = sct.grab(bounds)
        return Image.frombytes("RGB", shot.size, shot.rgb)


def capture_from_image(config: dict[str, Any]) -> Any:
    Image = require_package("PIL.Image", "Pillow")
    path = Path(str(config["path"])).expanduser()
    if not path.exists():
        raise AutomationError(f"Image capture path does not exist: {path}")
    return Image.open(path).convert("RGB")


def capture_screenshot(config: dict[str, Any]) -> Any:
    capture = config.get("capture", {})
    backend = capture.get("backend", "adb")
    if backend == "adb":
        image = capture_from_adb(capture)
    elif backend == "screen":
        image = capture_from_screen(capture)
    elif backend == "image":
        image = capture_from_image(capture)
    else:
        raise AutomationError("capture.backend must be one of: adb, screen, image")

    return image


def movement_snapshot_source(config: dict[str, Any]) -> tuple[Any, tuple[int, int], str]:
    snapshot_config = config.get("advance", {}).get("movement_snapshots", {})
    backend = str(snapshot_config.get("backend", "adb"))

    if backend == "adb":
        image = capture_from_adb(config.get("capture", {}))
        origin_x = 0
        origin_y = 0
        coordinate_space = "adb"
    elif backend == "screen":
        monitor = snapshot_config.get("monitor", config.get("capture", {}).get("monitor", 1))
        screen_config = {"monitor": monitor}
        bounds = screen_monitor_bounds(screen_config)
        image = capture_from_screen(screen_config)
        origin_x = int(bounds.get("left", 0))
        origin_y = int(bounds.get("top", 0))
        coordinate_space = "screen"
    else:
        raise AutomationError("advance.movement_snapshots.backend must be one of: adb, screen")

    crop = parse_rect(snapshot_config.get("crop"), "advance.movement_snapshots.crop")
    if crop is not None:
        left, top, _width, _height = crop
        origin_x += left
        origin_y += top
        image = crop_image(image, crop)
    return image, (origin_x, origin_y), coordinate_space


def capture_movement_snapshot_image(config: dict[str, Any]) -> Any:
    image, _origin, _coordinate_space = movement_snapshot_source(config)
    return image


def capture_equipment_image(config: dict[str, Any]) -> Any:
    capture = config.get("capture", {})
    image = capture_screenshot(config)
    crop = parse_rect(capture.get("crop"), "capture.crop")
    return crop_image(image, crop)


def resolve_ocr_regions(config: dict[str, Any]) -> list[dict[str, Any]]:
    capture = config.get("capture", {})
    regions = capture.get("regions")
    if regions is None:
        return [
            {
                "name": "equipment",
                "role": "primary",
                "primary": True,
                "crop": capture.get("crop"),
            }
        ]

    if not isinstance(regions, list) or not regions:
        raise AutomationError("capture.regions must be a non-empty list.")

    resolved: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for index, region in enumerate(regions):
        if not isinstance(region, dict):
            raise AutomationError("Every capture.regions entry must be an object.")
        name = str(region.get("name") or f"region_{index}")
        if not re.match(r"^[A-Za-z0-9_-]+$", name):
            raise AutomationError(f"Region name must contain only letters, numbers, _ or -: {name}")
        if name in seen_names:
            raise AutomationError(f"Duplicate capture region name: {name}")
        seen_names.add(name)

        resolved_region = dict(region)
        resolved_region["name"] = name
        resolved_region["role"] = str(resolved_region.get("role", "reference"))
        resolved_region["crop"] = parse_rect(resolved_region.get("crop"), f"capture.regions.{name}.crop")
        resolved.append(resolved_region)

    if not any(region.get("primary") for region in resolved):
        resolved[0]["primary"] = True
        resolved[0]["role"] = "primary"
    return resolved


def preprocess_for_ocr(image: Any, config: dict[str, Any]) -> Any:
    ImageFilter = require_package("PIL.ImageFilter", "Pillow")
    ImageOps = require_package("PIL.ImageOps", "Pillow")
    ImageEnhance = require_package("PIL.ImageEnhance", "Pillow")

    ocr_config = config.get("ocr", {})
    processed = image

    if ocr_config.get("grayscale", True):
        processed = processed.convert("L")

    scale = float(ocr_config.get("scale", 2.0))
    if scale != 1.0:
        width, height = processed.size
        processed = processed.resize((int(width * scale), int(height * scale)))

    contrast = float(ocr_config.get("contrast", 1.4))
    if contrast != 1.0:
        processed = ImageEnhance.Contrast(processed).enhance(contrast)

    if ocr_config.get("autocontrast", True):
        processed = ImageOps.autocontrast(processed)

    if ocr_config.get("sharpen", True):
        processed = processed.filter(ImageFilter.SHARPEN)

    threshold = ocr_config.get("threshold")
    if threshold is not None:
        threshold_int = int(threshold)
        processed = processed.point(lambda pixel: 255 if pixel > threshold_int else 0)

    return processed


def ocr_with_tesseract(image: Any, config: dict[str, Any]) -> str:
    ocr_config = config.get("ocr", {})
    language = str(ocr_config.get("language", "eng"))
    psm = str(ocr_config.get("psm", 6))
    extra_args = str(ocr_config.get("extra_args", "")).strip()
    command_config = f"--psm {psm}"
    if extra_args:
        command_config = f"{command_config} {extra_args}"

    engine = str(ocr_config.get("engine", "pytesseract"))
    if engine == "pytesseract":
        pytesseract = require_package("pytesseract")
        return pytesseract.image_to_string(image, lang=language, config=command_config).strip()

    if engine == "tesseract-cli":
        with tempfile.NamedTemporaryFile(suffix=".png") as tmp:
            image.save(tmp.name)
            args = ["tesseract", tmp.name, "stdout", "-l", language, "--psm", psm]
            if extra_args:
                args.extend(shlex.split(extra_args))
            return run_command(args, timeout=float(ocr_config.get("timeout_seconds", 20))).decode(
                "utf-8", errors="replace"
            ).strip()

    raise AutomationError("ocr.engine must be one of: pytesseract, tesseract-cli")


def normalize_text(text: str, config: dict[str, Any]) -> str:
    normalize = config.get("normalize", {})
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")

    replacements = normalize.get("replacements", [])
    if not isinstance(replacements, list):
        raise AutomationError("normalize.replacements must be a list.")
    for replacement in replacements:
        if not isinstance(replacement, dict):
            raise AutomationError("Each replacement must be an object.")
        pattern = str(replacement.get("pattern", ""))
        replace = str(replacement.get("replace", ""))
        flags = re.IGNORECASE if replacement.get("ignore_case", False) else 0
        normalized = re.sub(pattern, replace, normalized, flags=flags)

    if normalize.get("collapse_spaces", True):
        normalized = "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in normalized.splitlines())
    if normalize.get("drop_empty_lines", True):
        normalized = "\n".join(line for line in normalized.splitlines() if line)
    return normalized


def cast_value(value: str, field: dict[str, Any]) -> Any:
    value = value.strip()
    value_type = field.get("type", "text")

    transforms = field.get("transforms", {})
    if value in transforms:
        value = str(transforms[value])

    if value_type == "text":
        return value
    if value_type == "int":
        cleaned = re.sub(r"[^0-9-]", "", value)
        return int(cleaned) if cleaned else None
    if value_type == "float":
        cleaned = re.sub(r"[^0-9.-]", "", value)
        return float(cleaned) if cleaned else None
    if value_type == "percent":
        cleaned = re.sub(r"[^0-9.-]", "", value)
        return float(cleaned) if cleaned else None
    if value_type == "bool":
        return value.lower() in {"1", "true", "yes", "y", "on"}
    raise AutomationError(f"Unsupported field type for {field.get('name')}: {value_type}")


def parse_field(text: str, field: dict[str, Any]) -> Any:
    name = field.get("name")
    if not name:
        raise AutomationError("Every parser field must have a name.")

    patterns = field.get("regexes")
    if patterns is None:
        pattern = field.get("regex")
        patterns = [pattern] if pattern else []
    if not patterns:
        return field.get("default")
    if not isinstance(patterns, list):
        raise AutomationError(f"Field {name} regexes must be a list.")

    flags = re.IGNORECASE | re.MULTILINE
    if field.get("dotall", False):
        flags |= re.DOTALL

    for pattern in patterns:
        if not pattern:
            continue
        match = re.search(str(pattern), text, flags)
        if not match:
            continue

        value_group = field.get("value_group")
        if value_group is not None:
            raw_value = match.group(value_group)
        elif "value" in match.groupdict():
            raw_value = match.group("value")
        elif match.groups():
            raw_value = next((group for group in match.groups() if group is not None), "")
        else:
            raw_value = match.group(0)
        return cast_value(raw_value, field)

    return field.get("default")


def parse_number(value: str, value_type: str) -> Any:
    if value_type in {"float", "percent"}:
        cleaned = re.sub(r"[^0-9.-]", "", value)
        return float(cleaned) if cleaned else None

    cleaned = re.sub(r"[^0-9-]", "", value)
    return int(cleaned) if cleaned else None


def infer_tier_from_text(text: str) -> int | None:
    match = re.search(r"(?:^|[^A-Za-z0-9])(?:Tier|T)\s*[:.+-]?\s*([1-4])\b", text, re.IGNORECASE)
    if match:
        return int(match.group(1))

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    header_lines: list[str] = []
    for line in lines:
        if re.search(r"\bOn[-\s]?Equip Effect\b", line, re.IGNORECASE):
            break
        header_lines.append(line)

    for index, line in enumerate(header_lines):
        if re.search(r"\bJob Skill Lv\.?\b", line, re.IGNORECASE):
            continue
        if not re.search(r"\b(?:Level|Lv\.?|Enhance)\s*\+?\d+\b", line, re.IGNORECASE):
            continue
        for candidate in reversed(header_lines[max(0, index - 2) : index]):
            candidate_match = re.search(
                r"(?:^|[^A-Za-z0-9])(?:Tier|T)\s*[:.+-]?\s*([1-4])\b",
                candidate,
                re.IGNORECASE,
            )
            if candidate_match:
                return int(candidate_match.group(1))

            compact_candidate = re.sub(r"[^A-Za-z0-9]", "", candidate).upper()
            compact_match = re.search(r"T([1-4])", compact_candidate)
            if compact_match:
                return int(compact_match.group(1))

            # The tier is visually right above Lv.; OCR often drops the T and reads
            # T4/T3/T2/T1 as 14/73/12/11 on that line.
            digits = re.findall(r"[1-4]", candidate)
            if digits and len(candidate) <= 16:
                return int(digits[-1])

    compact = re.sub(r"[^A-Za-z0-9]", "", text).upper()
    if len(compact) <= 16:
        match = re.search(r"T([1-4])", compact)
        if match:
            return int(match.group(1))
    if len(compact) <= 3:
        match = re.search(r"[TI1]([1-4])", compact)
        if match:
            return int(match.group(1))
        digits = re.findall(r"[1-4]", compact)
        if len(digits) == 1:
            return int(digits[0])
    return None


def normalize_effect_label(label: str) -> str:
    cleaned = label.strip()
    cleaned = re.sub(r"^[^\w]+|[^\w.]+$", "", cleaned)
    cleaned = re.sub(r"\bCRI(?:TI)*CAL\b", "Critical", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bA\s*th(?=\s+Job Skill\b)", "4th", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bLV\b\.?", "Lv.", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" :-|")
    alias_key = cleaned.lower().rstrip(".")
    return EFFECT_LABEL_ALIASES.get(alias_key, cleaned)


def normalize_effect_value(label: str, value: str) -> tuple[Any, str]:
    cleaned = value.strip()
    is_percent = "%" in cleaned
    numeric_text = re.sub(r"[^0-9.-]", "", cleaned)
    if not numeric_text:
        return None, ""

    if re.search(r"\bJob Skill Lv\.?$", label, re.IGNORECASE):
        digits = re.sub(r"[^0-9]", "", numeric_text)
        if len(digits) > 1 and int(digits) > 10:
            numeric_text = digits[0]

    if is_percent:
        value_obj: Any = float(numeric_text)
        value_text = str(value_obj).rstrip("0").rstrip(".") if "." in str(value_obj) else str(value_obj)
        return value_obj, value_text

    value_obj = int(re.sub(r"[^0-9-]", "", numeric_text))
    return value_obj, str(value_obj)


def repair_effect_raw_value(label: str, value_fragment: str, fallback: str) -> str:
    if re.search(r"\bJob Skill Lv\.?$", label, re.IGNORECASE):
        return fallback

    fragment = value_fragment.strip()
    repairs = [
        # OCR occasionally reads the 7 in values like 2,745 as a slash: 2,/45.
        (r"^([-+]?\d{1,3})\s*,\s*[/\\]\s*(\d{2})(?!\d)", lambda m: f"{m.group(1)},7{m.group(2)}"),
        (r"^([-+]?\d{1,3})\s*[/\\]\s*(\d{2})(?!\d)", lambda m: f"{m.group(1)},7{m.group(2)}"),
        # OCR sometimes drops the comma entirely: 2 745. Keep 3-digit leading
        # values like "Defense 687 687" as comparison pairs, not 687,687.
        (r"^([-+]?\d{1,2})\s+(\d{3})(?!\d)", lambda m: f"{m.group(1)},{m.group(2)}"),
    ]
    for pattern, replacement in repairs:
        match = re.search(pattern, fragment)
        if match:
            return replacement(match)
    return fallback


def parse_ocr_letter_prefixed_effect_line(cleaned: str) -> tuple[str, str] | None:
    # Accuracy 32 can read as "Accuracy S23" when the comparison delta is also
    # adjacent; the generic number splitter would otherwise see label
    # "Accuracy S2" and value "3".
    match = re.match(r"^(?P<label>Accuracy)\s+(?P<value>[Ss][0-9])(?:[0-9])?(?:\D.*)?$", cleaned, re.IGNORECASE)
    if not match:
        return None
    raw_value = re.sub(r"^[Ss]", "3", match.group("value"))
    return match.group("label"), raw_value


def parse_effect_line(line: str) -> dict[str, Any] | None:
    cleaned = line.strip()
    if not cleaned:
        return None

    ocr_prefixed = parse_ocr_letter_prefixed_effect_line(cleaned)
    if ocr_prefixed:
        raw_label, raw_value = ocr_prefixed
        label = normalize_effect_label(raw_label)
        value, value_text = normalize_effect_value(label, raw_value)
        if value is None:
            return None
        effect = {
            "name": label,
            "value": value,
            "value_text": value_text,
            "raw_line": line,
            "raw_label": raw_label,
            "raw_value": raw_value,
        }
        option_id = MIRPG_EFFECT_OPTION_IDS.get(label)
        if option_id:
            effect["mirpg_option_id"] = option_id
        return effect

    number_re = re.compile(r"(?<![A-Za-z])[-+]?\d[\d,]*(?:\.\d+)?%?(?![A-Za-z])")
    matches = list(number_re.finditer(cleaned))
    if not matches:
        return None

    first = matches[0]
    raw_label = cleaned[: first.start()].strip(" :-|")
    raw_value = first.group(0)

    if not raw_label:
        raw_label = cleaned[first.end() :].strip(" :-|")
    if not raw_label:
        return None

    label = normalize_effect_label(raw_label)
    raw_value = repair_effect_raw_value(label, cleaned[first.start() :], raw_value)
    value, value_text = normalize_effect_value(label, raw_value)
    if value is None:
        return None

    effect = {
        "name": label,
        "value": value,
        "value_text": value_text,
        "raw_line": line,
        "raw_label": raw_label,
        "raw_value": raw_value,
    }
    option_id = MIRPG_EFFECT_OPTION_IDS.get(label)
    if option_id:
        effect["mirpg_option_id"] = option_id
    return effect


def parse_on_equip_effects(text: str) -> list[dict[str, Any]]:
    lines = text.splitlines()
    start_index = None
    for index, line in enumerate(lines):
        if re.search(r"\bOn[-\s]?Equip Effect\b", line, re.IGNORECASE):
            start_index = index + 1
            break

    if start_index is None:
        return []

    effects: list[dict[str, Any]] = []
    for line in lines[start_index:]:
        effect = parse_effect_line(line)
        if effect:
            effects.append(effect)
    return effects


def infer_equipment_slot(text: str, item_name: str | None = None) -> str | None:
    slot_pattern = "|".join(re.escape(slot) for slot in EQUIPMENT_SLOTS)
    match = re.search(
        rf"\b(?:Common|Rare|Epic|Unique|Legendary|Mythic|Ancient|gendary|egendary)\s+(?P<slot>{slot_pattern})\b",
        text,
        re.IGNORECASE,
    )
    if match:
        found = match.group("slot").lower()
        return next(slot for slot in EQUIPMENT_SLOTS if slot.lower() == found)

    grade_words = r"Common|Rare|Epic|Unique|Legendary|Mythic|Ancient|gendary|egendary"
    slot_aliases = {
        "should": "Shoulder",
        "shoulder": "Shoulder",
        "shoulders": "Shoulder",
    }
    alias_pattern = "|".join(re.escape(alias) for alias in slot_aliases)
    match = re.search(
        rf"\b(?:{grade_words})\s+(?P<slot>{alias_pattern})\b",
        text,
        re.IGNORECASE,
    )
    if match:
        return slot_aliases[match.group("slot").lower()]

    name = item_name or ""
    suffix_map = {
        "Boots": "Shoes",
        "Shoe": "Shoes",
        "Shoes": "Shoes",
        "Cap": "Hat",
        "Hat": "Hat",
        "Gloves": "Gloves",
        "Glove": "Gloves",
        "Cape": "Cape",
        "Cloak": "Cape",
        "Belt": "Belt",
        "Ring": "Ring",
        "Pendant": "Necklace",
        "Necklace": "Necklace",
        "Earring": "Earring",
        "Earrings": "Earring",
        "Pauldron": "Shoulder",
        "Pauldrons": "Shoulder",
        "Shoulder": "Shoulder",
        "Shoulders": "Shoulder",
        "Eye": "Eye",
        "Face": "Face",
        "Top": "Top",
        "Bottom": "Bottom",
    }
    for suffix, slot in suffix_map.items():
        if re.search(rf"\b{re.escape(suffix)}\b", name, re.IGNORECASE):
            return slot
    return None


def infer_item_name(text: str, current_name: str | None = None) -> str | None:
    slot_pattern = "|".join(re.escape(slot) for slot in EQUIPMENT_SLOTS)
    grade_slot_re = re.compile(
        rf"\b(?:Common|Rare|Epic|Unique|Legendary|Mythic|Ancient|gendary|egendary)\s+(?:{slot_pattern})\b",
        re.IGNORECASE,
    )
    stop_re = re.compile(r"\b(?:On[-\s]?Equip Effect|CP Change|Lv\.?|Equipped|MAIN OPTION|SUB OPTION)\b", re.IGNORECASE)
    candidates: list[str] = []

    for raw_line in text.splitlines():
        line = raw_line.strip(" \t:-|,.;")
        if not line:
            continue
        if re.search(r"\bOn[-\s]?Equip Effect\b", line, re.IGNORECASE):
            break
        if stop_re.search(line) or grade_slot_re.search(line):
            continue
        if re.search(r"[^A-Za-z0-9 '&.-]", line):
            continue
        words = re.findall(r"[A-Za-z][A-Za-z'&.-]*", line)
        if len(words) < 2:
            continue
        if sum(1 for word in words if word[:1].isupper()) < 2:
            continue
        candidates.append(" ".join(words))

    if candidates:
        return candidates[0]

    if current_name and not grade_slot_re.search(str(current_name)):
        return str(current_name)
    return current_name


def build_equipment_name(parsed: dict[str, Any]) -> str:
    parts: list[str] = []
    grade = parsed.get("grade")
    level = parsed.get("level")
    if grade:
        grade_name = str(grade).strip()
        grade_abbreviations = {
            "legendary": "leg",
            "unique": "unq",
        }
        parts.append(grade_abbreviations.get(grade_name.lower(), grade_name))
    if level not in {None, ""}:
        parts.append(f"Lv.{level}")
    return " - ".join(parts)


def apply_default_tier(parsed: dict[str, Any], parser: dict[str, Any]) -> None:
    if parsed.get("tier") not in {None, ""}:
        return

    slot = parsed.get("equipment_slot")
    default_by_slot = parser.get("default_tier_by_slot", {})
    if default_by_slot:
        if not isinstance(default_by_slot, dict):
            raise AutomationError("parser.default_tier_by_slot must be an object.")
        for slot_name, tier in default_by_slot.items():
            if slot and str(slot_name).lower() == str(slot).lower() and tier not in {None, ""}:
                parsed["tier"] = tier
                return

    if parser.get("default_tier") not in {None, ""}:
        parsed["tier"] = parser.get("default_tier")


def parse_stat_rows(text: str, parser: dict[str, Any], fields: list[dict[str, Any]]) -> dict[str, Any]:
    aliases = parser.get("stat_aliases", {})
    if not aliases:
        return {}
    if not isinstance(aliases, dict):
        raise AutomationError("parser.stat_aliases must be an object.")

    field_types = {
        str(field.get("name")): str(field.get("type", "int"))
        for field in fields
        if isinstance(field, dict) and field.get("name")
    }
    number_re = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")
    stat_values: dict[str, Any] = {}

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or not number_re.search(line):
            continue

        for stat_name, patterns in aliases.items():
            if isinstance(patterns, str):
                patterns = [patterns]
            if not isinstance(patterns, list):
                raise AutomationError(f"parser.stat_aliases.{stat_name} must be a string or list.")

            if not any(re.search(str(pattern), line, re.IGNORECASE) for pattern in patterns):
                continue

            numbers = number_re.findall(line)
            ocr_prefixed = parse_ocr_letter_prefixed_effect_line(line)
            if ocr_prefixed and str(stat_name) == "accuracy":
                _ocr_label, ocr_value = ocr_prefixed
                numbers = [ocr_value]
            if not numbers:
                continue

            value_type = field_types.get(str(stat_name), "percent" if "%" in line else "int")
            value = parse_number(numbers[0], value_type)
            if value is not None and str(stat_name) not in stat_values:
                stat_values[str(stat_name)] = value

    return stat_values


def parse_equipment_text(text: str, config: dict[str, Any]) -> dict[str, Any]:
    parser = config.get("parser", {})
    fields = parser.get("fields", [])
    if not isinstance(fields, list):
        raise AutomationError("parser.fields must be a list.")

    parsed = {"raw_text": text}
    for field in fields:
        if not isinstance(field, dict):
            raise AutomationError("Every parser field entry must be an object.")
        parsed[str(field["name"])] = parse_field(text, field)

    stat_values = parse_stat_rows(text, parser, fields)
    for name, value in stat_values.items():
        if parsed.get(name) in {None, ""}:
            parsed[name] = value
    if parsed.get("tier") in {None, ""}:
        inferred_tier = infer_tier_from_text(text)
        if inferred_tier is not None:
            parsed["tier"] = inferred_tier
    if parser.get("keep_stat_rows", True):
        parsed["stat_rows"] = stat_values

    effects = parse_on_equip_effects(text)
    parsed["item_name"] = infer_item_name(text, parsed.get("item_name"))
    parsed["on_equip_effects"] = effects
    parsed["equipment_slot"] = infer_equipment_slot(text, parsed.get("item_name"))
    apply_default_tier(parsed, parser)
    parsed["equipment_name"] = build_equipment_name(parsed)
    parsed["unresolved_effects"] = []

    if parser.get("keep_lines", True):
        parsed["lines"] = text.splitlines()

    return parsed


def debug_artifacts_config(config: dict[str, Any]) -> dict[str, Any]:
    value = config.get("debug_artifacts", {})
    return value if isinstance(value, dict) else {}


def debug_save_full_screenshot(config: dict[str, Any]) -> bool:
    return bool(debug_artifacts_config(config).get("save_full_screenshot", True))


def debug_save_region_images(config: dict[str, Any]) -> bool:
    return bool(debug_artifacts_config(config).get("save_region_images", True))


def debug_save_legacy_primary_copy(config: dict[str, Any]) -> bool:
    return bool(debug_artifacts_config(config).get("save_legacy_primary_copy", True))


def save_debug_artifacts(
    *,
    debug_dir: Path,
    index: int,
    image: Any,
    raw_text: str,
    parsed: dict[str, Any],
    save_image: bool = True,
) -> tuple[Path | None, Path, Path]:
    debug_dir.mkdir(parents=True, exist_ok=True)
    prefix = f"{index:04d}"
    image_path: Path | None = debug_dir / f"{prefix}_equipment.png"
    ocr_path = debug_dir / f"{prefix}_ocr.txt"
    parsed_path = debug_dir / f"{prefix}_parsed.json"
    if save_image:
        image.save(image_path)
    else:
        image_path = None
    ocr_path.write_text(raw_text + "\n", encoding="utf-8")
    parsed_path.write_text(json.dumps(parsed, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return image_path, ocr_path, parsed_path


def save_region_debug_artifacts(
    *,
    debug_dir: Path,
    index: int,
    region_name: str,
    image: Any,
    raw_text: str,
    parsed: dict[str, Any],
    save_image: bool = True,
) -> tuple[Path | None, Path, Path]:
    debug_dir.mkdir(parents=True, exist_ok=True)
    prefix = f"{index:04d}_{region_name}"
    image_path: Path | None = debug_dir / f"{prefix}_equipment.png"
    ocr_path = debug_dir / f"{prefix}_ocr.txt"
    parsed_path = debug_dir / f"{prefix}_parsed.json"
    if save_image:
        image.save(image_path)
    else:
        image_path = None
    ocr_path.write_text(raw_text + "\n", encoding="utf-8")
    parsed_path.write_text(json.dumps(parsed, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return image_path, ocr_path, parsed_path


def load_playwright(config: dict[str, Any]) -> tuple[Any, Any, Any, bool]:
    website = config.get("website", {})
    url = website.get("url")
    if not url:
        raise AutomationError("website.url is required unless --dry-run is used.")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise AutomationError(
            "Missing Python package 'playwright'. Install it with:\n"
            "python3 -m pip install playwright\nplaywright install chromium"
        ) from exc

    pw = sync_playwright().start()
    cdp_url = website.get("connect_over_cdp_url")
    if cdp_url:
        try:
            browser = pw.chromium.connect_over_cdp(str(cdp_url))
        except Exception as exc:
            pw.stop()
            raise AutomationError(
                "Could not connect to Chrome over CDP. Browser launch is disabled by default; "
                "open a supported debugging browser yourself or use website.driver = chrome_applescript."
            ) from exc
        context = browser.contexts[0] if browser.contexts else browser.new_context(
            viewport=website.get("viewport", {"width": 1440, "height": 1000})
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(str(url), wait_until=website.get("wait_until", "domcontentloaded"))

        if website.get("manual_login", True):
            print("Connected to Chrome. Log in / navigate to the analyzer form, then press Enter here.")
            input()

        return pw, browser, page, bool(website.get("close_cdp_browser", False))

    if not website.get("allow_browser_launch", False):
        pw.stop()
        raise AutomationError(
            "Browser launch is disabled to avoid touching your open Chrome windows. "
            "Use website.driver = chrome_applescript, set website.connect_over_cdp_url, "
            "or explicitly set website.allow_browser_launch = true."
        )

    browser_name = website.get("browser", "chromium")
    browser_launcher = getattr(pw, browser_name, None)
    if browser_launcher is None:
        pw.stop()
        raise AutomationError("website.browser must be chromium, firefox, or webkit.")

    user_data_dir = Path(str(website.get("user_data_dir") or ".equipment_submitter_browser")).expanduser()
    launch_options: dict[str, Any] = {
        "user_data_dir": str(user_data_dir),
        "headless": bool(website.get("headless", False)),
        "viewport": website.get("viewport", {"width": 1440, "height": 1000}),
    }
    browser_channel = website.get("browser_channel")
    if browser_channel:
        if browser_name != "chromium":
            pw.stop()
            raise AutomationError("website.browser_channel is only supported with website.browser = chromium.")
        launch_options["channel"] = str(browser_channel)
    executable_path = website.get("executable_path")
    if executable_path:
        launch_options["executable_path"] = str(Path(str(executable_path)).expanduser())
    launch_args = website.get("launch_args", [])
    if launch_args:
        if not isinstance(launch_args, list):
            pw.stop()
            raise AutomationError("website.launch_args must be a list.")
        launch_options["args"] = [str(arg) for arg in launch_args]

    browser = browser_launcher.launch_persistent_context(**launch_options)
    page = browser.pages[0] if browser.pages else browser.new_page()
    page.goto(str(url), wait_until=website.get("wait_until", "domcontentloaded"))

    if website.get("manual_login", True):
        print("Browser is open. Log in / navigate to the analyzer form, then press Enter here.")
        input()

    return pw, browser, page, bool(website.get("close_browser_on_exit", False))


def run_chrome_javascript(js_code: str, *, timeout: float = 20.0) -> str:
    if sys.platform != "darwin":
        raise AutomationError("website.driver = chrome_applescript only works on macOS.")

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tmp:
            tmp.write(js_code)
            tmp_path = Path(tmp.name)

        escaped_path = str(tmp_path).replace("\\", "\\\\").replace('"', '\\"')
        applescript = f'''
set jsFile to POSIX file "{escaped_path}"
set jsCode to read jsFile
tell application "Google Chrome"
    if not (exists front window) then error "No Google Chrome window is open."
    return execute front window's active tab javascript jsCode
end tell
'''
        return run_command(["osascript", "-e", applescript], timeout=timeout).decode(
            "utf-8", errors="replace"
        ).strip()
    except AutomationError as exc:
        raise AutomationError(
            "Could not execute JavaScript in your front Chrome tab. In Chrome, enable "
            "View > Developer > Allow JavaScript from Apple Events, then keep the MIRPG "
            "equipment page as the active tab."
        ) from exc
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass


def prepare_chrome_applescript(config: dict[str, Any]) -> None:
    website = config.get("website", {})
    url = str(website.get("url", ""))
    if website.get("manual_login", True):
        print(
            "Open your normal Chrome tab at the MIRPG equipment page, log in if needed, "
            "then press Enter here. The script will not launch or close Chrome."
        )
        input()

    if website.get("navigate_active_tab", False) and url:
        run_chrome_javascript(f"location.href = {json.dumps(url)}; 'navigating';")
        time.sleep(float(website.get("wait_after_navigation_seconds", 2.0)))

    href = run_chrome_javascript("location.href")
    if url and "mirpg-optimizer.netlify.app/equipment" not in href:
        raise AutomationError(
            "The active Chrome tab is not the MIRPG equipment page. "
            f"Active tab: {href or '(unknown)'}"
        )


def require_mirpg_equipment_slot(parsed: dict[str, Any], website: dict[str, Any]) -> str | None:
    if not bool(website.get("require_equipment_slot_for_submit", True)):
        slot = parsed.get("equipment_slot")
        return str(slot) if slot else None

    slot = parsed.get("equipment_slot")
    if slot:
        return str(slot)

    equipment_name = parsed.get("equipment_name") or build_equipment_name(parsed) or "(unknown item)"
    raw_lines = parsed.get("lines")
    if not isinstance(raw_lines, list):
        raw_text = str(parsed.get("raw_text", ""))
        raw_lines = raw_text.splitlines()
    header = " | ".join(str(line).strip() for line in raw_lines[:6] if str(line).strip())
    raise AutomationError(
        "Could not infer MIRPG equipment slot for "
        f"{equipment_name}; refusing to submit so it does not overwrite the currently selected website category. "
        f"OCR header: {header or '(empty)'}"
    )


def mirpg_applescript_payload(parsed: dict[str, Any], website: dict[str, Any] | None = None) -> dict[str, Any]:
    website = website or {}
    effects = [dict(effect) for effect in parsed.get("on_equip_effects", [])]
    equipment_name = build_equipment_name(parsed) or parsed.get("equipment_name")
    if not equipment_name:
        raise AutomationError("Parsed item does not have an equipment_name.")
    if not effects:
        raise AutomationError(f"No On-Equip Effect rows parsed for {equipment_name}.")
    slot = require_mirpg_equipment_slot(parsed, website)
    return {
        "equipment_name": str(equipment_name),
        "equipment_slot": slot,
        "on_equip_effects": effects,
    }


def mirpg_applescript_js(payload: dict[str, Any], website: dict[str, Any]) -> str:
    settings = {
        "select_slot_tab": bool(website.get("select_slot_tab", True)),
        "top_level_slot_max_y": website.get("top_level_slot_max_y"),
        "use_current_editor": bool(website.get("use_current_editor", False)),
        "reuse_empty_sub_option_row": bool(website.get("reuse_empty_sub_option_row", True)),
        "skip_known_unsupported_effects": bool(website.get("skip_known_unsupported_effects", True)),
        "known_unsupported_effects": list(MIRPG_KNOWN_UNSUPPORTED_EFFECTS),
        "wait_after_add_equipment_ms": int(float(website.get("wait_after_add_equipment_seconds", 0.5)) * 1000),
        "wait_after_submit_ms": int(float(website.get("wait_after_submit_seconds", 0.5)) * 1000),
    }
    return f"""
(() => {{
  const payload = {json.dumps(payload)};
  const settings = {json.dumps(settings)};
  const resultKey = "__mirpgOptimizerSubmitResult";
  window[resultKey] = {{ done: false, error: null }};

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const visible = (el) => {{
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const box = el.getBoundingClientRect();
    return style.visibility !== "hidden" && style.display !== "none" && box.width > 0 && box.height > 0;
  }};
  const textOf = (el) => (el.innerText || el.textContent || "").trim();
  const elementText = (el) => [textOf(el), el.getAttribute("aria-label"), el.getAttribute("title")]
    .filter(Boolean)
    .join(" ")
    .replace(/\\s+/g, " ")
    .trim();
  const labelKey = (value) => String(value || "").replace(/\\s+/g, " ").trim().replace(/\\.$/, "").toLowerCase();
  const labelAliases = {{
    "avoidance": "evasion",
    "avoid chance": "evasion"
  }};
  const canonicalLabelKey = (value) => labelAliases[labelKey(value)] || labelKey(value);
  const numberOnly = (value) => String(value ?? "").replace(/,/g, "").replace(/%$/, "");
  const slotIdFromName = (slot) => String(slot || "")
    .trim()
    .toLowerCase()
    .replace(/\\s+/g, "-");
  const slotTextMatcher = (slot) => {{
    const wanted = labelKey(slot);
    return (text) => {{
      const key = labelKey(text);
      return key === wanted || key === `${{wanted}} ${{wanted}}` || key.split(/\\s+/).includes(wanted);
    }};
  }};
  const areaOf = (el) => {{
    const box = el.getBoundingClientRect();
    return box.width * box.height;
  }};
  const clickElement = (el) => {{
    el.scrollIntoView({{ block: "center", inline: "center" }});
    el.click();
  }};
  const clickableTarget = (el) => {{
    const target = el.closest("button, [role='button'], a, [onclick]");
    return target && visible(target) ? target : el;
  }};
  const setRawValue = (el, value) => {{
    const proto = el instanceof HTMLSelectElement
      ? HTMLSelectElement.prototype
      : el instanceof HTMLTextAreaElement
        ? HTMLTextAreaElement.prototype
        : HTMLInputElement.prototype;
    const descriptor = Object.getOwnPropertyDescriptor(proto, "value");
    if (descriptor && descriptor.set) descriptor.set.call(el, String(value));
    else el.value = String(value);
  }};
  const setNativeValue = (el, value) => {{
    setRawValue(el, value);
    el.dispatchEvent(new Event("input", {{ bubbles: true }}));
    el.dispatchEvent(new Event("change", {{ bubbles: true }}));
  }};
  const visibleElements = (selector) => Array.from(document.querySelectorAll(selector)).filter(visible);
  const byText = (selector, matcher) => visibleElements(selector).filter((el) => matcher(elementText(el)));
  const exactText = (expected) => (actual) => labelKey(actual) === labelKey(expected);
  const visibleButtonSummary = (scope = document) => Array.from(scope.querySelectorAll("button, [role='button']"))
    .filter(visible)
    .sort((a, b) => {{
      const ab = a.getBoundingClientRect();
      const bb = b.getBoundingClientRect();
      return ab.top - bb.top || ab.left - bb.left;
    }})
    .map((el) => {{
      const box = el.getBoundingClientRect();
      return `${{elementText(el) || el.tagName}} @${{Math.round(box.left)}},${{Math.round(box.top)}}`;
    }})
    .slice(0, 80)
    .join(" | ");
  const visibleTextSummary = (scope = document, matcher = /.*/) => Array.from(scope.querySelectorAll("button, [role='button'], a, [onclick], [aria-label], span, div"))
    .filter(visible)
    .filter((el) => matcher.test(elementText(el)))
    .sort((a, b) => areaOf(a) - areaOf(b))
    .map((el) => {{
      const box = el.getBoundingClientRect();
      return `${{elementText(el).slice(0, 90)}} @${{Math.round(box.left)}},${{Math.round(box.top)}}`;
    }})
    .slice(0, 40)
    .join(" | ");
  const findClickableByText = (scope, matcher) => Array.from(scope.querySelectorAll("button, [role='button']"))
    .filter(visible)
    .find((el) => matcher(elementText(el)));
  const findSmallestTextElement = (scope, matcher) => Array.from(scope.querySelectorAll("button, [role='button'], a, [onclick], [aria-label], span, div"))
    .filter(visible)
    .filter((el) => matcher(elementText(el)))
    .sort((a, b) => areaOf(a) - areaOf(b))[0];
  const firstByPosition = (items) => items.sort((a, b) => {{
    const ab = a.getBoundingClientRect();
    const bb = b.getBoundingClientRect();
    return ab.top - bb.top || ab.left - bb.left;
  }})[0];
  const findActiveEditor = () => {{
    const candidates = visibleElements("div, section, form").filter((el) => {{
      const text = textOf(el);
      const inputs = Array.from(el.querySelectorAll("input")).filter(visible);
      return inputs.length > 0
        && text.includes("MAIN OPTION")
        && text.includes("SUB OPTION")
        && /(?:\\+\\s*)?Add/i.test(text);
    }});
    if (!candidates.length) {{
      throw new Error(
        `Could not find the active MIRPG equipment editor. Visible editor-ish text: `
        + `${{visibleTextSummary(document, /MAIN OPTION|SUB OPTION|Add|Edit|Equipment/i)}}`
      );
    }}
    return candidates.sort((a, b) => {{
      const ab = a.getBoundingClientRect();
      const bb = b.getBoundingClientRect();
      return (ab.width * ab.height) - (bb.width * bb.height);
    }})[0];
  }};

  const extractMainOptionLabels = (editor) => {{
    const lines = textOf(editor).split(/\\n+/).map((line) => line.trim()).filter(Boolean);
    const start = lines.findIndex((line) => line.toUpperCase() === "MAIN OPTION");
    if (start < 0) return [];
    let end = lines.length;
    for (let index = start + 1; index < lines.length; index += 1) {{
      if (lines[index].toUpperCase() === "SUB OPTION") {{
        end = index;
        break;
      }}
    }}
    return lines.slice(start + 1, end).filter((line) => {{
      return /[A-Za-z]/.test(line) && !/^[-+]?\\d[\\d,]*(?:\\.\\d+)?%?$/.test(line);
    }});
  }};

  const popFirstEffect = (effects, label) => {{
    const wanted = canonicalLabelKey(label);
    const index = effects.findIndex((effect) => canonicalLabelKey(effect.name) === wanted);
    if (index < 0) return null;
    return effects.splice(index, 1)[0];
  }};

  const findAddButton = (scope, label) => {{
    const matcher = new RegExp(label, "i");
    return findClickableByText(scope, (text) => matcher.test(text));
  }};
  const findSubOptionAddButton = (editor) => {{
    return Array.from(editor.querySelectorAll("button, [role='button'], [onclick]"))
      .filter(visible)
      .find((el) => /addComparisonStat|addEquippedStat/.test(el.getAttribute("onclick") || ""))
      || findAddButton(editor, "(?:\\\\+\\\\s*)?Add")
      || findSmallestTextElement(editor, (text) => /(?:\\+\\s*)?Add/i.test(text));
  }};
  const ensureSubOptionSelect = async (editor, subIndex) => {{
    let currentEditor = editor;
    let selects = Array.from(currentEditor.querySelectorAll("select")).filter(visible);
    if (settings.reuse_empty_sub_option_row && subIndex < selects.length) {{
      return {{ editor: currentEditor, select: selects[subIndex] }};
    }}

    const addButton = findSubOptionAddButton(currentEditor);
    if (!addButton) {{
      throw new Error(
        `Could not click + Add for a MIRPG sub-option row. Visible editor buttons: ${{visibleButtonSummary(currentEditor)}}. `
        + `Visible add text: ${{visibleTextSummary(currentEditor, /Add/i)}}`
      );
    }}
    clickElement(clickableTarget(addButton));

    const deadline = Date.now() + 3000;
    while (Date.now() < deadline) {{
      await sleep(150);
      currentEditor = findActiveEditor();
      selects = Array.from(currentEditor.querySelectorAll("select")).filter(visible);
      if (subIndex < selects.length) {{
        return {{ editor: currentEditor, select: selects[subIndex] }};
      }}
    }}

    throw new Error(
      `Could not find select for sub-option row ${{subIndex + 1}} after clicking Add. `
      + `Rows: ${{subOptionRowSummary(currentEditor) || "(none)"}}. `
      + `Visible editor buttons: ${{visibleButtonSummary(currentEditor)}}`
    );
  }};

  const setAddEquipmentSlot = (slot, slotId) => {{
    const select = document.getElementById("add-equipment-slot");
    if (!select) throw new Error("Could not find #add-equipment-slot.");
    const options = Array.from(select.options || []);
    const option = options.find((item) => item.value === slotId || labelKey(item.textContent) === labelKey(slot));
    if (!option) {{
      const available = options.map((item) => `${{item.textContent.trim()}}=${{item.value}}`).filter(Boolean).join(", ");
      throw new Error(`Could not select Add Equipment slot ${{slot}} (${{slotId}}). Available: ${{available}}`);
    }}
    setRawValue(select, option.value);
    if (select.value !== option.value) {{
      throw new Error(`Add Equipment slot did not stay selected. Wanted ${{option.value}}, got ${{select.value || "(empty)"}}.`);
    }}
    return option.value;
  }};

  const selectSubOption = (select, effect) => {{
    const wantedLabel = canonicalLabelKey(effect.name);
    const wantedId = String(effect.mirpg_option_id || "");
    const options = Array.from(select.options || []);
    const option = options.find((item) => canonicalLabelKey(item.textContent) === wantedLabel)
      || options.find((item) => wantedId && item.value === wantedId);
    if (!option) {{
      const available = options.map((item) => item.textContent.trim()).filter(Boolean).join(", ");
      throw new Error(`Dropdown option not found for On-Equip Effect: ${{effect.name}}. Available: ${{available}}`);
    }}
    setNativeValue(select, option.value);
  }};
  const editorItemId = (editor) => {{
    const node = editor.closest("[data-item-id]") || editor.querySelector("[data-item-id]");
    const id = Number(node?.dataset?.itemId);
    if (Number.isFinite(id)) return id;
    const attrNode = Array.from(editor.querySelectorAll("[onchange], [onclick]")).find((el) => {{
      const script = el.getAttribute("onchange") || el.getAttribute("onclick") || "";
      return /updateComparison(?:Item|Stat)\\(\\d+/.test(script) || /addComparisonStat\\(\\d+/.test(script);
    }});
    const script = attrNode ? (attrNode.getAttribute("onchange") || attrNode.getAttribute("onclick") || "") : "";
    const match = script.match(/(?:updateComparison(?:Item|Stat)|addComparisonStat)\\((\\d+)/);
    return match ? Number(match[1]) : null;
  }};
  const directUpdateDiagnostics = (itemId) => {{
    return [
      `itemId=${{itemId || "missing"}}`,
      `updateComparisonStat=${{typeof globalThis.updateComparisonStat}}`,
      `addComparisonStat=${{typeof globalThis.addComparisonStat}}`,
      `renderComparisonPanel=${{typeof globalThis.renderComparisonPanel}}`
    ].join(", ");
  }};
  const updateSubOptionDirectly = async (itemId, subIndex, effect) => {{
    const updateStat = globalThis.updateComparisonStat || window.updateComparisonStat;
    const addStat = globalThis.addComparisonStat || window.addComparisonStat;
    const renderPanel = globalThis.renderComparisonPanel || window.renderComparisonPanel;
    const renderInventory = globalThis.renderInventoryGrid || window.renderInventoryGrid;
    const renderComparison = globalThis.renderItemComparison || window.renderItemComparison;
    if (!itemId || !effect.mirpg_option_id || typeof updateStat !== "function") return false;
    if (subIndex > 0) {{
      if (typeof addStat !== "function") return false;
      addStat(itemId);
      await sleep(150);
    }}
    updateStat(itemId, subIndex, "type", effect.mirpg_option_id);
    await sleep(100);
    updateStat(itemId, subIndex, "value", numberOnly(effect.value_text || effect.value));
    if (typeof renderPanel === "function") renderPanel();
    if (typeof renderInventory === "function") renderInventory();
    if (typeof renderComparison === "function") renderComparison();
    await sleep(100);
    return true;
  }};
  const subOptionRows = (editor) => Array.from(editor.querySelectorAll("select"))
    .filter(visible)
    .map((select) => {{
      const row = select.closest(".flex.items-center") || select.parentElement;
      return {{ select, row }};
    }});
  const subOptionInputFor = (editor, subIndex, effect) => {{
    const wantedId = String(effect.mirpg_option_id || "");
    const rows = subOptionRows(editor);
    const matched = rows.find((entry) => wantedId && entry.select.value === wantedId) || rows[subIndex];
    if (!matched) return null;
    const rowInput = matched.row
      ? Array.from(matched.row.querySelectorAll("input[type='number']")).filter(visible)[0]
      : null;
    return rowInput || Array.from(editor.querySelectorAll("input[type='number']")).filter(visible)[subIndex + 1] || null;
  }};
  const subOptionRowSummary = (editor) => subOptionRows(editor).map((entry, index) => {{
    const options = Array.from(entry.select.options || []);
    const label = options.find((option) => option.value === entry.select.value)?.textContent?.trim() || entry.select.value || "(empty)";
    const input = entry.row ? Array.from(entry.row.querySelectorAll("input[type='number']")).find(visible) : null;
    return `row ${{index + 1}} value=${{entry.select.value || "(empty)"}} label=${{label}} input=${{input ? "yes" : "no"}}`;
  }}).join(" | ");
  const knownUnsupportedEffectKeys = new Set((settings.known_unsupported_effects || []).map(canonicalLabelKey));
  const shouldSkipEffect = (effect) => settings.skip_known_unsupported_effects
    && knownUnsupportedEffectKeys.has(canonicalLabelKey(effect.name));

  (async () => {{
    try {{
      const slot = payload.equipment_slot;
      const slotId = slotIdFromName(slot);
      if (slot && settings.select_slot_tab) {{
        if (slotId && typeof window.selectComparisonSlot === "function") {{
          window.selectComparisonSlot(slotId);
        }} else {{
          let slotButtons = byText("button, [role='button']", slotTextMatcher(slot));
          if (settings.top_level_slot_max_y !== null && settings.top_level_slot_max_y !== undefined) {{
            slotButtons = slotButtons.filter((button) => button.getBoundingClientRect().top <= Number(settings.top_level_slot_max_y));
          }}
          if (!slotButtons.length) throw new Error(`Could not find top-level equipment slot button: ${{slot}}`);
          clickElement(firstByPosition(slotButtons));
        }}
        await sleep(250);
      }}

      if (!settings.use_current_editor) {{
        if (slot && slotId) {{
          setAddEquipmentSlot(slot, slotId);
        }}
        if (typeof window.addComparisonItem === "function") {{
          window.addComparisonItem(false);
        }} else if (document.getElementById("add-equipment-slot") && slotId) {{
          document.getElementById("add-equipment-slot").dispatchEvent(new Event("change", {{ bubbles: true }}));
        }} else {{
          let addEquipment = findClickableByText(document, (text) => /(?:\\+\\s*)?Add Equipment/i.test(text))
            || findSmallestTextElement(document, (text) => /(?:\\+\\s*)?Add Equipment/i.test(text));
          if (!addEquipment) {{
            throw new Error(
              `Could not click + Add Equipment. Visible buttons: ${{visibleButtonSummary(document)}}. `
              + `Visible add/equipment text: ${{visibleTextSummary(document, /Add|Equipment|Preset|New/i)}}`
            );
          }}
          addEquipment = clickableTarget(addEquipment);
          clickElement(addEquipment);
        }}
        await sleep(settings.wait_after_add_equipment_ms);
        if (slot && typeof window.addComparisonItem !== "function") {{
          let menuChoices = byText("[role='menuitem']", exactText(slot));
          if (!menuChoices.length) {{
            menuChoices = byText("button", exactText(slot));
          }}
          const menuChoice = menuChoices[0];
          if (menuChoice) {{
            clickElement(menuChoice);
            await sleep(250);
          }}
        }}
      }}

      let editor = findActiveEditor();
      const itemId = editorItemId(editor);
      const effects = payload.on_equip_effects.map((effect) => ({{ ...effect }}));
      const inputs = Array.from(editor.querySelectorAll("input")).filter(visible);
      if (!inputs.length) throw new Error("Could not find the equipment name input.");
      if (!settings.use_current_editor && slot) {{
        const defaultName = String(inputs[0].value || "");
        if (defaultName && !slotTextMatcher(slot)(defaultName)) {{
          throw new Error(`New editor appears to be for "${{defaultName}}" instead of slot "${{slot}}".`);
        }}
      }}
      setNativeValue(inputs[0], payload.equipment_name);

      const mainLabels = extractMainOptionLabels(editor);
      mainLabels.forEach((label, labelIndex) => {{
        const effect = popFirstEffect(effects, label);
        if (!effect) return;
        const input = Array.from(editor.querySelectorAll("input")).filter(visible)[1 + labelIndex];
        if (!input) throw new Error(`Could not find input for main option: ${{label}}`);
        setNativeValue(input, numberOnly(effect.value_text || effect.value));
      }});

      const skippedEffects = [];
      let subIndex = 0;
      for (const effect of effects) {{
        if (shouldSkipEffect(effect)) {{
          skippedEffects.push({{
            ...effect,
            reason: `${{effect.name}} is parsed locally but is not available in the MIRPG Optimizer dropdown.`
          }});
          continue;
        }}

        if (await updateSubOptionDirectly(itemId, subIndex, effect)) {{
          subIndex += 1;
          editor = findActiveEditor();
          continue;
        }}

        const rowState = await ensureSubOptionSelect(editor, subIndex);
        editor = rowState.editor;
        const select = rowState.select;
        selectSubOption(select, effect);
        await sleep(300);
        editor = findActiveEditor();

        const input = subOptionInputFor(editor, subIndex, effect);
        if (!input) {{
          throw new Error(
            `Could not find value input for sub-option: ${{effect.name}}. `
            + `Direct update unavailable: ${{directUpdateDiagnostics(itemId)}}. `
            + `Rows: ${{subOptionRowSummary(editor) || "(none)"}}`
          );
        }}
        setNativeValue(input, numberOnly(effect.value_text || effect.value));
        subIndex += 1;
      }}

      await sleep(settings.wait_after_submit_ms);
      window[resultKey] = {{
        done: true,
        error: null,
        result: {{
          equipment_name: payload.equipment_name,
          effects_entered: payload.on_equip_effects.length - skippedEffects.length,
          skipped_effects: skippedEffects
        }}
      }};
    }} catch (error) {{
      window[resultKey] = {{ done: true, error: String(error && error.message ? error.message : error) }};
    }}
  }})();

  return "started";
}})();
"""


def submit_to_mirpg_optimizer_applescript(parsed: dict[str, Any], config: dict[str, Any]) -> None:
    website = config.get("website", {})
    payload = mirpg_applescript_payload(parsed, website)
    run_chrome_javascript(mirpg_applescript_js(payload, website), timeout=float(website.get("applescript_timeout_seconds", 20)))

    timeout_seconds = float(website.get("applescript_timeout_seconds", 20))
    poll_interval = float(website.get("applescript_poll_interval_seconds", 0.25))
    deadline = time.monotonic() + timeout_seconds
    last_state: dict[str, Any] = {}
    while time.monotonic() < deadline:
        raw_state = run_chrome_javascript(
            "JSON.stringify(window.__mirpgOptimizerSubmitResult || {})",
            timeout=min(5.0, timeout_seconds),
        )
        try:
            last_state = json.loads(raw_state) if raw_state else {}
        except json.JSONDecodeError:
            last_state = {"raw_state": raw_state}

        if last_state.get("done"):
            if last_state.get("error"):
                parsed["unresolved_effects"] = [
                    effect for effect in parsed.get("on_equip_effects", []) if str(effect.get("name", "")) in str(last_state["error"])
                ]
                raise AutomationError(str(last_state["error"]))
            result = last_state.get("result") if isinstance(last_state.get("result"), dict) else {}
            skipped_effects = result.get("skipped_effects") if isinstance(result, dict) else []
            parsed["unresolved_effects"] = skipped_effects if isinstance(skipped_effects, list) else []
            return
        time.sleep(poll_interval)

    raise AutomationError(f"Timed out waiting for Chrome AppleScript automation. Last state: {last_state}")


def mirpg_unequip_slots(website: dict[str, Any]) -> list[str]:
    slots = website.get("unequip_slots", EQUIPMENT_SLOTS)
    if not isinstance(slots, list):
        raise AutomationError("website.unequip_slots must be a list.")
    cleaned = [str(slot).strip() for slot in slots if str(slot).strip()]
    if not cleaned:
        raise AutomationError("website.unequip_slots must include at least one slot.")
    return cleaned


def mirpg_unequip_all_js(website: dict[str, Any]) -> str:
    settings = {
        "slots": mirpg_unequip_slots(website),
        "top_level_slot_max_y": website.get("top_level_slot_max_y"),
        "wait_after_slot_change_ms": int(float(website.get("wait_after_slot_change_seconds", 0.35)) * 1000),
        "wait_after_unequip_ms": int(float(website.get("wait_after_unequip_seconds", 0.45)) * 1000),
        "require_unequip_button": bool(website.get("require_unequip_button", False)),
    }
    return f"""
(() => {{
  const settings = {json.dumps(settings)};
  const resultKey = "__mirpgOptimizerUnequipResult";
  window[resultKey] = {{ done: false, error: null }};

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const visible = (el) => {{
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const box = el.getBoundingClientRect();
    return style.visibility !== "hidden" && style.display !== "none" && box.width > 0 && box.height > 0;
  }};
  const textOf = (el) => (el.innerText || el.textContent || "").trim();
  const elementText = (el) => [textOf(el), el.getAttribute("aria-label"), el.getAttribute("title")]
    .filter(Boolean)
    .join(" ")
    .replace(/\\s+/g, " ")
    .trim();
  const labelKey = (value) => String(value || "").replace(/\\s+/g, " ").trim().replace(/\\.$/, "").toLowerCase();
  const slotIdFromName = (slot) => String(slot || "")
    .trim()
    .toLowerCase()
    .replace(/\\s+/g, "-");
  const slotTextMatcher = (slot) => {{
    const wanted = labelKey(slot);
    return (text) => {{
      const key = labelKey(text);
      return key === wanted || key === `${{wanted}} ${{wanted}}` || key.split(/\\s+/).includes(wanted);
    }};
  }};
  const visibleElements = (selector, scope = document) => Array.from(scope.querySelectorAll(selector)).filter(visible);
  const areaOf = (el) => {{
    const box = el.getBoundingClientRect();
    return box.width * box.height;
  }};
  const firstByPosition = (items) => items.sort((a, b) => {{
    const ab = a.getBoundingClientRect();
    const bb = b.getBoundingClientRect();
    return ab.top - bb.top || ab.left - bb.left;
  }})[0];
  const clickElement = (el) => {{
    el.scrollIntoView({{ block: "center", inline: "center" }});
    el.click();
  }};
  const visibleButtonSummary = (scope = document) => Array.from(scope.querySelectorAll("button, [role='button']"))
    .filter(visible)
    .sort((a, b) => {{
      const ab = a.getBoundingClientRect();
      const bb = b.getBoundingClientRect();
      return ab.top - bb.top || ab.left - bb.left;
    }})
    .map((el) => {{
      const box = el.getBoundingClientRect();
      return `${{elementText(el) || el.tagName}} @${{Math.round(box.left)}},${{Math.round(box.top)}}`;
    }})
    .slice(0, 80)
    .join(" | ");
  const findComparePanel = () => {{
    const candidates = visibleElements("div, section, article").filter((el) => {{
      const text = textOf(el);
      return text.includes("Compare Equipment") && text.includes("MAIN OPTION");
    }});
    return candidates.sort((a, b) => areaOf(a) - areaOf(b))[0] || null;
  }};
  const findLeftUnequipButton = () => {{
    const panel = findComparePanel();
    if (!panel) {{
      throw new Error(`Could not find Compare Equipment panel. Visible buttons: ${{visibleButtonSummary(document)}}`);
    }}
    const cardCandidates = visibleElements("div, section, article, form", panel).filter((el) => {{
      const text = textOf(el);
      return text.includes("MAIN OPTION")
        && /\\bUnequip\\b/i.test(text)
        && visibleElements("button, [role='button'], a, [onclick]", el).some((button) => /\\bUnequip\\b/i.test(elementText(button)));
    }});
    const cards = cardCandidates.sort((a, b) => {{
      const ab = a.getBoundingClientRect();
      const bb = b.getBoundingClientRect();
      return areaOf(a) - areaOf(b) || ab.left - bb.left || ab.top - bb.top;
    }});
    const card = cards[0] || panel;
    const buttons = visibleElements("button, [role='button'], a, [onclick]", card)
      .filter((button) => /\\bUnequip\\b/i.test(elementText(button)))
      .sort((a, b) => {{
        const ab = a.getBoundingClientRect();
        const bb = b.getBoundingClientRect();
        return ab.left - bb.left || ab.top - bb.top;
      }});
    return buttons[0] || null;
  }};
  const selectSlot = async (slot) => {{
    const slotId = slotIdFromName(slot);
    if (slotId && typeof window.selectComparisonSlot === "function") {{
      window.selectComparisonSlot(slotId);
      await sleep(settings.wait_after_slot_change_ms);
      return;
    }}

    let slotButtons = visibleElements("button, [role='button']").filter((button) => slotTextMatcher(slot)(elementText(button)));
    if (settings.top_level_slot_max_y !== null && settings.top_level_slot_max_y !== undefined) {{
      slotButtons = slotButtons.filter((button) => button.getBoundingClientRect().top <= Number(settings.top_level_slot_max_y));
    }}
    if (!slotButtons.length) {{
      throw new Error(`Could not find top-level equipment slot button: ${{slot}}. Visible buttons: ${{visibleButtonSummary(document)}}`);
    }}
    clickElement(firstByPosition(slotButtons));
    await sleep(settings.wait_after_slot_change_ms);
  }};

  (async () => {{
    const results = [];
    try {{
      for (const slot of settings.slots) {{
        await selectSlot(slot);
        const button = findLeftUnequipButton();
        if (!button) {{
          if (settings.require_unequip_button) {{
            throw new Error(`Could not find left/equipped Unequip button for slot: ${{slot}}`);
          }}
          results.push({{ slot, status: "not_equipped" }});
          continue;
        }}

        const box = button.getBoundingClientRect();
        clickElement(button);
        await sleep(settings.wait_after_unequip_ms);
        results.push({{
          slot,
          status: "unequipped",
          button_center: [Math.round(box.left + box.width / 2), Math.round(box.top + box.height / 2)]
        }});
      }}

      window[resultKey] = {{ done: true, error: null, result: {{ slots: results }} }};
    }} catch (error) {{
      window[resultKey] = {{ done: true, error: String(error && error.message ? error.message : error), result: {{ slots: results }} }};
    }}
  }})();

  return "started";
}})();
"""


def poll_mirpg_unequip_applescript(config: dict[str, Any]) -> dict[str, Any]:
    website = config.get("website", {})
    timeout_seconds = float(website.get("unequip_timeout_seconds", website.get("applescript_timeout_seconds", 20)))
    poll_interval = float(website.get("applescript_poll_interval_seconds", 0.25))
    deadline = time.monotonic() + timeout_seconds
    last_state: dict[str, Any] = {}
    while time.monotonic() < deadline:
        raw_state = run_chrome_javascript(
            "JSON.stringify(window.__mirpgOptimizerUnequipResult || {})",
            timeout=min(5.0, timeout_seconds),
        )
        try:
            last_state = json.loads(raw_state) if raw_state else {}
        except json.JSONDecodeError:
            last_state = {"raw_state": raw_state}

        if last_state.get("done"):
            if last_state.get("error"):
                raise AutomationError(str(last_state["error"]))
            result = last_state.get("result")
            return result if isinstance(result, dict) else {}
        time.sleep(poll_interval)

    raise AutomationError(f"Timed out waiting for Chrome AppleScript unequip automation. Last state: {last_state}")


def poll_mirpg_unequip_playwright(page: Any, config: dict[str, Any]) -> dict[str, Any]:
    website = config.get("website", {})
    timeout_seconds = float(website.get("unequip_timeout_seconds", website.get("applescript_timeout_seconds", 20)))
    poll_interval = float(website.get("applescript_poll_interval_seconds", 0.25))
    deadline = time.monotonic() + timeout_seconds
    last_state: dict[str, Any] = {}
    while time.monotonic() < deadline:
        raw_state = page.evaluate("JSON.stringify(window.__mirpgOptimizerUnequipResult || {})")
        try:
            last_state = json.loads(raw_state) if raw_state else {}
        except json.JSONDecodeError:
            last_state = {"raw_state": raw_state}

        if last_state.get("done"):
            if last_state.get("error"):
                raise AutomationError(str(last_state["error"]))
            result = last_state.get("result")
            return result if isinstance(result, dict) else {}
        time.sleep(poll_interval)

    raise AutomationError(f"Timed out waiting for Playwright unequip automation. Last state: {last_state}")


def print_mirpg_unequip_summary(result: dict[str, Any]) -> None:
    slots = result.get("slots") if isinstance(result.get("slots"), list) else []
    unequipped = [slot for slot in slots if isinstance(slot, dict) and slot.get("status") == "unequipped"]
    skipped = [slot for slot in slots if isinstance(slot, dict) and slot.get("status") != "unequipped"]
    for entry in slots:
        if not isinstance(entry, dict):
            continue
        status = entry.get("status", "unknown")
        slot = entry.get("slot", "(unknown)")
        center = entry.get("button_center")
        suffix = f" at {center}" if center else ""
        print(f"{slot}: {status}{suffix}")
    print(f"Website unequip summary: unequipped={len(unequipped)}, skipped={len(skipped)}")


def run_mirpg_unequip_all_website(config: dict[str, Any], driver: str, page: Any = None) -> None:
    website = config.get("website", {})
    if website.get("automation_mode") != "mirpg_optimizer":
        raise AutomationError("--unequip-website requires website.automation_mode = mirpg_optimizer.")
    js_code = mirpg_unequip_all_js(website)
    if driver == "chrome_applescript":
        run_chrome_javascript(js_code, timeout=float(website.get("applescript_timeout_seconds", 20)))
        result = poll_mirpg_unequip_applescript(config)
    elif driver == "playwright":
        if page is None:
            raise AutomationError("Playwright unequip automation requires an active page.")
        page.evaluate(js_code)
        result = poll_mirpg_unequip_playwright(page, config)
    else:
        raise AutomationError("website.driver must be one of: chrome_applescript, playwright")
    print_mirpg_unequip_summary(result)


def mirpg_dismantle_slots(website: dict[str, Any]) -> list[str]:
    slots = website.get("dismantle_slots", website.get("unequip_slots", EQUIPMENT_SLOTS))
    if not isinstance(slots, list):
        raise AutomationError("website.dismantle_slots must be a list.")
    cleaned = [str(slot).strip() for slot in slots if str(slot).strip()]
    if not cleaned:
        raise AutomationError("website.dismantle_slots must include at least one slot.")
    return cleaned


def mirpg_dismantle_all_js(website: dict[str, Any]) -> str:
    settings = {
        "slots": mirpg_dismantle_slots(website),
        "top_level_slot_max_y": website.get("top_level_slot_max_y"),
        "wait_after_slot_change_ms": int(float(website.get("wait_after_slot_change_seconds", 0.35)) * 1000),
        "wait_after_manage_filter_ms": int(float(website.get("wait_after_manage_filter_seconds", 0.25)) * 1000),
        "wait_after_manage_item_click_ms": int(float(website.get("wait_after_manage_item_click_seconds", 0.25)) * 1000),
        "wait_after_dismantle_ms": int(float(website.get("wait_after_dismantle_seconds", 0.45)) * 1000),
        "max_dismantle_per_slot": int(website.get("max_dismantle_per_slot", 300)),
        "require_dismantle_button": bool(website.get("require_dismantle_button", True)),
        "require_dismantle_progress": bool(website.get("require_dismantle_progress", True)),
        "click_manage_slot_filter": bool(website.get("click_manage_slot_filter", True)),
        "auto_confirm_dismantle": bool(website.get("auto_confirm_dismantle", True)),
    }
    template = r"""
(() => {
  const settings = __SETTINGS__;
  const resultKey = "__mirpgOptimizerDismantleResult";
  window[resultKey] = { done: false, error: null };

  const originalConfirm = window.confirm;
  if (settings.auto_confirm_dismantle) {
    window.confirm = () => true;
  }

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const visible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const box = el.getBoundingClientRect();
    return style.visibility !== "hidden" && style.display !== "none" && box.width > 0 && box.height > 0;
  };
  const textOf = (el) => (el.innerText || el.textContent || "").trim();
  const elementText = (el) => [textOf(el), el.getAttribute("aria-label"), el.getAttribute("title")]
    .filter(Boolean)
    .join(" ")
    .replace(/\s+/g, " ")
    .trim();
  const labelKey = (value) => String(value || "").replace(/\s+/g, " ").trim().replace(/\.$/, "").toLowerCase();
  const slotIdFromName = (slot) => String(slot || "")
    .trim()
    .toLowerCase()
    .replace(/\s+/g, "-");
  const slotTextMatcher = (slot) => {
    const wanted = labelKey(slot);
    return (text) => {
      const key = labelKey(text);
      return key === wanted || key === `${wanted} ${wanted}` || key.split(/\s+/).includes(wanted);
    };
  };
  const visibleElements = (selector, scope = document) => Array.from(scope.querySelectorAll(selector)).filter(visible);
  const areaOf = (el) => {
    const box = el.getBoundingClientRect();
    return box.width * box.height;
  };
  const firstByPosition = (items) => items.sort((a, b) => {
    const ab = a.getBoundingClientRect();
    const bb = b.getBoundingClientRect();
    return ab.top - bb.top || ab.left - bb.left;
  })[0];
  const clickElement = (el) => {
    el.scrollIntoView({ block: "center", inline: "center" });
    el.click();
  };
  const clickableTarget = (el) => {
    const target = el.closest("button, [role='button'], a, [onclick]");
    return target && visible(target) ? target : el;
  };
  const visibleButtonSummary = (scope = document) => Array.from(scope.querySelectorAll("button, [role='button'], a, [onclick]"))
    .filter(visible)
    .sort((a, b) => {
      const ab = a.getBoundingClientRect();
      const bb = b.getBoundingClientRect();
      return ab.top - bb.top || ab.left - bb.left;
    })
    .map((el) => {
      const box = el.getBoundingClientRect();
      return `${elementText(el) || el.tagName} @${Math.round(box.left)},${Math.round(box.top)}`;
    })
    .slice(0, 100)
    .join(" | ");
  const findSmallestPanel = (heading, extraMatcher = () => true) => {
    const candidates = visibleElements("div, section, article, main").filter((el) => {
      const text = textOf(el);
      return text.includes(heading) && extraMatcher(text, el);
    });
    return candidates.sort((a, b) => areaOf(a) - areaOf(b))[0] || null;
  };
  const findManagePanel = () => {
    const panel = findSmallestPanel("Manage Equipment", (text) => {
      const key = labelKey(text);
      return key.includes("add equipment") && /\ball\b/i.test(text) && settings.slots.some((slot) => key.includes(labelKey(slot)));
    });
    if (!panel) {
      throw new Error(`Could not find Manage Equipment panel. Visible buttons: ${visibleButtonSummary(document)}`);
    }
    return panel;
  };
  const findComparePanel = () => {
    const panel = findSmallestPanel("Compare Equipment", (text) => text.includes("MAIN OPTION"));
    if (!panel) {
      throw new Error(`Could not find Compare Equipment panel. Visible buttons: ${visibleButtonSummary(document)}`);
    }
    return panel;
  };
  const selectSlot = async (slot) => {
    const slotId = slotIdFromName(slot);
    if (slotId && typeof window.selectComparisonSlot === "function") {
      window.selectComparisonSlot(slotId);
      await sleep(settings.wait_after_slot_change_ms);
      return;
    }

    let slotButtons = visibleElements("button, [role='button']").filter((button) => slotTextMatcher(slot)(elementText(button)));
    if (settings.top_level_slot_max_y !== null && settings.top_level_slot_max_y !== undefined) {
      slotButtons = slotButtons.filter((button) => button.getBoundingClientRect().top <= Number(settings.top_level_slot_max_y));
    }
    if (!slotButtons.length) {
      throw new Error(`Could not find top-level equipment slot button: ${slot}. Visible buttons: ${visibleButtonSummary(document)}`);
    }
    clickElement(firstByPosition(slotButtons));
    await sleep(settings.wait_after_slot_change_ms);
  };
  const clickManageSlotFilter = async (slot) => {
    if (!settings.click_manage_slot_filter) return;
    const panel = findManagePanel();
    const wanted = labelKey(slot);
    const buttons = visibleElements("button, [role='button']", panel)
      .filter((button) => {
        const key = labelKey(elementText(button));
        return key === wanted || key === `${wanted} ${wanted}`;
      });
    if (buttons.length) {
      clickElement(firstByPosition(buttons));
      await sleep(settings.wait_after_manage_filter_ms);
    }
  };
  const manageCardFingerprint = (el) => {
    const box = el.getBoundingClientRect();
    return {
      text: elementText(el),
      center: [Math.round(box.left + box.width / 2), Math.round(box.top + box.height / 2)],
      size: [Math.round(box.width), Math.round(box.height)]
    };
  };
  const sameFingerprint = (a, b) => {
    if (!a || !b) return false;
    return a.text === b.text
      && Math.abs(a.center[0] - b.center[0]) <= 2
      && Math.abs(a.center[1] - b.center[1]) <= 2;
  };
  const findManageItemCards = (slot) => {
    const panel = findManagePanel();
    const panelBox = panel.getBoundingClientRect();
    const wanted = labelKey(slot);
    const filterKeys = new Set(["all", "all all", ...settings.slots.map(labelKey), ...settings.slots.map((item) => `${labelKey(item)} ${labelKey(item)}`)]);
    const raw = visibleElements("button, [role='button'], a, [onclick], div, section, article", panel)
      .filter((el) => {
        const text = elementText(el);
        const key = labelKey(text);
        if (!key || filterKeys.has(key)) return false;
        if (/add equipment/i.test(text)) return false;
        if (!key.includes(wanted)) return false;
        const box = el.getBoundingClientRect();
        if (box.top < panelBox.top + 70) return false;
        if (box.width < 55 || box.height < 35 || box.width > 260 || box.height > 170) return false;
        return true;
      })
      .map(clickableTarget)
      .filter((el) => panel.contains(el) && visible(el));

    const unique = [];
    const seen = new Set();
    for (const el of raw.sort((a, b) => areaOf(a) - areaOf(b))) {
      const box = el.getBoundingClientRect();
      const key = `${Math.round(box.left / 4)}:${Math.round(box.top / 4)}:${Math.round(box.width / 4)}:${Math.round(box.height / 4)}:${elementText(el)}`;
      if (seen.has(key)) continue;
      seen.add(key);
      unique.push(el);
    }
    return unique.sort((a, b) => {
      const ab = a.getBoundingClientRect();
      const bb = b.getBoundingClientRect();
      return ab.top - bb.top || ab.left - bb.left || areaOf(a) - areaOf(b);
    });
  };
  const findDismantleButton = () => {
    const panel = findComparePanel();
    const cardCandidates = visibleElements("div, section, article, form", panel)
      .filter((el) => {
        const text = textOf(el);
        return text.includes("MAIN OPTION")
          && /\bDismantle\b/i.test(text)
          && visibleElements("button, [role='button'], a, [onclick]", el).some((button) => /\bDismantle\b/i.test(elementText(button)));
      })
      .sort((a, b) => {
        const ab = a.getBoundingClientRect();
        const bb = b.getBoundingClientRect();
        return areaOf(a) - areaOf(b) || bb.left - ab.left || ab.top - bb.top;
      });
    const scope = cardCandidates[0] || panel;
    const buttons = visibleElements("button, [role='button'], a, [onclick]", scope)
      .filter((button) => /\bDismantle\b/i.test(elementText(button)))
      .sort((a, b) => {
        const ab = a.getBoundingClientRect();
        const bb = b.getBoundingClientRect();
        return bb.left - ab.left || ab.top - bb.top;
      });
    return buttons[0] || null;
  };
  const clickDialogConfirmationIfPresent = async () => {
    await sleep(120);
    const dialogs = visibleElements("[role='dialog'], dialog, [aria-modal='true'], .modal, [class*='modal'], [class*='Modal']");
    if (!dialogs.length) return false;
    const dialog = dialogs.sort((a, b) => areaOf(a) - areaOf(b))[0];
    const buttons = visibleElements("button, [role='button'], a, [onclick]", dialog)
      .filter((button) => /confirm|ok|yes|dismantle/i.test(elementText(button)) && !/cancel|no/i.test(elementText(button)))
      .sort((a, b) => areaOf(a) - areaOf(b));
    if (!buttons.length) return false;
    clickElement(buttons[0]);
    return true;
  };

  (async () => {
    const slotResults = [];
    const itemResults = [];
    try {
      for (const slot of settings.slots) {
        await selectSlot(slot);
        await clickManageSlotFilter(slot);
        let dismantled = 0;
        let skipped = 0;

        for (let attempt = 0; attempt < settings.max_dismantle_per_slot; attempt += 1) {
          const beforeCards = findManageItemCards(slot);
          if (!beforeCards.length) break;

          const beforeCount = beforeCards.length;
          const card = beforeCards[0];
          const beforeFingerprint = manageCardFingerprint(card);
          clickElement(card);
          await sleep(settings.wait_after_manage_item_click_ms);

          const button = findDismantleButton();
          if (!button) {
            if (settings.require_dismantle_button) {
              throw new Error(`Could not find Dismantle button after selecting ${slot} item ${JSON.stringify(beforeFingerprint)}. Compare buttons: ${visibleButtonSummary(findComparePanel())}`);
            }
            skipped += 1;
            itemResults.push({ slot, status: "missing_dismantle", item: beforeFingerprint });
            break;
          }

          const buttonBox = button.getBoundingClientRect();
          clickElement(button);
          await clickDialogConfirmationIfPresent();
          await sleep(settings.wait_after_dismantle_ms);
          await clickManageSlotFilter(slot);

          const afterCards = findManageItemCards(slot);
          const afterFingerprint = afterCards.length ? manageCardFingerprint(afterCards[0]) : null;
          if (settings.require_dismantle_progress && afterCards.length >= beforeCount && sameFingerprint(beforeFingerprint, afterFingerprint)) {
            throw new Error(
              `Dismantle did not appear to remove the first ${slot} item. `
              + `Before count=${beforeCount}, after count=${afterCards.length}, item=${JSON.stringify(beforeFingerprint)}`
            );
          }

          dismantled += 1;
          itemResults.push({
            slot,
            status: "dismantled",
            item: beforeFingerprint,
            button_center: [Math.round(buttonBox.left + buttonBox.width / 2), Math.round(buttonBox.top + buttonBox.height / 2)]
          });
        }

        slotResults.push({ slot, dismantled, skipped });
      }

      if (settings.auto_confirm_dismantle) {
        window.confirm = originalConfirm;
      }
      window[resultKey] = { done: true, error: null, result: { slots: slotResults, items: itemResults } };
    } catch (error) {
      if (settings.auto_confirm_dismantle) {
        window.confirm = originalConfirm;
      }
      window[resultKey] = {
        done: true,
        error: String(error && error.message ? error.message : error),
        result: { slots: slotResults, items: itemResults }
      };
    }
  })();

  return "started";
})();
"""
    return template.replace("__SETTINGS__", json.dumps(settings))


def poll_mirpg_dismantle_applescript(config: dict[str, Any]) -> dict[str, Any]:
    website = config.get("website", {})
    timeout_seconds = float(website.get("dismantle_timeout_seconds", website.get("applescript_timeout_seconds", 20)))
    poll_interval = float(website.get("applescript_poll_interval_seconds", 0.25))
    deadline = time.monotonic() + timeout_seconds
    last_state: dict[str, Any] = {}
    while time.monotonic() < deadline:
        raw_state = run_chrome_javascript(
            "JSON.stringify(window.__mirpgOptimizerDismantleResult || {})",
            timeout=min(5.0, timeout_seconds),
        )
        try:
            last_state = json.loads(raw_state) if raw_state else {}
        except json.JSONDecodeError:
            last_state = {"raw_state": raw_state}

        if last_state.get("done"):
            if last_state.get("error"):
                raise AutomationError(str(last_state["error"]))
            result = last_state.get("result")
            return result if isinstance(result, dict) else {}
        time.sleep(poll_interval)

    raise AutomationError(f"Timed out waiting for Chrome AppleScript dismantle automation. Last state: {last_state}")


def poll_mirpg_dismantle_playwright(page: Any, config: dict[str, Any]) -> dict[str, Any]:
    website = config.get("website", {})
    timeout_seconds = float(website.get("dismantle_timeout_seconds", website.get("applescript_timeout_seconds", 20)))
    poll_interval = float(website.get("applescript_poll_interval_seconds", 0.25))
    deadline = time.monotonic() + timeout_seconds
    last_state: dict[str, Any] = {}
    while time.monotonic() < deadline:
        raw_state = page.evaluate("JSON.stringify(window.__mirpgOptimizerDismantleResult || {})")
        try:
            last_state = json.loads(raw_state) if raw_state else {}
        except json.JSONDecodeError:
            last_state = {"raw_state": raw_state}

        if last_state.get("done"):
            if last_state.get("error"):
                raise AutomationError(str(last_state["error"]))
            result = last_state.get("result")
            return result if isinstance(result, dict) else {}
        time.sleep(poll_interval)

    raise AutomationError(f"Timed out waiting for Playwright dismantle automation. Last state: {last_state}")


def print_mirpg_dismantle_summary(result: dict[str, Any]) -> None:
    slots = result.get("slots") if isinstance(result.get("slots"), list) else []
    items = result.get("items") if isinstance(result.get("items"), list) else []
    for entry in slots:
        if not isinstance(entry, dict):
            continue
        print(
            f"{entry.get('slot', '(unknown)')}: "
            f"dismantled={entry.get('dismantled', 0)}, skipped={entry.get('skipped', 0)}"
        )
    dismantled = [item for item in items if isinstance(item, dict) and item.get("status") == "dismantled"]
    skipped = [item for item in items if isinstance(item, dict) and item.get("status") != "dismantled"]
    print(f"Website dismantle summary: dismantled={len(dismantled)}, skipped={len(skipped)}")


def run_mirpg_dismantle_all_website(config: dict[str, Any], driver: str, page: Any = None) -> None:
    website = config.get("website", {})
    if website.get("automation_mode") != "mirpg_optimizer":
        raise AutomationError("--dismantle-website requires website.automation_mode = mirpg_optimizer.")
    js_code = mirpg_dismantle_all_js(website)
    if driver == "chrome_applescript":
        run_chrome_javascript(js_code, timeout=float(website.get("applescript_timeout_seconds", 20)))
        result = poll_mirpg_dismantle_applescript(config)
    elif driver == "playwright":
        if page is None:
            raise AutomationError("Playwright dismantle automation requires an active page.")
        page.evaluate(js_code)
        result = poll_mirpg_dismantle_playwright(page, config)
    else:
        raise AutomationError("website.driver must be one of: chrome_applescript, playwright")
    print_mirpg_dismantle_summary(result)


def submit_to_website_applescript(parsed: dict[str, Any], config: dict[str, Any]) -> None:
    website = config.get("website", {})
    if website.get("automation_mode") == "mirpg_optimizer":
        submit_to_mirpg_optimizer_applescript(parsed, config)
        return
    raise AutomationError("website.driver = chrome_applescript currently supports automation_mode = mirpg_optimizer only.")


def coerce_submit_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "\n".join(str(part) for part in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def is_empty_value(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def fill_selector(page: Any, selector_config: Any, value: Any) -> None:
    value_text = coerce_submit_value(value)
    if selector_config is None:
        return

    if isinstance(selector_config, str):
        page.locator(selector_config).fill(value_text)
        return

    if not isinstance(selector_config, dict):
        raise AutomationError("Website field selector entries must be strings or objects.")

    selector = selector_config.get("selector")
    if not selector:
        return

    action = selector_config.get("action", "fill")
    locator = page.locator(str(selector))

    if action == "fill":
        locator.fill(value_text)
    elif action == "type":
        locator.click()
        locator.press("Meta+A" if sys.platform == "darwin" else "Control+A")
        locator.type(value_text, delay=int(selector_config.get("delay_ms", 0)))
    elif action == "select":
        locator.select_option(value_text)
    elif action == "check":
        desired = bool(value)
        if locator.is_checked() != desired:
            locator.click()
    elif action == "click":
        locator.click()
    else:
        raise AutomationError(f"Unsupported website field action: {action}")


def click_if_visible(locator: Any, *, timeout_ms: int = 1200) -> bool:
    try:
        locator.wait_for(state="visible", timeout=timeout_ms)
        locator.click()
        return True
    except Exception:
        return False


def visible_locator_candidates(locator: Any) -> list[tuple[float, int, dict[str, float]]]:
    candidates: list[tuple[float, int, dict[str, float]]] = []
    count = locator.count()
    for index in range(count):
        item = locator.nth(index)
        try:
            if not item.is_visible():
                continue
            box = item.bounding_box()
            if not box:
                continue
            area = float(box["width"] * box["height"])
            candidates.append((area, index, box))
        except Exception:
            continue
    return candidates


def click_visible_candidate(locator: Any, index: int) -> None:
    locator.nth(index).scroll_into_view_if_needed()
    locator.nth(index).click()


def first_visible_smallest(page: Any, selectors: list[str]) -> Any:
    candidates: list[tuple[float, int, str]] = []
    for selector in selectors:
        locator = page.locator(selector)
        for area, index, _box in visible_locator_candidates(locator):
            candidates.append((area, index, selector))

    if not candidates:
        raise AutomationError("Could not find the active MIRPG equipment editor.")

    _area, index, selector = min(candidates, key=lambda entry: entry[0])
    return page.locator(selector).nth(index)


def select_mirpg_top_level_slot(page: Any, slot: str, website: dict[str, Any]) -> None:
    escaped = re.escape(slot)
    selector = website.get("top_level_slot_button_selector", "button")
    locator = page.locator(str(selector)).filter(has_text=re.compile(rf"^{escaped}$", re.IGNORECASE))
    candidates = visible_locator_candidates(locator)
    if not candidates:
        raise AutomationError(f"Could not find MIRPG top-level equipment slot button: {slot}")

    max_y = website.get("top_level_slot_max_y")
    filtered = candidates
    if max_y is not None:
        filtered = [candidate for candidate in candidates if candidate[2]["y"] <= float(max_y)]
        if not filtered:
            raise AutomationError(f"Could not find top-level slot button above y={max_y}: {slot}")

    _area, index, _box = min(filtered, key=lambda entry: (entry[2]["y"], entry[2]["x"]))
    click_visible_candidate(locator, index)


def active_mirpg_editor(page: Any, website: dict[str, Any]) -> Any:
    selectors = website.get(
        "editor_selector_candidates",
        [
            "div:has-text('MAIN OPTION'):has-text('SUB OPTION'):has(button:has-text('+ Add'))",
            "section:has-text('MAIN OPTION'):has-text('SUB OPTION'):has(button:has-text('+ Add'))",
            "form:has-text('MAIN OPTION'):has-text('SUB OPTION'):has(button:has-text('+ Add'))",
        ],
    )
    if not isinstance(selectors, list):
        raise AutomationError("website.editor_selector_candidates must be a list.")
    return first_visible_smallest(page, [str(selector) for selector in selectors])


def extract_main_option_labels(editor_text: str) -> list[str]:
    lines = [line.strip() for line in editor_text.splitlines() if line.strip()]
    try:
        start = next(index for index, line in enumerate(lines) if line.upper() == "MAIN OPTION") + 1
    except StopIteration:
        return []

    end = len(lines)
    for index in range(start, len(lines)):
        if lines[index].upper() == "SUB OPTION":
            end = index
            break

    labels: list[str] = []
    for line in lines[start:end]:
        if not re.search(r"[A-Za-z]", line):
            continue
        if re.fullmatch(r"[-+]?[\d,]+(?:\.\d+)?%?", line):
            continue
        labels.append(normalize_effect_label(line))
    return labels


def value_for_input(effect: dict[str, Any]) -> str:
    value_text = str(effect.get("value_text", ""))
    if value_text:
        return value_text
    value = effect.get("value")
    if value is None:
        return ""
    return str(value).replace(",", "").rstrip("%")


def should_skip_mirpg_effect(effect: dict[str, Any], website: dict[str, Any]) -> bool:
    if not website.get("skip_known_unsupported_effects", True):
        return False
    label = normalize_effect_label(str(effect.get("name", "")))
    return label in MIRPG_KNOWN_UNSUPPORTED_EFFECTS


def skipped_mirpg_effect(effect: dict[str, Any]) -> dict[str, Any]:
    skipped = dict(effect)
    label = normalize_effect_label(str(effect.get("name", "")))
    skipped["reason"] = MIRPG_KNOWN_UNSUPPORTED_EFFECTS.get(label, "Unsupported by MIRPG Optimizer.")
    return skipped


def pop_first_effect(effects: list[dict[str, Any]], label: str) -> dict[str, Any] | None:
    normalized = normalize_effect_label(label)
    for index, effect in enumerate(effects):
        if normalize_effect_label(str(effect.get("name", ""))) == normalized:
            return effects.pop(index)
    return None


def fill_mirpg_main_options(editor: Any, effects: list[dict[str, Any]]) -> None:
    main_labels = extract_main_option_labels(editor.inner_text())
    inputs = editor.locator("input")
    input_count = inputs.count()

    for label_index, label in enumerate(main_labels):
        effect = pop_first_effect(effects, label)
        if not effect:
            continue

        input_index = 1 + label_index
        if input_index >= input_count:
            raise AutomationError(f"Could not find input for main option: {label}")
        inputs.nth(input_index).fill(value_for_input(effect))


def select_sub_option(select_locator: Any, effect: dict[str, Any]) -> None:
    label = str(effect.get("name", "")).strip()
    option_id = str(effect.get("mirpg_option_id", "")).strip()
    try:
        select_locator.select_option(label=label)
    except Exception as exc:
        if option_id:
            try:
                select_locator.select_option(value=option_id)
                return
            except Exception:
                pass
        raise AutomationError(f"Dropdown option not found for On-Equip Effect: {label}") from exc


def ensure_sub_option_row(editor: Any, sub_index: int, website: dict[str, Any]) -> None:
    reuse_empty = bool(website.get("reuse_empty_sub_option_row", True))
    selects = editor.locator("select")
    if reuse_empty and sub_index < selects.count():
        return

    add_button = editor.get_by_role("button", name=re.compile(r"\+\s*Add", re.IGNORECASE))
    if not click_if_visible(add_button, timeout_ms=1000):
        add_button = editor.get_by_text(re.compile(r"\+\s*Add", re.IGNORECASE))
        if not click_if_visible(add_button, timeout_ms=1000):
            raise AutomationError("Could not click + Add for a MIRPG sub-option row.")


def fill_mirpg_sub_options(editor: Any, effects: list[dict[str, Any]], website: dict[str, Any]) -> list[dict[str, Any]]:
    main_count = len(extract_main_option_labels(editor.inner_text()))
    skipped_effects: list[dict[str, Any]] = []
    sub_index = 0
    for effect in effects:
        if should_skip_mirpg_effect(effect, website):
            skipped_effects.append(skipped_mirpg_effect(effect))
            continue

        ensure_sub_option_row(editor, sub_index, website)
        selects = editor.locator("select")
        if sub_index >= selects.count():
            raise AutomationError(f"Could not find select for sub-option row {sub_index + 1}.")

        select_locator = selects.nth(sub_index)
        select_sub_option(select_locator, effect)

        inputs = editor.locator("input")
        input_index = 1 + main_count + sub_index
        if input_index >= inputs.count():
            raise AutomationError(f"Could not find value input for sub-option: {effect.get('name')}")
        inputs.nth(input_index).fill(value_for_input(effect))
        sub_index += 1

    return skipped_effects


def submit_to_mirpg_optimizer(page: Any, parsed: dict[str, Any], config: dict[str, Any]) -> None:
    website = config.get("website", {})
    effects = [dict(effect) for effect in parsed.get("on_equip_effects", [])]
    equipment_name = build_equipment_name(parsed) or parsed.get("equipment_name")
    if not equipment_name:
        raise AutomationError("Parsed item does not have an equipment_name.")
    if not effects:
        raise AutomationError(f"No On-Equip Effect rows parsed for {equipment_name}.")

    slot = require_mirpg_equipment_slot(parsed, website)
    if slot and website.get("select_slot_tab", True):
        select_mirpg_top_level_slot(page, str(slot), website)

    if not website.get("use_current_editor", False):
        add_button = page.get_by_role("button", name=re.compile(r"\+\s*Add Equipment", re.IGNORECASE))
        if not click_if_visible(add_button, timeout_ms=2000):
            raise AutomationError("Could not click + Add Equipment.")
        page.wait_for_timeout(int(float(website.get("wait_after_add_equipment_seconds", 0.5)) * 1000))
        if slot:
            menu_choice = page.get_by_role("menuitem", name=re.compile(rf"^{re.escape(str(slot))}$", re.IGNORECASE))
            click_if_visible(menu_choice, timeout_ms=500)

    editor = active_mirpg_editor(page, website)
    editor.locator("input").first.fill(str(equipment_name))

    fill_mirpg_main_options(editor, effects)
    skipped_effects = fill_mirpg_sub_options(editor, effects, website)

    parsed["unresolved_effects"] = skipped_effects
    wait_after_submit = float(website.get("wait_after_submit_seconds", 0.5))
    if wait_after_submit > 0:
        page.wait_for_timeout(int(wait_after_submit * 1000))


def submit_to_website(page: Any, parsed: dict[str, Any], config: dict[str, Any]) -> None:
    website = config.get("website", {})
    if website.get("automation_mode") == "mirpg_optimizer":
        submit_to_mirpg_optimizer(page, parsed, config)
        return

    field_selectors = website.get("field_selectors", {})
    if not isinstance(field_selectors, dict):
        raise AutomationError("website.field_selectors must be an object.")

    before_each = website.get("before_each_selector")
    if before_each:
        page.locator(str(before_each)).click()

    for name, selector_config in field_selectors.items():
        value = parsed.get(name)
        if website.get("skip_empty_fields", True) and is_empty_value(value):
            continue
        fill_selector(page, selector_config, value)

    submit_selector = website.get("submit_selector")
    if submit_selector:
        page.locator(str(submit_selector)).click()

    wait_after_submit = float(website.get("wait_after_submit_seconds", 0.5))
    if wait_after_submit > 0:
        page.wait_for_timeout(int(wait_after_submit * 1000))


def adb_prefix(config: dict[str, Any]) -> list[str]:
    capture = config.get("capture", {})
    adb_path = capture.get("adb_path", "adb")
    serial = capture.get("serial")
    args = [adb_path]
    if serial:
        args.extend(["-s", str(serial)])
    return args


def adb_connect_target(config: dict[str, Any]) -> str | None:
    capture = config.get("capture", {})
    connect = capture.get("connect")
    if connect:
        return str(connect)

    serial = capture.get("serial")
    if serial and ":" in str(serial):
        return str(serial)
    return None


def reconnect_adb(config: dict[str, Any]) -> None:
    capture = config.get("capture", {})
    advance = config.get("advance", {})
    target = adb_connect_target(config)
    if not target:
        return
    adb_path = str(capture.get("adb_path", "adb"))
    timeout = float(capture.get("timeout_seconds", 20))

    if advance.get("adb_disconnect_before_reconnect", True):
        try:
            run_command([adb_path, "disconnect", target], timeout=timeout)
        except AutomationError:
            pass
        time.sleep(float(advance.get("adb_reconnect_delay_seconds", 0.5)))

    run_command([adb_path, "connect", target], timeout=timeout)


def restart_adb_server(config: dict[str, Any]) -> None:
    capture = config.get("capture", {})
    adb_path = str(capture.get("adb_path", "adb"))
    timeout = float(capture.get("timeout_seconds", 20))
    run_command([adb_path, "kill-server"], timeout=timeout)
    run_command([adb_path, "start-server"], timeout=timeout)
    reconnect_adb(config)


def retryable_adb_error(error: Exception) -> bool:
    message = str(error).lower()
    retryable_fragments = (
        "closed",
        "device offline",
        "offline",
        "no devices/emulators found",
        "device not found",
        "failed to connect",
        "connection refused",
    )
    return any(fragment in message for fragment in retryable_fragments)


def adb_input_command(config: dict[str, Any], input_args: list[str], transport: str) -> list[str]:
    if transport == "shell":
        return adb_prefix(config) + ["shell", "input", *input_args]
    if transport == "exec-out":
        return adb_prefix(config) + ["exec-out", "input", *input_args]
    raise AutomationError("advance.adb_input_transport must be one of: shell, exec-out, auto")


def run_adb_input(config: dict[str, Any], input_args: list[str]) -> None:
    advance = config.get("advance", {})
    attempts = int(advance.get("adb_retries", 2)) + 1
    retry_delay = float(advance.get("adb_retry_delay_seconds", 0.8))
    restart_after = int(advance.get("adb_restart_server_after_retry", 2))
    transport = str(advance.get("adb_input_transport", "shell"))
    transports = ["shell", "exec-out"] if transport == "auto" else [transport]
    last_error: AutomationError | None = None

    for attempt in range(attempts):
        if attempt > 0:
            if advance.get("adb_restart_server_on_retry", True) and attempt >= restart_after:
                restart_adb_server(config)
            else:
                reconnect_adb(config)
            if retry_delay > 0:
                time.sleep(retry_delay)

        for input_transport in transports:
            try:
                run_command(adb_input_command(config, input_args, input_transport), timeout=10)
                return
            except AutomationError as exc:
                last_error = exc
                if transport != "auto" or not retryable_adb_error(exc):
                    if attempt >= attempts - 1 or not retryable_adb_error(exc):
                        raise
                continue

    if last_error:
        raise last_error


def desktop_point(config: dict[str, Any], x: Any, y: Any) -> tuple[int, int]:
    advance = config.get("advance", {})
    offset = advance.get("desktop_coordinate_offset", [0, 0])
    scale = advance.get("desktop_coordinate_scale", 1.0)

    if not isinstance(offset, list) or len(offset) != 2:
        raise AutomationError("advance.desktop_coordinate_offset must be [x, y].")
    if isinstance(scale, list):
        if len(scale) != 2:
            raise AutomationError("advance.desktop_coordinate_scale must be a number or [x_scale, y_scale].")
        scale_x, scale_y = float(scale[0]), float(scale[1])
    else:
        scale_x = scale_y = float(scale)

    return (
        int(round(float(offset[0]) + float(x) * scale_x)),
        int(round(float(offset[1]) + float(y) * scale_y)),
    )


def desktop_scale(config: dict[str, Any]) -> tuple[float, float]:
    advance = config.get("advance", {})
    scale = advance.get("desktop_coordinate_scale", 1.0)
    if isinstance(scale, list):
        if len(scale) != 2:
            raise AutomationError("advance.desktop_coordinate_scale must be a number or [x_scale, y_scale].")
        return float(scale[0]), float(scale[1])
    return float(scale), float(scale)


def set_desktop_offset_from_screen_point(
    config: dict[str, Any],
    game_x: float,
    game_y: float,
    screen_x: float,
    screen_y: float,
) -> tuple[int, int]:
    scale_x, scale_y = desktop_scale(config)
    offset_x = int(round(screen_x - game_x * scale_x))
    offset_y = int(round(screen_y - game_y * scale_y))
    config.setdefault("advance", {})["desktop_coordinate_offset"] = [offset_x, offset_y]
    return offset_x, offset_y


def set_desktop_transform_from_screen_points(
    config: dict[str, Any],
    game_a: tuple[float, float],
    screen_a: tuple[float, float],
    game_b: tuple[float, float],
    screen_b: tuple[float, float],
    uniform_scale: bool = True,
) -> tuple[tuple[float, float], tuple[int, int]]:
    game_dx = float(game_b[0]) - float(game_a[0])
    game_dy = float(game_b[1]) - float(game_a[1])
    screen_dx = float(screen_b[0]) - float(screen_a[0])
    screen_dy = float(screen_b[1]) - float(screen_a[1])

    if abs(game_dx) < 1 and abs(game_dy) < 1:
        raise AutomationError("Detected calibration points are too close together to calculate desktop scale.")

    current_scale_x, current_scale_y = desktop_scale(config)
    scale_x = current_scale_x
    scale_y = current_scale_y

    if uniform_scale:
        if abs(game_dx) >= abs(game_dy) and abs(game_dx) >= 1:
            scale_x = scale_y = screen_dx / game_dx
        elif abs(game_dy) >= 1:
            scale_x = scale_y = screen_dy / game_dy
    else:
        if abs(game_dx) >= 1:
            scale_x = screen_dx / game_dx
        if abs(game_dy) >= 1:
            scale_y = screen_dy / game_dy

    if scale_x <= 0 or scale_y <= 0:
        raise AutomationError(
            "Calculated desktop coordinate scale was not positive. "
            "Make sure you hover the detected items in the prompted order."
        )

    offset_x = int(round(float(screen_a[0]) - float(game_a[0]) * scale_x))
    offset_y = int(round(float(screen_a[1]) - float(game_a[1]) * scale_y))
    advance = config.setdefault("advance", {})
    advance["desktop_coordinate_scale"] = [scale_x, scale_y]
    advance["desktop_coordinate_offset"] = [offset_x, offset_y]
    return (scale_x, scale_y), (offset_x, offset_y)


def set_desktop_transform_from_screen_samples(
    config: dict[str, Any],
    samples: list[tuple[tuple[float, float], tuple[float, float]]],
    uniform_scale: bool = False,
) -> tuple[tuple[float, float], tuple[int, int]]:
    if len(samples) < 2:
        raise AutomationError("At least two calibration samples are required.")

    current_scale_x, current_scale_y = desktop_scale(config)
    x_ratios: list[float] = []
    y_ratios: list[float] = []

    for index, (game_a, screen_a) in enumerate(samples):
        for game_b, screen_b in samples[index + 1 :]:
            game_dx = float(game_b[0]) - float(game_a[0])
            game_dy = float(game_b[1]) - float(game_a[1])
            screen_dx = float(screen_b[0]) - float(screen_a[0])
            screen_dy = float(screen_b[1]) - float(screen_a[1])

            if abs(game_dx) >= 1 and abs(game_dx) >= abs(game_dy):
                ratio = screen_dx / game_dx
                if ratio > 0:
                    x_ratios.append(ratio)
            if abs(game_dy) >= 1 and abs(game_dy) >= abs(game_dx):
                ratio = screen_dy / game_dy
                if ratio > 0:
                    y_ratios.append(ratio)

    if uniform_scale:
        ratios = x_ratios + y_ratios
        if not ratios:
            raise AutomationError("Calibration points are too close together to calculate desktop scale.")
        scale_x = scale_y = median_number(ratios)
    else:
        scale_x = median_number(x_ratios) if x_ratios else current_scale_x
        scale_y = median_number(y_ratios) if y_ratios else current_scale_y

    if scale_x <= 0 or scale_y <= 0:
        raise AutomationError(
            "Calculated desktop coordinate scale was not positive. "
            "Make sure you hover calibration items in the prompted order."
        )

    offset_x = int(round(median_number([screen[0] - game[0] * scale_x for game, screen in samples])))
    offset_y = int(round(median_number([screen[1] - game[1] * scale_y for game, screen in samples])))
    advance = config.setdefault("advance", {})
    advance["desktop_coordinate_scale"] = [scale_x, scale_y]
    advance["desktop_coordinate_offset"] = [offset_x, offset_y]
    return (scale_x, scale_y), (offset_x, offset_y)


def first_item_grid_position(config: dict[str, Any]) -> tuple[float, float]:
    advance = config.get("advance", {})
    positions = advance.get("item_positions", [])
    if not positions:
        raise AutomationError("advance.item_positions must include the first visible item to calibrate.")

    index = int(advance.get("calibration_item_position_index", 0))
    if index < 0 or index >= len(positions):
        raise AutomationError("advance.calibration_item_position_index is outside advance.item_positions.")

    position = positions[index]
    if not isinstance(position, list) or len(position) != 2:
        raise AutomationError("advance.item_positions entries must be [x, y].")
    return float(position[0]), float(position[1])


def runtime_calibrate_first_item(config: dict[str, Any], delay_seconds: float | None = None) -> tuple[int, int]:
    if advance_input_backend(config) != "desktop":
        raise AutomationError("First-item runtime calibration only works with advance.input_backend='desktop'.")

    pyautogui = require_package("pyautogui")
    advance = config.get("advance", {})
    delay = float(delay_seconds if delay_seconds is not None else advance.get("runtime_calibration_delay_seconds", 3.0))
    game_x, game_y = first_item_grid_position(config)

    print("Hover over the center of the first equipment item to process.")
    if delay > 0:
        print(f"Reading mouse position in {delay:g}s...")
        time.sleep(delay)

    mouse_x, mouse_y = pyautogui.position()
    offset = set_desktop_offset_from_screen_point(config, game_x, game_y, mouse_x, mouse_y)
    print(f"Calibrated first item: game=({int(game_x)}, {int(game_y)}) screen=({mouse_x}, {mouse_y})")
    print(f"Runtime desktop offset: [{offset[0]}, {offset[1]}]")
    return offset


def describe_advance_action(config: dict[str, Any], action: dict[str, Any]) -> str:
    action_type = action.get("type")
    if action_type in {"click", "scroll"} and action.get("x") is not None and action.get("y") is not None:
        screen_x, screen_y = desktop_point(config, action["x"], action["y"])
        return f"{action_type} game=({action['x']}, {action['y']}) screen=({screen_x}, {screen_y})"
    if action_type == "drag":
        from_x, from_y = desktop_point(config, action["from_x"], action["from_y"])
        to_x, to_y = desktop_point(config, action["to_x"], action["to_y"])
        return (
            f"drag game=({action['from_x']}, {action['from_y']}) -> ({action['to_x']}, {action['to_y']}) "
            f"screen=({from_x}, {from_y}) -> ({to_x}, {to_y})"
        )
    if action_type == "adb_tap":
        return f"adb_tap ({action['x']}, {action['y']})"
    if action_type == "adb_swipe":
        return (
            f"adb_swipe ({action['from_x']}, {action['from_y']}) -> "
            f"({action['to_x']}, {action['to_y']})"
        )
    if action_type == "wait":
        return f"wait {action.get('seconds', 0.5)}s"
    if action_type == "hotkey":
        return f"hotkey {'+'.join(str(key) for key in action.get('keys', []))}"
    return json.dumps(action, ensure_ascii=False)


def advance_input_backend(config: dict[str, Any]) -> str:
    advance = config.get("advance", {})
    backend = str(advance.get("input_backend") or ("adb" if config.get("capture", {}).get("backend", "adb") == "adb" else "desktop"))
    if backend not in {"adb", "desktop"}:
        raise AutomationError("advance.input_backend must be one of: adb, desktop")
    return backend


def action_for_input_backend(action: dict[str, Any], backend: str) -> dict[str, Any]:
    converted = dict(action)
    action_type = converted.get("type")
    if backend == "adb":
        return converted

    if action_type == "adb_tap":
        converted["type"] = "click"
    elif action_type == "adb_swipe":
        converted["type"] = "drag"
        converted["duration_seconds"] = float(converted.get("duration_ms", 400)) / 1000.0
    return converted


def safe_filename_part(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return cleaned.strip("_")[:80] or "snapshot"


@dataclasses.dataclass
class MovementSnapshotter:
    debug_dir: Path
    config: dict[str, Any]
    index: int = 0
    entries: list[dict[str, Any]] = dataclasses.field(default_factory=list)

    @property
    def snapshot_config(self) -> dict[str, Any]:
        return self.config.get("advance", {}).get("movement_snapshots", {})

    def click_hold_seconds(self) -> float:
        return float(self.snapshot_config.get("click_hold_seconds", 0.16))

    def after_action_delay_seconds(self) -> float:
        return float(self.snapshot_config.get("after_action_delay_seconds", 0.04))

    def capture_wait_actions(self) -> bool:
        return bool(self.snapshot_config.get("capture_wait_actions", True))

    def capture_before_click(self) -> bool:
        return bool(self.snapshot_config.get("capture_before_click", True))

    def click_capture_mode(self) -> str:
        mode = str(self.snapshot_config.get("click_capture_mode", "after_click"))
        if mode not in {"after_click", "hold"}:
            raise AutomationError("advance.movement_snapshots.click_capture_mode must be one of: after_click, hold")
        return mode

    def capture(self, phase: str, action: dict[str, Any], label: str) -> Path:
        delay = self.after_action_delay_seconds()
        if phase in {"before_click", "click_down"}:
            delay = 0.0
        if delay > 0:
            time.sleep(delay)

        self.debug_dir.mkdir(parents=True, exist_ok=True)
        self.index += 1
        stem = f"{self.index:04d}_{safe_filename_part(label)}_{safe_filename_part(phase)}"
        image_path = self.debug_dir / f"{stem}.png"
        image, origin, coordinate_space = movement_snapshot_source(self.config)
        image.save(image_path)

        entry = {
            "index": self.index,
            "phase": phase,
            "label": label,
            "action": action,
            "description": describe_advance_action(self.config, action),
            "image": str(image_path),
            "image_size": list(image.size),
            "snapshot_origin": [origin[0], origin[1]],
            "snapshot_coordinate_space": coordinate_space,
            "timestamp": dt.datetime.now().isoformat(timespec="milliseconds"),
        }
        try:
            pyautogui = require_package("pyautogui")
            mouse_x, mouse_y = pyautogui.position()
            entry["mouse_position"] = [mouse_x, mouse_y]
        except AutomationError:
            pass

        self.entries.append(entry)
        return image_path

    def write_manifest(self) -> Path:
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = self.debug_dir / "manifest.json"
        manifest_path.write_text(json.dumps(self.entries, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return manifest_path


def run_pyautogui_action(
    config: dict[str, Any],
    action: dict[str, Any],
    snapshotter: MovementSnapshotter | None = None,
    label: str = "action",
) -> None:
    pyautogui = require_package("pyautogui")
    advance = config.get("advance", {})
    move_duration = float(advance.get("desktop_move_before_click_seconds", 0.15))
    action_type = action.get("type")
    if action_type == "click":
        x, y = desktop_point(config, action["x"], action["y"])
        if move_duration > 0:
            pyautogui.moveTo(x, y, duration=move_duration)
        if snapshotter is not None:
            if snapshotter.capture_before_click():
                snapshotter.capture("before_click", action, label)
            if snapshotter.click_capture_mode() == "hold":
                pyautogui.mouseDown(x, y, button="left")
                try:
                    hold_seconds = snapshotter.click_hold_seconds()
                    if hold_seconds > 0:
                        time.sleep(hold_seconds)
                    snapshotter.capture("click_down", action, label)
                finally:
                    pyautogui.mouseUp(x, y, button="left")
            else:
                pyautogui.click(x, y)
                snapshotter.capture("after_click", action, label)
        else:
            pyautogui.click(x, y)
    elif action_type == "scroll":
        if action.get("x") is not None and action.get("y") is not None:
            x, y = desktop_point(config, action["x"], action["y"])
            pyautogui.scroll(int(action["amount"]), x=x, y=y)
        else:
            pyautogui.scroll(int(action["amount"]))
        if snapshotter is not None:
            snapshotter.capture("after_scroll", action, label)
    elif action_type == "drag":
        from_x, from_y = desktop_point(config, action["from_x"], action["from_y"])
        to_x, to_y = desktop_point(config, action["to_x"], action["to_y"])
        duration = float(action.get("duration_seconds", 0.4))
        hold_before = float(action.get("hold_seconds_before", 0.0))
        pause_before_release = float(action.get("pause_before_release_seconds", 0.0))
        steps = int(action.get("steps", 0))
        if hold_before > 0 or pause_before_release > 0 or steps > 0:
            pyautogui.moveTo(from_x, from_y)
            pyautogui.mouseDown(button="left")
            try:
                if hold_before > 0:
                    time.sleep(hold_before)
                if steps > 0:
                    step_duration = duration / steps if duration > 0 else 0
                    for step in range(1, steps + 1):
                        progress = step / steps
                        x = int(round(from_x + (to_x - from_x) * progress))
                        y = int(round(from_y + (to_y - from_y) * progress))
                        pyautogui.moveTo(x, y, duration=step_duration)
                else:
                    pyautogui.moveTo(to_x, to_y, duration=duration)
                if pause_before_release > 0:
                    time.sleep(pause_before_release)
            finally:
                pyautogui.mouseUp(button="left")
        else:
            pyautogui.moveTo(from_x, from_y)
            pyautogui.dragTo(
                to_x,
                to_y,
                duration=duration,
                button="left",
            )
        if snapshotter is not None:
            snapshotter.capture("after_drag", action, label)
    elif action_type == "hotkey":
        pyautogui.hotkey(*list(action["keys"]))
        if snapshotter is not None:
            snapshotter.capture("after_hotkey", action, label)
    else:
        raise AutomationError(f"Unsupported pyautogui action type: {action_type}")


def run_advance_action(
    config: dict[str, Any],
    action: dict[str, Any],
    snapshotter: MovementSnapshotter | None = None,
    label: str = "action",
) -> None:
    action_type = action.get("type")
    if not action_type:
        raise AutomationError("Advance actions require a type.")

    if action_type == "wait":
        time.sleep(float(action.get("seconds", 0.5)))
        if snapshotter is not None and snapshotter.capture_wait_actions():
            snapshotter.capture("after_wait", action, label)
    elif action_type == "adb_tap":
        if snapshotter is not None and snapshotter.capture_before_click():
            snapshotter.capture("before_adb_tap", action, label)
        run_adb_input(config, ["tap", str(int(action["x"])), str(int(action["y"]))])
        if snapshotter is not None:
            snapshotter.capture("after_adb_tap", action, label)
    elif action_type == "adb_swipe":
        duration_ms = int(action.get("duration_ms", 400))
        run_adb_input(
            config,
            [
                "swipe",
                str(int(action["from_x"])),
                str(int(action["from_y"])),
                str(int(action["to_x"])),
                str(int(action["to_y"])),
                str(duration_ms),
            ],
        )
        if snapshotter is not None:
            snapshotter.capture("after_adb_swipe", action, label)
    elif action_type in {"click", "scroll", "drag", "hotkey"}:
        run_pyautogui_action(config, action, snapshotter, label)
    else:
        raise AutomationError(f"Unsupported advance action type: {action_type}")


def wait_after_advance_action_group(config: dict[str, Any]) -> None:
    wait_after_advance = float(config.get("advance", {}).get("wait_after_advance_seconds", 0.8))
    if wait_after_advance > 0:
        time.sleep(wait_after_advance)


def run_advance_action_group(
    config: dict[str, Any],
    actions: list[dict[str, Any]],
    snapshotter: MovementSnapshotter | None = None,
    label_prefix: str = "step",
) -> None:
    for action_index, action in enumerate(actions, start=1):
        if not isinstance(action, dict):
            raise AutomationError("Every advance action must be an object.")
        label = f"{label_prefix}_action_{action_index}"
        run_advance_action(config, action, snapshotter, label)
    wait_after_advance_action_group(config)


def item_grid_click_action(config: dict[str, Any], position: Any) -> dict[str, Any]:
    if not isinstance(position, list) or len(position) != 2:
        raise AutomationError("advance.item_positions entries must be [x, y].")
    backend = advance_input_backend(config)
    action_type = "adb_tap" if backend == "adb" else "click"
    return {"type": action_type, "x": position[0], "y": position[1]}


def first_item_grid_click_actions(config: dict[str, Any]) -> list[dict[str, Any]]:
    advance = config.get("advance", {})
    positions = advance.get("item_positions", [])
    if not positions:
        return []
    index = int(advance.get("calibration_item_position_index", 0))
    if index < 0 or index >= len(positions):
        raise AutomationError("advance.calibration_item_position_index is outside advance.item_positions.")
    return [item_grid_click_action(config, positions[index])]


def item_grid_actions(config: dict[str, Any], completed_index: int) -> list[dict[str, Any]]:
    advance = config.get("advance", {})
    positions = advance.get("item_positions", [])
    if not positions:
        return []
    backend = advance_input_backend(config)

    next_position_index = completed_index + 1

    actions: list[dict[str, Any]] = []
    if next_position_index < len(positions):
        actions.append(item_grid_click_action(config, positions[next_position_index]))
    else:
        actions.extend(action_for_input_backend(action, backend) for action in advance.get("scroll_actions", []))
        reset_position = advance.get("reset_position_after_scroll")
        if reset_position:
            actions.append(item_grid_click_action(config, reset_position))

    return actions


def advance_to_next_equipment(config: dict[str, Any], completed_index: int) -> None:
    advance = config.get("advance", {})
    mode = advance.get("mode", "none")
    if mode == "none":
        return

    if mode == "fixed_actions":
        actions = advance.get("actions", [])
    elif mode == "item_grid":
        actions = item_grid_actions(config, completed_index)
    else:
        raise AutomationError("advance.mode must be one of: none, fixed_actions, item_grid")

    if not isinstance(actions, list):
        raise AutomationError("advance actions must be a list.")

    run_advance_action_group(config, actions)


def preview_advance_actions(config: dict[str, Any], completed_index: int) -> list[dict[str, Any]]:
    advance = config.get("advance", {})
    mode = advance.get("mode", "none")
    if mode == "none":
        return []
    if mode == "fixed_actions":
        actions = advance.get("actions", [])
    elif mode == "item_grid":
        actions = item_grid_actions(config, completed_index)
    else:
        raise AutomationError("advance.mode must be one of: none, fixed_actions, item_grid")
    if not isinstance(actions, list):
        raise AutomationError("advance actions must be a list.")
    return actions


def tap_scroll_test_steps(config: dict[str, Any], item_count: int) -> list[list[dict[str, Any]]]:
    if item_count < 1:
        raise AutomationError("--tap-scroll-test must be at least 1.")

    steps = [first_item_grid_click_actions(config)]
    for completed_index in range(item_count - 1):
        steps.append(item_grid_actions(config, completed_index))
    return steps


def run_tap_scroll_test(
    config: dict[str, Any],
    item_count: int,
    snapshotter: MovementSnapshotter | None = None,
) -> None:
    steps = tap_scroll_test_steps(config, item_count)
    if not steps or not any(steps):
        print("No tap/scroll actions configured.")
        return

    print(f"Tap/scroll test items: {item_count}")
    for step_index, actions in enumerate(steps, start=1):
        if not actions:
            print(f"Step {step_index}: no actions")
            continue
        print(f"Step {step_index}:")
        for action in actions:
            print(f"  {describe_advance_action(config, action)}")
        run_advance_action_group(config, actions, snapshotter, f"tap_scroll_step_{step_index:04d}")


def item_grid_page_positions(config: dict[str, Any], page_index: int = 0) -> list[Any]:
    advance = config.get("advance", {})
    if page_index > 0 and advance.get("post_scroll_item_positions"):
        positions = advance.get("post_scroll_item_positions", [])
    else:
        positions = advance.get("item_positions", [])
    if not isinstance(positions, list):
        raise AutomationError("advance item positions must be a list.")
    return positions


def item_grid_page_click_steps(config: dict[str, Any], page_index: int = 0) -> list[list[dict[str, Any]]]:
    positions = item_grid_page_positions(config, page_index)
    if not positions:
        return []
    return [[item_grid_click_action(config, position)] for position in positions]


def page_scroll_actions(config: dict[str, Any]) -> list[dict[str, Any]]:
    advance = config.get("advance", {})
    backend = advance_input_backend(config)
    scroll_actions = advance.get("page_scroll_actions", advance.get("scroll_actions", []))
    if not isinstance(scroll_actions, list):
        raise AutomationError("advance.page_scroll_actions must be a list.")
    return [action_for_input_backend(action, backend) for action in scroll_actions]


def detected_items_config(config: dict[str, Any]) -> dict[str, Any]:
    return config.get("advance", {}).get("detect_items", {})


def detected_items_input_backend(config: dict[str, Any]) -> str:
    detect_config = detected_items_config(config)
    backend = str(detect_config.get("input_backend", advance_input_backend(config)))
    if backend not in {"adb", "desktop"}:
        raise AutomationError("advance.detect_items.input_backend must be one of: adb, desktop")
    return backend


def detected_item_action(config: dict[str, Any], center: tuple[float, float]) -> dict[str, Any]:
    x = int(round(center[0]))
    y = int(round(center[1]))
    action_type = "adb_tap" if detected_items_input_backend(config) == "adb" else "click"
    return {"type": action_type, "x": x, "y": y}


def detected_page_scroll_actions(config: dict[str, Any], mode: str = "page") -> list[dict[str, Any]]:
    detect_config = detected_items_config(config)
    backend = detected_items_input_backend(config)
    if mode == "all":
        scroll_actions = detect_config.get("all_items_scroll_actions", detect_config.get("page_scroll_actions", []))
    else:
        scroll_actions = detect_config.get("page_scroll_actions", config.get("advance", {}).get("page_scroll_actions", []))
    if not isinstance(scroll_actions, list):
        raise AutomationError("advance.detect_items scroll actions must be a list.")
    return [action_for_input_backend(action, backend) for action in scroll_actions]


def equipped_slot_positions(config: dict[str, Any]) -> list[dict[str, Any]]:
    positions = config.get("advance", {}).get("equipped_slot_positions", [])
    if not isinstance(positions, list):
        raise AutomationError("advance.equipped_slot_positions must be a list.")

    parsed: list[dict[str, Any]] = []
    for index, position in enumerate(positions, start=1):
        if isinstance(position, dict):
            if position.get("skip"):
                continue
            if "x" not in position or "y" not in position:
                raise AutomationError("Every advance.equipped_slot_positions object must include x and y.")
            label = str(position.get("label") or f"slot_{index:02d}")
            parsed.append({"label": label, "x": float(position["x"]), "y": float(position["y"])})
        elif isinstance(position, list) and len(position) in {2, 3}:
            label = str(position[2]) if len(position) == 3 else f"slot_{index:02d}"
            parsed.append({"label": label, "x": float(position[0]), "y": float(position[1])})
        else:
            raise AutomationError("advance.equipped_slot_positions entries must be objects or [x, y, label?].")
    return parsed


def equipped_slot_action(config: dict[str, Any], slot: dict[str, Any]) -> dict[str, Any]:
    action_type = "adb_tap" if advance_input_backend(config) == "adb" else "click"
    return {"type": action_type, "x": int(round(float(slot["x"]))), "y": int(round(float(slot["y"])))}


def equipped_slot_detection_config(config: dict[str, Any]) -> dict[str, Any]:
    advance = config.get("advance", {})
    detect_config = dict(detected_items_config(config))
    custom = advance.get("equipped_slot_detection", {})
    if isinstance(custom, dict):
        detect_config.update(custom)
    detect_config.setdefault("region", [40, 40, 920, 1240])
    detect_config.setdefault("max_items_per_page", len(advance.get("equipped_slot_positions", [])) or 12)
    return detect_config


def detect_equipped_slot_boxes(image: Any, config: dict[str, Any]) -> list[dict[str, Any]]:
    temp_config = dict(config)
    temp_advance = dict(config.get("advance", {}))
    temp_advance["detect_items"] = equipped_slot_detection_config(config)
    temp_config["advance"] = temp_advance
    return detect_visible_item_boxes(image, temp_config)


def detect_nearest_equipped_slot_box(
    image: Any,
    slot: dict[str, Any],
    config: dict[str, Any],
) -> tuple[dict[str, Any] | None, int]:
    boxes = detect_equipped_slot_boxes(image, config)
    if not boxes:
        return None, 0
    signed_boxes, _row_centers = prepare_manual_detected_viewport(boxes, config)
    slot_x = float(slot["x"])
    slot_y = float(slot["y"])
    return min(
        signed_boxes,
        key=lambda box: (
            (float(box["center"][0]) - slot_x) ** 2 + (float(box["center"][1]) - slot_y) ** 2,
            float(box["center"][1]),
            float(box["center"][0]),
        ),
    ), len(boxes)


def apply_equipped_slot_offset(slot: dict[str, Any], offset: tuple[float, float]) -> dict[str, Any]:
    shifted = dict(slot)
    shifted["x"] = float(slot["x"]) + float(offset[0])
    shifted["y"] = float(slot["y"]) + float(offset[1])
    return shifted


def equipped_slot_manual_click_delay(config: dict[str, Any]) -> float:
    advance = config.get("advance", {})
    detect_config = detected_items_config(config)
    return float(
        advance.get(
            "equipped_slot_manual_click_delay_seconds",
            detect_config.get("manual_page_click_delay_seconds", 0.75),
        )
    )


def estimate_equipped_slot_offset_from_manual_click(
    before_image: Any,
    after_image: Any,
    slot: dict[str, Any],
    config: dict[str, Any],
) -> tuple[tuple[float, float], dict[str, Any] | None]:
    detected_box, detected_count = detect_nearest_equipped_slot_box(after_image, slot, config)
    if detected_box is not None:
        detected_center = detected_box["center"]
        offset = (
            float(detected_center[0]) - float(slot["x"]),
            float(detected_center[1]) - float(slot["y"]),
        )
        detection = {
            "method": "equipped_slot_box_detection",
            "center": [round(float(detected_center[0]), 1), round(float(detected_center[1]), 1)],
            "bbox": detected_box.get("bbox"),
            "detected_label": detected_item_box_grid_label(detected_box),
            "detected_cards": detected_count,
            "nearest_to_configured_slot": True,
        }
        max_offset = float(config.get("advance", {}).get("equipped_slot_manual_max_offset_px", 120))
        if abs(offset[0]) > max_offset or abs(offset[1]) > max_offset:
            detection["offset_rejected"] = True
            detection["rejected_offset"] = [round(offset[0], 1), round(offset[1], 1)]
            return (0.0, 0.0), detection
        return offset, detection

    detection = find_changed_region_between_images(
        before_image,
        after_image,
        float(slot["x"]),
        float(slot["y"]),
        config,
    )
    if detection is None:
        return (0.0, 0.0), None

    detection["method"] = "changed_region"
    detected_center = detection["center"]
    offset = (
        float(detected_center[0]) - float(slot["x"]),
        float(detected_center[1]) - float(slot["y"]),
    )
    max_offset = float(config.get("advance", {}).get("equipped_slot_manual_max_offset_px", 120))
    if abs(offset[0]) > max_offset or abs(offset[1]) > max_offset:
        detection["offset_rejected"] = True
        detection["rejected_offset"] = [round(offset[0], 1), round(offset[1], 1)]
        return (0.0, 0.0), detection
    return offset, detection


def detected_verified_scroll_actions(config: dict[str, Any]) -> list[dict[str, Any]]:
    detect_config = detected_items_config(config)
    backend = detected_items_input_backend(config)
    scroll_actions = detect_config.get("verified_scroll_actions", detect_config.get("all_items_scroll_actions", []))
    if not isinstance(scroll_actions, list):
        raise AutomationError("advance.detect_items.verified_scroll_actions must be a list.")
    return [action_for_input_backend(action, backend) for action in scroll_actions]


def detected_row_snap_action(config: dict[str, Any], delta_y: float) -> dict[str, Any]:
    detect_config = detected_items_config(config)
    region = parse_rect(detect_config.get("region"), "advance.detect_items.region")
    if region is None:
        raise AutomationError("advance.detect_items.region is required for detected row snapping.")

    left, top, width, height = region
    anchor = detect_config.get("row_snap_anchor")
    if isinstance(anchor, list) and len(anchor) == 2:
        anchor_x = float(anchor[0])
        anchor_y = float(anchor[1])
    else:
        anchor_x = left + width / 2.0
        anchor_y = top + height / 2.0

    action = {
        "type": "adb_swipe",
        "from_x": int(round(anchor_x)),
        "from_y": int(round(anchor_y)),
        "to_x": int(round(anchor_x)),
        "to_y": int(round(anchor_y + delta_y)),
        "duration_ms": int(detect_config.get("row_snap_duration_ms", 450)),
        "hold_seconds_before": float(detect_config.get("row_snap_hold_seconds_before", 0.08)),
        "pause_before_release_seconds": float(detect_config.get("row_snap_pause_before_release_seconds", 0.12)),
        "steps": int(detect_config.get("row_snap_steps", 8)),
    }
    return action_for_input_backend(action, detected_items_input_backend(config))


def snap_detected_item_rows_after_scroll(
    config: dict[str, Any],
    target_rows: list[float] | None,
    snapshotter: MovementSnapshotter | None = None,
    label_prefix: str = "detected_row_snap",
) -> None:
    detect_config = detected_items_config(config)
    if not bool(detect_config.get("row_snap_after_scroll", True)):
        return
    if not target_rows:
        return

    tolerance = float(detect_config.get("row_snap_tolerance_px", 8))
    group_tolerance = float(detect_config.get("row_group_tolerance_px", 35))
    max_match_delta = float(detect_config.get("row_snap_max_match_delta_px", 95))
    max_correction = float(detect_config.get("row_snap_max_correction_px", 80))
    attempts = max(1, int(detect_config.get("row_snap_attempts", 1)))

    for attempt in range(attempts):
        image = capture_screenshot(config)
        boxes = detect_visible_item_boxes(image, config)
        current_rows = detected_item_row_centers(boxes, group_tolerance)
        delta = detected_item_row_snap_delta(target_rows, current_rows, max_match_delta)
        if delta is None:
            print("Row snap: no matching row centers found.")
            return
        print(
            "Row snap: "
            f"target={[round(value, 1) for value in target_rows]} "
            f"current={[round(value, 1) for value in current_rows]} "
            f"delta={delta:.1f}"
        )
        if abs(delta) <= tolerance:
            return

        correction = max(-max_correction, min(max_correction, delta))
        action = detected_row_snap_action(config, correction)
        print(f"Row snap correction {attempt + 1}/{attempts}: {describe_advance_action(config, action)}")
        run_advance_action_group(config, [action], snapshotter, f"{label_prefix}_{attempt + 1:04d}")


def item_detection_mask_pixel(rgb: tuple[int, int, int], detect_config: dict[str, Any]) -> bool:
    r, g, b = rgb
    green = detect_config.get("green_threshold", {})
    dark_green = detect_config.get("dark_green_threshold", {})
    orange = detect_config.get("orange_threshold", {})
    dark_orange = detect_config.get("dark_orange_threshold", {})
    colored_card = detect_config.get("colored_card_threshold", {})
    mint = detect_config.get("mint_threshold", {})

    green_match = (
        g >= int(green.get("min_g", 135))
        and r <= int(green.get("max_r", 125))
        and b >= int(green.get("min_b", 70))
    )
    dark_green_match = (
        g >= int(dark_green.get("min_g", 75))
        and r <= int(dark_green.get("max_r", 90))
        and b <= int(dark_green.get("max_b", 110))
        and g >= r + int(dark_green.get("min_g_over_r", 20))
        and g >= b + int(dark_green.get("min_g_over_b", 5))
    )
    orange_match = (
        r >= int(orange.get("min_r", 180))
        and g >= int(orange.get("min_g", 110))
        and b <= int(orange.get("max_b", 130))
    )
    dark_orange_match = (
        r >= int(dark_orange.get("min_r", 70))
        and g >= int(dark_orange.get("min_g", 45))
        and b <= int(dark_orange.get("max_b", 90))
        and r >= g + int(dark_orange.get("min_r_over_g", 8))
        and g >= b + int(dark_orange.get("min_g_over_b", 5))
    )
    mint_match = (
        g >= int(mint.get("min_g", 170))
        and b >= int(mint.get("min_b", 120))
        and r <= int(mint.get("max_r", 180))
    )
    channel_max = max(r, g, b)
    channel_min = min(r, g, b)
    saturation = channel_max - channel_min
    neutral_tolerance = int(colored_card.get("neutral_tolerance", 12))
    neutral_min_value = int(colored_card.get("neutral_min_value", 145))
    neutral_match = (
        abs(r - g) <= neutral_tolerance
        and abs(g - b) <= neutral_tolerance
        and abs(r - b) <= neutral_tolerance
        and channel_max >= neutral_min_value
    )
    colored_card_match = (
        bool(colored_card.get("enabled", True))
        and channel_max <= int(colored_card.get("max_value", 245))
        and not neutral_match
        and (
            saturation >= int(colored_card.get("min_saturation", 18))
            or (
                channel_max <= int(colored_card.get("dark_max_value", 130))
                and saturation >= int(colored_card.get("dark_min_saturation", 8))
            )
        )
    )
    return green_match or dark_green_match or orange_match or dark_orange_match or mint_match or colored_card_match


def find_mask_components(mask: bytearray, width: int, height: int) -> list[dict[str, Any]]:
    seen = bytearray(width * height)
    components: list[dict[str, Any]] = []

    for start in range(width * height):
        if not mask[start] or seen[start]:
            continue

        stack = [start]
        seen[start] = 1
        min_x = max_x = start % width
        min_y = max_y = start // width
        pixels = 0

        while stack:
            index = stack.pop()
            pixels += 1
            x = index % width
            y = index // width
            min_x = min(min_x, x)
            max_x = max(max_x, x)
            min_y = min(min_y, y)
            max_y = max(max_y, y)

            if x > 0:
                neighbor = index - 1
                if mask[neighbor] and not seen[neighbor]:
                    seen[neighbor] = 1
                    stack.append(neighbor)
            if x < width - 1:
                neighbor = index + 1
                if mask[neighbor] and not seen[neighbor]:
                    seen[neighbor] = 1
                    stack.append(neighbor)
            if y > 0:
                neighbor = index - width
                if mask[neighbor] and not seen[neighbor]:
                    seen[neighbor] = 1
                    stack.append(neighbor)
            if y < height - 1:
                neighbor = index + width
                if mask[neighbor] and not seen[neighbor]:
                    seen[neighbor] = 1
                    stack.append(neighbor)

        components.append(
            {
                "bbox": [min_x, min_y, max_x + 1, max_y + 1],
                "pixels": pixels,
                "center": [(min_x + max_x + 1) / 2.0, (min_y + max_y + 1) / 2.0],
            }
        )
    return components


def dedupe_detected_item_boxes(boxes: list[dict[str, Any]], min_center_distance: float) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for box in sorted(boxes, key=lambda item: item["pixels"], reverse=True):
        center_x, center_y = box["center"]
        if any(
            abs(center_x - other["center"][0]) <= min_center_distance
            and abs(center_y - other["center"][1]) <= min_center_distance
            for other in selected
        ):
            continue
        selected.append(box)
    return sorted(selected, key=lambda item: (item["center"][1], item["center"][0]))


def detected_item_row_centers(boxes: list[dict[str, Any]], tolerance: float = 35.0) -> list[float]:
    rows: list[list[float]] = []
    for center_y in sorted(float(box["center"][1]) for box in boxes):
        if not rows:
            rows.append([center_y])
            continue
        row_center = sum(rows[-1]) / len(rows[-1])
        if abs(center_y - row_center) <= tolerance:
            rows[-1].append(center_y)
        else:
            rows.append([center_y])
    return [sum(row) / len(row) for row in rows]


def detected_item_row_snap_delta(
    target_rows: list[float],
    current_rows: list[float],
    max_match_delta: float,
) -> float | None:
    deltas: list[float] = []
    for current_y in current_rows:
        nearest_target = min(target_rows, key=lambda target_y: abs(target_y - current_y), default=None)
        if nearest_target is None:
            continue
        delta = nearest_target - current_y
        if abs(delta) <= max_match_delta:
            deltas.append(delta)

    if not deltas:
        return None
    return median_number(deltas)


def detected_item_row_index(center_y: float, row_centers: list[float], tolerance: float) -> int | None:
    if not row_centers:
        return None
    nearest_index, nearest_y = min(enumerate(row_centers), key=lambda item: abs(item[1] - center_y))
    if abs(nearest_y - center_y) <= tolerance:
        return nearest_index
    return None


def detected_item_row_spacing(row_centers: list[float]) -> float | None:
    if len(row_centers) < 2:
        return None
    deltas = [
        float(row_centers[index + 1]) - float(row_centers[index])
        for index in range(len(row_centers) - 1)
        if float(row_centers[index + 1]) > float(row_centers[index])
    ]
    if not deltas:
        return None
    return median_number(deltas)


def assign_detected_item_grid_indexes(
    signed_boxes: list[dict[str, Any]],
    row_centers: list[float],
    config: dict[str, Any],
) -> None:
    detect_config = detected_items_config(config)
    row_tolerance = float(detect_config.get("row_group_tolerance_px", 35))
    rows: dict[int, list[dict[str, Any]]] = {}

    for ordinal, box in enumerate(sorted(signed_boxes, key=lambda item: (item["center"][1], item["center"][0])), start=1):
        box["viewport_ordinal"] = ordinal
        row_index = box.get("row_index")
        if row_index is None:
            row_index = detected_item_row_index(float(box["center"][1]), row_centers, row_tolerance)
            if row_index is not None:
                box["row_index"] = row_index
        if row_index is not None:
            rows.setdefault(int(row_index), []).append(box)

    for row_index, row_boxes in rows.items():
        for column_index, box in enumerate(sorted(row_boxes, key=lambda item: item["center"][0]), start=1):
            box["column_index"] = column_index - 1


def detected_item_box_grid_label(box: dict[str, Any]) -> str:
    row_index = box.get("row_index")
    column_index = box.get("column_index")
    if row_index is not None and column_index is not None:
        return f"r{int(row_index) + 1}c{int(column_index) + 1}"
    ordinal = box.get("viewport_ordinal")
    if ordinal is not None:
        return f"item{int(ordinal)}"
    return "item?"


def detected_calibration_boxes(boxes: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    if not boxes:
        return []

    detect_config = detected_items_config(config)
    first = boxes[0]
    first_x = float(first["center"][0])
    first_y = float(first["center"][1])
    row_tolerance = float(detect_config.get("calibration_row_tolerance_px", detect_config.get("row_group_tolerance_px", 35)))
    column_tolerance = float(detect_config.get("calibration_column_tolerance_px", 45))
    min_axis_delta = float(detect_config.get("calibration_min_axis_delta_px", 45))

    horizontal_candidates = [
        box
        for box in boxes[1:]
        if abs(float(box["center"][1]) - first_y) <= row_tolerance
        and float(box["center"][0]) - first_x >= min_axis_delta
    ]
    vertical_candidates = [
        box
        for box in boxes[1:]
        if abs(float(box["center"][0]) - first_x) <= column_tolerance
        and float(box["center"][1]) - first_y >= min_axis_delta
    ]

    selected = [first]
    if horizontal_candidates:
        selected.append(min(horizontal_candidates, key=lambda box: float(box["center"][0]) - first_x))
    elif len(boxes) > 1:
        selected.append(boxes[1])

    if vertical_candidates:
        vertical = min(vertical_candidates, key=lambda box: float(box["center"][1]) - first_y)
        if all(vertical is not existing for existing in selected):
            selected.append(vertical)

    return selected


def detect_visible_item_boxes(image: Any, config: dict[str, Any]) -> list[dict[str, Any]]:
    detect_config = detected_items_config(config)
    region = parse_rect(detect_config.get("region"), "advance.detect_items.region")
    if region is None:
        raise AutomationError("advance.detect_items.region is required for detected item clicking.")

    left, top, width, height = region
    crop = image.crop((left, top, left + width, top + height)).convert("RGB")
    pixels = crop.load()
    mask = bytearray(width * height)
    for y in range(height):
        row = y * width
        for x in range(width):
            if item_detection_mask_pixel(pixels[x, y], detect_config):
                mask[row + x] = 1

    min_width = int(detect_config.get("min_card_width", 120))
    max_width = int(detect_config.get("max_card_width", 230))
    min_height = int(detect_config.get("min_card_height", 120))
    max_height = int(detect_config.get("max_card_height", 230))
    min_pixels = int(detect_config.get("min_component_pixels", 2500))
    min_center_distance = float(detect_config.get("min_center_distance", 45))

    candidates: list[dict[str, Any]] = []
    for component in find_mask_components(mask, width, height):
        box_left, box_top, box_right, box_bottom = component["bbox"]
        box_width = box_right - box_left
        box_height = box_bottom - box_top
        if not (min_width <= box_width <= max_width and min_height <= box_height <= max_height):
            continue
        if component["pixels"] < min_pixels:
            continue

        full_box = [box_left + left, box_top + top, box_right + left, box_bottom + top]
        component["bbox"] = full_box
        component["center"] = [(full_box[0] + full_box[2]) / 2.0, (full_box[1] + full_box[3]) / 2.0]
        candidates.append(component)

    boxes = dedupe_detected_item_boxes(candidates, min_center_distance)
    max_items = detect_config.get("max_items_per_page")
    if max_items not in {None, ""}:
        boxes = boxes[: int(max_items)]
    return boxes


def detected_item_card_signature(image: Any, box: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    detect_config = detected_items_config(config)
    bbox = box.get("bbox")
    if not isinstance(bbox, list) or len(bbox) != 4:
        raise AutomationError("Detected item box is missing bbox.")

    inset = int(detect_config.get("signature_inset_px", 12))
    left = int(round(float(bbox[0]))) + inset
    top = int(round(float(bbox[1]))) + inset
    right = int(round(float(bbox[2]))) - inset
    bottom = int(round(float(bbox[3]))) - inset
    if right <= left or bottom <= top:
        left, top, right, bottom = [int(round(float(value))) for value in bbox]

    crop_rgb = image.crop((left, top, right, bottom)).convert("RGB")
    crop_bytes = crop_rgb.tobytes()
    pixel_count = max(1, len(crop_bytes) // 3)
    mean_rgb = [sum(crop_bytes[channel::3]) / pixel_count for channel in range(3)]
    crop = crop_rgb.convert("L")
    resize = detect_config.get("signature_resize", [64, 64])
    width = 64
    height = 64
    if isinstance(resize, list) and len(resize) == 2:
        width = max(2, int(resize[0]))
        height = max(2, int(resize[1]))

    # Difference hashes are deliberately less sensitive to the selected-card
    # highlight than exact pixels, while still tracking icon/text edges.
    resized = crop.resize((width + 1, height + 1))
    pixels = resized.load()
    bits: list[str] = []
    for y in range(height):
        for x in range(width):
            bits.append("1" if pixels[x + 1, y] > pixels[x, y] else "0")
    for y in range(height):
        for x in range(width):
            bits.append("1" if pixels[x, y + 1] > pixels[x, y] else "0")

    value = int("".join(bits), 2) if bits else 0
    hex_width = (len(bits) + 3) // 4
    return {
        "hash": f"{value:0{hex_width}x}",
        "bits": len(bits),
        "mean_rgb": mean_rgb,
        "bbox": [left, top, right, bottom],
    }


def signature_hamming_distance(left: dict[str, Any], right: dict[str, Any]) -> int:
    left_hash = str(left.get("hash", ""))
    right_hash = str(right.get("hash", ""))
    if not left_hash or not right_hash:
        return 10**9

    left_bits = int(left.get("bits", len(left_hash) * 4))
    right_bits = int(right.get("bits", len(right_hash) * 4))
    bit_count = max(left_bits, right_bits, len(left_hash) * 4, len(right_hash) * 4)
    left_value = int(left_hash, 16)
    right_value = int(right_hash, 16)
    return (left_value ^ right_value).bit_count() + abs(left_bits - right_bits) + max(0, bit_count - max(left_bits, right_bits))


def signature_color_distance(left: dict[str, Any], right: dict[str, Any]) -> float:
    left_rgb = left.get("mean_rgb")
    right_rgb = right.get("mean_rgb")
    if not isinstance(left_rgb, list) or not isinstance(right_rgb, list) or len(left_rgb) != 3 or len(right_rgb) != 3:
        return 10**9
    return sum((float(left_rgb[index]) - float(right_rgb[index])) ** 2 for index in range(3)) ** 0.5


def seen_signature_match(
    signature: dict[str, Any],
    seen_signatures: list[dict[str, Any]],
    config: dict[str, Any],
) -> tuple[bool, int | None, float | None]:
    detect_config = detected_items_config(config)
    hamming_threshold = int(detect_config.get("signature_hamming_threshold", 90))
    color_threshold = float(detect_config.get("signature_color_distance_threshold", 90))
    best_distance: int | None = None
    best_color_distance: float | None = None
    for seen in seen_signatures:
        distance = signature_hamming_distance(signature, seen)
        color_distance = signature_color_distance(signature, seen)
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best_color_distance = color_distance
        if distance <= hamming_threshold and color_distance <= color_threshold:
            return True, distance, color_distance
    return False, best_distance, best_color_distance


def signed_detected_item_boxes(
    image: Any,
    boxes: list[dict[str, Any]],
    seen_signatures: list[dict[str, Any]],
    config: dict[str, Any],
    row_centers: list[float] | None = None,
) -> list[dict[str, Any]]:
    detect_config = detected_items_config(config)
    signed_boxes: list[dict[str, Any]] = []
    for box in boxes:
        signed = dict(box)
        signature = detected_item_card_signature(image, box, config)
        signed["signature"] = signature
        matched, distance, color_distance = seen_signature_match(signature, seen_signatures, config)
        signed["seen_signature_match"] = matched
        row_index = None
        if row_centers is not None:
            row_tolerance = float(detect_config.get("row_group_tolerance_px", 35))
            row_index = detected_item_row_index(float(box["center"][1]), row_centers, row_tolerance)
            if row_index is not None:
                signed["row_index"] = row_index
        if distance is not None:
            signed["signature_distance"] = distance
        if color_distance is not None:
            signed["signature_color_distance"] = round(color_distance, 2)
        signed_boxes.append(signed)
    return signed_boxes


def split_unseen_detected_item_boxes(
    image: Any,
    boxes: list[dict[str, Any]],
    seen_signatures: list[dict[str, Any]],
    config: dict[str, Any],
    skip_row_limit: int | None = None,
    row_centers: list[float] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    detect_config = detected_items_config(config)
    if not bool(detect_config.get("skip_seen_cards", True)):
        signed_boxes = signed_detected_item_boxes(image, boxes, [], config, row_centers)
        return signed_boxes, []

    matched_counts_by_row: dict[int, int] = {}
    signed_boxes = signed_detected_item_boxes(image, boxes, seen_signatures, config, row_centers)
    for signed in signed_boxes:
        matched = bool(signed.get("seen_signature_match"))
        row_index = signed.get("row_index")
        if matched and row_index is not None:
            matched_counts_by_row[row_index] = matched_counts_by_row.get(row_index, 0) + 1

    min_matches_per_row = int(detect_config.get("skip_seen_min_matches_per_row", 2))
    skippable_rows = {
        row_index
        for row_index, match_count in matched_counts_by_row.items()
        if match_count >= min_matches_per_row
    }

    unseen: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for signed in signed_boxes:
        matched = bool(signed.get("seen_signature_match"))
        row_index = signed.get("row_index")
        can_skip_for_row = skip_row_limit is None or (row_index is not None and row_index < skip_row_limit)
        row_has_enough_matches = skip_row_limit is None or row_index in skippable_rows
        if matched and can_skip_for_row and row_has_enough_matches:
            skipped.append(signed)
        else:
            if matched:
                signed["seen_match_ignored"] = True
            unseen.append(signed)
    return unseen, skipped


def detected_item_signature_id(signature: dict[str, Any]) -> str:
    value = str(signature.get("hash", ""))
    return value[:12] if value else ""


def detected_viewport_id(signed_boxes: list[dict[str, Any]]) -> str:
    parts = [
        f"{detected_item_signature_id(box.get('signature', {}))}:{int(round(float(box['center'][0])))}:{int(round(float(box['center'][1])))}"
        for box in sorted(signed_boxes, key=lambda item: (item["center"][1], item["center"][0]))
    ]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]


def best_previous_signed_box_match(
    signature: dict[str, Any],
    previous_boxes: list[dict[str, Any]],
    config: dict[str, Any],
) -> tuple[dict[str, Any] | None, int | None, float | None]:
    detect_config = detected_items_config(config)
    hamming_threshold = int(detect_config.get("signature_hamming_threshold", 90))
    color_threshold = float(detect_config.get("signature_color_distance_threshold", 90))
    best_box: dict[str, Any] | None = None
    best_distance: int | None = None
    best_color_distance: float | None = None

    for previous in previous_boxes:
        previous_signature = previous.get("signature")
        if not isinstance(previous_signature, dict):
            continue
        distance = signature_hamming_distance(signature, previous_signature)
        color_distance = signature_color_distance(signature, previous_signature)
        if best_distance is None or (distance, color_distance) < (best_distance, best_color_distance or 10**9):
            best_box = previous
            best_distance = distance
            best_color_distance = color_distance

    if (
        best_box is not None
        and best_distance is not None
        and best_color_distance is not None
        and best_distance <= hamming_threshold
        and best_color_distance <= color_threshold
    ):
        return best_box, best_distance, best_color_distance
    return None, best_distance, best_color_distance


def detected_viewport_progress(
    previous_boxes: list[dict[str, Any]] | None,
    current_boxes: list[dict[str, Any]],
    config: dict[str, Any],
    row_spacing: float | None,
) -> dict[str, Any] | None:
    if not previous_boxes:
        return None

    matches: list[dict[str, Any]] = []
    for current in current_boxes:
        signature = current.get("signature")
        if not isinstance(signature, dict):
            continue
        previous, distance, color_distance = best_previous_signed_box_match(signature, previous_boxes, config)
        if previous is None:
            continue
        previous_center = previous["center"]
        current_center = current["center"]
        shift_y = float(previous_center[1]) - float(current_center[1])
        match = {
            "previous": detected_item_box_grid_label(previous),
            "current": detected_item_box_grid_label(current),
            "previous_center": [round(float(previous_center[0]), 1), round(float(previous_center[1]), 1)],
            "current_center": [round(float(current_center[0]), 1), round(float(current_center[1]), 1)],
            "shift_y": round(shift_y, 1),
            "hamming_distance": distance,
            "color_distance": round(color_distance, 2) if color_distance is not None else None,
        }
        if row_spacing:
            match["shift_rows"] = round(shift_y / row_spacing, 2)
        matches.append(match)

    shifts = [float(match["shift_y"]) for match in matches]
    median_shift = median_number(shifts) if shifts else None
    result: dict[str, Any] = {
        "previous_visible_count": len(previous_boxes),
        "current_visible_count": len(current_boxes),
        "matched_previous_count": len(matches),
        "matches": matches,
    }
    if median_shift is not None:
        result["median_shift_y"] = round(median_shift, 1)
        if row_spacing:
            result["median_shift_rows"] = round(median_shift / row_spacing, 2)
    return result


def detected_item_box_status(
    box: dict[str, Any],
    boxes_to_click: list[dict[str, Any]],
    skipped_boxes: list[dict[str, Any]],
) -> str:
    if box.get("manual_selected"):
        return "manual_start"
    center = tuple(box.get("center", []))
    click_centers = {tuple(item.get("center", [])) for item in boxes_to_click}
    skipped_centers = {tuple(item.get("center", [])) for item in skipped_boxes}
    if center in skipped_centers:
        return "seen_skip"
    if center in click_centers:
        if box.get("seen_match_ignored"):
            return "tap_seen_match"
        return "tap"
    if box.get("seen_match_ignored"):
        return "seen_match_ignored"
    return "visible"


def prepare_detected_viewport(
    image: Any,
    boxes: list[dict[str, Any]],
    seen_signatures: list[dict[str, Any]],
    config: dict[str, Any],
    skip_row_limit: int | None,
    previous_signed_boxes: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[float], dict[str, Any] | None]:
    detect_config = detected_items_config(config)
    group_tolerance = float(detect_config.get("row_group_tolerance_px", 35))
    row_centers = detected_item_row_centers(boxes, group_tolerance)
    boxes_to_click, skipped_boxes = split_unseen_detected_item_boxes(
        image,
        boxes,
        seen_signatures,
        config,
        skip_row_limit=skip_row_limit,
        row_centers=row_centers,
    )
    signed_boxes = sorted([*boxes_to_click, *skipped_boxes], key=lambda item: (item["center"][1], item["center"][0]))
    assign_detected_item_grid_indexes(signed_boxes, row_centers, config)
    progress = detected_viewport_progress(
        previous_signed_boxes,
        signed_boxes,
        config,
        detected_item_row_spacing(row_centers),
    )
    return signed_boxes, boxes_to_click, skipped_boxes, row_centers, progress


def prepare_manual_detected_viewport(
    boxes: list[dict[str, Any]],
    config: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[float]]:
    detect_config = detected_items_config(config)
    group_tolerance = float(detect_config.get("row_group_tolerance_px", 35))
    row_centers = detected_item_row_centers(boxes, group_tolerance)
    signed_boxes = [dict(box) for box in sorted(boxes, key=lambda item: (item["center"][1], item["center"][0]))]
    assign_detected_item_grid_indexes(signed_boxes, row_centers, config)
    return signed_boxes, row_centers


def changed_pixels_in_bbox(
    before_image: Any,
    after_image: Any,
    bbox: list[float] | tuple[float, float, float, float],
    config: dict[str, Any],
) -> int:
    if before_image.size != after_image.size:
        raise AutomationError("Manual selection screenshots have different sizes.")

    detect_config = detected_items_config(config)
    threshold = max(0, int(detect_config.get("manual_selection_diff_threshold", 18)))
    margin = max(0, int(detect_config.get("manual_selection_bbox_margin_px", 6)))
    width, height = before_image.size
    left = max(0, int(float(bbox[0])) - margin)
    top = max(0, int(float(bbox[1])) - margin)
    right = min(width, int(round(float(bbox[2]))) + margin)
    bottom = min(height, int(round(float(bbox[3]))) + margin)
    if right <= left or bottom <= top:
        return 0

    ImageChops = require_package("PIL.ImageChops", "Pillow")
    before_crop = before_image.crop((left, top, right, bottom)).convert("RGB")
    after_crop = after_image.crop((left, top, right, bottom)).convert("RGB")
    diff = ImageChops.difference(before_crop, after_crop).tobytes()
    changed = 0
    for index in range(0, len(diff), 3):
        if diff[index] >= threshold or diff[index + 1] >= threshold or diff[index + 2] >= threshold:
            changed += 1
    return changed


def detect_manual_selected_box(
    before_image: Any,
    after_image: Any,
    signed_boxes: list[dict[str, Any]],
    config: dict[str, Any],
) -> dict[str, Any]:
    if not signed_boxes:
        raise AutomationError("No visible equipment cards were detected after your manual click.")

    detect_config = detected_items_config(config)
    minimum_changed = int(detect_config.get("manual_selection_min_changed_pixels", 35))
    scored: list[tuple[int, dict[str, Any]]] = []
    for box in signed_boxes:
        bbox = box.get("bbox")
        if not isinstance(bbox, list) or len(bbox) != 4:
            continue
        changed = changed_pixels_in_bbox(before_image, after_image, bbox, config)
        box["manual_selection_changed_pixels"] = changed
        scored.append((changed, box))

    if not scored:
        raise AutomationError("Detected equipment cards did not include usable bounding boxes.")

    scored.sort(key=lambda item: item[0], reverse=True)
    best_changed, selected = scored[0]
    if best_changed < minimum_changed:
        raise AutomationError(
            "Could not identify which equipment card you clicked. "
            f"Best changed-pixel score was {best_changed}, below {minimum_changed}."
        )

    selected["manual_selected"] = True
    return selected


def boxes_after_manual_selection(
    signed_boxes: list[dict[str, Any]],
    selected_box: dict[str, Any],
) -> list[dict[str, Any]]:
    selected_row = selected_box.get("row_index")
    selected_column = selected_box.get("column_index")
    if selected_row is not None and selected_column is not None:
        row = int(selected_row)
        column = int(selected_column)
        return sorted(
            [
                box
                for box in signed_boxes
                if box is not selected_box
                and box.get("row_index") is not None
                and box.get("column_index") is not None
                and (
                    int(box["row_index"]) > row
                    or (int(box["row_index"]) == row and int(box["column_index"]) > column)
                )
            ],
            key=lambda item: (int(item["row_index"]), int(item["column_index"])),
        )

    selected_ordinal = selected_box.get("viewport_ordinal")
    if selected_ordinal is None:
        return []
    return sorted(
        [
            box
            for box in signed_boxes
            if box is not selected_box and box.get("viewport_ordinal") is not None and int(box["viewport_ordinal"]) > int(selected_ordinal)
        ],
        key=lambda item: int(item["viewport_ordinal"]),
    )


def write_detected_viewport_debug(
    config: dict[str, Any],
    debug_dir: Path | None,
    label: str,
    image: Any,
    signed_boxes: list[dict[str, Any]],
    boxes_to_click: list[dict[str, Any]],
    skipped_boxes: list[dict[str, Any]],
    row_centers: list[float],
    progress: dict[str, Any] | None,
) -> tuple[Path | None, Path | None]:
    detect_config = detected_items_config(config)
    if debug_dir is None or not bool(detect_config.get("page_diagnostics", False)):
        return None, None

    diag_dir = debug_dir / "detected_viewports"
    diag_dir.mkdir(parents=True, exist_ok=True)
    viewport_id = detected_viewport_id(signed_boxes)
    stem = f"{safe_filename_part(label)}_{viewport_id}"
    json_path = diag_dir / f"{stem}.json"
    image_path = diag_dir / f"{stem}.png"

    boxes_payload = []
    for box in sorted(signed_boxes, key=lambda item: (item["center"][1], item["center"][0])):
        center = box["center"]
        payload = {
            "label": detected_item_box_grid_label(box),
            "status": detected_item_box_status(box, boxes_to_click, skipped_boxes),
            "center": [round(float(center[0]), 1), round(float(center[1]), 1)],
            "bbox": [round(float(value), 1) for value in box["bbox"]],
            "row_index": box.get("row_index"),
            "column_index": box.get("column_index"),
            "signature": detected_item_signature_id(box.get("signature", {})),
            "seen_signature_match": bool(box.get("seen_signature_match")),
        }
        if box.get("manual_selected"):
            payload["manual_selected"] = True
        if box.get("manual_selection_changed_pixels") is not None:
            payload["manual_selection_changed_pixels"] = box["manual_selection_changed_pixels"]
        if box.get("signature_distance") is not None:
            payload["nearest_hamming_distance"] = box["signature_distance"]
        if box.get("signature_color_distance") is not None:
            payload["nearest_color_distance"] = box["signature_color_distance"]
        if box.get("seen_match_ignored"):
            payload["seen_match_ignored"] = True
        boxes_payload.append(payload)

    row_spacing = detected_item_row_spacing(row_centers)
    payload = {
        "label": label,
        "viewport_id": viewport_id,
        "timestamp": dt.datetime.now().isoformat(timespec="milliseconds"),
        "image_size": list(image.size),
        "detected_cards": len(signed_boxes),
        "tap_count": len(boxes_to_click),
        "seen_skip_count": len(skipped_boxes),
        "row_centers": [round(float(value), 1) for value in row_centers],
        "row_spacing": round(row_spacing, 1) if row_spacing is not None else None,
        "progress_from_previous": progress,
        "boxes": boxes_payload,
    }
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    if bool(detect_config.get("page_diagnostics_images", True)):
        ImageDraw = require_package("PIL.ImageDraw", "Pillow")
        annotated = image.copy().convert("RGB")
        draw = ImageDraw.Draw(annotated)
        colors = {
            "tap": (36, 180, 90),
            "tap_seen_match": (210, 70, 70),
            "manual_start": (80, 90, 255),
            "seen_skip": (245, 170, 42),
            "seen_match_ignored": (210, 70, 70),
            "visible": (80, 150, 255),
        }
        for box_payload in boxes_payload:
            bbox = box_payload["bbox"]
            status = box_payload["status"]
            color = colors.get(status, (80, 150, 255))
            draw.rectangle(bbox, outline=color, width=4)
            label_text = f"{box_payload['label']} {status}"
            text_x = int(float(bbox[0])) + 4
            text_y = int(float(bbox[1])) + 4
            draw.rectangle((text_x - 2, text_y - 2, text_x + 132, text_y + 18), fill=(0, 0, 0))
            draw.text((text_x, text_y), label_text, fill=color)
        annotated.save(image_path)
    else:
        image_path = None

    return json_path, image_path


def print_detected_viewport_summary(
    config: dict[str, Any],
    label: str,
    signed_boxes: list[dict[str, Any]],
    boxes_to_click: list[dict[str, Any]],
    skipped_boxes: list[dict[str, Any]],
    row_centers: list[float],
    progress: dict[str, Any] | None,
    json_path: Path | None = None,
    image_path: Path | None = None,
) -> None:
    detect_config = detected_items_config(config)
    viewport_id = detected_viewport_id(signed_boxes)
    row_text = ", ".join(str(round(float(value), 1)) for value in row_centers) or "none"
    summary = (
        f"{label}: id={viewport_id} cards={len(signed_boxes)} rows={len(row_centers)} "
        f"row_y=[{row_text}] tap={len(boxes_to_click)} seen_skip={len(skipped_boxes)}"
    )
    if progress is not None:
        summary += f" prev_matches={progress.get('matched_previous_count', 0)}"
        if progress.get("median_shift_y") is not None:
            summary += f" scroll={progress['median_shift_y']}px"
        if progress.get("median_shift_rows") is not None:
            summary += f"/{progress['median_shift_rows']} rows"
    print(summary)

    min_rows = float(detect_config.get("min_crawl_scroll_rows", 0))
    if (
        progress is not None
        and min_rows > 0
        and progress.get("matched_previous_count", 0) >= int(detect_config.get("scroll_progress_min_matches", 2))
        and progress.get("median_shift_rows") is not None
        and float(progress["median_shift_rows"]) < min_rows
    ):
        print(
            f"  scroll warning: matched cards moved only {progress['median_shift_rows']} rows; "
            f"target minimum is {min_rows:g}."
        )

    tap_plan = [
        (
            f"{detected_item_box_grid_label(box)}@({float(box['center'][0]):.0f},{float(box['center'][1]):.0f})"
            + (" seen-match" if box.get("seen_match_ignored") else "")
        )
        for box in boxes_to_click
    ]
    if tap_plan:
        print(f"  tap plan: {', '.join(tap_plan)}")
    if skipped_boxes:
        skipped_plan = [
            f"{detected_item_box_grid_label(box)}@({float(box['center'][0]):.0f},{float(box['center'][1]):.0f})"
            for box in skipped_boxes
        ]
        print(f"  skipped seen: {', '.join(skipped_plan)}")
    if json_path:
        print(f"  viewport debug: {json_path}")
    if image_path:
        print(f"  annotated screenshot: {image_path}")


def detected_viewport_scroll_stalled(progress: dict[str, Any] | None, config: dict[str, Any]) -> bool:
    if progress is None:
        return False
    detect_config = detected_items_config(config)
    matched = int(progress.get("matched_previous_count", 0))
    min_matches = int(detect_config.get("all_items_stop_min_matched_cards", 4))
    shift_rows = progress.get("median_shift_rows")
    if shift_rows is None:
        return False
    threshold = float(detect_config.get("all_items_stop_scroll_rows_threshold", 0.5))
    return matched >= min_matches and abs(float(shift_rows)) <= threshold


def crop_detected_items_region(image: Any, config: dict[str, Any]) -> Any:
    detect_config = detected_items_config(config)
    region = parse_rect(detect_config.get("region"), "advance.detect_items.region")
    if region is None:
        raise AutomationError("advance.detect_items.region is required for detected item image shift.")
    return crop_image(image, region)


def mean_abs_image_difference(left_image: Any, right_image: Any) -> float:
    ImageChops = require_package("PIL.ImageChops", "Pillow")
    ImageStat = require_package("PIL.ImageStat", "Pillow")
    diff = ImageChops.difference(left_image, right_image)
    return float(ImageStat.Stat(diff).mean[0])


def estimate_detected_region_vertical_shift(
    previous_image: Any,
    current_image: Any,
    config: dict[str, Any],
) -> dict[str, Any] | None:
    detect_config = detected_items_config(config)
    previous_crop = crop_detected_items_region(previous_image, config).convert("L")
    current_crop = crop_detected_items_region(current_image, config).convert("L")

    resize_width = int(detect_config.get("viewport_shift_resize_width", 160))
    step_px = max(1, int(detect_config.get("viewport_shift_search_step_px", 4)))
    max_shift_px = int(detect_config.get("viewport_shift_max_px", 900))
    min_overlap_ratio = float(detect_config.get("viewport_shift_min_overlap_ratio", 0.45))

    width, height = previous_crop.size
    if width <= 0 or height <= 0:
        return None
    if resize_width > 0 and width > resize_width:
        scale = resize_width / width
        resized_height = max(1, int(round(height * scale)))
        previous_crop = previous_crop.resize((resize_width, resized_height))
        current_crop = current_crop.resize((resize_width, resized_height))
        scale_y = height / resized_height
    else:
        scale_y = 1.0

    width, height = previous_crop.size
    max_shift = min(height - 1, max(0, int(round(max_shift_px / scale_y))))
    min_overlap = max(1, int(round(height * min_overlap_ratio)))
    baseline = mean_abs_image_difference(previous_crop, current_crop)

    best_shift = 0
    best_score = baseline
    scores: list[tuple[int, float]] = [(0, baseline)]
    for shift in range(step_px, max_shift + 1, step_px):
        overlap_height = height - shift
        if overlap_height < min_overlap:
            break
        previous_band = previous_crop.crop((0, shift, width, height))
        current_band = current_crop.crop((0, 0, width, overlap_height))
        score = mean_abs_image_difference(previous_band, current_band)
        scores.append((shift, score))
        if score < best_score:
            best_shift = shift
            best_score = score

    full_shift = best_shift * scale_y
    improvement = baseline - best_score
    return {
        "shift_y": round(full_shift, 1),
        "score": round(best_score, 3),
        "baseline_score": round(baseline, 3),
        "improvement": round(improvement, 3),
        "scale_y": round(scale_y, 4),
        "sampled_scores": len(scores),
    }


def detected_region_shift_rows(shift: dict[str, Any] | None, row_centers: list[float]) -> float | None:
    if shift is None:
        return None
    row_spacing = detected_item_row_spacing(row_centers)
    if not row_spacing:
        return None
    return float(shift["shift_y"]) / row_spacing


def detected_box_signatures_match(left_box: dict[str, Any] | None, right_box: dict[str, Any] | None, config: dict[str, Any]) -> bool:
    if not left_box or not right_box:
        return False
    left_signature = left_box.get("signature")
    right_signature = right_box.get("signature")
    if not isinstance(left_signature, dict) or not isinstance(right_signature, dict):
        return False
    matched, _distance, _color_distance = seen_signature_match(left_signature, [right_signature], config)
    return matched


def configured_tap_retry_offsets(config: dict[str, Any]) -> list[tuple[float, float]]:
    detect_config = detected_items_config(config)
    offsets = detect_config.get("tap_retry_offsets", [[0, 0]])
    if not isinstance(offsets, list) or not offsets:
        return [(0.0, 0.0)]
    parsed: list[tuple[float, float]] = []
    for offset in offsets:
        if isinstance(offset, list) and len(offset) == 2:
            parsed.append((float(offset[0]), float(offset[1])))
    return parsed or [(0.0, 0.0)]


def detected_item_action_with_offset(config: dict[str, Any], center: tuple[float, float], offset: tuple[float, float]) -> dict[str, Any]:
    return detected_item_action(config, (center[0] + offset[0], center[1] + offset[1]))


def should_retry_unchanged_equipment_tap(
    config: dict[str, Any],
    identity_key: str | None,
    image_hash_value: str,
    previous_identity_key: str | None,
    previous_image_hash: str | None,
    current_box: dict[str, Any],
    previous_displayed_box: dict[str, Any] | None,
    attempt_index: int,
    max_retries: int,
) -> bool:
    detect_config = detected_items_config(config)
    if not bool(detect_config.get("verify_tap_changed_equipment", True)):
        return False
    if attempt_index >= max_retries:
        return False
    same_details = False
    if identity_key and previous_identity_key and identity_key == previous_identity_key:
        same_details = True
    if image_hash_value and previous_image_hash and image_hash_value == previous_image_hash:
        same_details = True
    if not same_details:
        return False
    if detected_box_signatures_match(current_box, previous_displayed_box, config):
        return False
    return True


def detected_item_click_actions_from_screenshot(config: dict[str, Any]) -> tuple[list[dict[str, Any]], Any, list[dict[str, Any]]]:
    image = capture_screenshot(config)
    boxes = detect_visible_item_boxes(image, config)
    actions = [detected_item_action(config, tuple(box["center"])) for box in boxes]
    return actions, image, boxes


def runtime_calibrate_detected_item(config: dict[str, Any], center: tuple[float, float], delay_seconds: float | None = None) -> None:
    if detected_items_input_backend(config) != "desktop":
        return

    pyautogui = require_package("pyautogui")
    advance = config.get("advance", {})
    delay = float(delay_seconds if delay_seconds is not None else advance.get("runtime_calibration_delay_seconds", 3.0))
    print(f"Hover over the detected first visible item center at ADB ({center[0]:.0f}, {center[1]:.0f}).")
    if delay > 0:
        print(f"Reading mouse position in {delay:g}s...")
        time.sleep(delay)

    mouse_x, mouse_y = pyautogui.position()
    offset = set_desktop_offset_from_screen_point(config, center[0], center[1], mouse_x, mouse_y)
    print(f"Detected-item desktop offset: [{offset[0]}, {offset[1]}]")


def runtime_calibrate_detected_items(
    config: dict[str, Any],
    boxes: list[dict[str, Any]],
    delay_seconds: float | None = None,
) -> None:
    if detected_items_input_backend(config) != "desktop":
        return
    if not boxes:
        return

    detect_config = detected_items_config(config)
    use_multi_point = bool(detect_config.get("three_point_calibration", True))
    calibration_boxes = detected_calibration_boxes(boxes, config) if use_multi_point else boxes[:2]
    if len(calibration_boxes) < 2:
        runtime_calibrate_detected_item(config, tuple(boxes[0]["center"]), delay_seconds)
        return

    pyautogui = require_package("pyautogui")
    advance = config.get("advance", {})
    delay = float(delay_seconds if delay_seconds is not None else advance.get("runtime_calibration_delay_seconds", 3.0))
    samples: list[tuple[tuple[float, float], tuple[float, float]]] = []

    if len(calibration_boxes) < 3:
        print("Detected calibration found only two points; vertical scale may be approximate.")

    for index, box in enumerate(calibration_boxes, start=1):
        center = tuple(float(value) for value in box["center"])
        print(f"Hover over the center of calibration item {index} at ADB ({center[0]:.0f}, {center[1]:.0f}).")
        if delay > 0:
            print(f"Reading mouse position in {delay:g}s...")
            time.sleep(delay)
        mouse = tuple(float(value) for value in pyautogui.position())
        samples.append((center, mouse))

    uniform_scale = bool(detect_config.get("calibration_uniform_scale", detect_config.get("two_point_uniform_scale", False)))
    scale, offset = set_desktop_transform_from_screen_samples(
        config,
        samples,
        uniform_scale=uniform_scale,
    )
    print(f"Detected-item desktop scale: [{scale[0]:.6g}, {scale[1]:.6g}]")
    print(f"Detected-item desktop offset: [{offset[0]}, {offset[1]}]")
    for index, (center, mouse) in enumerate(samples, start=1):
        mapped = desktop_point(config, center[0], center[1])
        print(
            f"Mapped calibration item {index}: "
            f"ADB=({center[0]:.0f}, {center[1]:.0f}) "
            f"screen=({mapped[0]}, {mapped[1]}) "
            f"hover=({mouse[0]:.0f}, {mouse[1]:.0f})"
        )


def tap_page_test_steps(config: dict[str, Any], page_count: int) -> list[list[dict[str, Any]]]:
    if page_count < 1:
        raise AutomationError("--tap-page-test must be at least 1.")

    page_steps = item_grid_page_click_steps(config, 0)
    if not page_steps:
        return []

    steps: list[list[dict[str, Any]]] = []
    for page_index in range(page_count):
        steps.extend(item_grid_page_click_steps(config, page_index))
        if page_index < page_count - 1:
            steps.append(page_scroll_actions(config))
    return steps


def run_tap_page_test(
    config: dict[str, Any],
    page_count: int,
    snapshotter: MovementSnapshotter | None = None,
) -> None:
    steps = tap_page_test_steps(config, page_count)
    if not steps or not any(steps):
        print("No page tap/scroll actions configured.")
        return

    positions_per_page = len(config.get("advance", {}).get("item_positions", []))
    print(f"Tap page test pages: {page_count}")
    print(f"Configured positions per page: {positions_per_page}")
    for step_index, actions in enumerate(steps, start=1):
        if not actions:
            print(f"Step {step_index}: no actions")
            continue
        print(f"Step {step_index}:")
        for action in actions:
            print(f"  {describe_advance_action(config, action)}")
        run_advance_action_group(config, actions, snapshotter, f"tap_page_step_{step_index:04d}")


def all_items_scroll_strategy(config: dict[str, Any]) -> str:
    strategy = str(detected_items_config(config).get("all_items_scroll_strategy", "fixed"))
    if strategy not in {"fixed", "verified_row_step"}:
        raise AutomationError("advance.detect_items.all_items_scroll_strategy must be one of: fixed, verified_row_step")
    return strategy


def advance_after_detected_viewport(
    config: dict[str, Any],
    scroll_mode: str,
    previous_image: Any,
    previous_signed_boxes: list[dict[str, Any]],
    snapshotter: MovementSnapshotter | None,
    label_prefix: str,
    snap_target_rows: list[float] | None = None,
) -> bool:
    detect_config = detected_items_config(config)
    if scroll_mode != "all" or all_items_scroll_strategy(config) == "fixed":
        scroll_actions = detected_page_scroll_actions(config, scroll_mode)
        if not scroll_actions:
            raise AutomationError("No detected page scroll actions configured.")
        print("Scroll after viewport:")
        for action in scroll_actions:
            print(f"  {describe_advance_action(config, action)}")
        run_advance_action_group(config, scroll_actions, snapshotter, f"{label_prefix}_scroll")
        snap_detected_item_rows_after_scroll(
            config,
            snap_target_rows,
            snapshotter,
            f"{label_prefix}_scroll_snap",
        )
        return True

    scroll_actions = detected_verified_scroll_actions(config)
    if not scroll_actions:
        raise AutomationError("No verified all-items scroll actions configured.")

    target_rows = float(detect_config.get("verified_scroll_target_rows", 2.0))
    max_attempts = max(1, int(detect_config.get("verified_scroll_max_attempts", 5)))
    best_shift_rows: float | None = None

    for attempt_index in range(max_attempts):
        print(f"Verified row-step scroll {attempt_index + 1}/{max_attempts}; target={target_rows:g} rows:")
        for action in scroll_actions:
            print(f"  {describe_advance_action(config, action)}")
        run_advance_action_group(
            config,
            scroll_actions,
            snapshotter,
            f"{label_prefix}_verified_scroll_{attempt_index + 1:04d}",
        )

        image = capture_screenshot(config)
        boxes = detect_visible_item_boxes(image, config)
        signed_boxes, _boxes_to_click, _skipped_boxes, row_centers, progress = prepare_detected_viewport(
            image,
            boxes,
            [],
            config,
            -1,
            previous_signed_boxes,
        )
        image_shift = estimate_detected_region_vertical_shift(previous_image, image, config)
        image_shift_rows = detected_region_shift_rows(image_shift, row_centers)
        progress_matches = int(progress.get("matched_previous_count", 0)) if progress is not None else 0
        if image_shift_rows is not None:
            best_shift_rows = image_shift_rows
            progress_text = (
                f"{best_shift_rows:.2f} rows by image shift "
                f"({image_shift['shift_y']}px, score={image_shift['score']}, baseline={image_shift['baseline_score']}, "
                f"fingerprint_matches={progress_matches})"
            )
        elif image_shift is not None:
            progress_text = (
                f"{image_shift['shift_y']}px by image shift, row spacing unavailable "
                f"(score={image_shift['score']}, baseline={image_shift['baseline_score']}, "
                f"fingerprint_matches={progress_matches})"
            )
        else:
            progress_text = f"image shift unavailable, fingerprint_matches={progress_matches}"
        print(
            f"  observed after scroll: cards={len(signed_boxes)} rows={len(row_centers)} "
            f"movement={progress_text}"
        )

        if best_shift_rows is not None and best_shift_rows >= target_rows:
            print(f"  verified scroll target reached: {best_shift_rows:.2f} rows.")
            return True
        if best_shift_rows is not None and abs(best_shift_rows) <= float(detect_config.get("all_items_stop_scroll_rows_threshold", 0.5)):
            print("  image shift shows little movement; continuing with another small scroll.")
            continue

        if image_shift is None:
            print("  image shift was unavailable; continuing with another small scroll.")
            continue

        if image_shift_rows is None:
            print("  row spacing unavailable; stopping before re-clicking an unverified viewport.")
            return False

    if best_shift_rows is None:
        print("  verified scroll ended without a measured row movement; stopping before re-clicking.")
    else:
        print(
            f"  verified scroll max attempts reached at {best_shift_rows:.2f} rows, "
            f"below target={target_rows:g}; stopping before re-clicking."
        )
    return False


def run_tap_detected_page_test(
    config: dict[str, Any],
    page_count: int | None,
    snapshotter: MovementSnapshotter | None = None,
    calibration_delay: float | None = None,
    calibrate_desktop: bool = True,
    debug_dir: Path | None = None,
) -> None:
    if page_count is not None and page_count < 1:
        raise AutomationError("--tap-detected-page-test must be at least 1.")

    detect_config = detected_items_config(config)
    skip_seen_cards = bool(detect_config.get("skip_seen_cards", True))
    min_new_after_scroll = int(detect_config.get("min_new_cards_after_scroll", 1))
    max_extra_scrolls = int(detect_config.get("max_extra_scrolls_per_page", 3))
    max_all_pages = int(detect_config.get("max_all_pages", 200))
    skip_seen_rows_after_scroll = int(detect_config.get("skip_seen_rows_after_scroll", 2))
    skip_seen_rows_after_crawl = int(detect_config.get("skip_seen_rows_after_crawl_scroll", 1))
    row_snap_enabled = bool(detect_config.get("row_snap_after_scroll", True))
    log_detected_items = bool(detect_config.get("log_detected_items", False))
    seen_card_signatures: list[dict[str, Any]] = []
    snap_target_rows: list[float] | None = None
    previous_signed_boxes: list[dict[str, Any]] | None = None
    total_clicked = 0

    if page_count is None:
        print(f"Detected all-items test; max pages: {max_all_pages}")
    else:
        print(f"Detected page test pages: {page_count}")
    scroll_mode = "all" if page_count is None else "page"
    did_calibrate = False
    page_index = 0
    while page_count is None or page_index < page_count:
        if page_count is None and page_index >= max_all_pages:
            raise AutomationError(
                f"Stopped after advance.detect_items.max_all_pages={max_all_pages}. "
                "Increase that value if the inventory is larger."
            )

        extra_scrolls = 0
        while True:
            image = capture_screenshot(config)
            boxes = detect_visible_item_boxes(image, config)
            skip_row_limit = None
            if page_index > 0:
                if page_count is None and skip_seen_rows_after_crawl >= 0:
                    skip_row_limit = skip_seen_rows_after_crawl
                elif page_count is not None and skip_seen_rows_after_scroll >= 0:
                    skip_row_limit = skip_seen_rows_after_scroll
            signed_boxes, boxes_to_click, skipped_boxes, row_centers, progress = prepare_detected_viewport(
                image,
                boxes,
                seen_card_signatures,
                config,
                skip_row_limit,
                previous_signed_boxes,
            )
            actions = [detected_item_action(config, tuple(box["center"])) for box in boxes_to_click]

            if row_snap_enabled and page_index == 0 and snap_target_rows is None and boxes:
                snap_target_rows = row_centers
                print(f"Row snap target centers: {[round(value, 1) for value in snap_target_rows]}")
            viewport_label = f"tap_detected_{'all' if page_count is None else 'page'}_{page_index + 1:04d}"
            if extra_scrolls:
                viewport_label += f"_extra_{extra_scrolls:04d}"
            json_path, image_path = write_detected_viewport_debug(
                config,
                debug_dir,
                viewport_label,
                image,
                signed_boxes,
                boxes_to_click,
                skipped_boxes,
                row_centers,
                progress,
            )
            print_detected_viewport_summary(
                config,
                f"Viewport {page_index + 1}",
                signed_boxes,
                boxes_to_click,
                skipped_boxes,
                row_centers,
                progress,
                json_path,
                image_path,
            )
            if log_detected_items:
                skipped_centers = {tuple(skipped.get("center", [])) for skipped in skipped_boxes}
                signed_by_center = {
                    tuple(signed.get("center", [])): signed
                    for signed in signed_boxes
                }
                for number, box in enumerate(boxes, start=1):
                    center = box["center"]
                    status = "seen" if tuple(center) in skipped_centers else "new"
                    signed = signed_by_center.get(tuple(center), {})
                    match_detail = ""
                    if signed.get("signature_distance") is not None:
                        match_detail = (
                            f" nearest_h={signed['signature_distance']} "
                            f"nearest_color={signed.get('signature_color_distance', '?')}"
                        )
                    if signed.get("seen_match_ignored"):
                        match_detail += " seen_match_ignored"
                    if signed.get("row_index") is not None:
                        match_detail += f" row={signed['row_index'] + 1}"
                    print(
                        f"  item {number}: center=({center[0]:.0f}, {center[1]:.0f}) "
                        f"bbox={box['bbox']} pixels={box['pixels']} {status}{match_detail}"
                    )

            if not boxes:
                if page_count is None:
                    print("No visible equipment item cards detected; stopping all-items test.")
                    print(f"Total clicked: {total_clicked}")
                    return
                raise AutomationError("No visible equipment item cards detected. Check advance.detect_items.region.")

            if page_count is None and page_index > 0 and detected_viewport_scroll_stalled(progress, config):
                print(
                    "Scroll appears stalled at the same viewport; stopping all-items test "
                    "before re-clicking repeated cards."
                )
                print(f"Total clicked: {total_clicked}")
                return

            should_scroll_again = (
                page_index > 0
                and skip_seen_cards
                and len(actions) < min_new_after_scroll
                and extra_scrolls < max_extra_scrolls
            )
            if not should_scroll_again:
                break

            scroll_actions = detected_page_scroll_actions(config, scroll_mode)
            if not scroll_actions:
                raise AutomationError("No detected page scroll actions configured.")
            print(
                f"Only {len(actions)} new card(s) visible after scroll; "
                f"scrolling again ({extra_scrolls + 1}/{max_extra_scrolls})."
            )
            for action in scroll_actions:
                print(f"  {describe_advance_action(config, action)}")
            previous_signed_boxes = signed_boxes
            run_advance_action_group(
                config,
                scroll_actions,
                snapshotter,
                f"tap_detected_page_{page_index + 1:04d}_extra_scroll_{extra_scrolls + 1:04d}",
            )
            snap_detected_item_rows_after_scroll(
                config,
                snap_target_rows,
                snapshotter,
                f"tap_detected_page_{page_index + 1:04d}_extra_scroll_snap",
            )
            extra_scrolls += 1

        if not actions:
            if page_count is None:
                print("No unseen equipment item cards detected after scrolling; stopping all-items test.")
                print(f"Total clicked: {total_clicked}")
                return
            raise AutomationError(
                "No unseen equipment item cards detected after scrolling. "
                "Try increasing advance.detect_items.max_extra_scrolls_per_page or disable skip_seen_cards."
            )

        if (
            page_index == 0
            and calibrate_desktop
            and not did_calibrate
            and detected_items_input_backend(config) == "desktop"
        ):
            runtime_calibrate_detected_items(config, boxes, calibration_delay)
            did_calibrate = True

        for item_index, action in enumerate(actions, start=1):
            box = boxes_to_click[item_index - 1]
            print(
                f"Click viewport {page_index + 1} {detected_item_box_grid_label(box)}: "
                f"{describe_advance_action(config, action)}"
            )
            run_advance_action_group(
                config,
                [action],
                snapshotter,
                f"tap_detected_page_{page_index + 1:04d}_item_{item_index:04d}",
            )
            signature = boxes_to_click[item_index - 1].get("signature")
            if isinstance(signature, dict):
                seen_card_signatures.append(signature)
            total_clicked += 1

        should_scroll_to_next_page = page_count is None or page_index < page_count - 1
        if should_scroll_to_next_page:
            previous_signed_boxes = signed_boxes
            advanced = advance_after_detected_viewport(
                config,
                scroll_mode,
                image,
                previous_signed_boxes,
                snapshotter,
                f"tap_detected_page_{page_index + 1:04d}",
                snap_target_rows,
            )
            if not advanced:
                print("Stopping detected tap test because verified scroll did not reach the next viewport.")
                return
        page_index += 1

    print(f"Total clicked: {total_clicked}")


def print_mouse_position(delay_seconds: float) -> None:
    pyautogui = require_package("pyautogui")
    if delay_seconds > 0:
        print(f"Move your mouse to the target; reading position in {delay_seconds:g}s...")
        time.sleep(delay_seconds)
    x, y = pyautogui.position()
    print(f"Mouse position: ({x}, {y})")


def create_movement_snapshotter(debug_dir: Path, config: dict[str, Any]) -> MovementSnapshotter:
    snapshot_dir = debug_dir / "movement_snapshots" / now_stamp()
    snapshotter = MovementSnapshotter(snapshot_dir, config)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    return snapshotter


def finish_movement_snapshotter(snapshotter: MovementSnapshotter | None) -> None:
    if snapshotter is None:
        return
    manifest_path = snapshotter.write_manifest()
    print(f"Movement snapshots: {snapshotter.debug_dir}")
    print(f"Movement snapshot manifest: {manifest_path}")


def resolve_snapshot_manifest(path: Path) -> Path:
    expanded = path.expanduser()
    if expanded.is_dir():
        manifest_path = expanded / "manifest.json"
    else:
        manifest_path = expanded
    if not manifest_path.exists():
        raise AutomationError(f"Movement snapshot manifest does not exist: {manifest_path}")
    return manifest_path


def resolve_snapshot_image_path(manifest_path: Path, image_value: Any) -> Path:
    image_path = Path(str(image_value)).expanduser()
    if image_path.is_absolute() and image_path.exists():
        return image_path
    if image_path.exists():
        return image_path

    sibling = manifest_path.parent / image_path.name
    if sibling.exists():
        return sibling

    relative_to_manifest = manifest_path.parent / image_path
    if relative_to_manifest.exists():
        return relative_to_manifest
    return image_path


def movement_entry_image_click_point(entry: dict[str, Any]) -> tuple[float, float] | None:
    action = entry.get("action", {})
    if action.get("type") not in {"click", "adb_tap"}:
        return None

    origin = entry.get("snapshot_origin") or [0, 0]
    coordinate_space = entry.get("snapshot_coordinate_space", "screen")
    if coordinate_space == "adb":
        return float(action["x"]) - float(origin[0]), float(action["y"]) - float(origin[1])

    mouse_position = entry.get("mouse_position")
    if not mouse_position:
        return None
    return float(mouse_position[0]) - float(origin[0]), float(mouse_position[1]) - float(origin[1])


def movement_entry_full_point(entry: dict[str, Any], image_x: float, image_y: float) -> tuple[float, float]:
    origin = entry.get("snapshot_origin") or [0, 0]
    return image_x + float(origin[0]), image_y + float(origin[1])


def find_changed_region_between_images(
    before_image: Any,
    after_image: Any,
    center_x: float,
    center_y: float,
    config: dict[str, Any],
) -> dict[str, Any] | None:
    ImageChops = require_package("PIL.ImageChops", "Pillow")
    analysis = config.get("advance", {}).get("movement_snapshot_analysis", {})
    radius = analysis.get("search_radius", [260, 100])
    if not isinstance(radius, list) or len(radius) != 2:
        raise AutomationError("advance.movement_snapshot_analysis.search_radius must be [x_radius, y_radius].")

    threshold = int(analysis.get("diff_threshold", 18))
    min_pixels = int(analysis.get("min_changed_pixels", 35))
    radius_x = int(radius[0])
    radius_y = int(radius[1])

    width, height = before_image.size
    left = max(0, int(round(center_x - radius_x)))
    top = max(0, int(round(center_y - radius_y)))
    right = min(width, int(round(center_x + radius_x)))
    bottom = min(height, int(round(center_y + radius_y)))
    if right <= left or bottom <= top:
        return None

    before_crop = before_image.crop((left, top, right, bottom)).convert("RGB")
    after_crop = after_image.crop((left, top, right, bottom)).convert("RGB")
    diff = ImageChops.difference(before_crop, after_crop).convert("L")

    pixels = diff.load()
    changed: list[tuple[int, int]] = []
    crop_width, crop_height = diff.size
    for y in range(crop_height):
        for x in range(crop_width):
            if pixels[x, y] >= threshold:
                changed.append((x, y))

    if len(changed) < min_pixels:
        return None

    min_x = min(point[0] for point in changed)
    max_x = max(point[0] for point in changed)
    min_y = min(point[1] for point in changed)
    max_y = max(point[1] for point in changed)
    bbox = [left + min_x, top + min_y, left + max_x + 1, top + max_y + 1]
    return {
        "bbox": bbox,
        "center": [(bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0],
        "changed_pixels": len(changed),
        "search_box": [left, top, right, bottom],
    }


def median_number(values: list[float]) -> float:
    sorted_values = sorted(values)
    count = len(sorted_values)
    if count == 0:
        raise AutomationError("Cannot calculate median of an empty list.")
    midpoint = count // 2
    if count % 2:
        return sorted_values[midpoint]
    return (sorted_values[midpoint - 1] + sorted_values[midpoint]) / 2.0


def suggested_click_point_from_detection(
    entry: dict[str, Any],
    detected_full_x: float,
    detected_full_y: float,
    config: dict[str, Any],
) -> tuple[float, float]:
    action = entry.get("action", {})
    coordinate_space = entry.get("snapshot_coordinate_space", "screen")
    if coordinate_space == "adb":
        return detected_full_x, detected_full_y

    mouse_position = entry.get("mouse_position")
    if not mouse_position:
        return float(action["x"]), float(action["y"])
    scale_x, scale_y = desktop_scale(config)
    delta_x = (detected_full_x - float(mouse_position[0])) / scale_x
    delta_y = (detected_full_y - float(mouse_position[1])) / scale_y
    return float(action["x"]) + delta_x, float(action["y"]) + delta_y


def analyze_movement_snapshots(manifest_path: Path, config: dict[str, Any]) -> dict[str, Any]:
    Image = require_package("PIL.Image", "Pillow")
    manifest = resolve_snapshot_manifest(manifest_path)
    entries = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(entries, list):
        raise AutomationError(f"Movement snapshot manifest must contain a list: {manifest}")

    by_label: dict[str, dict[str, dict[str, Any]]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("label", ""))
        phase = str(entry.get("phase", ""))
        if label and phase:
            by_label.setdefault(label, {})[phase] = entry

    detections: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    suggestions_by_action: dict[tuple[float, float], list[tuple[float, float]]] = {}

    for label, phases in sorted(by_label.items()):
        before_entry = phases.get("before_click") or phases.get("before_adb_tap")
        click_entry = phases.get("after_click") or phases.get("click_down") or phases.get("after_adb_tap")
        if not before_entry or not click_entry:
            continue

        action = click_entry.get("action", {})
        if action.get("type") not in {"click", "adb_tap"}:
            continue

        click_point = movement_entry_image_click_point(click_entry)
        if click_point is None:
            unresolved.append({"label": label, "reason": "Could not resolve click point in snapshot image."})
            continue

        before_path = resolve_snapshot_image_path(manifest, before_entry.get("image"))
        click_path = resolve_snapshot_image_path(manifest, click_entry.get("image"))
        if not before_path.exists() or not click_path.exists():
            unresolved.append({"label": label, "reason": "Snapshot image file is missing."})
            continue

        before_image = Image.open(before_path).convert("RGB")
        click_image = Image.open(click_path).convert("RGB")
        region = find_changed_region_between_images(before_image, click_image, click_point[0], click_point[1], config)
        if region is None:
            unresolved.append(
                {
                    "label": label,
                    "reason": "No click highlight detected near the tap. Check ADB screenshots, crop, or diff thresholds.",
                    "click_image_point": [round(click_point[0], 2), round(click_point[1], 2)],
                }
            )
            continue

        detected_full = movement_entry_full_point(click_entry, region["center"][0], region["center"][1])
        suggested = suggested_click_point_from_detection(click_entry, detected_full[0], detected_full[1], config)
        current = (float(action["x"]), float(action["y"]))
        suggestions_by_action.setdefault(current, []).append(suggested)
        detections.append(
            {
                "label": label,
                "action": action,
                "detected_full_point": [round(detected_full[0], 2), round(detected_full[1], 2)],
                "suggested_game_point": [round(suggested[0], 2), round(suggested[1], 2)],
                "delta_game": [round(suggested[0] - current[0], 2), round(suggested[1] - current[1], 2)],
                "bbox": [round(value, 2) for value in region["bbox"]],
                "changed_pixels": region["changed_pixels"],
            }
        )

    suggested_positions = []
    for current, suggestions in sorted(suggestions_by_action.items(), key=lambda item: (item[0][1], item[0][0])):
        xs = [point[0] for point in suggestions]
        ys = [point[1] for point in suggestions]
        suggested_positions.append(
            {
                "current": [round(current[0], 2), round(current[1], 2)],
                "suggested": [round(median_number(xs), 2), round(median_number(ys), 2)],
                "samples": len(suggestions),
            }
        )

    return {
        "manifest": str(manifest),
        "detections": detections,
        "unresolved": unresolved,
        "suggested_positions": suggested_positions,
    }


def print_movement_snapshot_analysis(analysis: dict[str, Any]) -> None:
    print(f"Analyzed movement snapshots: {analysis['manifest']}")
    print(f"Detected click highlights: {len(analysis['detections'])}")
    print(f"Unresolved click pairs: {len(analysis['unresolved'])}")

    if analysis["suggested_positions"]:
        print("\nSuggested item_positions:")
        print(
            json.dumps(
                [entry["suggested"] for entry in analysis["suggested_positions"]],
                indent=2,
                ensure_ascii=False,
            )
        )
        print("\nPer-position detail:")
        for entry in analysis["suggested_positions"]:
            print(
                f"  current={entry['current']} suggested={entry['suggested']} "
                f"samples={entry['samples']}"
            )
    else:
        print(
            "\nNo position suggestions were produced. Re-run a movement test with "
            "--movement-snapshots so the manifest includes before_click/click_down ADB pairs."
        )

    if analysis["unresolved"]:
        print("\nUnresolved examples:")
        for entry in analysis["unresolved"][:8]:
            print(f"  {entry.get('label', '(unknown)')}: {entry.get('reason')}")


def latest_movement_snapshot_manifest(debug_dir: Path) -> Path:
    manifests = sorted(
        (debug_dir / "movement_snapshots").glob("*/manifest.json"),
        key=lambda path: path.stat().st_mtime,
    )
    if not manifests:
        raise AutomationError(f"No movement snapshot manifests found under: {debug_dir / 'movement_snapshots'}")
    return manifests[-1]


def resolve_detected_tap_analysis_paths(path: Path) -> tuple[Path, Path]:
    expanded = path.expanduser()
    if expanded.is_file():
        manifest = resolve_snapshot_manifest(expanded)
        if manifest.parent.parent.name == "movement_snapshots":
            return manifest.parent.parent.parent, manifest
        return manifest.parent, manifest

    if (expanded / "manifest.json").exists():
        manifest = resolve_snapshot_manifest(expanded)
        if manifest.parent.parent.name == "movement_snapshots":
            return manifest.parent.parent.parent, manifest
        return expanded, manifest

    debug_dir = expanded
    return debug_dir, latest_movement_snapshot_manifest(debug_dir)


def load_detected_viewport_records(debug_dir: Path) -> list[dict[str, Any]]:
    viewport_dir = debug_dir / "detected_viewports"
    if not viewport_dir.exists():
        raise AutomationError(f"Detected viewport directory does not exist: {viewport_dir}")

    records: list[dict[str, Any]] = []
    for json_path in sorted(viewport_dir.glob("*.json"), key=lambda path: (path.stat().st_mtime, path.name)):
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        payload["_path"] = str(json_path)
        records.append(payload)
    if not records:
        raise AutomationError(f"No detected viewport JSON files found under: {viewport_dir}")
    return records


def detected_tap_label_candidates(label: str) -> tuple[list[str], int] | None:
    tap_match = re.search(r"tap_detected_page_(\d+)_item_(\d+)_action_\d+$", label)
    if tap_match:
        page_number = int(tap_match.group(1))
        item_number = int(tap_match.group(2))
        page = f"{page_number:04d}"
        return [f"tap_detected_all_{page}", f"tap_detected_page_{page}"], item_number

    submit_match = re.search(r"submit_detected_page_(\d+)_card_(\d+)_action_\d+$", label)
    if submit_match:
        page_number = int(submit_match.group(1))
        card_number = int(submit_match.group(2))
        page = f"{page_number:04d}"
        return [f"submit_detected_all_{page}"], card_number
    return None


def select_detected_viewport_record(
    viewport_records: list[dict[str, Any]],
    candidate_labels: list[str],
    item_number: int,
) -> dict[str, Any] | None:
    for candidate in candidate_labels:
        matches = [
            record
            for record in viewport_records
            if str(record.get("label", "")) == candidate
            or str(record.get("label", "")).startswith(f"{candidate}_extra_")
        ]
        matches = [
            record
            for record in matches
            if int(record.get("tap_count", 0)) >= item_number
        ]
        if matches:
            return matches[-1]
    return None


def detected_viewport_tap_boxes(viewport_record: dict[str, Any]) -> list[dict[str, Any]]:
    boxes = viewport_record.get("boxes", [])
    if not isinstance(boxes, list):
        return []
    return [
        box
        for box in boxes
        if isinstance(box, dict) and str(box.get("status", "")).startswith("tap")
    ]


def point_in_bbox(point: tuple[float, float], bbox: Any) -> bool:
    if not isinstance(bbox, list) or len(bbox) != 4:
        return False
    x, y = point
    left, top, right, bottom = [float(value) for value in bbox]
    return left <= x <= right and top <= y <= bottom


def nearest_detected_viewport_box(
    viewport_record: dict[str, Any],
    point: tuple[float, float],
) -> tuple[dict[str, Any] | None, float | None]:
    boxes = viewport_record.get("boxes", [])
    if not isinstance(boxes, list):
        return None, None

    containing = [
        box
        for box in boxes
        if isinstance(box, dict) and point_in_bbox(point, box.get("bbox"))
    ]
    candidates = containing or [box for box in boxes if isinstance(box, dict)]
    if not candidates:
        return None, None

    def distance(box: dict[str, Any]) -> float:
        center = box.get("center", [0, 0])
        return ((float(center[0]) - point[0]) ** 2 + (float(center[1]) - point[1]) ** 2) ** 0.5

    nearest = min(candidates, key=distance)
    return nearest, round(distance(nearest), 2)


def analyze_detected_taps(path: Path, config: dict[str, Any]) -> dict[str, Any]:
    Image = require_package("PIL.Image", "Pillow")
    debug_dir, manifest = resolve_detected_tap_analysis_paths(path)
    entries = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(entries, list):
        raise AutomationError(f"Movement snapshot manifest must contain a list: {manifest}")

    viewport_records = load_detected_viewport_records(debug_dir)
    by_label: dict[str, dict[str, dict[str, Any]]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("label", ""))
        phase = str(entry.get("phase", ""))
        if label and phase:
            by_label.setdefault(label, {})[phase] = entry

    detections: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []

    for label, phases in sorted(by_label.items()):
        parsed_label = detected_tap_label_candidates(label)
        if parsed_label is None:
            continue
        candidate_labels, item_number = parsed_label
        before_entry = phases.get("before_adb_tap") or phases.get("before_click")
        click_entry = phases.get("after_adb_tap") or phases.get("after_click") or phases.get("click_down")
        if not before_entry or not click_entry:
            unresolved.append({"label": label, "reason": "Missing before/after tap snapshots. Re-run with --movement-snapshots."})
            continue

        viewport = select_detected_viewport_record(viewport_records, candidate_labels, item_number)
        if viewport is None:
            unresolved.append({"label": label, "reason": f"No viewport JSON found for labels {candidate_labels}."})
            continue

        tap_boxes = detected_viewport_tap_boxes(viewport)
        if item_number < 1 or item_number > len(tap_boxes):
            unresolved.append({"label": label, "reason": f"Viewport has {len(tap_boxes)} planned taps; wanted tap {item_number}."})
            continue
        intended = tap_boxes[item_number - 1]

        click_point = movement_entry_image_click_point(click_entry)
        if click_point is None:
            unresolved.append({"label": label, "reason": "Could not resolve tap point in snapshot image."})
            continue

        before_path = resolve_snapshot_image_path(manifest, before_entry.get("image"))
        click_path = resolve_snapshot_image_path(manifest, click_entry.get("image"))
        if not before_path.exists() or not click_path.exists():
            unresolved.append({"label": label, "reason": "Snapshot image file is missing."})
            continue

        before_image = Image.open(before_path).convert("RGB")
        click_image = Image.open(click_path).convert("RGB")
        region = find_changed_region_between_images(before_image, click_image, click_point[0], click_point[1], config)
        if region is None:
            unresolved.append(
                {
                    "label": label,
                    "reason": "No tap highlight detected near the planned center.",
                    "viewport_label": viewport.get("label"),
                    "intended_label": intended.get("label"),
                    "intended_center": intended.get("center"),
                }
            )
            continue

        detected_full = movement_entry_full_point(click_entry, region["center"][0], region["center"][1])
        detected_point = (float(detected_full[0]), float(detected_full[1]))
        nearest, nearest_distance = nearest_detected_viewport_box(viewport, detected_point)
        intended_center = intended.get("center", [0, 0])
        delta = [
            round(detected_point[0] - float(intended_center[0]), 2),
            round(detected_point[1] - float(intended_center[1]), 2),
        ]
        status = "ok"
        if nearest is None:
            status = "unknown"
        elif nearest.get("label") != intended.get("label") and not point_in_bbox(detected_point, intended.get("bbox")):
            status = "mismatch"

        detections.append(
            {
                "label": label,
                "status": status,
                "viewport_label": viewport.get("label"),
                "tap_number": item_number,
                "intended_label": intended.get("label"),
                "intended_center": intended.get("center"),
                "detected_highlight_center": [round(detected_point[0], 2), round(detected_point[1], 2)],
                "delta_from_intended": delta,
                "nearest_label": nearest.get("label") if nearest else None,
                "nearest_status": nearest.get("status") if nearest else None,
                "nearest_distance": nearest_distance,
                "changed_pixels": region["changed_pixels"],
            }
        )

    viewport_summaries = [
        {
            "label": record.get("label"),
            "path": record.get("_path"),
            "tap_count": record.get("tap_count", 0),
            "seen_skip_count": record.get("seen_skip_count", 0),
            "row_centers": record.get("row_centers", []),
            "progress_from_previous": record.get("progress_from_previous"),
            "skipped_labels": [
                box.get("label")
                for box in record.get("boxes", [])
                if isinstance(box, dict) and box.get("status") == "seen_skip"
            ],
        }
        for record in viewport_records
    ]

    return {
        "debug_dir": str(debug_dir),
        "manifest": str(manifest),
        "detections": detections,
        "unresolved": unresolved,
        "viewport_summaries": viewport_summaries,
    }


def print_detected_tap_analysis(analysis: dict[str, Any]) -> None:
    detections = analysis["detections"]
    unresolved = analysis["unresolved"]
    mismatches = [entry for entry in detections if entry.get("status") == "mismatch"]
    print(f"Analyzed detected taps: {analysis['manifest']}")
    print(f"Detected tap highlights: {len(detections)}")
    print(f"Mismatched highlights: {len(mismatches)}")
    print(f"Unresolved taps: {len(unresolved)}")

    if detections:
        print("\nTap detail:")
        for entry in detections[:80]:
            print(
                f"  {entry['label']}: {entry['status']} intended={entry['intended_label']} "
                f"highlight={entry['detected_highlight_center']} delta={entry['delta_from_intended']} "
                f"nearest={entry['nearest_label']}({entry['nearest_status']})"
            )

    skipped_viewports = [
        entry
        for entry in analysis["viewport_summaries"]
        if int(entry.get("seen_skip_count", 0)) > 0
    ]
    if skipped_viewports:
        print("\nViewport skips:")
        for entry in skipped_viewports[:40]:
            progress = entry.get("progress_from_previous") or {}
            scroll = ""
            if progress.get("median_shift_rows") is not None:
                scroll = f" scroll_rows={progress['median_shift_rows']}"
            print(
                f"  {entry['label']}: tap={entry['tap_count']} seen_skip={entry['seen_skip_count']}"
                f"{scroll} skipped={entry['skipped_labels']}"
            )

    if unresolved:
        print("\nUnresolved examples:")
        for entry in unresolved[:12]:
            print(f"  {entry.get('label', '(unknown)')}: {entry.get('reason')}")


def calibrate_advance_offset(config: dict[str, Any], completed_index: int, delay_seconds: float | None) -> None:
    pyautogui = require_package("pyautogui")
    actions = preview_advance_actions(config, completed_index)
    click_action = next((action for action in actions if action.get("type") == "click"), None)
    if not click_action:
        raise AutomationError("No desktop click action found for this completed item index.")

    advance = config.get("advance", {})
    delay = float(delay_seconds if delay_seconds is not None else advance.get("runtime_calibration_delay_seconds", 3.0))
    game_x = float(click_action["x"])
    game_y = float(click_action["y"])
    scale_x, scale_y = desktop_scale(config)

    print(f"Advance click to calibrate: game=({int(game_x)}, {int(game_y)})")
    if delay > 0:
        print(f"Hover over the actual target item; reading position in {delay:g}s...")
        time.sleep(delay)
    mouse_x, mouse_y = pyautogui.position()
    offset_x = int(round(mouse_x - game_x * scale_x))
    offset_y = int(round(mouse_y - game_y * scale_y))
    print(f"Mouse position: ({mouse_x}, {mouse_y})")
    print(f'Set "desktop_coordinate_offset": [{offset_x}, {offset_y}]')


def write_csv_log(csv_path: Path, rows: Iterable[dict[str, Any]]) -> None:
    rows = list(rows)
    if not rows:
        return

    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)

    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            flat = {
                key: json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value
                for key, value in row.items()
            }
            writer.writerow(flat)


def equipment_identity_key(parsed: dict[str, Any]) -> str | None:
    equipment_name = parsed.get("equipment_name") or build_equipment_name(parsed)
    effects = parsed.get("on_equip_effects", [])
    if not equipment_name or not isinstance(effects, list) or not effects:
        return None

    normalized_effects = []
    for effect in effects:
        if not isinstance(effect, dict):
            continue
        normalized_effects.append(
            {
                "name": str(effect.get("name", "")).strip(),
                "value": effect.get("value"),
            }
        )

    return json.dumps(
        {
            "equipment_name": str(equipment_name).strip(),
            "equipment_slot": parsed.get("equipment_slot"),
            "on_equip_effects": normalized_effects,
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def parsed_duplicate_status(
    parsed: dict[str, Any],
    seen_equipment_keys: set[str],
    config: dict[str, Any],
) -> tuple[str | None, str]:
    if not config.get("skip_duplicate_parsed_equipment", True):
        return None, ""

    identity_key = equipment_identity_key(parsed)
    if not identity_key:
        return None, ""
    if identity_key in seen_equipment_keys:
        return identity_key, "parsed_equipment"
    return identity_key, ""


def default_reparse_output_path(ocr_path: Path) -> Path:
    if ocr_path.name.endswith("_ocr.txt"):
        return ocr_path.with_name(ocr_path.name[: -len("_ocr.txt")] + "_parsed.json")
    if ocr_path.suffix:
        return ocr_path.with_suffix(".json")
    return ocr_path.with_name(ocr_path.name + ".json")


def reparse_ocr_file(
    ocr_path: Path,
    config: dict[str, Any],
    output_path: Path | None = None,
) -> tuple[dict[str, Any], Path]:
    if not ocr_path.exists():
        raise AutomationError(f"OCR text file does not exist: {ocr_path}")

    raw_text = normalize_text(ocr_path.read_text(encoding="utf-8"), config)
    parsed = parse_equipment_text(raw_text, config)
    output = output_path or default_reparse_output_path(ocr_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(parsed, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return parsed, output


def compact_region_parse(parsed: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in parsed.items() if key not in {"ocr_regions"}}


def select_primary_ocr_region(
    regions: list[dict[str, Any]],
    preferred_name_or_role: str | None = None,
) -> dict[str, Any]:
    if preferred_name_or_role:
        preferred = str(preferred_name_or_role).lower()
        for region in regions:
            if str(region.get("name", "")).lower() == preferred or str(region.get("role", "")).lower() == preferred:
                return region
        available = ", ".join(str(region.get("name", "?")) for region in regions)
        raise AutomationError(f"Could not find OCR region '{preferred_name_or_role}'. Available: {available}")

    for region in regions:
        if region.get("primary"):
            return region
    raise AutomationError("No primary OCR region is configured.")


def process_one(
    index: int,
    config: dict[str, Any],
    debug_dir: Path,
    primary_only: bool = False,
    primary_region_name: str | None = None,
) -> EquipmentResult:
    full_image = capture_screenshot(config)
    debug_dir.mkdir(parents=True, exist_ok=True)
    full_image_path: Path | None = None
    if debug_save_full_screenshot(config):
        full_image_path = debug_dir / f"{index:04d}_full.png"
        full_image.save(full_image_path)

    regions = resolve_ocr_regions(config)
    primary_region = select_primary_ocr_region(regions, primary_region_name)
    if primary_only:
        regions = [primary_region]

    region_images: dict[str, Path | None] = {}
    region_ocr_paths: dict[str, Path] = {}
    region_parsed: dict[str, dict[str, Any]] = {}
    region_texts: dict[str, str] = {}
    region_hashes: dict[str, str] = {}

    for region in regions:
        name = str(region["name"])
        original = crop_image(full_image, region.get("crop"))
        processed = preprocess_for_ocr(original, config)
        raw_text = normalize_text(ocr_with_tesseract(processed, config), config)
        parsed = parse_equipment_text(raw_text, config)
        parsed["region_name"] = name
        parsed["region_role"] = region.get("role", "reference")
        if region.get("description"):
            parsed["region_description"] = region["description"]

        image_path, ocr_path, _parsed_path = save_region_debug_artifacts(
            debug_dir=debug_dir,
            index=index,
            region_name=name,
            image=processed,
            raw_text=raw_text,
            parsed=parsed,
            save_image=debug_save_region_images(config),
        )
        region_images[name] = image_path
        region_ocr_paths[name] = ocr_path
        region_parsed[name] = parsed
        region_texts[name] = raw_text
        region_hashes[name] = image_hash(processed)

    target_region = str(primary_region["name"])
    parsed = dict(region_parsed[target_region])
    parsed["primary_region"] = target_region
    parsed["ocr_regions"] = {name: compact_region_parse(value) for name, value in region_parsed.items()}

    for region in regions:
        name = str(region["name"])
        if name == target_region:
            continue
        attach_as = region.get("attach_as")
        if not attach_as and region.get("role") == "equipped":
            attach_as = "equipped_item"
        if attach_as:
            parsed[str(attach_as)] = compact_region_parse(region_parsed[name])

    image_path = region_images[target_region]
    ocr_path = region_ocr_paths[target_region]
    raw_text = region_texts[target_region]
    img_hash = region_hashes[target_region]

    if debug_save_legacy_primary_copy(config):
        # Keep the legacy debug filenames pointing at the primary/submitted item.
        save_debug_artifacts(
            debug_dir=debug_dir,
            index=index,
            image=preprocess_for_ocr(crop_image(full_image, primary_region.get("crop")), config),
            raw_text=raw_text,
            parsed=parsed,
            save_image=debug_save_region_images(config),
        )

    return EquipmentResult(
        index=index,
        image_path=image_path,
        ocr_text_path=ocr_path,
        raw_text=raw_text,
        parsed=parsed,
        image_hash=img_hash,
        target_region=target_region,
        region_image_paths=region_images,
        region_ocr_text_paths=region_ocr_paths,
        full_image_path=full_image_path,
    )


def crop_rect_from_bbox_or_center(image: Any, target: dict[str, Any], config: dict[str, Any]) -> Rect:
    width, height = image.size
    bbox = target.get("bbox")
    margin = config.get("advance", {}).get("card_tier_ocr_bbox_margin_px", 8)
    if isinstance(bbox, list) and len(bbox) == 4:
        left = float(bbox[0]) - float(margin)
        top = float(bbox[1]) - float(margin)
        right = float(bbox[2]) + float(margin)
        bottom = float(bbox[3]) + float(margin)
    else:
        center = target.get("center")
        if center is None and "x" in target and "y" in target:
            center = [target["x"], target["y"]]
        if not isinstance(center, list) or len(center) != 2:
            raise AutomationError("Card tier OCR target must include bbox, center, or x/y.")
        crop_size = config.get("advance", {}).get("card_tier_ocr_crop_size", [190, 170])
        if not isinstance(crop_size, list) or len(crop_size) != 2:
            raise AutomationError("advance.card_tier_ocr_crop_size must be [width, height].")
        half_width = float(crop_size[0]) / 2.0
        half_height = float(crop_size[1]) / 2.0
        left = float(center[0]) - half_width
        top = float(center[1]) - half_height
        right = float(center[0]) + half_width
        bottom = float(center[1]) + half_height

    left_i = max(0, int(round(left)))
    top_i = max(0, int(round(top)))
    right_i = min(width, int(round(right)))
    bottom_i = min(height, int(round(bottom)))
    if right_i <= left_i or bottom_i <= top_i:
        raise AutomationError(f"Card tier OCR crop is empty: {[left_i, top_i, right_i, bottom_i]}")
    return (left_i, top_i, right_i - left_i, bottom_i - top_i)


def crop_inner_rect(image: Any, rect: Rect | None) -> Any:
    if rect is None:
        return image
    left, top, width, height = rect
    image_width, image_height = image.size
    left_i = max(0, int(round(left)))
    top_i = max(0, int(round(top)))
    right_i = min(image_width, int(round(left + width)))
    bottom_i = min(image_height, int(round(top + height)))
    if right_i <= left_i or bottom_i <= top_i:
        raise AutomationError(f"Inner OCR crop is empty: {[left_i, top_i, right_i, bottom_i]}")
    return image.crop((left_i, top_i, right_i, bottom_i))


def card_tier_ocr_config(config: dict[str, Any]) -> dict[str, Any]:
    tier_config = json.loads(json.dumps(config))
    ocr_config = tier_config.setdefault("ocr", {})
    advance = tier_config.get("advance", {})
    ocr_config["psm"] = str(advance.get("card_tier_ocr_psm", 7))
    ocr_config["scale"] = float(advance.get("card_tier_ocr_scale", 5.0))
    ocr_config["contrast"] = float(advance.get("card_tier_ocr_contrast", 2.0))
    ocr_config["threshold"] = advance.get("card_tier_ocr_threshold", ocr_config.get("threshold"))
    extra_args = str(ocr_config.get("extra_args", "")).strip()
    whitelist = str(advance.get("card_tier_ocr_whitelist", "TtIilL1234"))
    whitelist_arg = f"-c tessedit_char_whitelist={whitelist}"
    ocr_config["extra_args"] = f"{extra_args} {whitelist_arg}".strip()
    return tier_config


def extract_card_tier(
    image: Any,
    target: dict[str, Any],
    config: dict[str, Any],
    debug_dir: Path | None = None,
    index: int | None = None,
    label: str = "card",
) -> tuple[int | None, str]:
    if not bool(config.get("advance", {}).get("ocr_card_tier", False)):
        return None, ""

    rect = crop_rect_from_bbox_or_center(image, target, config)
    card_image = crop_image(image, rect)
    inner_crop = parse_rect(config.get("advance", {}).get("card_tier_ocr_inner_crop"), "advance.card_tier_ocr_inner_crop")
    tier_image = crop_inner_rect(card_image, inner_crop)
    tier_config = card_tier_ocr_config(config)
    processed = preprocess_for_ocr(tier_image, tier_config)
    raw_text = normalize_text(ocr_with_tesseract(processed, tier_config), tier_config)
    tier = infer_tier_from_text(raw_text)

    if debug_dir is not None and index is not None:
        safe_label = safe_filename_part(label)
        save_region_debug_artifacts(
            debug_dir=debug_dir,
            index=index,
            region_name=f"{safe_label}_card_tier",
            image=processed,
            raw_text=raw_text,
            parsed={"tier": tier, "raw_text": raw_text, "crop": list(rect)},
            save_image=debug_save_region_images(config),
        )

    return tier, raw_text


def apply_tier_override(parsed: dict[str, Any], tier: int | None, source: str) -> None:
    if tier is None:
        return
    previous_tier = parsed.get("tier")
    parsed["tier"] = tier
    parsed["tier_source"] = source
    if previous_tier not in {None, "", tier}:
        parsed["tier_overrode"] = previous_tier
    parsed["equipment_name"] = build_equipment_name(parsed)


def print_result(result: EquipmentResult) -> None:
    print(f"\n[{result.index}] OCR text ({result.target_region})")
    print("-" * 40)
    print(result.raw_text or "(empty)")
    print("-" * 40)
    parsed_preview = {
        key: value
        for key, value in result.parsed.items()
        if key not in {"raw_text", "lines", "ocr_regions"}
    }
    print(json.dumps(parsed_preview, indent=2, ensure_ascii=False))
    if result.full_image_path:
        print(f"Saved full screenshot: {result.full_image_path}")
    if result.image_path:
        print(f"Saved image: {result.image_path}")
    print(f"Saved OCR:   {result.ocr_text_path}")
    if len(result.region_image_paths) > 1 and any(result.region_image_paths.values()):
        print("Region images:")
        for name, path in result.region_image_paths.items():
            if path:
                print(f"  {name}: {path}")


def submit_result_to_configured_website(
    result: EquipmentResult,
    config: dict[str, Any],
    driver: str,
    page: Any,
    debug_dir: Path,
) -> None:
    try:
        if driver == "chrome_applescript":
            submit_to_website_applescript(result.parsed, config)
        elif page is not None:
            submit_to_website(page, result.parsed, config)
    except AutomationError as exc:
        error_path = debug_dir / f"{result.index:04d}_submit_error.json"
        error_path.write_text(
            json.dumps(
                {
                    "error": str(exc),
                    "parsed": result.parsed,
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"Submit error debug: {error_path}")
        raise


def record_and_maybe_submit_result(
    result: EquipmentResult,
    config: dict[str, Any],
    driver: str,
    page: Any,
    debug_dir: Path,
    dry_run: bool,
    seen_equipment_keys: set[str],
    csv_rows: list[dict[str, Any]],
    metadata: dict[str, Any],
) -> tuple[str | None, str, bool]:
    identity_key, duplicate_reason = parsed_duplicate_status(result.parsed, seen_equipment_keys, config)
    if duplicate_reason:
        print(
            f"Duplicate parsed equipment detected at item {result.index}; "
            "skipping website submit."
        )
        submitted_or_new = False
    elif not dry_run:
        submit_result_to_configured_website(result, config, driver, page, debug_dir)
        submitted_or_new = True
        if identity_key:
            seen_equipment_keys.add(identity_key)
    else:
        submitted_or_new = True
        if identity_key:
            seen_equipment_keys.add(identity_key)

    csv_rows.append(
        {
            **metadata,
            "index": result.index,
            "skipped_duplicate": bool(duplicate_reason),
            "duplicate_reason": duplicate_reason,
            **result.parsed,
        }
    )
    return identity_key, duplicate_reason, submitted_or_new


def run_submit_manual_pages(
    config: dict[str, Any],
    debug_dir: Path,
    csv_log: Path,
    dry_run: bool,
    driver: str,
    page: Any = None,
    snapshotter: MovementSnapshotter | None = None,
    page_limit: int | None = None,
    primary_only: bool = False,
) -> None:
    detect_config = detected_items_config(config)
    max_pages = page_limit if page_limit is not None else int(detect_config.get("max_all_pages", 200))
    click_delay = float(detect_config.get("manual_page_click_delay_seconds", 2.0))
    seen_equipment_keys: set[str] = set()
    csv_rows: list[dict[str, Any]] = []
    total_processed = 0
    total_submitted = 0
    total_duplicates = 0
    total_tap_retries = 0
    previous_observed_identity_key: str | None = None
    previous_observed_image_hash: str | None = None
    previous_observed_box: dict[str, Any] | None = None

    retry_offsets = configured_tap_retry_offsets(config)
    max_tap_retries = max(0, int(detect_config.get("tap_retry_attempts", 0)))
    retry_delay = float(detect_config.get("tap_retry_delay_seconds", 0.15))

    print(
        "Manual-page submit mode. For each visible page, you choose the first card; "
        "the script enters that card, then only cards to its right and rows below."
    )
    print("After each page finishes, scroll manually and repeat. Type q at a prompt to stop.")

    for page_index in range(max_pages):
        response = input(
            f"\nManual page {page_index + 1}: press Enter, then click the first card to enter "
            f"within {click_delay:g}s. Type q then Enter to finish: "
        ).strip().lower()
        if response in {"q", "quit", "done", "stop", "exit"}:
            break

        before_image = capture_screenshot(config)
        print(f"Click the first desired card now. Capturing selection in {click_delay:g}s...")
        if click_delay > 0:
            time.sleep(click_delay)
        after_image = capture_screenshot(config)

        boxes = detect_visible_item_boxes(after_image, config)
        if not boxes:
            print("No visible equipment item cards detected after your click; stopping.")
            break

        signed_boxes, row_centers = prepare_manual_detected_viewport(boxes, config)
        progress = None
        selected_box = detect_manual_selected_box(before_image, after_image, signed_boxes, config)
        boxes_to_click = boxes_after_manual_selection(signed_boxes, selected_box)
        json_path, image_path = write_detected_viewport_debug(
            config,
            debug_dir,
            f"submit_manual_page_{page_index + 1:04d}",
            after_image,
            signed_boxes,
            boxes_to_click,
            [],
            row_centers,
            progress,
        )
        selected_label = detected_item_box_grid_label(selected_box)
        selected_score = selected_box.get("manual_selection_changed_pixels")
        print(
            f"Manual start: {selected_label}@({float(selected_box['center'][0]):.0f},"
            f"{float(selected_box['center'][1]):.0f}) changed_pixels={selected_score}"
        )
        print_detected_viewport_summary(
            config,
            f"Manual viewport {page_index + 1}",
            signed_boxes,
            boxes_to_click,
            [],
            row_centers,
            progress,
            json_path,
            image_path,
        )

        page_submitted = 0
        page_duplicates = 0

        result = process_one(total_processed, config, debug_dir, primary_only=primary_only)
        print_result(result)
        identity_key, duplicate_reason, submitted_or_new = record_and_maybe_submit_result(
            result,
            config,
            driver,
            page,
            debug_dir,
            dry_run,
            seen_equipment_keys,
            csv_rows,
            {
                "manual_page": page_index + 1,
                "manual_selected": True,
                "detected_card": 0,
                "detected_card_label": selected_label,
                "detected_card_center": selected_box.get("center"),
                "tap_retry_discarded": False,
                "tap_retry_count": 0,
            },
        )
        total_processed += 1
        if duplicate_reason:
            page_duplicates += 1
            total_duplicates += 1
        elif submitted_or_new:
            page_submitted += 1
            total_submitted += 1
        previous_observed_identity_key = identity_key or equipment_identity_key(result.parsed)
        previous_observed_image_hash = result.image_hash
        previous_observed_box = selected_box

        for card_index, box in enumerate(boxes_to_click, start=1):
            result = None
            result_index = None
            identity_key = None
            duplicate_reason = ""
            retry_discarded = 0

            for attempt_index in range(max_tap_retries + 1):
                offset = retry_offsets[min(attempt_index, len(retry_offsets) - 1)]
                action = detected_item_action_with_offset(config, tuple(box["center"]), offset)
                retry_note = "" if attempt_index == 0 else f" retry {attempt_index}/{max_tap_retries}"
                offset_note = "" if offset == (0.0, 0.0) else f" offset=({offset[0]:g},{offset[1]:g})"
                print(
                    f"Click manual page {page_index + 1} {detected_item_box_grid_label(box)}{retry_note}{offset_note}: "
                    f"{describe_advance_action(config, action)}"
                )
                run_advance_action_group(
                    config,
                    [action],
                    snapshotter,
                    f"submit_manual_page_{page_index + 1:04d}_card_{card_index:04d}_try_{attempt_index + 1:02d}",
                )

                result_index = total_processed
                result = process_one(result_index, config, debug_dir, primary_only=primary_only)
                print_result(result)
                total_processed += 1

                identity_key, duplicate_reason = parsed_duplicate_status(result.parsed, seen_equipment_keys, config)
                should_retry = should_retry_unchanged_equipment_tap(
                    config,
                    identity_key,
                    result.image_hash,
                    previous_observed_identity_key,
                    previous_observed_image_hash,
                    box,
                    previous_observed_box,
                    attempt_index,
                    max_tap_retries,
                )
                if should_retry:
                    retry_discarded += 1
                    total_tap_retries += 1
                    print(
                        "Tap verification saw the same equipment details after tapping a different card; "
                        "retrying this card."
                    )
                    csv_rows.append(
                        {
                            "index": result_index,
                            "manual_page": page_index + 1,
                            "manual_selected": False,
                            "detected_card": card_index,
                            "detected_card_label": detected_item_box_grid_label(box),
                            "detected_card_center": box.get("center"),
                            "tap_retry_discarded": True,
                            "tap_retry_attempt": attempt_index + 1,
                            "retry_reason": "unchanged_equipment_after_tap",
                            "skipped_duplicate": bool(duplicate_reason),
                            "duplicate_reason": duplicate_reason,
                            **result.parsed,
                        }
                    )
                    if retry_delay > 0:
                        time.sleep(retry_delay)
                    continue
                break

            if result is None or result_index is None:
                continue

            identity_key, duplicate_reason, submitted_or_new = record_and_maybe_submit_result(
                result,
                config,
                driver,
                page,
                debug_dir,
                dry_run,
                seen_equipment_keys,
                csv_rows,
                {
                    "manual_page": page_index + 1,
                    "manual_selected": False,
                    "detected_card": card_index,
                    "detected_card_label": detected_item_box_grid_label(box),
                    "detected_card_center": box.get("center"),
                    "tap_retry_discarded": False,
                    "tap_retry_count": retry_discarded,
                },
            )
            if duplicate_reason:
                page_duplicates += 1
                total_duplicates += 1
            elif submitted_or_new:
                page_submitted += 1
                total_submitted += 1

            previous_observed_identity_key = identity_key or equipment_identity_key(result.parsed)
            previous_observed_image_hash = result.image_hash
            previous_observed_box = box

        print(
            f"Manual page {page_index + 1} summary: submitted/new={page_submitted}, "
            f"duplicates={page_duplicates}, processed={1 + len(boxes_to_click)}"
        )
    else:
        print(f"Stopped after manual page limit ({max_pages}).")

    write_csv_log(csv_log, csv_rows)
    print(f"\nParsed CSV log: {csv_log}")
    print(
        f"Total processed={total_processed}, submitted/new={total_submitted}, "
        f"duplicates={total_duplicates}, tap_retries={total_tap_retries}"
    )


def run_submit_equipped_slots(
    config: dict[str, Any],
    debug_dir: Path,
    csv_log: Path,
    dry_run: bool,
    driver: str,
    page: Any = None,
    snapshotter: MovementSnapshotter | None = None,
    limit: int | None = None,
    manual_first: bool | None = None,
) -> None:
    slots = equipped_slot_positions(config)
    if not slots:
        raise AutomationError("No equipped slots configured in advance.equipped_slot_positions.")
    if limit is not None:
        slots = slots[:limit]

    primary_region_name = str(config.get("advance", {}).get("equipped_slot_ocr_region", "equipped"))
    if manual_first is None:
        manual_first = bool(config.get("advance", {}).get("equipped_slot_manual_first", True))
    seen_equipment_keys: set[str] = set()
    csv_rows: list[dict[str, Any]] = []
    total_submitted = 0
    total_duplicates = 0
    offset = (0.0, 0.0)
    start_index = 0

    print(
        f"Submit equipped slots: {len(slots)} configured slot(s); "
        f"OCR primary region='{primary_region_name}'."
    )
    if manual_first:
        first_slot = slots[0]
        delay = equipped_slot_manual_click_delay(config)
        response = input(
            f"\nEquipped slots: press Enter, then click the top-left equipped item "
            f"({first_slot['label']}) within {delay:g}s. Type q then Enter to stop: "
        ).strip().lower()
        if response in {"q", "quit", "done", "stop", "exit"}:
            write_csv_log(csv_log, csv_rows)
            print(f"\nParsed CSV log: {csv_log}")
            return

        before_image = capture_screenshot(config)
        print(f"Click the top-left equipped item now. Capturing selection in {delay:g}s...")
        if delay > 0:
            time.sleep(delay)
        after_image = capture_screenshot(config)
        offset, detection = estimate_equipped_slot_offset_from_manual_click(before_image, after_image, first_slot, config)
        if detection is None:
            raise AutomationError(
                "Could not calibrate the top-left equipped slot. "
                "Run again with --save-debug-images and make sure you click the top-left equipped card."
            )
        elif detection.get("offset_rejected"):
            raise AutomationError(
                "Top-left equipped-slot calibration looked too large, so the run stopped before tapping other slots. "
                f"Rejected offset={detection.get('rejected_offset')}, method={detection.get('method')}, "
                f"center={detection.get('center')}, detected_cards={detection.get('detected_cards')}. "
                "Run with --save-debug-images if we need to tune advance.equipped_slot_positions."
            )
        else:
            detail = (
                f"detected_cards={detection.get('detected_cards')}"
                if detection.get("method") == "equipped_slot_box_detection"
                else f"changed_pixels={detection.get('changed_pixels')}"
            )
            print(
                f"Top-left click offset: ({offset[0]:.1f}, {offset[1]:.1f}) "
                f"from {detection.get('method')} {detail} center={detection.get('center')}"
            )

        result = process_one(
            0,
            config,
            debug_dir,
            primary_only=True,
            primary_region_name=primary_region_name,
        )
        print_result(result)
        identity_key, duplicate_reason, submitted_or_new = record_and_maybe_submit_result(
            result,
            config,
            driver,
            page,
            debug_dir,
            dry_run,
            seen_equipment_keys,
            csv_rows,
            {
                "equipped_slot_index": 1,
                "equipped_slot_label": first_slot["label"],
                "equipped_slot_center": [first_slot["x"], first_slot["y"]],
                "manual_first_equipped_slot": True,
                "tap_offset": [round(offset[0], 1), round(offset[1], 1)],
                "detected_click_center": detection.get("center") if detection else None,
            },
        )
        if duplicate_reason:
            total_duplicates += 1
        elif submitted_or_new:
            total_submitted += 1
        if identity_key and not duplicate_reason:
            seen_equipment_keys.add(identity_key)
        start_index = 1

    for index, slot in enumerate(slots[start_index:], start=start_index):
        tap_slot = apply_equipped_slot_offset(slot, offset)
        action = equipped_slot_action(config, tap_slot)
        print(
            f"Click equipped slot {index + 1}/{len(slots)} {slot['label']}: "
            f"{describe_advance_action(config, action)}"
        )
        run_advance_action_group(
            config,
            [action],
            snapshotter,
            f"submit_equipped_slot_{index + 1:04d}_{safe_filename_part(str(slot['label']))}",
        )

        result = process_one(
            index,
            config,
            debug_dir,
            primary_only=True,
            primary_region_name=primary_region_name,
        )
        print_result(result)
        identity_key, duplicate_reason, submitted_or_new = record_and_maybe_submit_result(
            result,
            config,
            driver,
            page,
            debug_dir,
            dry_run,
            seen_equipment_keys,
            csv_rows,
            {
                "equipped_slot_index": index + 1,
                "equipped_slot_label": slot["label"],
                "equipped_slot_center": [slot["x"], slot["y"]],
                "equipped_slot_tap_center": [round(float(tap_slot["x"]), 1), round(float(tap_slot["y"]), 1)],
                "tap_offset": [round(offset[0], 1), round(offset[1], 1)],
                "manual_first_equipped_slot": False,
            },
        )
        if duplicate_reason:
            total_duplicates += 1
        elif submitted_or_new:
            total_submitted += 1
        if identity_key and not duplicate_reason:
            seen_equipment_keys.add(identity_key)

    write_csv_log(csv_log, csv_rows)
    print(f"\nParsed CSV log: {csv_log}")
    print(
        f"Equipped slot summary: processed={len(slots)}, "
        f"submitted/new={total_submitted}, duplicates={total_duplicates}"
    )


def run_submit_detected_all(
    config: dict[str, Any],
    debug_dir: Path,
    csv_log: Path,
    dry_run: bool,
    driver: str,
    page: Any = None,
    snapshotter: MovementSnapshotter | None = None,
    primary_only: bool = False,
) -> None:
    detect_config = detected_items_config(config)
    max_all_pages = int(detect_config.get("max_all_pages", 200))
    max_duplicate_pages = int(detect_config.get("max_duplicate_pages_before_stop", 3))
    seen_equipment_keys: set[str] = set()
    seen_card_signatures: list[dict[str, Any]] = []
    previous_signed_boxes: list[dict[str, Any]] | None = None
    snap_target_rows: list[float] | None = None
    csv_rows: list[dict[str, Any]] = []
    total_clicked = 0
    total_submitted = 0
    total_duplicates = 0
    total_visual_skips = 0
    total_tap_retries = 0
    previous_observed_identity_key: str | None = None
    previous_observed_image_hash: str | None = None
    previous_observed_box: dict[str, Any] | None = None
    duplicate_pages = 0

    print(f"Submit detected all-items; max pages: {max_all_pages}")
    for page_index in range(max_all_pages):
        image = capture_screenshot(config)
        boxes = detect_visible_item_boxes(image, config)
        if not boxes:
            print("No visible equipment item cards detected; stopping.")
            break

        skip_row_limit = None
        if page_index > 0:
            skip_rows = int(detect_config.get("skip_seen_rows_after_crawl_scroll", 1))
            if skip_rows >= 0:
                skip_row_limit = skip_rows
        signed_boxes, boxes_to_click, skipped_boxes, row_centers, progress = prepare_detected_viewport(
            image,
            boxes,
            seen_card_signatures,
            config,
            skip_row_limit,
            previous_signed_boxes,
        )
        json_path, image_path = write_detected_viewport_debug(
            config,
            debug_dir,
            f"submit_detected_all_{page_index + 1:04d}",
            image,
            signed_boxes,
            boxes_to_click,
            skipped_boxes,
            row_centers,
            progress,
        )
        print_detected_viewport_summary(
            config,
            f"Submit viewport {page_index + 1}",
            signed_boxes,
            boxes_to_click,
            skipped_boxes,
            row_centers,
            progress,
            json_path,
            image_path,
        )
        if snap_target_rows is None and row_centers:
            snap_target_rows = row_centers
            print(f"Submit row snap target centers: {[round(value, 1) for value in snap_target_rows]}")
        page_submitted = 0
        page_duplicates = 0
        page_visual_skips = len(skipped_boxes)
        total_visual_skips += page_visual_skips

        retry_offsets = configured_tap_retry_offsets(config)
        max_tap_retries = max(0, int(detect_config.get("tap_retry_attempts", 0)))
        retry_delay = float(detect_config.get("tap_retry_delay_seconds", 0.15))

        for card_index, box in enumerate(boxes_to_click, start=1):
            result: EquipmentResult | None = None
            result_index: int | None = None
            identity_key: str | None = None
            duplicate_reason = ""
            retry_discarded = 0

            for attempt_index in range(max_tap_retries + 1):
                offset = retry_offsets[min(attempt_index, len(retry_offsets) - 1)]
                action = detected_item_action_with_offset(config, tuple(box["center"]), offset)
                retry_note = "" if attempt_index == 0 else f" retry {attempt_index}/{max_tap_retries}"
                offset_note = "" if offset == (0.0, 0.0) else f" offset=({offset[0]:g},{offset[1]:g})"
                print(
                    f"Click submit viewport {page_index + 1} {detected_item_box_grid_label(box)}{retry_note}{offset_note}: "
                    f"{describe_advance_action(config, action)}"
                )
                run_advance_action_group(
                    config,
                    [action],
                    snapshotter,
                    f"submit_detected_page_{page_index + 1:04d}_card_{card_index:04d}_try_{attempt_index + 1:02d}",
                )
                signature = box.get("signature")
                if isinstance(signature, dict):
                    seen_card_signatures.append(signature)

                result_index = total_clicked
                result = process_one(result_index, config, debug_dir, primary_only=primary_only)
                print_result(result)
                total_clicked += 1

                identity_key, duplicate_reason = parsed_duplicate_status(result.parsed, seen_equipment_keys, config)
                should_retry = should_retry_unchanged_equipment_tap(
                    config,
                    identity_key,
                    result.image_hash,
                    previous_observed_identity_key,
                    previous_observed_image_hash,
                    box,
                    previous_observed_box,
                    attempt_index,
                    max_tap_retries,
                )
                if should_retry:
                    retry_discarded += 1
                    total_tap_retries += 1
                    print(
                        "Tap verification saw the same equipment details after tapping a different card; "
                        "retrying this card."
                    )
                    csv_rows.append(
                        {
                            "index": result_index,
                            "detected_page": page_index + 1,
                            "detected_card": card_index,
                            "detected_card_label": detected_item_box_grid_label(box),
                            "detected_card_center": box.get("center"),
                            "tap_retry_discarded": True,
                            "tap_retry_attempt": attempt_index + 1,
                            "retry_reason": "unchanged_equipment_after_tap",
                            "skipped_duplicate": bool(duplicate_reason),
                            "duplicate_reason": duplicate_reason,
                            **result.parsed,
                        }
                    )
                    if retry_delay > 0:
                        time.sleep(retry_delay)
                    continue
                break

            if result is None or result_index is None:
                continue

            if duplicate_reason:
                page_duplicates += 1
                total_duplicates += 1
                print(
                    f"Duplicate parsed equipment detected at clicked item {result_index}; "
                    "skipping website submit."
                )
            elif not dry_run:
                submit_result_to_configured_website(result, config, driver, page, debug_dir)
                page_submitted += 1
                total_submitted += 1
                if identity_key:
                    seen_equipment_keys.add(identity_key)
            else:
                page_submitted += 1
                total_submitted += 1
                if identity_key:
                    seen_equipment_keys.add(identity_key)

            previous_observed_identity_key = identity_key or equipment_identity_key(result.parsed)
            previous_observed_image_hash = result.image_hash
            previous_observed_box = box

            csv_rows.append(
                {
                    "index": result_index,
                    "detected_page": page_index + 1,
                    "detected_card": card_index,
                    "detected_card_label": detected_item_box_grid_label(box),
                    "detected_card_center": box.get("center"),
                    "tap_retry_discarded": False,
                    "tap_retry_count": retry_discarded,
                    "skipped_duplicate": bool(duplicate_reason),
                    "duplicate_reason": duplicate_reason,
                    **result.parsed,
                }
            )

        print(
            f"Page {page_index + 1} summary: submitted/new={page_submitted}, "
            f"duplicates={page_duplicates}, visual_skips={page_visual_skips}"
        )
        if page_submitted == 0 and (page_duplicates > 0 or page_visual_skips > 0 or not boxes_to_click):
            duplicate_pages += 1
        else:
            duplicate_pages = 0

        if duplicate_pages >= max_duplicate_pages:
            print(
                f"Stopping after {duplicate_pages} consecutive duplicate-only page(s). "
                f"Total clicked={total_clicked}, submitted/new={total_submitted}, duplicates={total_duplicates}."
            )
            break

        previous_signed_boxes = signed_boxes
        advanced = advance_after_detected_viewport(
            config,
            "all",
            image,
            previous_signed_boxes,
            snapshotter,
            f"submit_detected_page_{page_index + 1:04d}",
            snap_target_rows,
        )
        if not advanced:
            print("Stopping submit crawl because verified scroll did not reach the next viewport.")
            break
    else:
        raise AutomationError(
            f"Stopped after advance.detect_items.max_all_pages={max_all_pages}. "
            "Increase that value if the inventory is larger."
        )

    write_csv_log(csv_log, csv_rows)
    print(f"\nParsed CSV log: {csv_log}")
    print(
        f"Total clicked={total_clicked}, submitted/new={total_submitted}, "
        f"duplicates={total_duplicates}, visual_skips={total_visual_skips}, tap_retries={total_tap_retries}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path, help="Path to JSON config.")
    parser.add_argument("--limit", type=int, default=1, help="Maximum number of items to process.")
    parser.add_argument("--dry-run", action="store_true", help="OCR/parse only; do not submit or advance.")
    parser.add_argument(
        "--no-advance",
        action="store_true",
        help="Submit to the website but do not click/scroll to the next equipment item.",
    )
    parser.add_argument("--debug-dir", type=Path, help="Where screenshots/OCR logs should be written.")
    parser.add_argument(
        "--save-debug-images",
        action="store_true",
        help="Write full/cropped PNG debug images even when the config disables them.",
    )
    parser.add_argument("--csv-log", type=Path, help="Optional CSV path for parsed results.")
    parser.add_argument(
        "--reparse-ocr",
        type=Path,
        help="Parse an existing OCR text file and overwrite the matching *_parsed.json.",
    )
    parser.add_argument(
        "--reparse-output",
        type=Path,
        help="Optional output path for --reparse-ocr.",
    )
    parser.add_argument(
        "--reparse-debug-dir",
        type=Path,
        help="Reparse every *_ocr.txt file in an existing debug directory.",
    )
    parser.add_argument(
        "--pause-first",
        action="store_true",
        help="Wait for Enter before the first screenshot so you can position windows.",
    )
    parser.add_argument(
        "--advance-only",
        type=int,
        metavar="COMPLETED_INDEX",
        help="Run only the configured advance action for this completed item index, then exit.",
    )
    parser.add_argument(
        "--tap-scroll-test",
        type=int,
        metavar="ITEM_COUNT",
        help="Calibrate, then tap through ITEM_COUNT equipment slots using the configured scroll behavior.",
    )
    parser.add_argument(
        "--tap-page-test",
        type=int,
        metavar="PAGE_COUNT",
        help="Calibrate, then tap every configured row on each page, scrolling once between pages.",
    )
    parser.add_argument(
        "--tap-detected-page-test",
        type=int,
        metavar="PAGE_COUNT",
        help="Detect visible item cards from ADB screenshots, tap them, then scroll between pages.",
    )
    parser.add_argument(
        "--tap-detected-all-test",
        action="store_true",
        help="Detect and tap visible item cards, scrolling until no unseen cards remain.",
    )
    parser.add_argument(
        "--submit-detected-all",
        action="store_true",
        help="Click detected item cards, OCR/submit new parsed equipment, and scroll until duplicates indicate the end.",
    )
    parser.add_argument(
        "--submit-gear-list",
        dest="submit_gear_list",
        action="store_true",
        help="Manually choose the first card on each visible page, then OCR/submit that card and later cards only.",
    )
    parser.add_argument(
        "--submit-manual-pages",
        dest="submit_gear_list",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--submit-equipped-slots",
        action="store_true",
        help="Tap configured equipped-slot cards on the character screen and submit the equipped/left OCR region.",
    )
    parser.add_argument(
        "--unequip-website",
        action="store_true",
        help="On MIRPG Optimizer, visit each equipment category and click Unequip on the left/equipped compare item.",
    )
    parser.add_argument(
        "--dismantle-website",
        action="store_true",
        help="On MIRPG Optimizer, visit each equipment category, select every Manage Equipment item, and click Dismantle.",
    )
    parser.add_argument(
        "--equipped-slot-limit",
        type=int,
        default=None,
        help="Only process the first N configured equipped slots.",
    )
    parser.add_argument(
        "--no-equipped-manual-first",
        action="store_true",
        help="Do not prompt for a manual top-left equipped-slot click before tapping remaining equipped slots.",
    )
    parser.add_argument(
        "--manual-page-limit",
        type=int,
        default=None,
        help="Safety limit for --submit-gear-list prompts. Defaults to advance.detect_items.max_all_pages.",
    )
    parser.add_argument(
        "--primary-only",
        action="store_true",
        help="OCR only the primary/candidate region and skip equipped/reference regions for non-manual runs.",
    )
    parser.add_argument(
        "--include-reference-regions",
        action="store_true",
        help="With --submit-gear-list, also OCR equipped/reference regions for debugging.",
    )
    parser.add_argument(
        "--movement-snapshots",
        action="store_true",
        help="Save ADB/screen screenshots during movement tests, including before-click and after-click frames.",
    )
    parser.add_argument(
        "--analyze-movement-snapshots",
        type=Path,
        metavar="PATH",
        help="Analyze a movement snapshot directory or manifest and suggest corrected click positions.",
    )
    parser.add_argument(
        "--analyze-detected-taps",
        type=Path,
        metavar="PATH",
        help="Analyze detected-card viewport JSON plus movement snapshots to verify tap centers.",
    )
    parser.add_argument(
        "--mouse-position",
        nargs="?",
        const=3.0,
        type=float,
        metavar="SECONDS",
        help="Print the current desktop mouse position after an optional delay, then exit.",
    )
    parser.add_argument(
        "--calibrate-advance",
        type=int,
        metavar="COMPLETED_INDEX",
        help="Read the mouse position and print the desktop offset for the next item-grid click.",
    )
    parser.add_argument(
        "--calibration-delay",
        type=float,
        default=None,
        help="Seconds to wait before reading the mouse for startup or manual advance calibration.",
    )
    parser.add_argument(
        "--no-runtime-calibration",
        action="store_true",
        help="Skip the startup hover calibration for desktop item-grid advancement.",
    )
    args = parser.parse_args(argv)

    config = load_json(args.config.expanduser())
    if args.save_debug_images:
        debug_config = config.setdefault("debug_artifacts", {})
        if not isinstance(debug_config, dict):
            debug_config = {}
            config["debug_artifacts"] = debug_config
        debug_config["save_full_screenshot"] = True
        debug_config["save_region_images"] = True
        debug_config["save_legacy_primary_copy"] = True
    debug_dir = args.debug_dir or Path(config.get("debug_dir", f"equipment_ocr_debug/{now_stamp()}"))
    csv_log = args.csv_log or (debug_dir / "parsed_results.csv")

    if args.limit < 1:
        raise AutomationError("--limit must be at least 1.")
    if args.equipped_slot_limit is not None and args.equipped_slot_limit < 1:
        raise AutomationError("--equipped-slot-limit must be at least 1.")
    if args.manual_page_limit is not None and args.manual_page_limit < 1:
        raise AutomationError("--manual-page-limit must be at least 1.")
    if args.include_reference_regions and not args.submit_gear_list:
        raise AutomationError("--include-reference-regions is only used with --submit-gear-list.")
    if args.include_reference_regions and args.primary_only:
        raise AutomationError("Use either --primary-only or --include-reference-regions, not both.")

    if args.mouse_position is not None:
        print_mouse_position(args.mouse_position)
        return 0

    if args.analyze_movement_snapshots is not None:
        analysis = analyze_movement_snapshots(args.analyze_movement_snapshots, config)
        print_movement_snapshot_analysis(analysis)
        return 0

    if args.analyze_detected_taps is not None:
        analysis = analyze_detected_taps(args.analyze_detected_taps, config)
        print_detected_tap_analysis(analysis)
        return 0

    if args.calibrate_advance is not None:
        calibrate_advance_offset(config, args.calibrate_advance, args.calibration_delay)
        return 0

    if args.tap_scroll_test is not None:
        if args.tap_scroll_test < 1:
            raise AutomationError("--tap-scroll-test must be at least 1.")
        if (
            config.get("advance", {}).get("calibrate_first_item_on_start", False)
            and not args.no_runtime_calibration
            and advance_input_backend(config) == "desktop"
        ):
            runtime_calibrate_first_item(config, args.calibration_delay)
        snapshotter = create_movement_snapshotter(debug_dir, config) if args.movement_snapshots else None
        try:
            run_tap_scroll_test(config, args.tap_scroll_test, snapshotter)
        finally:
            finish_movement_snapshotter(snapshotter)
        return 0

    if args.tap_page_test is not None:
        if args.tap_page_test < 1:
            raise AutomationError("--tap-page-test must be at least 1.")
        if (
            config.get("advance", {}).get("calibrate_first_item_on_start", False)
            and not args.no_runtime_calibration
            and advance_input_backend(config) == "desktop"
        ):
            runtime_calibrate_first_item(config, args.calibration_delay)
        snapshotter = create_movement_snapshotter(debug_dir, config) if args.movement_snapshots else None
        try:
            run_tap_page_test(config, args.tap_page_test, snapshotter)
        finally:
            finish_movement_snapshotter(snapshotter)
        return 0

    if args.tap_detected_page_test is not None:
        if args.tap_detected_page_test < 1:
            raise AutomationError("--tap-detected-page-test must be at least 1.")
        snapshotter = create_movement_snapshotter(debug_dir, config) if args.movement_snapshots else None
        try:
            run_tap_detected_page_test(
                config,
                args.tap_detected_page_test,
                snapshotter,
                args.calibration_delay,
                not args.no_runtime_calibration,
                debug_dir,
            )
        finally:
            finish_movement_snapshotter(snapshotter)
        return 0

    if args.tap_detected_all_test:
        snapshotter = create_movement_snapshotter(debug_dir, config) if args.movement_snapshots else None
        try:
            run_tap_detected_page_test(
                config,
                None,
                snapshotter,
                args.calibration_delay,
                not args.no_runtime_calibration,
                debug_dir,
            )
        finally:
            finish_movement_snapshotter(snapshotter)
        return 0

    if args.unequip_website:
        if args.dry_run:
            slots = mirpg_unequip_slots(config.get("website", {}))
            print("Dry run: would click Unequip on the left/equipped compare item for slots:")
            print(", ".join(slots))
            return 0

        driver = website_driver(config)
        pw = browser = page = None
        close_browser = True
        try:
            if driver == "chrome_applescript":
                prepare_chrome_applescript(config)
            elif driver == "playwright":
                pw, browser, page, close_browser = load_playwright(config)
            else:
                raise AutomationError("website.driver must be one of: chrome_applescript, playwright")
            run_mirpg_unequip_all_website(config, driver, page)
        finally:
            if browser is not None and close_browser:
                browser.close()
            if pw is not None:
                pw.stop()
        return 0

    if args.dismantle_website:
        if args.dry_run:
            slots = mirpg_dismantle_slots(config.get("website", {}))
            print("Dry run: would click every Manage Equipment item and then Dismantle for slots:")
            print(", ".join(slots))
            return 0

        driver = website_driver(config)
        pw = browser = page = None
        close_browser = True
        try:
            if driver == "chrome_applescript":
                prepare_chrome_applescript(config)
            elif driver == "playwright":
                pw, browser, page, close_browser = load_playwright(config)
            else:
                raise AutomationError("website.driver must be one of: chrome_applescript, playwright")
            run_mirpg_dismantle_all_website(config, driver, page)
        finally:
            if browser is not None and close_browser:
                browser.close()
            if pw is not None:
                pw.stop()
        return 0

    if args.submit_detected_all:
        driver = website_driver(config)
        pw = browser = page = None
        close_browser = True
        if not args.dry_run:
            if driver == "chrome_applescript":
                prepare_chrome_applescript(config)
            elif driver == "playwright":
                pw, browser, page, close_browser = load_playwright(config)
            else:
                raise AutomationError("website.driver must be one of: chrome_applescript, playwright")
        snapshotter = create_movement_snapshotter(debug_dir, config) if args.movement_snapshots else None
        try:
            run_submit_detected_all(
                config,
                debug_dir,
                csv_log,
                args.dry_run,
                driver,
                page,
                snapshotter,
                args.primary_only,
            )
        finally:
            finish_movement_snapshotter(snapshotter)
            if browser is not None and close_browser:
                browser.close()
            if pw is not None:
                pw.stop()
        return 0

    if args.submit_gear_list:
        driver = website_driver(config)
        pw = browser = page = None
        close_browser = True
        if not args.dry_run:
            if driver == "chrome_applescript":
                prepare_chrome_applescript(config)
            elif driver == "playwright":
                pw, browser, page, close_browser = load_playwright(config)
            else:
                raise AutomationError("website.driver must be one of: chrome_applescript, playwright")
        snapshotter = create_movement_snapshotter(debug_dir, config) if args.movement_snapshots else None
        try:
            manual_primary_only = not args.include_reference_regions
            run_submit_manual_pages(
                config,
                debug_dir,
                csv_log,
                args.dry_run,
                driver,
                page,
                snapshotter,
                args.manual_page_limit,
                manual_primary_only,
            )
        finally:
            finish_movement_snapshotter(snapshotter)
            if browser is not None and close_browser:
                browser.close()
            if pw is not None:
                pw.stop()
        return 0

    if args.submit_equipped_slots:
        driver = website_driver(config)
        pw = browser = page = None
        close_browser = True
        if not args.dry_run:
            if driver == "chrome_applescript":
                prepare_chrome_applescript(config)
            elif driver == "playwright":
                pw, browser, page, close_browser = load_playwright(config)
            else:
                raise AutomationError("website.driver must be one of: chrome_applescript, playwright")
        snapshotter = create_movement_snapshotter(debug_dir, config) if args.movement_snapshots else None
        try:
            run_submit_equipped_slots(
                config,
                debug_dir,
                csv_log,
                args.dry_run,
                driver,
                page,
                snapshotter,
                args.equipped_slot_limit,
                not args.no_equipped_manual_first,
            )
        finally:
            finish_movement_snapshotter(snapshotter)
            if browser is not None and close_browser:
                browser.close()
            if pw is not None:
                pw.stop()
        return 0

    if args.advance_only is not None:
        if (
            config.get("advance", {}).get("calibrate_first_item_on_start", False)
            and not args.no_runtime_calibration
            and advance_input_backend(config) == "desktop"
        ):
            runtime_calibrate_first_item(config, args.calibration_delay)
        actions = preview_advance_actions(config, args.advance_only)
        backend = advance_input_backend(config)
        print(f"Advance mode: {config.get('advance', {}).get('mode', 'none')}")
        print(f"Input backend: {backend}")
        if not actions:
            print("No advance actions configured.")
            return 0
        for number, action in enumerate(actions, start=1):
            print(f"Action {number}: {describe_advance_action(config, action)}")
        snapshotter = create_movement_snapshotter(debug_dir, config) if args.movement_snapshots else None
        try:
            run_advance_action_group(config, actions, snapshotter, f"advance_only_{args.advance_only:04d}")
        finally:
            finish_movement_snapshotter(snapshotter)
        print(f"Ran advance action for completed item index {args.advance_only}.")
        return 0

    if args.reparse_output and not args.reparse_ocr:
        raise AutomationError("--reparse-output can only be used with --reparse-ocr.")
    if args.reparse_ocr and args.reparse_debug_dir:
        raise AutomationError("Use either --reparse-ocr or --reparse-debug-dir, not both.")
    if args.reparse_ocr:
        parsed, output_path = reparse_ocr_file(args.reparse_ocr.expanduser(), config, args.reparse_output)
        print(json.dumps(parsed, indent=2, ensure_ascii=False))
        print(f"\nReparsed JSON: {output_path}")
        return 0
    if args.reparse_debug_dir:
        debug_reparse_dir = args.reparse_debug_dir.expanduser()
        ocr_paths = sorted(debug_reparse_dir.glob("*_ocr.txt"))
        if not ocr_paths:
            raise AutomationError(f"No *_ocr.txt files found in {debug_reparse_dir}.")
        for ocr_path in ocr_paths:
            parsed, output_path = reparse_ocr_file(ocr_path, config)
            print(
                f"Reparsed {ocr_path.name} -> {output_path.name} "
                f"({len(parsed.get('on_equip_effects', []))} effects)"
            )
        return 0

    if args.pause_first:
        print("Position the game/equipment screen and website, then press Enter.")
        input()

    if (
        config.get("advance", {}).get("calibrate_first_item_on_start", False)
        and not args.no_runtime_calibration
        and args.limit > 1
        and not args.dry_run
        and not args.no_advance
        and advance_input_backend(config) == "desktop"
    ):
        runtime_calibrate_first_item(config, args.calibration_delay)

    driver = website_driver(config)
    pw = browser = page = None
    close_browser = True
    if not args.dry_run:
        if driver == "chrome_applescript":
            prepare_chrome_applescript(config)
        elif driver == "playwright":
            pw, browser, page, close_browser = load_playwright(config)
        else:
            raise AutomationError("website.driver must be one of: chrome_applescript, playwright")

    seen_hashes: set[str] = set()
    seen_equipment_keys: set[str] = set()
    csv_rows: list[dict[str, Any]] = []

    try:
        for index in range(args.limit):
            result = process_one(index, config, debug_dir, primary_only=args.primary_only)
            print_result(result)

            duplicate_reason = ""
            if config.get("stop_on_duplicate_image", True):
                if result.image_hash in seen_hashes:
                    print(f"Duplicate image detected at item {index}; stopping.")
                    break
                seen_hashes.add(result.image_hash)

            identity_key, duplicate_reason = parsed_duplicate_status(result.parsed, seen_equipment_keys, config)
            if duplicate_reason:
                print(
                    f"Duplicate parsed equipment detected at item {index}; "
                    "skipping website submit and advancing."
                )

            csv_rows.append(
                {
                    "index": index,
                    "skipped_duplicate": bool(duplicate_reason),
                    "duplicate_reason": duplicate_reason,
                    **result.parsed,
                }
            )

            if duplicate_reason:
                pass
            elif not args.dry_run:
                try:
                    if driver == "chrome_applescript":
                        submit_to_website_applescript(result.parsed, config)
                    elif page is not None:
                        submit_to_website(page, result.parsed, config)
                except AutomationError as exc:
                    error_path = debug_dir / f"{index:04d}_submit_error.json"
                    error_path.write_text(
                        json.dumps(
                            {
                                "error": str(exc),
                                "parsed": result.parsed,
                            },
                            indent=2,
                            ensure_ascii=False,
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    print(f"Submit error debug: {error_path}")
                    raise
                if identity_key:
                    seen_equipment_keys.add(identity_key)
            elif identity_key:
                seen_equipment_keys.add(identity_key)

            if index < args.limit - 1 and not args.dry_run and not args.no_advance:
                advance_to_next_equipment(config, index)

        write_csv_log(csv_log, csv_rows)
        print(f"\nParsed CSV log: {csv_log}")
    finally:
        if browser is not None and close_browser:
            browser.close()
        if pw is not None:
            pw.stop()

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AutomationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
