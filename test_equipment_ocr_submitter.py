#!/usr/bin/env python3
"""Focused parser tests for equipment_ocr_submitter.py."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "equipment_ocr_submitter.py"
CONFIG = ROOT / "tools" / "equipment_ocr_config.example.json"


def load_module():
    spec = importlib.util.spec_from_file_location("equipment_ocr_submitter", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["equipment_ocr_submitter"] = module
    spec.loader.exec_module(module)
    return module


class EquipmentOcrParserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module()
        cls.config = json.loads(CONFIG.read_text(encoding="utf-8"))

    def parse(self, text: str) -> dict:
        normalized = self.module.normalize_text(text, self.config)
        return self.module.parse_equipment_text(normalized, self.config)

    def test_example_config_uses_adb_for_game_input(self) -> None:
        self.assertEqual(self.config["capture"]["backend"], "adb")
        self.assertEqual(self.config["advance"]["input_backend"], "adb")
        self.assertEqual(self.config["advance"]["detect_items"]["input_backend"], "adb")
        self.assertFalse(self.config["advance"]["calibrate_first_item_on_start"])
        self.assertFalse(self.config["advance"]["detect_items"]["row_snap_after_scroll"])
        self.assertFalse(self.config["advance"]["detect_items"]["log_detected_items"])
        self.assertGreaterEqual(self.config["advance"]["detect_items"]["max_all_pages"], 1)
        self.assertLessEqual(self.config["advance"]["wait_after_advance_seconds"], 0.2)

    def test_candidate_sample_builds_name_and_effects(self) -> None:
        parsed = self.parse(
            """oot oobi toons —>=E
Dark Crescent Boots
T4
pe: Wazg) Legendary Shoes
ey Lv.98
CP Change
< Go +12B 124M
On-Equip Effect
Attack 10,761 4,173
Max HP 54,981 21,747
Max MP 1,416 411
CRITICAL Damage 7.3% 7.3%
3rd Job Skill Lv. 66
1st Job Skill Lv. 77
"""
        )

        self.assertEqual(parsed["equipment_name"], "T4 - Legendary - Lv.98")
        self.assertEqual(parsed["equipment_slot"], "Shoes")
        self.assertEqual(
            [(effect["name"], effect["value"]) for effect in parsed["on_equip_effects"]],
            [
                ("Attack", 10761),
                ("Max HP", 54981),
                ("Max MP", 1416),
                ("Critical Damage", 7.3),
                ("3rd Job Skill Lv.", 6),
                ("1st Job Skill Lv.", 7),
            ],
        )

    def test_duplicate_effects_are_preserved_in_order(self) -> None:
        parsed = self.parse(
            """Dark Crescent Boots
Legendary Shoes
Lv.87
On-Equip Effect
Attack 6,588
Max HP 33,234
Attack 2,745
CRITICAL Rate 6.5%
"""
        )

        self.assertEqual(
            [(effect["name"], effect["value"]) for effect in parsed["on_equip_effects"]],
            [
                ("Attack", 6588),
                ("Max HP", 33234),
                ("Attack", 2745),
                ("Critical Rate", 6.5),
            ],
        )

    def test_equipment_identity_key_uses_name_slot_and_ordered_effects(self) -> None:
        parsed = {
            "equipment_name": "T4 - Legendary - Lv.98",
            "equipment_slot": "Shoes",
            "on_equip_effects": [
                {"name": "Attack", "value": 10761},
                {"name": "Attack", "value": 4173},
                {"name": "Critical Damage", "value": 7.3},
            ],
        }
        same = {
            "equipment_name": "T4 - Legendary - Lv.98",
            "equipment_slot": "Shoes",
            "on_equip_effects": [
                {"name": "Attack", "value": 10761},
                {"name": "Attack", "value": 4173},
                {"name": "Critical Damage", "value": 7.3},
            ],
        }
        reordered = {
            "equipment_name": "T4 - Legendary - Lv.98",
            "equipment_slot": "Shoes",
            "on_equip_effects": [
                {"name": "Attack", "value": 4173},
                {"name": "Attack", "value": 10761},
                {"name": "Critical Damage", "value": 7.3},
            ],
        }

        self.assertEqual(
            self.module.equipment_identity_key(parsed),
            self.module.equipment_identity_key(same),
        )
        self.assertNotEqual(
            self.module.equipment_identity_key(parsed),
            self.module.equipment_identity_key(reordered),
        )

    def test_equipment_slot_infers_non_shoes_type(self) -> None:
        parsed = self.parse(
            """Storm Grip
Legendary Gloves
Lv.96
On-Equip Effect
Attack 1,234
"""
        )

        self.assertEqual(parsed["equipment_name"], "Storm Grip - Legendary - Lv.96")
        self.assertEqual(parsed["equipment_slot"], "Gloves")

    def test_equipment_name_uses_default_tier_when_ocr_misses_it(self) -> None:
        config = json.loads(json.dumps(self.config))
        config["parser"]["default_tier"] = 4
        normalized = self.module.normalize_text(
            """Dark Crescent Boots
Legendary Shoes
Lv.98
On-Equip Effect
Attack 1,234
""",
            config,
        )
        parsed = self.module.parse_equipment_text(normalized, config)

        self.assertEqual(parsed["equipment_name"], "T4 - Legendary - Lv.98")
        self.assertEqual(parsed["tier"], 4)

    def test_reparse_ocr_file_writes_effect_rows_to_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ocr_path = Path(tmp) / "0000_candidate_ocr.txt"
            ocr_path.write_text(
                """Dark Crescent Boots
Legendary Shoes
Lv.98
On-Equip Effect
3rd Job Skill Lv. 66
1st Job Skill Lv. 77
""",
                encoding="utf-8",
            )

            parsed, output_path = self.module.reparse_ocr_file(ocr_path, self.config)
            output = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(output_path.name, "0000_candidate_parsed.json")
        self.assertEqual(parsed["equipment_name"], "T4 - Legendary - Lv.98")
        self.assertEqual(
            [(effect["name"], effect["value"]) for effect in output["on_equip_effects"]],
            [
                ("3rd Job Skill Lv.", 6),
                ("1st Job Skill Lv.", 7),
            ],
        )

    def test_retryable_adb_error_detects_closed_connection(self) -> None:
        self.assertTrue(self.module.retryable_adb_error(Exception("error: closed")))
        self.assertTrue(self.module.retryable_adb_error(Exception("error: no devices/emulators found")))
        self.assertFalse(self.module.retryable_adb_error(Exception("bad tap coordinate")))

    def test_adb_connect_target_prefers_configured_connect(self) -> None:
        config = {
            "capture": {
                "connect": "127.0.0.1:5555",
                "serial": "emulator-5554",
            }
        }

        self.assertEqual(self.module.adb_connect_target(config), "127.0.0.1:5555")

    def test_desktop_point_applies_offset_and_scale(self) -> None:
        config = {
            "advance": {
                "desktop_coordinate_offset": [100, 50],
                "desktop_coordinate_scale": [2, 0.5],
            }
        }

        self.assertEqual(self.module.desktop_point(config, 10, 20), (120, 60))

    def test_runtime_calibration_updates_desktop_offset(self) -> None:
        config = {
            "advance": {
                "desktop_coordinate_scale": [2, 0.5],
            }
        }

        offset = self.module.set_desktop_offset_from_screen_point(config, 10, 20, 120, 60)

        self.assertEqual(offset, (100, 50))
        self.assertEqual(config["advance"]["desktop_coordinate_offset"], [100, 50])
        self.assertEqual(self.module.desktop_point(config, 10, 20), (120, 60))

    def test_two_point_calibration_updates_uniform_scale_and_offset(self) -> None:
        config = {"advance": {"desktop_coordinate_scale": 1.0}}

        scale, offset = self.module.set_desktop_transform_from_screen_points(
            config,
            (100, 200),
            (1000, 500),
            (200, 200),
            (1200, 500),
            uniform_scale=True,
        )

        self.assertEqual(scale, (2.0, 2.0))
        self.assertEqual(offset, (800, 100))
        self.assertEqual(config["advance"]["desktop_coordinate_scale"], [2.0, 2.0])
        self.assertEqual(config["advance"]["desktop_coordinate_offset"], [800, 100])
        self.assertEqual(self.module.desktop_point(config, 100, 200), (1000, 500))
        self.assertEqual(self.module.desktop_point(config, 200, 200), (1200, 500))

    def test_two_point_calibration_can_update_non_uniform_scale(self) -> None:
        config = {"advance": {"desktop_coordinate_scale": 1.0}}

        scale, offset = self.module.set_desktop_transform_from_screen_points(
            config,
            (100, 200),
            (1000, 500),
            (200, 300),
            (1300, 650),
            uniform_scale=False,
        )

        self.assertEqual(scale, (3.0, 1.5))
        self.assertEqual(offset, (700, 200))
        self.assertEqual(self.module.desktop_point(config, 100, 200), (1000, 500))
        self.assertEqual(self.module.desktop_point(config, 200, 300), (1300, 650))

    def test_multi_point_calibration_updates_non_uniform_scale(self) -> None:
        config = {"advance": {"desktop_coordinate_scale": 1.0}}

        scale, offset = self.module.set_desktop_transform_from_screen_samples(
            config,
            [
                ((100, 200), (1000, 500)),
                ((200, 200), (1250, 500)),
                ((100, 350), (1000, 800)),
            ],
        )

        self.assertEqual(scale, (2.5, 2.0))
        self.assertEqual(offset, (750, 100))
        self.assertEqual(self.module.desktop_point(config, 100, 200), (1000, 500))
        self.assertEqual(self.module.desktop_point(config, 200, 200), (1250, 500))
        self.assertEqual(self.module.desktop_point(config, 100, 350), (1000, 800))

    def test_adb_input_command_supports_exec_out_transport(self) -> None:
        config = {"capture": {"adb_path": "adb", "serial": "127.0.0.1:5555"}}

        self.assertEqual(
            self.module.adb_input_command(config, ["tap", "10", "20"], "exec-out"),
            ["adb", "-s", "127.0.0.1:5555", "exec-out", "input", "tap", "10", "20"],
        )

    def test_item_grid_uses_desktop_backend_for_generated_actions(self) -> None:
        config = {
            "capture": {"backend": "adb"},
            "advance": {
                "input_backend": "desktop",
                "item_positions": [[10, 20], [30, 40]],
                "scroll_actions": [{"type": "adb_swipe", "from_x": 1, "from_y": 2, "to_x": 3, "to_y": 4}],
            },
        }

        self.assertEqual(
            self.module.item_grid_actions(config, 0),
            [{"type": "click", "x": 30, "y": 40}],
        )
        self.assertEqual(
            self.module.item_grid_actions(config, 1),
            [{"type": "drag", "from_x": 1, "from_y": 2, "to_x": 3, "to_y": 4, "duration_seconds": 0.4}],
        )
        self.assertEqual(
            self.module.item_grid_actions(config, 2),
            [{"type": "drag", "from_x": 1, "from_y": 2, "to_x": 3, "to_y": 4, "duration_seconds": 0.4}],
        )

    def test_tap_scroll_test_steps_include_first_item_and_bottom_scrolls(self) -> None:
        config = {
            "advance": {
                "input_backend": "desktop",
                "item_positions": [[10, 20], [30, 40]],
                "scroll_actions": [
                    {"type": "adb_swipe", "from_x": 1, "from_y": 2, "to_x": 3, "to_y": 4},
                    {"type": "wait", "seconds": 0.2},
                ],
                "reset_position_after_scroll": [30, 40],
            },
        }

        self.assertEqual(
            self.module.tap_scroll_test_steps(config, 4),
            [
                [{"type": "click", "x": 10, "y": 20}],
                [{"type": "click", "x": 30, "y": 40}],
                [
                    {"type": "drag", "from_x": 1, "from_y": 2, "to_x": 3, "to_y": 4, "duration_seconds": 0.4},
                    {"type": "wait", "seconds": 0.2},
                    {"type": "click", "x": 30, "y": 40},
                ],
                [
                    {"type": "drag", "from_x": 1, "from_y": 2, "to_x": 3, "to_y": 4, "duration_seconds": 0.4},
                    {"type": "wait", "seconds": 0.2},
                    {"type": "click", "x": 30, "y": 40},
                ],
            ],
        )

    def test_tap_page_test_steps_click_whole_pages_with_page_scroll(self) -> None:
        config = {
            "advance": {
                "input_backend": "desktop",
                "item_positions": [[10, 20], [30, 40]],
                "post_scroll_item_positions": [[11, 21], [31, 41]],
                "scroll_actions": [{"type": "adb_swipe", "from_x": 9, "from_y": 8, "to_x": 7, "to_y": 6}],
                "page_scroll_actions": [
                    {
                        "type": "adb_swipe",
                        "from_x": 1,
                        "from_y": 2,
                        "to_x": 3,
                        "to_y": 4,
                        "duration_ms": 1600,
                        "pause_before_release_seconds": 0.35,
                    },
                    {"type": "wait", "seconds": 0.8},
                ],
            },
        }

        self.assertEqual(
            self.module.tap_page_test_steps(config, 2),
            [
                [{"type": "click", "x": 10, "y": 20}],
                [{"type": "click", "x": 30, "y": 40}],
                [
                    {
                        "type": "drag",
                        "from_x": 1,
                        "from_y": 2,
                        "to_x": 3,
                        "to_y": 4,
                        "duration_ms": 1600,
                        "pause_before_release_seconds": 0.35,
                        "duration_seconds": 1.6,
                    },
                    {"type": "wait", "seconds": 0.8},
                ],
                [{"type": "click", "x": 11, "y": 21}],
                [{"type": "click", "x": 31, "y": 41}],
            ],
        )

    def test_analyze_movement_snapshots_suggests_detected_adb_center(self) -> None:
        config = {
            "advance": {
                "movement_snapshot_analysis": {
                    "search_radius": [80, 80],
                    "diff_threshold": 10,
                    "min_changed_pixels": 10,
                }
            }
        }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            before_path = tmp_path / "before.png"
            after_path = tmp_path / "after.png"
            before = Image.new("RGB", (120, 120), "white")
            after = Image.new("RGB", (120, 120), "white")
            pixels = after.load()
            for y in range(45, 55):
                for x in range(35, 45):
                    pixels[x, y] = (0, 0, 0)
            before.save(before_path)
            after.save(after_path)

            manifest_path = tmp_path / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    [
                        {
                            "phase": "before_click",
                            "label": "tap_page_step_0001_action_1",
                            "action": {"type": "click", "x": 10, "y": 20},
                            "image": str(before_path),
                            "snapshot_origin": [0, 0],
                            "snapshot_coordinate_space": "adb",
                        },
                        {
                            "phase": "after_click",
                            "label": "tap_page_step_0001_action_1",
                            "action": {"type": "click", "x": 10, "y": 20},
                            "image": str(after_path),
                            "snapshot_origin": [0, 0],
                            "snapshot_coordinate_space": "adb",
                        },
                    ]
                ),
                encoding="utf-8",
            )

            analysis = self.module.analyze_movement_snapshots(manifest_path, config)

        self.assertEqual(len(analysis["detections"]), 1)
        self.assertEqual(analysis["suggested_positions"][0]["current"], [10.0, 20.0])
        self.assertEqual(analysis["suggested_positions"][0]["suggested"], [40.0, 50.0])

    def test_detect_visible_item_boxes_finds_card_sized_color_components(self) -> None:
        image = Image.new("RGB", (400, 400), "black")
        pixels = image.load()
        cards = [(50, 40, 160, 150), (190, 40, 300, 150), (50, 190, 160, 300)]
        for left, top, right, bottom in cards:
            for y in range(top, bottom):
                for x in range(left, right):
                    pixels[x, y] = (40, 220, 150)

        config = {
            "advance": {
                "detect_items": {
                    "region": [0, 0, 400, 400],
                    "min_card_width": 90,
                    "max_card_width": 130,
                    "min_card_height": 90,
                    "max_card_height": 130,
                    "min_component_pixels": 1000,
                }
            }
        }

        boxes = self.module.detect_visible_item_boxes(image, config)

        self.assertEqual([box["center"] for box in boxes], [[105.0, 95.0], [245.0, 95.0], [105.0, 245.0]])

    def test_detected_item_row_centers_groups_cards_by_y(self) -> None:
        boxes = [
            {"center": [100.0, 95.0]},
            {"center": [240.0, 98.0]},
            {"center": [100.0, 245.0]},
            {"center": [240.0, 248.0]},
        ]

        self.assertEqual(
            self.module.detected_item_row_centers(boxes, tolerance=10),
            [96.5, 246.5],
        )

    def test_detected_item_row_snap_delta_uses_nearest_target_rows(self) -> None:
        target_rows = [360.0, 547.0, 733.0, 920.0, 1107.0]
        current_rows = [330.0, 517.0, 703.0, 890.0, 1077.0]

        self.assertEqual(
            self.module.detected_item_row_snap_delta(target_rows, current_rows, max_match_delta=95),
            30.0,
        )

    def test_detected_calibration_boxes_selects_horizontal_and_vertical_neighbors(self) -> None:
        boxes = [
            {"center": [2029.0, 360.0]},
            {"center": [2205.0, 360.0]},
            {"center": [2381.0, 360.0]},
            {"center": [2029.0, 547.0]},
            {"center": [2205.0, 547.0]},
        ]
        config = {
            "advance": {
                "detect_items": {
                    "calibration_row_tolerance_px": 35,
                    "calibration_column_tolerance_px": 45,
                }
            }
        }

        selected = self.module.detected_calibration_boxes(boxes, config)

        self.assertEqual(
            [box["center"] for box in selected],
            [[2029.0, 360.0], [2205.0, 360.0], [2029.0, 547.0]],
        )

    def test_detected_row_snap_action_uses_configured_anchor_and_backend(self) -> None:
        config = {
            "advance": {
                "detect_items": {
                    "input_backend": "desktop",
                    "region": [1900, 240, 585, 1000],
                    "row_snap_anchor": [2205, 740],
                    "row_snap_duration_ms": 450,
                }
            }
        }

        action = self.module.detected_row_snap_action(config, -24)

        self.assertEqual(action["type"], "drag")
        self.assertEqual(action["from_x"], 2205)
        self.assertEqual(action["from_y"], 740)
        self.assertEqual(action["to_x"], 2205)
        self.assertEqual(action["to_y"], 716)
        self.assertEqual(action["duration_seconds"], 0.45)

    def test_detected_item_card_signature_matches_same_card_content(self) -> None:
        image = Image.new("RGB", (320, 160), "black")
        pixels = image.load()
        cards = [(20, 20, 140, 140), (180, 20, 300, 140)]
        for left, top, right, bottom in cards:
            for y in range(top, bottom):
                for x in range(left, right):
                    pixels[x, y] = (40, 220, 150)
            for y in range(top + 25, top + 45):
                for x in range(left + 35, left + 85):
                    pixels[x, y] = (255, 255, 255)

        config = {
            "advance": {
                "detect_items": {
                    "signature_inset_px": 0,
                    "signature_resize": [32, 32],
                }
            }
        }
        first = {"bbox": [20, 20, 140, 140], "center": [80.0, 80.0], "pixels": 14400}
        second = {"bbox": [180, 20, 300, 140], "center": [240.0, 80.0], "pixels": 14400}

        self.assertEqual(
            self.module.detected_item_card_signature(image, first, config)["hash"],
            self.module.detected_item_card_signature(image, second, config)["hash"],
        )

    def test_seen_signature_match_allows_card_highlight_changes(self) -> None:
        normal = Image.new("RGB", (140, 140), (40, 220, 150))
        highlighted = Image.new("RGB", (140, 140), (90, 205, 180))
        for image in (normal, highlighted):
            pixels = image.load()
            for y in range(35, 55):
                for x in range(35, 95):
                    pixels[x, y] = (255, 255, 255)
            for y in range(78, 92):
                for x in range(48, 86):
                    pixels[x, y] = (20, 20, 20)

        config = {
            "advance": {
                "detect_items": {
                    "signature_inset_px": 0,
                    "signature_resize": [16, 16],
                    "signature_hamming_threshold": 30,
                }
            }
        }
        box = {"bbox": [0, 0, 140, 140], "center": [70.0, 70.0], "pixels": 19600}
        seen = [self.module.detected_item_card_signature(normal, box, config)]
        signature = self.module.detected_item_card_signature(highlighted, box, config)

        matched, distance, color_distance = self.module.seen_signature_match(signature, seen, config)

        self.assertTrue(matched)
        self.assertIsNotNone(distance)
        self.assertIsNotNone(color_distance)

    def test_split_unseen_detected_item_boxes_skips_seen_signatures(self) -> None:
        image = Image.new("RGB", (260, 140), "black")
        pixels = image.load()
        for y in range(20, 120):
            for x in range(20, 120):
                pixels[x, y] = (40, 220, 150)
            for x in range(140, 240):
                pixels[x, y] = (240, 170, 60)

        config = {
            "advance": {
                "detect_items": {
                    "signature_inset_px": 0,
                    "signature_resize": [16, 16],
                    "skip_seen_cards": True,
                    "skip_seen_min_matches_per_row": 1,
                }
            }
        }
        boxes = [
            {"bbox": [20, 20, 120, 120], "center": [70.0, 70.0], "pixels": 10000},
            {"bbox": [140, 20, 240, 120], "center": [190.0, 70.0], "pixels": 10000},
        ]
        seen = [self.module.detected_item_card_signature(image, boxes[0], config)]

        unseen, skipped = self.module.split_unseen_detected_item_boxes(image, boxes, seen, config)

        self.assertEqual([box["center"] for box in unseen], [[190.0, 70.0]])
        self.assertEqual([box["center"] for box in skipped], [[70.0, 70.0]])

    def test_split_unseen_detected_item_boxes_limits_seen_skips_to_overlap_rows(self) -> None:
        image = Image.new("RGB", (140, 320), "black")
        pixels = image.load()
        cards = [(20, 20, 120, 120), (20, 180, 120, 280)]
        for left, top, right, bottom in cards:
            for y in range(top, bottom):
                for x in range(left, right):
                    pixels[x, y] = (40, 220, 150)
            for y in range(top + 25, top + 45):
                for x in range(left + 25, left + 75):
                    pixels[x, y] = (255, 255, 255)

        config = {
            "advance": {
                "detect_items": {
                    "signature_inset_px": 0,
                    "signature_resize": [16, 16],
                    "skip_seen_cards": True,
                    "row_group_tolerance_px": 20,
                    "skip_seen_min_matches_per_row": 1,
                }
            }
        }
        boxes = [
            {"bbox": [20, 20, 120, 120], "center": [70.0, 70.0], "pixels": 10000},
            {"bbox": [20, 180, 120, 280], "center": [70.0, 230.0], "pixels": 10000},
        ]
        seen = [self.module.detected_item_card_signature(image, boxes[0], config)]
        row_centers = self.module.detected_item_row_centers(boxes, tolerance=20)

        unseen, skipped = self.module.split_unseen_detected_item_boxes(
            image,
            boxes,
            seen,
            config,
            skip_row_limit=1,
            row_centers=row_centers,
        )

        self.assertEqual([box["center"] for box in skipped], [[70.0, 70.0]])
        self.assertEqual([box["center"] for box in unseen], [[70.0, 230.0]])
        self.assertTrue(unseen[0]["seen_match_ignored"])

    def test_split_unseen_detected_item_boxes_keeps_single_seen_match_in_overlap_row(self) -> None:
        image = Image.new("RGB", (300, 140), "black")
        pixels = image.load()
        cards = [(20, 20, 120, 120), (170, 20, 270, 120)]
        for index, (left, top, right, bottom) in enumerate(cards):
            color = (40, 220, 150) if index == 0 else (240, 170, 60)
            for y in range(top, bottom):
                for x in range(left, right):
                    pixels[x, y] = color

        config = {
            "advance": {
                "detect_items": {
                    "signature_inset_px": 0,
                    "signature_resize": [16, 16],
                    "skip_seen_cards": True,
                    "row_group_tolerance_px": 20,
                    "skip_seen_min_matches_per_row": 2,
                }
            }
        }
        boxes = [
            {"bbox": [20, 20, 120, 120], "center": [70.0, 70.0], "pixels": 10000},
            {"bbox": [170, 20, 270, 120], "center": [220.0, 70.0], "pixels": 10000},
        ]
        seen = [self.module.detected_item_card_signature(image, boxes[0], config)]
        row_centers = self.module.detected_item_row_centers(boxes, tolerance=20)

        unseen, skipped = self.module.split_unseen_detected_item_boxes(
            image,
            boxes,
            seen,
            config,
            skip_row_limit=1,
            row_centers=row_centers,
        )

        self.assertEqual(skipped, [])
        self.assertEqual([box["center"] for box in unseen], [[70.0, 70.0], [220.0, 70.0]])
        self.assertTrue(unseen[0]["seen_match_ignored"])

    def test_split_unseen_detected_item_boxes_skips_row_with_multiple_seen_matches(self) -> None:
        image = Image.new("RGB", (300, 140), "black")
        pixels = image.load()
        cards = [(20, 20, 120, 120), (170, 20, 270, 120)]
        for left, top, right, bottom in cards:
            for y in range(top, bottom):
                for x in range(left, right):
                    pixels[x, y] = (40, 220, 150)
            for y in range(top + 25, top + 45):
                for x in range(left + 25, left + 75):
                    pixels[x, y] = (255, 255, 255)

        config = {
            "advance": {
                "detect_items": {
                    "signature_inset_px": 0,
                    "signature_resize": [16, 16],
                    "skip_seen_cards": True,
                    "row_group_tolerance_px": 20,
                    "skip_seen_min_matches_per_row": 2,
                }
            }
        }
        boxes = [
            {"bbox": [20, 20, 120, 120], "center": [70.0, 70.0], "pixels": 10000},
            {"bbox": [170, 20, 270, 120], "center": [220.0, 70.0], "pixels": 10000},
        ]
        seen = [self.module.detected_item_card_signature(image, boxes[0], config)]
        row_centers = self.module.detected_item_row_centers(boxes, tolerance=20)

        unseen, skipped = self.module.split_unseen_detected_item_boxes(
            image,
            boxes,
            seen,
            config,
            skip_row_limit=1,
            row_centers=row_centers,
        )

        self.assertEqual(unseen, [])
        self.assertEqual([box["center"] for box in skipped], [[70.0, 70.0], [220.0, 70.0]])

    def test_detected_item_action_uses_configured_backend(self) -> None:
        config = {"advance": {"detect_items": {"input_backend": "adb"}}}
        self.assertEqual(self.module.detected_item_action(config, (10.4, 20.6)), {"type": "adb_tap", "x": 10, "y": 21})

        config = {"advance": {"detect_items": {"input_backend": "desktop"}}}
        self.assertEqual(self.module.detected_item_action(config, (10.4, 20.6)), {"type": "click", "x": 10, "y": 21})

    def test_detected_page_scroll_actions_uses_crawl_actions_for_all_mode(self) -> None:
        config = {
            "advance": {
                "input_backend": "adb",
                "detect_items": {
                    "input_backend": "adb",
                    "page_scroll_actions": [
                        {"type": "adb_swipe", "from_x": 1, "from_y": 2, "to_x": 3, "to_y": 4}
                    ],
                    "all_items_scroll_actions": [
                        {"type": "adb_swipe", "from_x": 5, "from_y": 6, "to_x": 7, "to_y": 8}
                    ],
                },
            }
        }

        self.assertEqual(
            self.module.detected_page_scroll_actions(config, "page")[0]["from_x"],
            1,
        )
        self.assertEqual(
            self.module.detected_page_scroll_actions(config, "all")[0]["from_x"],
            5,
        )


if __name__ == "__main__":
    unittest.main()
