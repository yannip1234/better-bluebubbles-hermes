"""Unit tests for the ``bluebubbles-extender`` Hermes plugin (config_store, FastAPI routes, CLI/slash hooks)."""

from __future__ import annotations

from contextlib import suppress
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

with suppress(Exception):
    import hermes_bootstrap  # noqa: F401

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXTENDER_DIR = PROJECT_ROOT / "bluebubbles-extender"


def _load_extender_pkg():
    pkg_name = "bluebubbles_extender_pkg"
    if pkg_name in sys.modules:
        return sys.modules[pkg_name]
    spec = importlib.util.spec_from_file_location(
        pkg_name,
        EXTENDER_DIR / "__init__.py",
        submodule_search_locations=[str(EXTENDER_DIR)],
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[pkg_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_plugin_api():
    mod_name = "bluebubbles_extender_api_test"
    spec = importlib.util.spec_from_file_location(
        mod_name,
        EXTENDER_DIR / "dashboard" / "plugin_api.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


class BlueBubblesExtenderTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="bbx-test-")
        self.root = Path(self._tmp.name) / ".hermes"
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "profiles" / "family").mkdir(parents=True, exist_ok=True)
        (self.root / "profiles" / "personal").mkdir(parents=True, exist_ok=True)
        (self.root / ".env").write_text(
            "# Root env comment\nUNRELATED_SECRET=keep_me\nBLUEBUBBLES_SERVER_URL=http://127.0.0.1:1234\nBLUEBUBBLES_PASSWORD=root_pw\n",
            encoding="utf-8",
        )
        self.pkg = _load_extender_pkg()
        self.cs = self.pkg.cs

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_list_read_and_write_profile_settings(self) -> None:
        profiles = self.cs.list_profiles(root=self.root)
        names = [p["name"] for p in profiles]
        self.assertEqual(names, ["default", "family", "personal"])

        # Configure family as group-only
        updated_family = self.cs.write_profile_settings(
            "family",
            {
                "server_url": "http://127.0.0.1:1234/",
                "password": "family_secret_pw",
                "platform_enabled": True,
                "plugin_enabled": True,
                "toolsets_enabled": True,
                "allowed_chat_guids": ["any;+;chat0123456789abcdef0123456789ab"],
                "ignore_direct_messages": True,
                "ignore_group_messages": False,
                "home_channel": "any;+;chat0123456789abcdef0123456789ab",
                "home_channel_name": "Family Group",
                "require_mention": False,
                "send_read_receipts": True,
            },
            root=self.root,
        )
        eff = updated_family["effective"]
        self.assertEqual(eff["server_url"], "http://127.0.0.1:1234")
        self.assertEqual(eff["password"], self.cs.PASSWORD_MASK)
        self.assertEqual(eff["allowed_chat_guids"], ["any;+;chat0123456789abcdef0123456789ab"])
        self.assertTrue(eff["ignore_direct_messages"])
        self.assertFalse(eff["ignore_group_messages"])
        self.assertEqual(updated_family["meta"]["routing_mode"], "group_only")

        # Revealed read returns the actual password
        revealed = self.cs.read_profile_settings("family", root=self.root, reveal_password=True)
        self.assertEqual(revealed["effective"]["password"], "family_secret_pw")

        # Saving again with masked password preserves existing password
        saved_again = self.cs.write_profile_settings(
            "family",
            {"password": self.cs.PASSWORD_MASK, "require_mention": True, "mention_patterns": [r"\bhermes\b"]},
            root=self.root,
        )
        self.assertTrue(saved_again["effective"]["require_mention"])
        self.assertEqual(saved_again["effective"]["mention_patterns"], [r"\bhermes\b"])
        self.assertEqual(
            self.cs.read_profile_settings("family", root=self.root, reveal_password=True)["effective"]["password"],
            "family_secret_pw",
        )

        # Configure personal as DM-only
        updated_personal = self.cs.write_profile_settings(
            "personal",
            {
                "allowed_users": ["+15551234567", "user@icloud.com"],
                "home_channel": "any;-;+15551234567",
                "ignore_group_messages": True,
                "suppress_restart_push": True,
            },
            root=self.root,
        )
        self.assertEqual(updated_personal["meta"]["routing_mode"], "dm_only")
        self.assertTrue(updated_personal["effective"]["suppress_restart_push"])
        self.assertEqual(updated_personal["effective"]["allowed_users"], ["+15551234567", "user@icloud.com"])

    def test_sync_connector_plugin(self) -> None:
        res = self.cs.sync_connector_plugin(["family", "personal"], root=self.root)
        self.assertTrue(res["ok"])
        self.assertTrue((self.root / "profiles" / "family" / "plugins" / "platforms" / "bluebubbles" / "adapter.py").is_file())
        self.assertTrue((self.root / "profiles" / "personal" / "plugins" / "platforms" / "bluebubbles" / "findmy.py").is_file())

    def test_register_hooks_and_slash_command(self) -> None:
        ctx = MagicMock()
        self.pkg.register(ctx)
        ctx.register_cli_command.assert_called_once()
        ctx.register_command.assert_called_once()

        with patch.dict("os.environ", {"HERMES_HOME": str(self.root)}):
            out = self.pkg._slash_command_handler("set ignore_direct_messages true family")
            self.assertIn("Updated ignore_direct_messages = true", out)
            summary = self.pkg._slash_command_handler("show family")
            self.assertIn("group_only", summary)

    def test_dashboard_api_routes(self) -> None:
        api_mod = _load_plugin_api()
        with patch.dict("os.environ", {"HERMES_HOME": str(self.root)}):
            import asyncio

            state = asyncio.run(api_mod.get_extender_state(target_profile="family", reveal_password=False))
            self.assertTrue(state["ok"])
            self.assertEqual(state["selected_profile"], "family")

            payload = api_mod.SaveSettingsPayload(
                target_profile="family",
                settings={
                    "allowed_chat_guids": ["any;+;chat0123456789abcdef0123456789ab"],
                    "ignore_direct_messages": True,
                },
                sync_dotenv=True,
                sync_plugin=True,
                restart=False,
            )
            save_res = asyncio.run(api_mod.update_extender_settings(payload))
            self.assertTrue(save_res["ok"])
            self.assertEqual(save_res["profile_state"]["meta"]["routing_mode"], "group_only")
            self.assertTrue(save_res["sync_result"]["ok"])

    def test_desktop_plugin_and_config_schema_sync(self) -> None:
        desktop_js = EXTENDER_DIR / "desktop" / "plugin.js"
        self.assertTrue(desktop_js.is_file())
        js_text = desktop_js.read_text(encoding="utf-8")
        self.assertIn("registerSettingsPage", js_text)
        self.assertIn("@hermes/plugin-sdk", js_text)

        manifest_data = self.cs._load_yaml_file(EXTENDER_DIR / "plugin.yaml")
        schema = manifest_data.get("config_schema")
        self.assertIsInstance(schema, dict)
        self.assertIn("server_url", schema)
        self.assertIn("password", schema)
        self.assertIn("allowed_chat_guids", schema)
        self.assertIn("ignore_direct_messages", schema)
        self.assertIn("ignore_group_messages", schema)

        self.cs.write_profile_settings(
            "personal",
            {
                "server_url": "http://127.0.0.1:1234",
                "ignore_group_messages": True,
                "allowed_users": ["+15551234567"],
            },
            root=self.root,
        )
        cfg = self.cs._load_yaml_file(self.root / "profiles" / "personal" / "config.yaml")
        ext_entry = cfg.get("plugins", {}).get("entries", {}).get("bluebubbles-extender", {}).get("settings", {})
        self.assertTrue(ext_entry.get("ignore_group_messages"))
        self.assertEqual(ext_entry.get("allowed_users"), ["+15551234567"])


if __name__ == "__main__":
    unittest.main()
