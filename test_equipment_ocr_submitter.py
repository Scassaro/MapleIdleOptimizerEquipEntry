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
        self.assertLessEqual(self.config["advance"]["detect_items"]["min_card_width"], 90)
        self.assertLessEqual(self.config["advance"]["detect_items"]["min_card_height"], 90)
        self.assertGreaterEqual(self.config["advance"]["detect_items"]["max_all_pages"], 1)
        self.assertGreaterEqual(self.config["advance"]["detect_items"]["max_duplicate_pages_before_stop"], 1)
        self.assertLessEqual(self.config["advance"]["wait_after_advance_seconds"], 0.1)
        self.assertEqual(self.config["advance"]["detect_items"]["all_items_scroll_strategy"], "verified_row_step")
        all_items_swipe = self.config["advance"]["detect_items"]["all_items_scroll_actions"][0]
        self.assertEqual(all_items_swipe["from_y"] - all_items_swipe["to_y"], 760)
        self.assertEqual(all_items_swipe["duration_ms"], 1400)
        verified_swipe = self.config["advance"]["detect_items"]["verified_scroll_actions"][0]
        self.assertEqual(verified_swipe["from_y"] - verified_swipe["to_y"], 220)
        self.assertEqual(self.config["advance"]["detect_items"]["verified_scroll_target_rows"], 4.0)
        self.assertEqual(self.config["advance"]["detect_items"]["skip_seen_rows_after_scroll"], -1)
        self.assertEqual(self.config["advance"]["detect_items"]["skip_seen_rows_after_crawl_scroll"], -1)
        self.assertTrue(self.config["advance"]["detect_items"]["page_diagnostics"])
        self.assertFalse(self.config["advance"]["detect_items"]["page_diagnostics_images"])
        self.assertGreater(self.config["advance"]["detect_items"]["manual_page_click_delay_seconds"], 0)
        self.assertLessEqual(self.config["advance"]["detect_items"]["manual_page_click_delay_seconds"], 0.75)
        self.assertGreater(self.config["advance"]["detect_items"]["manual_selection_min_changed_pixels"], 0)
        dark_green = self.config["advance"]["detect_items"]["dark_green_threshold"]
        self.assertLessEqual(dark_green["min_g"], 75)
        self.assertGreaterEqual(dark_green["max_r"], 90)
        dark_orange = self.config["advance"]["detect_items"]["dark_orange_threshold"]
        self.assertLessEqual(dark_orange["min_r"], 70)
        self.assertLessEqual(dark_orange["min_g"], 45)
        self.assertGreaterEqual(dark_orange["max_b"], 90)
        colored_card = self.config["advance"]["detect_items"]["colored_card_threshold"]
        self.assertTrue(colored_card["enabled"])
        self.assertLessEqual(colored_card["min_saturation"], 18)
        self.assertFalse(self.config["debug_artifacts"]["save_full_screenshot"])
        self.assertFalse(self.config["debug_artifacts"]["save_region_images"])
        self.assertFalse(self.config["debug_artifacts"]["save_legacy_primary_copy"])
        self.assertEqual(self.config["capture"]["regions"][0]["crop"], [1185, 230, 700, 1000])
        self.assertEqual(self.config["advance"]["equipped_slot_ocr_region"], "equipped_slot_detail")
        self.assertTrue(self.config["advance"]["equipped_slot_manual_first"])
        self.assertLessEqual(self.config["advance"]["equipped_slot_manual_click_delay_seconds"], 0.75)
        self.assertEqual(self.config["advance"]["equipped_slot_detection"]["region"], [420, 160, 760, 1120])
        self.assertLessEqual(self.config["advance"]["equipped_slot_detection"]["min_card_width"], 90)
        self.assertLessEqual(self.config["advance"]["equipped_slot_detection"]["min_card_height"], 90)
        self.assertEqual(len(self.config["advance"]["equipped_slot_positions"]), 12)
        self.assertEqual(self.config["advance"]["equipped_slot_positions"][0]["x"], 533)
        self.assertEqual(self.config["advance"]["equipped_slot_positions"][0]["y"], 280)
        self.assertEqual(self.config["capture"]["regions"][2]["name"], "equipped_slot_detail")
        self.assertEqual(self.config["capture"]["regions"][2]["crop"], [1125, 160, 720, 1180])
        self.assertTrue(self.config["website"]["require_equipment_slot_for_submit"])
        self.assertFalse(self.config["website"]["require_equipment_tier_for_submit"])
        self.assertEqual(self.config["website"]["unequip_slots"], self.module.EQUIPMENT_SLOTS)
        self.assertGreaterEqual(self.config["website"]["unequip_timeout_seconds"], 30)
        self.assertEqual(self.config["website"]["dismantle_slots"], self.module.EQUIPMENT_SLOTS)
        self.assertGreaterEqual(self.config["website"]["dismantle_timeout_seconds"], 120)
        self.assertTrue(self.config["website"]["require_dismantle_progress"])
        self.assertFalse(self.config["advance"]["ocr_card_tier"])

    def test_dark_green_item_card_is_detected(self) -> None:
        image = Image.new("RGB", (220, 220), "white")
        image.paste((33, 90, 64), (40, 50, 170, 180))
        config = json.loads(json.dumps(self.config))
        detect_config = config["advance"]["detect_items"]
        detect_config["region"] = [0, 0, 220, 220]
        detect_config["min_card_width"] = 120
        detect_config["max_card_width"] = 160
        detect_config["min_card_height"] = 120
        detect_config["max_card_height"] = 160
        detect_config["min_component_pixels"] = 2500

        boxes = self.module.detect_visible_item_boxes(image, config)

        self.assertEqual(len(boxes), 1)
        self.assertEqual(boxes[0]["bbox"], [40, 50, 170, 180])

    def test_dark_orange_item_card_is_detected(self) -> None:
        image = Image.new("RGB", (220, 220), "white")
        image.paste((107, 79, 25), (40, 50, 170, 180))
        config = json.loads(json.dumps(self.config))
        detect_config = config["advance"]["detect_items"]
        detect_config["region"] = [0, 0, 220, 220]
        detect_config["min_card_width"] = 120
        detect_config["max_card_width"] = 160
        detect_config["min_card_height"] = 120
        detect_config["max_card_height"] = 160
        detect_config["min_component_pixels"] = 2500

        boxes = self.module.detect_visible_item_boxes(image, config)

        self.assertEqual(len(boxes), 1)
        self.assertEqual(boxes[0]["bbox"], [40, 50, 170, 180])

    def test_generic_colored_item_card_is_detected(self) -> None:
        image = Image.new("RGB", (220, 220), "white")
        image.paste((72, 86, 142), (40, 50, 170, 180))
        config = json.loads(json.dumps(self.config))
        detect_config = config["advance"]["detect_items"]
        detect_config["region"] = [0, 0, 220, 220]
        detect_config["min_card_width"] = 120
        detect_config["max_card_width"] = 160
        detect_config["min_card_height"] = 120
        detect_config["max_card_height"] = 160
        detect_config["min_component_pixels"] = 2500

        boxes = self.module.detect_visible_item_boxes(image, config)

        self.assertEqual(len(boxes), 1)
        self.assertEqual(boxes[0]["bbox"], [40, 50, 170, 180])

    def test_neutral_panel_color_is_not_detected_as_item_card(self) -> None:
        image = Image.new("RGB", (220, 220), "white")
        image.paste((190, 190, 190), (40, 50, 170, 180))
        config = json.loads(json.dumps(self.config))
        detect_config = config["advance"]["detect_items"]
        detect_config["region"] = [0, 0, 220, 220]
        detect_config["min_card_width"] = 120
        detect_config["max_card_width"] = 160
        detect_config["min_card_height"] = 120
        detect_config["max_card_height"] = 160
        detect_config["min_component_pixels"] = 2500

        boxes = self.module.detect_visible_item_boxes(image, config)

        self.assertEqual(boxes, [])

    def test_mirpg_unequip_js_uses_configured_slots(self) -> None:
        website = {
            "unequip_slots": ["Hat", "Shoes"],
            "top_level_slot_max_y": 520,
        }

        self.assertEqual(self.module.mirpg_unequip_slots(website), ["Hat", "Shoes"])
        js = self.module.mirpg_unequip_all_js(website)

        self.assertIn("__mirpgOptimizerUnequipResult", js)
        self.assertIn('"Hat"', js)
        self.assertIn('"Shoes"', js)
        self.assertIn("Unequip", js)

    def test_mirpg_dismantle_js_uses_configured_slots(self) -> None:
        website = {
            "dismantle_slots": ["Hat", "Shoes"],
            "top_level_slot_max_y": 520,
        }

        self.assertEqual(self.module.mirpg_dismantle_slots(website), ["Hat", "Shoes"])
        js = self.module.mirpg_dismantle_all_js(website)

        self.assertIn("__mirpgOptimizerDismantleResult", js)
        self.assertIn('"Hat"', js)
        self.assertIn('"Shoes"', js)
        self.assertIn("Dismantle", js)
        self.assertIn("require_dismantle_progress", js)

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

        self.assertEqual(parsed["equipment_name"], "leg - Lv.98")
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

    def test_mangled_thousands_separator_is_repaired_for_effect_values(self) -> None:
        parsed = self.parse(
            """Dark Crescent Boots
Legendary Shoes
T4
Lv.87
On-Equip Effect
Attack 7,137
Attack 2,/45 &
Attack 2 745
1st Job Skill Lv. 77
"""
        )

        self.assertEqual(
            [(effect["name"], effect["value"]) for effect in parsed["on_equip_effects"]],
            [
                ("Attack", 7137),
                ("Attack", 2745),
                ("Attack", 2745),
                ("1st Job Skill Lv.", 7),
            ],
        )
        self.assertEqual(parsed["on_equip_effects"][1]["raw_value"], "2,745")

    def test_three_digit_comparison_value_is_not_joined_as_thousands(self) -> None:
        parsed = self.parse(
            """Tenacious Knight Pauldron
Legendary Shoulder
Lv.96
On-Equip Effect
Defense 687 687
"""
        )

        self.assertEqual(
            [(effect["name"], effect["value"], effect["raw_value"]) for effect in parsed["on_equip_effects"]],
            [("Defense", 687, "687")],
        )

    def test_accuracy_s2_ocr_normalizes_to_32(self) -> None:
        parsed = self.parse(
            """Dark Korben
Legendary Gloves
Lv.98
On-Equip Effect
Accuracy S2
Accuracy S23
"""
        )

        self.assertEqual(
            [(effect["name"], effect["value"], effect["raw_value"]) for effect in parsed["on_equip_effects"]],
            [
                ("Accuracy", 32, "32"),
                ("Accuracy", 32, "32"),
            ],
        )
        self.assertEqual(parsed["stat_rows"]["accuracy"], 32.0)

    def test_ath_job_skill_ocr_normalizes_to_fourth_job_skill(self) -> None:
        parsed = self.parse(
            """Dark Crescent Boots
Legendary Shoes
Lv.87
On-Equip Effect
Ath Job Skill Lv. 77
A th Job Skill Lv. 66
"""
        )

        self.assertEqual(
            [(effect["name"], effect["value"]) for effect in parsed["on_equip_effects"]],
            [
                ("4th Job Skill Lv.", 7),
                ("4th Job Skill Lv.", 6),
            ],
        )

    def test_evasion_stays_evasion_for_cape_main_option(self) -> None:
        parsed = self.parse(
            """Black Cape
Legendary Cape
T3
Lv.98
On-Equip Effect
Attack 8,700
Max HP 43,290
Evasion 52
Critical Damage 6.8%
"""
        )

        evasion = parsed["on_equip_effects"][2]
        self.assertEqual(parsed["equipment_slot"], "Cape")
        self.assertEqual(evasion["name"], "Evasion")
        self.assertEqual(evasion["value"], 52)
        self.assertEqual(evasion["mirpg_option_id"], "evasion")

    def test_truncated_shoulder_ocr_infers_shoulder_slot(self) -> None:
        parsed = self.parse(
            """Tenacious Knight Pauldron
Legendary Should ,
Fa accessory Up
Lv.90
On-Equip Effect
Attack 8,450
Max HP 42,035
Evasion 52
"""
        )

        self.assertEqual(parsed["item_name"], "Tenacious Knight Pauldron")
        self.assertEqual(parsed["equipment_slot"], "Shoulder")
        self.assertEqual(parsed["equipment_name"], "leg - Lv.90")

    def test_tier_regex_accepts_noisy_prefix(self) -> None:
        parsed = self.parse(
            """Dark Korben
Legendary Gloves Equipped
_T3
Lv.92
On-Equip Effect
Attack 8,723
"""
        )

        self.assertEqual(parsed["tier"], 3)
        self.assertEqual(parsed["equipment_name"], "leg - Lv.92")

    def test_tier_infers_from_line_above_level(self) -> None:
        samples = [
            ("Gy 14", 4),
            ("Te) 12", 2),
            ("oS E) 73", 3),
            ("Le ,E} 11", 1),
        ]

        for tier_line, expected_tier in samples:
            with self.subTest(tier_line=tier_line):
                parsed = self.parse(
                    f"""Dark Crescent Boots
Legendary Shoes Equipped
{tier_line}
Lv.87
On-Equip Effect
Attack 7,137
"""
                )

                self.assertEqual(parsed["tier"], expected_tier)
                self.assertEqual(parsed["equipment_name"], "leg - Lv.87")

    def test_tier_override_does_not_change_equipment_name(self) -> None:
        parsed = self.parse(
            """Tenacious Knight Pauldron
Legendary Should ,
Lv.90
On-Equip Effect
Attack 8,450
"""
        )

        self.module.apply_tier_override(parsed, 3, "card_tier_ocr")

        self.assertEqual(parsed["tier"], 3)
        self.assertEqual(parsed["equipment_name"], "leg - Lv.90")
        self.assertEqual(parsed["tier_source"], "card_tier_ocr")

    def test_mirpg_submit_js_aliases_avoidance_to_evasion(self) -> None:
        payload = {
            "equipment_name": "leg - Lv.98",
            "equipment_slot": "Cape",
            "on_equip_effects": [
                {"name": "Avoidance", "value": 52, "value_text": "52", "mirpg_option_id": "evasion"},
            ],
        }

        js = self.module.mirpg_applescript_js(payload, self.config["website"])

        self.assertIn('"avoidance": "evasion"', js)
        self.assertIn("canonicalLabelKey(effect.name)", js)

    def test_mirpg_payload_requires_equipment_slot_by_default(self) -> None:
        parsed = {
            "equipment_name": "Tenacious Knight Pauldron - Legendary - Lv.90",
            "tier": 3,
            "equipment_slot": None,
            "on_equip_effects": [{"name": "Attack", "value": 8450}],
            "lines": ["Tenacious Knight Pauldron", "Legendary Should ,"],
        }

        with self.assertRaisesRegex(self.module.AutomationError, "refusing to submit"):
            self.module.mirpg_applescript_payload(parsed, self.config["website"])

    def test_mirpg_payload_allows_missing_equipment_tier(self) -> None:
        parsed = {
            "equipment_name": "Shoulder - Legendary - Lv.90",
            "tier": None,
            "equipment_slot": "Shoulder",
            "on_equip_effects": [{"name": "Attack", "value": 8450}],
            "lines": ["Tenacious Knight Pauldron", "Legendary Should ,"],
        }

        payload = self.module.mirpg_applescript_payload(parsed, self.config["website"])

        self.assertEqual(payload["equipment_name"], "Shoulder - Legendary - Lv.90")

    def test_equipment_identity_key_uses_name_slot_and_ordered_effects(self) -> None:
        parsed = {
            "equipment_name": "leg - Lv.98",
            "equipment_slot": "Shoes",
            "on_equip_effects": [
                {"name": "Attack", "value": 10761},
                {"name": "Attack", "value": 4173},
                {"name": "Critical Damage", "value": 7.3},
            ],
        }
        same = {
            "equipment_name": "leg - Lv.98",
            "equipment_slot": "Shoes",
            "on_equip_effects": [
                {"name": "Attack", "value": 10761},
                {"name": "Attack", "value": 4173},
                {"name": "Critical Damage", "value": 7.3},
            ],
        }
        reordered = {
            "equipment_name": "leg - Lv.98",
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

    def test_parsed_duplicate_status_uses_seen_identity_keys(self) -> None:
        parsed = {
            "equipment_name": "leg - Lv.98",
            "equipment_slot": "Shoes",
            "on_equip_effects": [{"name": "Attack", "value": 10761}],
        }
        seen: set[str] = set()
        config = {"skip_duplicate_parsed_equipment": True}

        identity_key, reason = self.module.parsed_duplicate_status(parsed, seen, config)
        self.assertEqual(reason, "")
        self.assertIsNotNone(identity_key)

        seen.add(identity_key)
        duplicate_key, duplicate_reason = self.module.parsed_duplicate_status(parsed, seen, config)
        self.assertEqual(duplicate_key, identity_key)
        self.assertEqual(duplicate_reason, "parsed_equipment")

    def test_equipment_slot_infers_non_shoes_type(self) -> None:
        parsed = self.parse(
            """Storm Grip
Legendary Gloves
Lv.96
On-Equip Effect
Attack 1,234
"""
        )

        self.assertEqual(parsed["equipment_name"], "leg - Lv.96")
        self.assertEqual(parsed["equipment_slot"], "Gloves")

    def test_equipment_name_abbreviates_unique_grade(self) -> None:
        parsed = self.parse(
            """Zakum Helmet
Unique Hat
Lv.92
On-Equip Effect
Attack 6,760
"""
        )

        self.assertEqual(parsed["equipment_name"], "unq - Lv.92")
        self.assertEqual(parsed["grade"], "Unique")

    def test_equipment_name_ignores_default_tier(self) -> None:
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

        self.assertEqual(parsed["equipment_name"], "leg - Lv.98")
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
        self.assertEqual(parsed["equipment_name"], "leg - Lv.98")
        self.assertEqual(
            [(effect["name"], effect["value"]) for effect in output["on_equip_effects"]],
            [
                ("3rd Job Skill Lv.", 6),
                ("1st Job Skill Lv.", 7),
            ],
        )

    def test_save_region_debug_artifacts_can_skip_png(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            debug_dir = Path(tmp)
            image = Image.new("RGB", (10, 10), "white")

            image_path, ocr_path, parsed_path = self.module.save_region_debug_artifacts(
                debug_dir=debug_dir,
                index=1,
                region_name="candidate",
                image=image,
                raw_text="Attack 1",
                parsed={"equipment_name": "leg - Lv.98"},
                save_image=False,
            )

        self.assertIsNone(image_path)
        self.assertEqual(ocr_path.name, "0001_candidate_ocr.txt")
        self.assertEqual(parsed_path.name, "0001_candidate_parsed.json")

    def test_select_primary_ocr_region_can_choose_equipped_region(self) -> None:
        regions = [
            {"name": "candidate", "role": "primary", "primary": True},
            {"name": "equipped", "role": "equipped"},
        ]

        self.assertEqual(
            self.module.select_primary_ocr_region(regions, "equipped")["name"],
            "equipped",
        )
        self.assertEqual(
            self.module.select_primary_ocr_region(regions)["name"],
            "candidate",
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

    def test_equipped_slot_positions_accepts_labeled_points(self) -> None:
        config = {
            "advance": {
                "input_backend": "adb",
                "equipped_slot_positions": [
                    {"label": "hat", "x": 175, "y": 158},
                    [835, 158, "shoulder"],
                    {"label": "skip_me", "x": 1, "y": 2, "skip": True},
                ],
            }
        }

        slots = self.module.equipped_slot_positions(config)

        self.assertEqual(
            slots,
            [
                {"label": "hat", "x": 175.0, "y": 158.0},
                {"label": "shoulder", "x": 835.0, "y": 158.0},
            ],
        )
        self.assertEqual(
            self.module.equipped_slot_action(config, slots[0]),
            {"type": "adb_tap", "x": 175, "y": 158},
        )

    def test_equipped_slot_manual_offset_uses_changed_region(self) -> None:
        before = Image.new("RGB", (240, 220), "black")
        after = before.copy()
        pixels = after.load()
        for y in range(78, 118):
            for x in range(68, 108):
                pixels[x, y] = (255, 255, 255)
        slot = {"label": "left_1", "x": 80.0, "y": 90.0}
        config = {
            "advance": {
                "movement_snapshot_analysis": {
                    "search_radius": [60, 60],
                    "diff_threshold": 10,
                    "min_changed_pixels": 20,
                }
            }
        }

        offset, detection = self.module.estimate_equipped_slot_offset_from_manual_click(before, after, slot, config)

        self.assertEqual(offset, (8.0, 8.0))
        self.assertIsNotNone(detection)
        self.assertEqual(detection["changed_pixels"], 1600)
        self.assertEqual(detection["method"], "changed_region")

    def test_equipped_slot_manual_offset_prefers_detected_top_left_card(self) -> None:
        before = Image.new("RGB", (360, 260), "black")
        after = before.copy()
        pixels = after.load()
        cards = [(70, 50, 170, 150), (210, 50, 310, 150)]
        for left, top, right, bottom in cards:
            for y in range(top, bottom):
                for x in range(left, right):
                    pixels[x, y] = (40, 220, 150)
        slot = {"label": "left_1", "x": 110.0, "y": 90.0}
        config = {
            "advance": {
                "equipped_slot_detection": {
                    "region": [0, 0, 360, 260],
                    "min_card_width": 90,
                    "max_card_width": 130,
                    "min_card_height": 90,
                    "max_card_height": 130,
                    "min_component_pixels": 1000,
                    "max_items_per_page": 12,
                },
                "equipped_slot_manual_max_offset_px": 120,
            }
        }

        offset, detection = self.module.estimate_equipped_slot_offset_from_manual_click(before, after, slot, config)

        self.assertEqual(offset, (10.0, 10.0))
        self.assertIsNotNone(detection)
        self.assertEqual(detection["method"], "equipped_slot_box_detection")
        self.assertEqual(detection["detected_label"], "r1c1")

    def test_equipped_slot_manual_offset_uses_nearest_detected_card(self) -> None:
        before = Image.new("RGB", (520, 320), "black")
        after = before.copy()
        pixels = after.load()
        cards = [(30, 30, 130, 130), (350, 150, 450, 250)]
        for left, top, right, bottom in cards:
            for y in range(top, bottom):
                for x in range(left, right):
                    pixels[x, y] = (40, 220, 150)
        slot = {"label": "left_1", "x": 390.0, "y": 190.0}
        config = {
            "advance": {
                "equipped_slot_detection": {
                    "region": [0, 0, 520, 320],
                    "min_card_width": 90,
                    "max_card_width": 130,
                    "min_card_height": 90,
                    "max_card_height": 130,
                    "min_component_pixels": 1000,
                    "max_items_per_page": 12,
                },
                "equipped_slot_manual_max_offset_px": 120,
            }
        }

        offset, detection = self.module.estimate_equipped_slot_offset_from_manual_click(before, after, slot, config)

        self.assertEqual(offset, (10.0, 10.0))
        self.assertIsNotNone(detection)
        self.assertEqual(detection["center"], [400.0, 200.0])

    def test_apply_equipped_slot_offset_shifts_without_mutating_source(self) -> None:
        slot = {"label": "left_1", "x": 175.0, "y": 158.0}

        shifted = self.module.apply_equipped_slot_offset(slot, (3.5, -2.0))

        self.assertEqual(shifted["x"], 178.5)
        self.assertEqual(shifted["y"], 156.0)
        self.assertEqual(slot["x"], 175.0)

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

    def test_analyze_detected_taps_matches_intended_card(self) -> None:
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
            debug_dir = Path(tmp)
            viewport_dir = debug_dir / "detected_viewports"
            snapshot_dir = debug_dir / "movement_snapshots" / "run"
            viewport_dir.mkdir(parents=True)
            snapshot_dir.mkdir(parents=True)

            before_path = snapshot_dir / "before.png"
            after_path = snapshot_dir / "after.png"
            before = Image.new("RGB", (220, 220), "white")
            after = Image.new("RGB", (220, 220), "white")
            pixels = after.load()
            for y in range(95, 105):
                for x in range(95, 105):
                    pixels[x, y] = (0, 0, 0)
            before.save(before_path)
            after.save(after_path)

            (viewport_dir / "tap_detected_all_0002_test.json").write_text(
                json.dumps(
                    {
                        "label": "tap_detected_all_0002",
                        "tap_count": 1,
                        "seen_skip_count": 0,
                        "row_centers": [100.0],
                        "boxes": [
                            {
                                "label": "r1c1",
                                "status": "tap",
                                "center": [100.0, 100.0],
                                "bbox": [50, 50, 150, 150],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            manifest_path = snapshot_dir / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    [
                        {
                            "phase": "before_adb_tap",
                            "label": "tap_detected_page_0002_item_0001_action_1",
                            "action": {"type": "adb_tap", "x": 100, "y": 100},
                            "image": str(before_path),
                            "snapshot_origin": [0, 0],
                            "snapshot_coordinate_space": "adb",
                        },
                        {
                            "phase": "after_adb_tap",
                            "label": "tap_detected_page_0002_item_0001_action_1",
                            "action": {"type": "adb_tap", "x": 100, "y": 100},
                            "image": str(after_path),
                            "snapshot_origin": [0, 0],
                            "snapshot_coordinate_space": "adb",
                        },
                    ]
                ),
                encoding="utf-8",
            )

            analysis = self.module.analyze_detected_taps(debug_dir, config)

        self.assertEqual(len(analysis["detections"]), 1)
        self.assertEqual(analysis["detections"][0]["status"], "ok")
        self.assertEqual(analysis["detections"][0]["intended_label"], "r1c1")
        self.assertEqual(analysis["detections"][0]["nearest_label"], "r1c1")
        self.assertEqual(analysis["unresolved"], [])

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

    def test_detect_manual_selected_box_uses_changed_pixels(self) -> None:
        before = Image.new("RGB", (360, 180), "black")
        after = before.copy()
        pixels = after.load()
        for y in range(30, 130):
            for x in range(150, 250):
                pixels[x, y] = (80, 120, 255)

        boxes = [
            {"bbox": [20, 30, 120, 130], "center": [70.0, 80.0]},
            {"bbox": [150, 30, 250, 130], "center": [200.0, 80.0]},
            {"bbox": [280, 30, 340, 130], "center": [310.0, 80.0]},
        ]
        config = {
            "advance": {
                "detect_items": {
                    "manual_selection_diff_threshold": 10,
                    "manual_selection_min_changed_pixels": 20,
                    "manual_selection_bbox_margin_px": 0,
                }
            }
        }

        selected = self.module.detect_manual_selected_box(before, after, boxes, config)

        self.assertIs(selected, boxes[1])
        self.assertTrue(selected["manual_selected"])
        self.assertGreater(selected["manual_selection_changed_pixels"], 9000)

    def test_boxes_after_manual_selection_uses_row_major_order(self) -> None:
        boxes = [
            {"row_index": 0, "column_index": 0, "center": [10.0, 10.0]},
            {"row_index": 0, "column_index": 1, "center": [20.0, 10.0]},
            {"row_index": 0, "column_index": 2, "center": [30.0, 10.0]},
            {"row_index": 1, "column_index": 0, "center": [10.0, 20.0]},
            {"row_index": 1, "column_index": 1, "center": [20.0, 20.0]},
            {"row_index": 2, "column_index": 0, "center": [10.0, 30.0]},
        ]

        remaining = self.module.boxes_after_manual_selection(boxes, boxes[1])

        self.assertEqual(
            [box["center"] for box in remaining],
            [[30.0, 10.0], [10.0, 20.0], [20.0, 20.0], [10.0, 30.0]],
        )

    def test_prepare_manual_detected_viewport_assigns_grid_without_signatures(self) -> None:
        boxes = [
            {"bbox": [10, 10, 110, 110], "center": [60.0, 60.0]},
            {"bbox": [140, 10, 240, 110], "center": [190.0, 60.0]},
            {"bbox": [10, 150, 110, 250], "center": [60.0, 200.0]},
        ]
        config = {"advance": {"detect_items": {"row_group_tolerance_px": 20}}}

        signed_boxes, row_centers = self.module.prepare_manual_detected_viewport(boxes, config)

        self.assertEqual(row_centers, [60.0, 200.0])
        self.assertEqual(
            [(box["row_index"], box["column_index"]) for box in signed_boxes],
            [(0, 0), (0, 1), (1, 0)],
        )
        self.assertNotIn("signature", signed_boxes[0])

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

    def test_detected_viewport_progress_estimates_scroll_rows(self) -> None:
        image = Image.new("RGB", (500, 700), "black")
        pixels = image.load()
        previous_boxes = [
            {"bbox": [50, 300, 150, 400], "center": [100.0, 350.0], "pixels": 10000},
            {"bbox": [220, 300, 320, 400], "center": [270.0, 350.0], "pixels": 10000},
        ]
        current_boxes = [
            {"bbox": [50, 120, 150, 220], "center": [100.0, 170.0], "pixels": 10000},
            {"bbox": [220, 120, 320, 220], "center": [270.0, 170.0], "pixels": 10000},
        ]
        for box in [*previous_boxes, *current_boxes]:
            left, top, right, bottom = box["bbox"]
            for y in range(top, bottom):
                for x in range(left, right):
                    pixels[x, y] = (30, 210, 150)
            for y in range(top + 25, top + 45):
                for x in range(left + 25, left + 75):
                    pixels[x, y] = (255, 255, 255)

        config = {
            "advance": {
                "detect_items": {
                    "signature_inset_px": 0,
                    "signature_resize": [16, 16],
                    "signature_hamming_threshold": 10,
                    "signature_color_distance_threshold": 10,
                    "row_group_tolerance_px": 20,
                }
            }
        }
        previous_signed = self.module.signed_detected_item_boxes(image, previous_boxes, [], config)
        current_signed = self.module.signed_detected_item_boxes(image, current_boxes, [], config)
        self.module.assign_detected_item_grid_indexes(previous_signed, [350.0], config)
        self.module.assign_detected_item_grid_indexes(current_signed, [170.0], config)

        progress = self.module.detected_viewport_progress(previous_signed, current_signed, config, row_spacing=180.0)

        self.assertIsNotNone(progress)
        self.assertEqual(progress["matched_previous_count"], 2)
        self.assertEqual(progress["median_shift_y"], 180.0)
        self.assertEqual(progress["median_shift_rows"], 1.0)

    def test_estimate_detected_region_vertical_shift_uses_image_alignment(self) -> None:
        previous = Image.new("RGB", (220, 320), "white")
        current = Image.new("RGB", (220, 320), "white")
        previous_pixels = previous.load()
        current_pixels = current.load()
        for y in range(320):
            color = ((y * 3) % 255, (y * 5) % 255, (y * 7) % 255)
            for x in range(220):
                previous_pixels[x, y] = color

        shift = 80
        for y in range(320 - shift):
            for x in range(220):
                current_pixels[x, y] = previous_pixels[x, y + shift]

        config = {
            "advance": {
                "detect_items": {
                    "region": [0, 0, 220, 320],
                    "viewport_shift_resize_width": 0,
                    "viewport_shift_search_step_px": 4,
                    "viewport_shift_max_px": 160,
                    "viewport_shift_min_overlap_ratio": 0.5,
                }
            }
        }

        estimate = self.module.estimate_detected_region_vertical_shift(previous, current, config)

        self.assertIsNotNone(estimate)
        self.assertEqual(estimate["shift_y"], 80.0)
        self.assertLess(estimate["score"], estimate["baseline_score"])
        self.assertEqual(
            self.module.detected_region_shift_rows(estimate, [100.0, 180.0, 260.0]),
            1.0,
        )

    def test_detected_viewport_scroll_stalled_uses_match_count_and_row_shift(self) -> None:
        config = {
            "advance": {
                "detect_items": {
                    "all_items_stop_min_matched_cards": 4,
                    "all_items_stop_scroll_rows_threshold": 0.5,
                }
            }
        }

        self.assertTrue(
            self.module.detected_viewport_scroll_stalled(
                {"matched_previous_count": 4, "median_shift_rows": 0.2},
                config,
            )
        )
        self.assertFalse(
            self.module.detected_viewport_scroll_stalled(
                {"matched_previous_count": 4, "median_shift_rows": 1.1},
                config,
            )
        )
        self.assertFalse(
            self.module.detected_viewport_scroll_stalled(
                {"matched_previous_count": 2, "median_shift_rows": 0.2},
                config,
            )
        )

    def test_should_retry_unchanged_equipment_tap_requires_different_card(self) -> None:
        config = {
            "advance": {
                "detect_items": {
                    "verify_tap_changed_equipment": True,
                    "signature_hamming_threshold": 1,
                    "signature_color_distance_threshold": 1,
                }
            }
        }
        previous_box = {
            "signature": {
                "hash": "0",
                "bits": 4,
                "mean_rgb": [10, 10, 10],
            }
        }
        different_box = {
            "signature": {
                "hash": "f",
                "bits": 4,
                "mean_rgb": [240, 240, 240],
            }
        }
        same_box = {
            "signature": {
                "hash": "0",
                "bits": 4,
                "mean_rgb": [10, 10, 10],
            }
        }

        self.assertTrue(
            self.module.should_retry_unchanged_equipment_tap(
                config,
                "same-identity",
                "same-image",
                "same-identity",
                "same-image",
                different_box,
                previous_box,
                0,
                2,
            )
        )
        self.assertFalse(
            self.module.should_retry_unchanged_equipment_tap(
                config,
                "same-identity",
                "same-image",
                "same-identity",
                "same-image",
                same_box,
                previous_box,
                0,
                2,
            )
        )
        self.assertFalse(
            self.module.should_retry_unchanged_equipment_tap(
                config,
                "same-identity",
                "same-image",
                "same-identity",
                "same-image",
                different_box,
                previous_box,
                2,
                2,
            )
        )
        self.assertFalse(
            self.module.should_retry_unchanged_equipment_tap(
                config,
                "new-identity",
                "new-image",
                "same-identity",
                "same-image",
                different_box,
                previous_box,
                0,
                2,
            )
        )


if __name__ == "__main__":
    unittest.main()
