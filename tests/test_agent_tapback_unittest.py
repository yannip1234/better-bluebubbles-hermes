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
    poll,
    remove_tapback,
    send_effect,
    send_later,
    sticker,
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

                # 8. Text formatting & animated text effects via send_effect
                res_fmt = json.loads(
                    send_effect(
                        {
                            "text": "Hello Bold Explode!",
                            "styles": ["bold", "italic"],
                            "text_effect": "explode",
                            "ranges": [
                                {"start": 0, "length": 5, "styles": ["underline"], "text_effect": "ripple"}
                            ],
                        }
                    )
                )
                self.assertTrue(res_fmt["success"])
                fmt_call = adapter._post_message.await_args_list[-1]
                self.assertEqual(fmt_call.args[0], "/api/v1/message/text")
                self.assertTrue(fmt_call.args[1]["effectId"].startswith("__bb_custom:"))
                fmt_spec = json.loads(fmt_call.args[1]["effectId"][len("__bb_custom:"):])
                self.assertEqual(fmt_spec["type"], "styled_text")
                self.assertEqual(fmt_spec["styles"], ["bold", "italic"])
                self.assertEqual(fmt_spec["text_effect"], 12)
                self.assertEqual(fmt_spec["ranges"][0]["text_effect"], 1)

                # 9. Send Later
                res_later = json.loads(
                    send_later(
                        {
                            "text": "Reminder in 5m",
                            "delay_seconds": 300,
                            "styles": ["bold"],
                        }
                    )
                )
                self.assertTrue(res_later["success"])
                later_call = adapter._post_message.await_args_list[-1]
                later_spec = json.loads(later_call.args[1]["effectId"][len("__bb_custom:"):])
                self.assertEqual(later_spec["type"], "send_later")
                self.assertEqual(later_spec["styles"], ["bold"])
                self.assertGreater(later_spec["scheduled_at_ms"], 1000000000000)

                # Past date must fail validation
                self.assertFalse(
                    json.loads(send_later({"text": "past", "send_at": 1000000000}))["success"]
                )

                # 10. Native iMessage Polls (create, vote, add_option)
                res_poll = json.loads(
                    poll(
                        {
                            "action": "create",
                            "title": "Dinner tonight?",
                            "options": ["Tacos", "Sushi", "Pizza"],
                        }
                    )
                )
                self.assertTrue(res_poll["success"])
                self.assertEqual(len(res_poll["options"]), 3)
                poll_call = adapter._post_message.await_args_list[-1]
                poll_spec = json.loads(poll_call.args[1]["effectId"][len("__bb_custom:"):])
                self.assertEqual(poll_spec["type"], "poll_create")
                self.assertEqual(poll_spec["title"], "Dinner tonight?")

                first_opt_id = res_poll["options"][0]["identifier"]
                res_vote = json.loads(
                    poll(
                        {
                            "action": "vote",
                            "poll_message_guid": "poll-guid-1",
                            "option_id": first_opt_id,
                            "option_text": "Tacos",
                        }
                    )
                )
                self.assertTrue(res_vote["success"])
                vote_spec = json.loads(
                    adapter._post_message.await_args_list[-1].args[1]["effectId"][len("__bb_custom:"):]
                )
                self.assertEqual(vote_spec["type"], "poll_vote")
                self.assertEqual(vote_spec["poll_guid"], "poll-guid-1")

                res_add_opt = json.loads(
                    poll(
                        {
                            "action": "add_option",
                            "poll_message_guid": "poll-guid-1",
                            "option_text": "Ramen",
                        }
                    )
                )
                self.assertTrue(res_add_opt["success"])
                add_opt_spec = json.loads(
                    adapter._post_message.await_args_list[-1].args[1]["effectId"][len("__bb_custom:"):]
                )
                self.assertEqual(add_opt_spec["type"], "poll_add_option")
                self.assertEqual(add_opt_spec["text"], "Ramen")

                # 11. Sticker tapbacks, freeform overlay, and standalone stickers
                import tempfile
                with tempfile.NamedTemporaryFile(suffix=".png") as tmp_sticker:
                    tmp_sticker.write(b"\x89PNG\r\n\x1a\n")
                    tmp_sticker.flush()

                    res_st_tapback = json.loads(
                        sticker({"mode": "tapback", "file_path": tmp_sticker.name})
                    )
                    self.assertTrue(res_st_tapback["success"])
                    st_tb_spec = json.loads(
                        adapter._post_message.await_args_list[-1].args[1]["effectId"][len("__bb_custom:"):]
                    )
                    self.assertEqual(st_tb_spec["type"], "sticker_send")
                    self.assertEqual(st_tb_spec["mode"], "tapback")
                    self.assertEqual(st_tb_spec["selected_guid"], "inbound-msg-1")

                    res_st_overlay = json.loads(
                        sticker(
                            {
                                "mode": "overlay",
                                "file_path": tmp_sticker.name,
                                "x": 0.35,
                                "y": 0.65,
                                "scale": 1.2,
                                "rotation": 15.0,
                            }
                        )
                    )
                    self.assertTrue(res_st_overlay["success"])
                    st_ov_spec = json.loads(
                        adapter._post_message.await_args_list[-1].args[1]["effectId"][len("__bb_custom:"):]
                    )
                    self.assertEqual(st_ov_spec["mode"], "overlay")
                    self.assertAlmostEqual(st_ov_spec["x_scalar"], 0.35)
                    self.assertAlmostEqual(st_ov_spec["y_scalar"], 0.65)
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

