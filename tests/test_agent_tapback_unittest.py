import asyncio
import json
import os
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from gateway import session_context as sc
from gateway.config import PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, SendResult
from plugin.adapter import BlueBubblesAdapter
from plugin.tools import (
    chat_action,
    edit_message,
    lookup,
    remove_tapback,
    send_effect,
    tapback,
    unsend_message,
)


class ToolRuntimeTest(unittest.TestCase):
    def test_classic_and_custom_fail_closed_without_capability_contract(self):
        adapter = BlueBubblesAdapter(PlatformConfig(extra={"server_url": "http://example.invalid", "password": "fake"}))
        adapter.client = object()
        adapter._private_api_enabled = adapter._helper_connected = True
        adapter._resolve_chat_guid = AsyncMock(return_value="iMessage;+;chat")
        adapter._post_message = AsyncMock(return_value=SendResult(success=True, message_id="reaction"))

        async def exercise():
            for reaction in ("love", "like", "dislike", "laugh", "emphasize", "question"):
                self.assertTrue((await adapter.add_reaction("chat", reaction, "message"))["success"])
            self.assertEqual([call.args[1]["reaction"] for call in adapter._post_message.await_args_list],
                             ["love", "like", "dislike", "laugh", "emphasize", "question"])
            adapter._post_message.reset_mock()
            for reaction in ("💙", "👍🏽", "👩🏽‍💻", "🇺🇸", "1️⃣", "🔥"):
                self.assertTrue((await adapter.add_reaction("chat", reaction, "message"))["success"])
            self.assertEqual([call.args[1]["reaction"] for call in adapter._post_message.await_args_list],
                             ["💙", "👍🏽", "👩🏽‍💻", "🇺🇸", "1️⃣", "🔥"])
            adapter._post_message.reset_mock()
            for invalid in ("-💙", "-love", "bad", "hello world", ""):
                self.assertFalse((await adapter.add_reaction("chat", invalid, "message"))["success"])
            adapter._post_message.assert_not_awaited()

        asyncio.run(exercise())

    def test_scoped_dispatch_and_env_forgery(self):
        adapter = BlueBubblesAdapter(PlatformConfig(extra={"server_url": "http://example.invalid", "password": "fake"}))
        adapter.client = object()
        adapter._private_api_enabled = adapter._helper_connected = True
        adapter._resolve_chat_guid = AsyncMock(return_value="iMessage;+;chat")
        adapter._post_message = AsyncMock(return_value=SendResult(success=True, message_id="reaction"))
        runner = SimpleNamespace(_gateway_loop=object())
        async def dispatch(runner, make_coro, log):
            return await make_coro()
        with patch.dict(os.environ, {"HERMES_SESSION_PLATFORM": "bluebubbles", "HERMES_SESSION_CHAT_ID": "forged"}), \
             patch("tools.send_message_senders._live_adapter", return_value=(runner, adapter)), \
             patch("tools.send_message_tool._dispatch_on_gateway_loop", dispatch):
            sc.reset_session_vars()
            self.assertFalse(json.loads(tapback({"reaction": "love"}))["success"])
            tokens = sc.set_session_vars(platform="bluebubbles", chat_id="chat", message_id="message", user_id="alice", session_key="key")
            try:
                self.assertTrue(json.loads(tapback({"reaction": "love"}))["success"])
                self.assertFalse(json.loads(tapback({"reaction": "love", "message_id": "other"}))["success"])
                self.assertFalse(json.loads(tapback({"reaction": "bad"}))["success"])
                adapter._post_message.assert_awaited_once_with("/api/v1/message/react", {
                    "chatGuid": "iMessage;+;chat", "selectedMessageGuid": "message", "reaction": "love", "partIndex": 0})
            finally:
                sc.clear_session_vars(tokens)
            self.assertFalse(json.loads(tapback({"reaction": "love"}))["success"])

    def test_real_gateway_loop_dispatch(self):
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever)
        thread.start()
        runner = SimpleNamespace(_gateway_loop=loop)
        async def react(chat, reaction, message):
            return {"success": asyncio.get_running_loop() is loop, "chat": chat, "message": message}
        adapter = SimpleNamespace(add_reaction=react)
        try:
            with patch("tools.send_message_senders._live_adapter", return_value=(runner, adapter)):
                tokens = sc.set_session_vars(platform="bluebubbles", chat_id="chat", message_id="guid", user_id="alice", session_key="key")
                try:
                    self.assertEqual(json.loads(tapback({"reaction": "like"})),
                                     {"success": True, "chat": "chat", "message": "guid"})
                finally:
                    sc.clear_session_vars(tokens)
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join()
            loop.close()

    def test_merged_anchor(self):
        adapter = BlueBubblesAdapter(PlatformConfig(extra={"server_url": "http://example.invalid", "password": "fake"}))
        source = adapter.build_source(chat_id="chat", user_id="user", message_id="old")
        event = MessageEvent(text="merged", source=source, message_id="new")
        seen = []
        async def parent(self, event, key):
            seen.append(event.source.message_id)
        with patch.object(BasePlatformAdapter, "_process_message_background", parent):
            asyncio.run(adapter._process_message_background(event, "key"))
        self.assertEqual(seen, ["new"])

    def test_private_api_tools_and_adapter_methods(self):
        adapter = BlueBubblesAdapter(PlatformConfig(extra={"server_url": "http://example.invalid", "password": "fake"}))
        adapter.client = object()
        adapter._private_api_enabled = adapter._helper_connected = True
        adapter._resolve_chat_guid = AsyncMock(return_value="iMessage;+;group-1")
        adapter._post_message = AsyncMock(return_value=SendResult(success=True, message_id="msg-sent-1"))
        adapter._api_put = AsyncMock(return_value={"status": 200, "data": {"displayName": "New Group"}})
        adapter._api_post = AsyncMock(return_value={"status": 200, "data": [{"handle": "+16179431941"}]})
        adapter._api_delete = AsyncMock(return_value={"status": 200})
        adapter._api_get = AsyncMock(
            side_effect=lambda path: (
                {"status": 200, "data": {"status": "silenced"}}
                if path.endswith("/focus")
                else {"status": 200, "data": {"available": True}}
                if "/availability/" in path
                else {"status": 200, "data": [{"name": "iPhone"}]}
            )
        )
        adapter._private_api_chat_call = AsyncMock(return_value=True)
        runner = SimpleNamespace(_gateway_loop=object())

        async def dispatch(runner, make_coro, log):
            return await make_coro()

        with patch("tools.send_message_senders._live_adapter", return_value=(runner, adapter)), \
             patch("tools.send_message_tool._dispatch_on_gateway_loop", dispatch):
            sc.reset_session_vars()
            self.assertFalse(json.loads(edit_message({"text": "updated"}))["success"])

            tokens = sc.set_session_vars(
                platform="bluebubbles",
                chat_id="iMessage;+;group-1",
                message_id="inbound-msg-1",
                user_id="alice",
                session_key="key",
            )
            try:
                # 1. Tapback + remove_tapback
                self.assertTrue(json.loads(tapback({"reaction": "love"}))["success"])
                self.assertTrue(json.loads(remove_tapback({}))["success"])
                self.assertEqual(
                    adapter._post_message.await_args_list[-1].args,
                    (
                        "/api/v1/message/react",
                        {
                            "chatGuid": "iMessage;+;group-1",
                            "selectedMessageGuid": "inbound-msg-1",
                            "reaction": "-love",
                            "partIndex": 0,
                        },
                    ),
                )

                # 2. Send with effect + subject -> records last sent message_id
                res_effect = json.loads(
                    send_effect(
                        {
                            "text": "Happy Birthday!",
                            "effect": "confetti",
                            "subject": "Party Time",
                            "reply_to_current": True,
                        }
                    )
                )
                self.assertTrue(res_effect["success"])
                self.assertEqual(res_effect["effect_id"], "com.apple.messages.effect.CKConfettiEffect")
                effect_call = adapter._post_message.await_args_list[-1]
                self.assertEqual(effect_call.args[0], "/api/v1/message/text")
                self.assertEqual(effect_call.args[1]["effectId"], "com.apple.messages.effect.CKConfettiEffect")
                self.assertEqual(effect_call.args[1]["subject"], "Party Time")
                self.assertEqual(effect_call.args[1]["selectedMessageGuid"], "inbound-msg-1")

                # 3. Edit last sent message
                res_edit = json.loads(edit_message({"text": "Happy Birthday!!"}))
                self.assertTrue(res_edit["success"])
                self.assertEqual(
                    adapter._post_message.await_args_list[-1].args,
                    (
                        "/api/v1/message/msg-sent-1/edit",
                        {
                            "editedMessage": "Happy Birthday!!",
                            "backwardsCompatibilityMessage": 'Edited to "Happy Birthday!!"',
                            "partIndex": 0,
                        },
                    ),
                )

                # 4. Notify anyway on last sent message
                res_notify = json.loads(chat_action({"action": "notify_anyway"}))
                self.assertTrue(res_notify["success"])
                self.assertEqual(
                    adapter._post_message.await_args_list[-1].args,
                    ("/api/v1/message/msg-sent-1/notify", {}),
                )

                # 5. Unsend last sent message
                res_unsend = json.loads(unsend_message({}))
                self.assertTrue(res_unsend["success"])
                self.assertEqual(
                    adapter._post_message.await_args_list[-1].args,
                    ("/api/v1/message/msg-sent-1/unsend", {"partIndex": 0}),
                )

                # 6. Group chat management & mark_unread
                self.assertTrue(json.loads(chat_action({"action": "mark_unread"}))["success"])
                self.assertTrue(
                    json.loads(chat_action({"action": "rename_group", "display_name": "New Group"}))["success"]
                )
                self.assertTrue(
                    json.loads(
                        chat_action({"action": "add_participant", "address": "+16179431941"})
                    )["success"]
                )
                self.assertTrue(
                    json.loads(
                        chat_action({"action": "remove_participant", "address": "+16179431941"})
                    )["success"]
                )
                self.assertTrue(json.loads(chat_action({"action": "remove_group_icon"}))["success"])
                self.assertTrue(json.loads(chat_action({"action": "leave_group"}))["success"])

                # 7. Lookups: focus_status, availability, FindMy
                res_focus = json.loads(lookup({"query": "focus_status", "address": "+16179431941"}))
                self.assertTrue(res_focus["success"])
                self.assertEqual(res_focus["status"], "silenced")

                res_imsg = json.loads(lookup({"query": "imessage_availability", "address": "+16179431941"}))
                self.assertTrue(res_imsg["success"])
                self.assertTrue(res_imsg["available"])

                res_ft = json.loads(lookup({"query": "facetime_availability", "address": "+16179431941"}))
                self.assertTrue(res_ft["success"])
                self.assertTrue(res_ft["available"])

                res_friends = json.loads(lookup({"query": "findmy_friends", "refresh": True}))
                self.assertTrue(res_friends["success"])
                res_devices = json.loads(lookup({"query": "findmy_devices"}))
                self.assertTrue(res_devices["success"])
            finally:
                sc.clear_session_vars(tokens)

    def test_findmy_contact_enrichment_and_macos_fallback(self):
        adapter = BlueBubblesAdapter(PlatformConfig(extra={"server_url": "http://localhost:1234", "password": "fake"}))
        adapter.client = object()
        adapter._private_api_enabled = adapter._helper_connected = True
        adapter._can_use_local_findmy_helper = lambda: True

        contacts_payload = [
            {
                "firstName": "Dominik",
                "lastName": "Airey",
                "displayName": "Dominik Airey",
                "phoneNumbers": [{"address": "+1 (781) 354-2095"}],
                "emails": [{"address": "inkdo2@gmail.com"}],
            },
            {
                "firstName": "Ian",
                "lastName": "Pang",
                "displayName": "Ian Pang",
                "phoneNumbers": [{"address": "+16178035651"}],
                "emails": [],
            },
        ]
        raw_locations = [
            {
                "handle": "+17813542095",
                "coordinates": [42.50547, -71.10775],
                "short_address": "318 South St, Reading, MA",
                "long_address": "318 South St, Reading, MA 01867, United States",
                "title": None,
                "status": "shallow",
                "last_updated": 1793992234000,
            },
            {
                "handle": "+16178035651",
                "coordinates": [42.35046, -71.04524],
                "short_address": "127 Autumn Ln, Boston, MA",
                "long_address": "127 Autumn Ln, Boston, MA 02210, United States",
                "title": None,
                "status": "shallow",
                "last_updated": 1793992234000,
            },
        ]

        adapter._api_get = AsyncMock(
            side_effect=lambda path: (
                {"status": 200, "data": contacts_payload}
                if path == "/api/v1/contact"
                else {"status": 200, "data": []}
            )
        )
        pushed = []

        with patch("plugin.adapter.fetch_macos_findmy_friends", return_value=raw_locations), \
             patch("plugin.adapter.push_findmy_locations_to_bluebubbles", side_effect=lambda items: pushed.extend(items) or True):
            res_all = asyncio.run(adapter.get_findmy_locations("friends", refresh=False))
            self.assertTrue(res_all["success"])
            self.assertEqual(len(res_all["data"]), 2)
            self.assertEqual(res_all["data"][0]["display_name"], "Dominik Airey")
            self.assertEqual(res_all["data"][0]["title"], "Dominik Airey")
            self.assertEqual(len(pushed), 2)

            res_filtered = asyncio.run(
                adapter.get_findmy_locations("friends", refresh=True, address="Dominik")
            )
            self.assertTrue(res_filtered["success"])
            self.assertEqual(len(res_filtered["data"]), 1)
            self.assertEqual(res_filtered["data"][0]["handle"], "+17813542095")
            self.assertEqual(res_filtered["data"][0]["display_name"], "Dominik Airey")
            self.assertIn("Reading, MA", res_filtered["data"][0]["short_address"])

