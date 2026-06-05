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
    "avoidance": "Avoidance",
    "avoid chance": "Avoidance",
    "evasion": "Avoidance",
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
    image_path: Path
    ocr_text_path: Path
    raw_text: str
    parsed: dict[str, Any]
    image_hash: str
    target_region: str = "equipment"
    region_image_paths: dict[str, Path] = dataclasses.field(default_factory=dict)
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


def normalize_effect_label(label: str) -> str:
    cleaned = label.strip()
    cleaned = re.sub(r"^[^\w]+|[^\w.]+$", "", cleaned)
    cleaned = re.sub(r"\bCRI(?:TI)*CAL\b", "Critical", cleaned, flags=re.IGNORECASE)
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


def parse_effect_line(line: str) -> dict[str, Any] | None:
    cleaned = line.strip()
    if not cleaned:
        return None

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
    item_name = parsed.get("item_name")
    tier = parsed.get("tier")
    grade = parsed.get("grade")
    level = parsed.get("level")
    if tier not in {None, ""}:
        parts.append(f"T{tier}")
    elif item_name:
        parts.append(str(item_name))
    if grade:
        parts.append(str(grade))
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


def save_debug_artifacts(
    *,
    debug_dir: Path,
    index: int,
    image: Any,
    raw_text: str,
    parsed: dict[str, Any],
) -> tuple[Path, Path, Path]:
    debug_dir.mkdir(parents=True, exist_ok=True)
    prefix = f"{index:04d}"
    image_path = debug_dir / f"{prefix}_equipment.png"
    ocr_path = debug_dir / f"{prefix}_ocr.txt"
    parsed_path = debug_dir / f"{prefix}_parsed.json"
    image.save(image_path)
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
) -> tuple[Path, Path, Path]:
    debug_dir.mkdir(parents=True, exist_ok=True)
    prefix = f"{index:04d}_{region_name}"
    image_path = debug_dir / f"{prefix}_equipment.png"
    ocr_path = debug_dir / f"{prefix}_ocr.txt"
    parsed_path = debug_dir / f"{prefix}_parsed.json"
    image.save(image_path)
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


def mirpg_applescript_payload(parsed: dict[str, Any]) -> dict[str, Any]:
    equipment_name = parsed.get("equipment_name") or build_equipment_name(parsed)
    effects = [dict(effect) for effect in parsed.get("on_equip_effects", [])]
    if not equipment_name:
        raise AutomationError("Parsed item does not have an equipment_name.")
    if not effects:
        raise AutomationError(f"No On-Equip Effect rows parsed for {equipment_name}.")
    return {
        "equipment_name": str(equipment_name),
        "equipment_slot": parsed.get("equipment_slot"),
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
    const wanted = labelKey(label);
    const index = effects.findIndex((effect) => labelKey(effect.name) === wanted);
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
    const wantedLabel = labelKey(effect.name);
    const wantedId = String(effect.mirpg_option_id || "");
    const options = Array.from(select.options || []);
    const option = options.find((item) => labelKey(item.textContent) === wantedLabel)
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
  const knownUnsupportedEffectKeys = new Set((settings.known_unsupported_effects || []).map(labelKey));
  const shouldSkipEffect = (effect) => settings.skip_known_unsupported_effects
    && knownUnsupportedEffectKeys.has(labelKey(effect.name));

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
    payload = mirpg_applescript_payload(parsed)
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
    equipment_name = parsed.get("equipment_name") or build_equipment_name(parsed)
    effects = [dict(effect) for effect in parsed.get("on_equip_effects", [])]
    if not equipment_name:
        raise AutomationError("Parsed item does not have an equipment_name.")
    if not effects:
        raise AutomationError(f"No On-Equip Effect rows parsed for {equipment_name}.")

    slot = parsed.get("equipment_slot")
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
    orange = detect_config.get("orange_threshold", {})
    mint = detect_config.get("mint_threshold", {})

    green_match = (
        g >= int(green.get("min_g", 135))
        and r <= int(green.get("max_r", 125))
        and b >= int(green.get("min_b", 70))
    )
    orange_match = (
        r >= int(orange.get("min_r", 180))
        and g >= int(orange.get("min_g", 110))
        and b <= int(orange.get("max_b", 130))
    )
    mint_match = (
        g >= int(mint.get("min_g", 170))
        and b >= int(mint.get("min_b", 120))
        and r <= int(mint.get("max_r", 180))
    )
    return green_match or orange_match or mint_match


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
        signed_boxes = []
        for box in boxes:
            signed = dict(box)
            signed["signature"] = detected_item_card_signature(image, box, config)
            signed_boxes.append(signed)
        return signed_boxes, []

    signed_boxes: list[dict[str, Any]] = []
    matched_counts_by_row: dict[int, int] = {}
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
        if matched and row_index is not None:
            matched_counts_by_row[row_index] = matched_counts_by_row.get(row_index, 0) + 1
        signed_boxes.append(signed)

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


def run_tap_detected_page_test(
    config: dict[str, Any],
    page_count: int | None,
    snapshotter: MovementSnapshotter | None = None,
    calibration_delay: float | None = None,
    calibrate_desktop: bool = True,
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
            needs_row_centers = row_snap_enabled or (page_index > 0 and skip_seen_rows_after_scroll >= 0)
            row_centers: list[float] = []
            if needs_row_centers:
                group_tolerance = float(detect_config.get("row_group_tolerance_px", 35))
                row_centers = detected_item_row_centers(boxes, group_tolerance)
            if row_snap_enabled and page_index == 0 and snap_target_rows is None and boxes:
                snap_target_rows = row_centers
                print(f"Row snap target centers: {[round(value, 1) for value in snap_target_rows]}")
            skip_row_limit = None
            if page_index > 0:
                if page_count is None and skip_seen_rows_after_crawl >= 0:
                    skip_row_limit = skip_seen_rows_after_crawl
                elif page_count is not None and skip_seen_rows_after_scroll >= 0:
                    skip_row_limit = skip_seen_rows_after_scroll
            boxes_to_click, skipped_boxes = split_unseen_detected_item_boxes(
                image,
                boxes,
                seen_card_signatures,
                config,
                skip_row_limit=skip_row_limit,
                row_centers=row_centers,
            )
            actions = [detected_item_action(config, tuple(box["center"])) for box in boxes_to_click]

            print(
                f"Page {page_index + 1}: detected {len(boxes)} visible item cards; "
                f"clicking {len(actions)}"
            )
            if log_detected_items and row_centers:
                print(f"  row centers: {[round(value, 1) for value in row_centers]}")
            if skipped_boxes:
                print(f"  skipped already-seen cards: {len(skipped_boxes)}")
            if log_detected_items:
                skipped_centers = {tuple(skipped.get("center", [])) for skipped in skipped_boxes}
                signed_by_center = {
                    tuple(signed.get("center", [])): signed
                    for signed in [*boxes_to_click, *skipped_boxes]
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
            print(f"Click page {page_index + 1} item {item_index}: {describe_advance_action(config, action)}")
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
            scroll_actions = detected_page_scroll_actions(config, scroll_mode)
            if not scroll_actions:
                raise AutomationError("No detected page scroll actions configured.")
            print(f"Scroll after page {page_index + 1}:")
            for action in scroll_actions:
                print(f"  {describe_advance_action(config, action)}")
            run_advance_action_group(
                config,
                scroll_actions,
                snapshotter,
                f"tap_detected_page_{page_index + 1:04d}_scroll",
            )
            snap_detected_item_rows_after_scroll(
                config,
                snap_target_rows,
                snapshotter,
                f"tap_detected_page_{page_index + 1:04d}_scroll_snap",
            )
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
        before_entry = phases.get("before_click")
        click_entry = phases.get("after_click") or phases.get("click_down")
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


def process_one(index: int, config: dict[str, Any], debug_dir: Path) -> EquipmentResult:
    full_image = capture_screenshot(config)
    debug_dir.mkdir(parents=True, exist_ok=True)
    full_image_path = debug_dir / f"{index:04d}_full.png"
    full_image.save(full_image_path)

    regions = resolve_ocr_regions(config)
    primary_region = next(region for region in regions if region.get("primary"))

    region_images: dict[str, Path] = {}
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

    # Keep the legacy debug filenames pointing at the primary/submitted item.
    save_debug_artifacts(
        debug_dir=debug_dir,
        index=index,
        image=preprocess_for_ocr(crop_image(full_image, primary_region.get("crop")), config),
        raw_text=raw_text,
        parsed=parsed,
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
    print(f"Saved image: {result.image_path}")
    print(f"Saved OCR:   {result.ocr_text_path}")
    if len(result.region_image_paths) > 1:
        print("Region images:")
        for name, path in result.region_image_paths.items():
            print(f"  {name}: {path}")


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
    debug_dir = args.debug_dir or Path(config.get("debug_dir", f"equipment_ocr_debug/{now_stamp()}"))
    csv_log = args.csv_log or (debug_dir / "parsed_results.csv")

    if args.limit < 1:
        raise AutomationError("--limit must be at least 1.")

    if args.mouse_position is not None:
        print_mouse_position(args.mouse_position)
        return 0

    if args.analyze_movement_snapshots is not None:
        analysis = analyze_movement_snapshots(args.analyze_movement_snapshots, config)
        print_movement_snapshot_analysis(analysis)
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
            )
        finally:
            finish_movement_snapshotter(snapshotter)
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
            result = process_one(index, config, debug_dir)
            print_result(result)

            duplicate_reason = ""
            if config.get("stop_on_duplicate_image", True):
                if result.image_hash in seen_hashes:
                    print(f"Duplicate image detected at item {index}; stopping.")
                    break
                seen_hashes.add(result.image_hash)

            identity_key = equipment_identity_key(result.parsed)
            if config.get("skip_duplicate_parsed_equipment", True) and identity_key:
                if identity_key in seen_equipment_keys:
                    duplicate_reason = "parsed_equipment"
                    print(
                        f"Duplicate parsed equipment detected at item {index}; "
                        "skipping website submit and advancing."
                    )
                else:
                    seen_equipment_keys.add(identity_key)

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
