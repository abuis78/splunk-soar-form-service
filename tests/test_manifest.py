"""Static checks of the app manifest against the app spec and known SOAR pitfalls."""

import json
import unittest
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = json.loads((ROOT / "soar_form_service.json").read_text(encoding="utf-8"))
ACTIONS = {a["identifier"]: a for a in APP["actions"]}


class ManifestTest(unittest.TestCase):
    def test_appid_is_uuid(self):
        self.assertEqual(str(uuid.UUID(APP["appid"])), APP["appid"])

    def test_main_module_and_logos_exist(self):
        for key in ("main_module", "logo", "logo_dark"):
            self.assertIn(key, APP)
        for key in ("logo", "logo_dark"):
            ET.parse(ROOT / APP[key])  # valid SVG/XML
        self.assertEqual(APP["main_module"], "soar_form_service_connector.py")
        self.assertTrue((ROOT / "__init__.py").exists())

    def test_python_version_is_explicit(self):
        # SOAR 8.7 reads "3" as 3.9 and rejects the install ("Supported versions: ['3.13']")
        self.assertEqual(APP["python_version"], "3.13")

    def test_no_dependencies(self):
        self.assertNotIn("pip3_dependencies", APP)
        self.assertNotIn("pip_dependencies", APP)

    def test_asset_config(self):
        cfg = APP["configuration"]
        self.assertEqual(cfg["api_key"]["data_type"], "password")
        self.assertTrue(cfg["base_url"]["required"])
        self.assertIs(cfg["verify_server_cert"]["default"], True)
        self.assertEqual(sorted(c["order"] for c in cfg.values()), list(range(len(cfg))))

    def test_actions_match_spec(self):
        self.assertEqual(
            set(ACTIONS), {"test_connectivity", "create_form_link", "get_link_status", "revoke_link", "list_forms"}
        )
        self.assertEqual(ACTIONS["test_connectivity"]["type"], "test")
        self.assertTrue(ACTIONS["get_link_status"]["read_only"])
        self.assertFalse(ACTIONS["revoke_link"]["read_only"])

    def test_link_id_chains_between_actions(self):
        created = {o["data_path"]: o for o in ACTIONS["create_form_link"]["output"]}
        self.assertIn("form link id", created["action_result.data.*.link_id"]["contains"])
        self.assertIn("url", created["action_result.data.*.url"]["contains"])
        for ident in ("get_link_status", "revoke_link"):
            param = ACTIONS[ident]["parameters"]["link_id"]
            self.assertIn("form link id", param["contains"])
            self.assertTrue(param["required"])

    def test_output_paths_cover_parameters(self):
        for ident, action in ACTIONS.items():
            paths = {o["data_path"] for o in action["output"]}
            for name in action["parameters"]:
                self.assertIn(f"action_result.parameter.{name}", paths, ident)


if __name__ == "__main__":
    unittest.main()
