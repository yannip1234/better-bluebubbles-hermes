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
        adapter.client = SimpleNamespace(
            post=AsyncMock(
                return_value=SimpleNamespace(
                    raise_for_status=lambda: None,
                    json=lambda: {"status": 200, "data": {"guid": "sticker-sent-1"}},
                )
            )
        )
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
        adapter.client = SimpleNamespace(
            post=AsyncMock(
                return_value=SimpleNamespace(
                    raise_for_status=lambda: None,
                    json=lambda: {"status": 200, "data": {"guid": "sticker-sent-1"}},
                )
            )
        )
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
        adapter.client = SimpleNamespace(
            post=AsyncMock(
                return_value=SimpleNamespace(
                    raise_for_status=lambda: None,
                    json=lambda: {"status": 200, "data": {"guid": "sticker-sent-1"}},
                )
            )
        )
        adapter._private_api_enabled = adapter._helper_connected = True
        adapter._resolve_chat_guid = AsyncMock(return_value="iMessage;+;group-1")
        adapter._post_message = AsyncMock(return_value=SendResult(success=True, message_id="msg-sent-1"))
        adapter._api_put = AsyncMock(return_value={"status": 200, "data": {"displayName": "New Group"}})
        adapter._api_post = AsyncMock(return_value={"status": 200, "data": [{"handle": "+15551234567"}]})
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
                        chat_action({"action": "add_participant", "address": "+15551234567"})
                    )["success"]
                )
                self.assertTrue(
                    json.loads(
                        chat_action({"action": "remove_participant", "address": "+15551234567"})
                    )["success"]
                )
                self.assertTrue(json.loads(chat_action({"action": "remove_group_icon"}))["success"])
                self.assertTrue(json.loads(chat_action({"action": "leave_group"}))["success"])

                # 7. Lookups: focus_status, availability, FindMy
                res_focus = json.loads(lookup({"query": "focus_status", "address": "+15551234567"}))
                self.assertTrue(res_focus["success"])
                self.assertEqual(res_focus["status"], "silenced")

                res_imsg = json.loads(lookup({"query": "imessage_availability", "address": "+15551234567"}))
                self.assertTrue(res_imsg["success"])
                self.assertTrue(res_imsg["available"])

                res_ft = json.loads(lookup({"query": "facetime_availability", "address": "+15551234567"}))
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
                self.assertEqual(fmt_spec["mode"], "formatted_text")
                self.assertEqual(fmt_spec["styles"], ["bold", "italic"])
                self.assertEqual(fmt_spec["text_effect"], "explode")
                self.assertEqual(fmt_spec["ranges"][0]["text_effect"], "ripple")

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
                self.assertEqual(later_spec["mode"], "send_later")
                self.assertEqual(later_spec["styles"], ["bold"])
                self.assertGreater(later_spec["scheduled_at"], 1000000000)

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
                self.assertEqual(poll_spec["mode"], "poll_create")
                self.assertEqual(poll_spec["title"], "Dinner tonight?")

                res_vote = json.loads(
                    poll(
                        {
                            "action": "vote",
                            "message_id": "poll-guid-1",
                            "option": "Tacos",
                        }
                    )
                )
                self.assertTrue(res_vote["success"])
                vote_spec = json.loads(
                    adapter._post_message.await_args_list[-1].args[1]["effectId"][len("__bb_custom:"):]
                )
                self.assertEqual(vote_spec["mode"], "poll_vote")
                self.assertEqual(vote_spec["poll_guid"], "poll-guid-1")
                self.assertEqual(vote_spec["option"], "Tacos")

                res_add_opt = json.loads(
                    poll(
                        {
                            "action": "add_option",
                            "message_id": "poll-guid-1",
                            "option": "Ramen",
                        }
                    )
                )
                self.assertTrue(res_add_opt["success"])
                add_opt_spec = json.loads(
                    adapter._post_message.await_args_list[-1].args[1]["effectId"][len("__bb_custom:"):]
                )
                self.assertEqual(add_opt_spec["mode"], "poll_add_option")
                self.assertEqual(add_opt_spec["option_text"], "Ramen")

                # 11. Sticker tapbacks, freeform overlay, and standalone stickers
                import tempfile
                with tempfile.NamedTemporaryFile(suffix=".png") as tmp_sticker:
                    tmp_sticker.write(b"\x89PNG\r\n\x1a\n")
                    tmp_sticker.flush()

                    res_st_tapback = json.loads(
                        sticker({"mode": "tapback", "file_path": tmp_sticker.name})
                    )
                    self.assertTrue(res_st_tapback["success"])
                    st_tb_form = adapter.client.post.await_args_list[-1].kwargs["data"]
                    st_tb_spec = json.loads(st_tb_form["effectId"][len("__bb_custom:"):])
                    self.assertEqual(st_tb_spec["mode"], "sticker_tapback")
                    self.assertTrue(st_tb_spec["is_tapback"])
                    self.assertEqual(st_tb_form["selectedMessageGuid"], "inbound-msg-1")

                    res_st_overlay = json.loads(
                        sticker(
                            {
                                "mode": "overlay",
                                "file_path": tmp_sticker.name,
                                "x_scalar": 0.35,
                                "y_scalar": 0.65,
                                "scale": 1.2,
                                "rotation": 15.0,
                            }
                        )
                    )
                    self.assertTrue(res_st_overlay["success"])
                    st_ov_form = adapter.client.post.await_args_list[-1].kwargs["data"]
                    st_ov_spec = json.loads(st_ov_form["effectId"][len("__bb_custom:"):])
                    self.assertEqual(st_ov_spec["mode"], "sticker")
                    self.assertFalse(st_ov_spec["is_tapback"])
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
                "firstName": "Alice",
                "lastName": "Example",
                "displayName": "Alice Example",
                "phoneNumbers": [{"address": "+1 (555) 234-5678"}],
                "emails": [{"address": "alice@example.com"}],
            },
            {
                "firstName": "Bob",
                "lastName": "Sample",
                "displayName": "Bob Sample",
                "phoneNumbers": [{"address": "+15558765432"}],
                "emails": [],
            },
        ]
        raw_locations = [
            {
                "handle": "+15552345678",
                "coordinates": [37.3349, -122.0090],
                "short_address": "1 Apple Park Way, Cupertino, CA",
                "long_address": "1 Apple Park Way, Cupertino, CA 95014, United States",
                "title": None,
                "status": "shallow",
                "last_updated": 1793992234000,
            },
            {
                "handle": "+15558765432",
                "coordinates": [37.7749, -122.4194],
                "short_address": "100 Market St, San Francisco, CA",
                "long_address": "100 Market St, San Francisco, CA 94105, United States",
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
            self.assertEqual(res_all["data"][0]["display_name"], "Alice Example")
            self.assertEqual(res_all["data"][0]["title"], "Alice Example")
            self.assertEqual(len(pushed), 2)

            res_filtered = asyncio.run(
                adapter.get_findmy_locations("friends", refresh=True, address="Alice")
            )
            self.assertTrue(res_filtered["success"])
            self.assertEqual(len(res_filtered["data"]), 1)
            self.assertEqual(res_filtered["data"][0]["handle"], "+15552345678")
            self.assertEqual(res_filtered["data"][0]["display_name"], "Alice Example")
            self.assertIn("Cupertino, CA", res_filtered["data"][0]["short_address"])

    def test_turn_working_placeholder_progress_rollover_and_final_morph(self):
        from gateway.platforms.event import MessageEvent, MessageType
        from plugin.adapter import _turn_chat_key

        adapter = BlueBubblesAdapter(
            PlatformConfig(
                extra={
                    "server_url": "http://localhost:1234",
                    "password": "fake",
                    "status_tapbacks": False,
                    "working_placeholder": True,
                    "working_placeholder_delay": 0.02,
                    "working_placeholder_text": "⏳ Working on it...",
                }
            )
        )
        adapter.client = AsyncMock()
        adapter._private_api_enabled = True
        adapter._helper_connected = True
        adapter._guid_cache["+15551234567"] = "iMessage;-;+15551234567"
        adapter._guid_cache["iMessage;-;+15551234567"] = "iMessage;-;+15551234567"

        api_calls: list[tuple[str, dict]] = []
        sent_counter = [0]

        async def fake_post_message(path: str, payload: dict):
            api_calls.append((path, dict(payload)))
            if path == "/api/v1/message/text":
                sent_counter[0] += 1
                return SendResult(success=True, message_id=f"bubble-{sent_counter[0]}")
            return SendResult(success=True, message_id="ok")

        adapter._post_message = AsyncMock(side_effect=fake_post_message)

        async def scenario():
            source = adapter.build_source(chat_id="+15551234567", chat_type="dm", user_id="+15551234567")
            event = MessageEvent(
                text="run long task",
                message_type=MessageType.TEXT,
                source=source,
                message_id="user-msg-99",
            )
            turn_key = _turn_chat_key("+15551234567")

            async def fake_super_process(_self, ev, sk):
                # Wait for working placeholder to fire automatically
                await asyncio.sleep(0.05)
                self.assertEqual(adapter._turn_progress_bubbles.get(turn_key), "bubble-1")
                self.assertEqual(len(api_calls), 1)
                self.assertEqual(api_calls[0][0], "/api/v1/message/text")
                self.assertEqual(api_calls[0][1]["message"], "⏳ Working on it...")
                self.assertEqual(api_calls[0][1]["selectedMessageGuid"], "user-msg-99")

                # 4 mid-turn progress updates should edit bubble-1 in place (edits 1..4)
                for step in range(1, 5):
                    r = await adapter.send(
                        "+15551234567",
                        f"🔧 Step {step} running...",
                        metadata={"_interim_send": True},
                    )
                    self.assertTrue(r.success)
                    self.assertEqual(r.message_id, "bubble-1")
                    self.assertEqual(api_calls[-1][0], "/api/v1/message/bubble-1/edit")
                    self.assertEqual(api_calls[-1][1]["editedMessage"], f"🔧 Step {step} running...")

                # 5th mid-turn progress update triggers rollover (unsend bubble-1 + send bubble-2)
                # because the 5th edit on bubble-1 was reserved for finalize=True!
                r5 = await adapter.send(
                    "+15551234567",
                    "🔧 Step 5 running...",
                    metadata={"_interim_send": True},
                )
                self.assertTrue(r5.success)
                self.assertEqual(r5.message_id, "bubble-1")
                self.assertEqual(api_calls[-2][0], "/api/v1/message/bubble-1/unsend")
                self.assertEqual(api_calls[-1][0], "/api/v1/message/text")
                self.assertEqual(api_calls[-1][1]["message"], "🔧 Step 5 running...")
                self.assertEqual(adapter._follow_replaced_message_id("bubble-1"), "bubble-2")

                # Subsequent edit via edit_message("bubble-1") edits bubble-2 in place!
                r6 = await adapter.edit_message(
                    "+15551234567",
                    "bubble-1",
                    "🔧 Step 6 almost done...",
                    metadata={"_interim_send": True},
                )
                self.assertTrue(r6.success)
                self.assertEqual(r6.message_id, "bubble-1")
                self.assertEqual(api_calls[-1][0], "/api/v1/message/bubble-2/edit")

                # Final answer send (notify=True) morphs bubble-2 in place into the final response!
                final_res = await adapter.send(
                    "+15551234567",
                    "Here is the final answer!\n\nSecond paragraph stays in the same bubble.",
                    metadata={"notify": True},
                )
                self.assertTrue(final_res.success)
                self.assertEqual(api_calls[-1][0], "/api/v1/message/bubble-2/edit")
                self.assertIn("Second paragraph stays in the same bubble.", api_calls[-1][1]["editedMessage"])

                # Post-delivery delete_message on a finalized progress bubble is a no-op
                self.assertTrue(await adapter.delete_message("+15551234567", "bubble-1"))
                self.assertNotEqual(api_calls[-1][0], "/api/v1/message/bubble-2/unsend")

            with patch.object(
                BlueBubblesAdapter.__mro__[2],
                "_process_message_background",
                new=fake_super_process,
            ):
                await adapter._process_message_background(event, "sess-1")

            # Also verify that if a turn finishes without a final text send, the leftover working bubble is unsent
            api_calls.clear()

            async def fake_aborted_process(_self, ev, sk):
                await asyncio.sleep(0.05)
                self.assertIn(turn_key, adapter._turn_progress_bubbles)

            with patch.object(
                BlueBubblesAdapter.__mro__[2],
                "_process_message_background",
                new=fake_aborted_process,
            ):
                await adapter._process_message_background(event, "sess-2")

            self.assertEqual(api_calls[-1][0], "/api/v1/message/bubble-3/unsend")

        asyncio.run(scenario())

    def test_status_tapbacks_seen_thinking_and_creative_agent_override(self):
        from gateway.platforms.event import MessageEvent, MessageType

        adapter = BlueBubblesAdapter(
            PlatformConfig(
                extra={
                    "server_url": "http://localhost:1234",
                    "password": "fake",
                    "thinking_tapback_delay": 0.02,
                }
            )
        )
        adapter.client = AsyncMock()
        adapter._private_api_enabled = True
        adapter._helper_connected = True
        adapter._guid_cache["+15551234567"] = "iMessage;-;+15551234567"
        adapter._guid_cache["iMessage;-;+15551234567"] = "iMessage;-;+15551234567"

        api_calls: list[tuple[str, dict]] = []
        sent_counter = [0]

        async def fake_post_message(path: str, payload: dict):
            api_calls.append((path, dict(payload)))
            if path == "/api/v1/message/text":
                sent_counter[0] += 1
                return SendResult(success=True, message_id=f"bubble-{sent_counter[0]}")
            return SendResult(success=True, message_id="ok")

        adapter._post_message = AsyncMock(side_effect=fake_post_message)

        async def scenario():
            source = adapter.build_source(chat_id="+15551234567", chat_type="dm", user_id="+15551234567")
            event = MessageEvent(
                text="check the schedule for office hours",
                message_type=MessageType.TEXT,
                source=source,
                message_id="user-msg-101",
            )

            async def fake_super_process_1(_self, ev, sk):
                # Yield once so immediate 👀 tapback task runs
                await asyncio.sleep(0.005)
                self.assertEqual(len(api_calls), 1)
                self.assertEqual(api_calls[0][0], "/api/v1/message/react")
                self.assertEqual(api_calls[0][1]["reaction"], "👀")
                self.assertEqual(api_calls[0][1]["selectedMessageGuid"], "user-msg-101")

                # Wait for thinking delay -> transitions to 🧠
                await asyncio.sleep(0.03)
                self.assertEqual(len(api_calls), 2)
                self.assertEqual(api_calls[1][0], "/api/v1/message/react")
                self.assertEqual(api_calls[1][1]["reaction"], "🧠")

                # Thinking/progress output creates bubble-1 and edits it in place
                r1 = await adapter.send(
                    "+15551234567",
                    "💭 Checking the schedule for office hours...",
                    metadata={"_interim_send": True},
                )
                self.assertTrue(r1.success)
                self.assertEqual(api_calls[-1][0], "/api/v1/message/text")
                self.assertEqual(api_calls[-1][1]["message"], "💭 Checking the schedule for office hours...")

                r2 = await adapter.edit_message(
                    "+15551234567",
                    "bubble-1",
                    "💭 Checking the schedule for office hours...\n🌐 Searching schedule...",
                    metadata={"_interim_send": True},
                )
                self.assertTrue(r2.success)
                self.assertEqual(api_calls[-1][0], "/api/v1/message/bubble-1/edit")

                # Final reply removes 🧠 tapback (-🧠) and edits bubble-1 in place!
                final_res = await adapter.send(
                    "+15551234567",
                    "Office hours are Tue 2-4pm and Thu 1-3pm.",
                    metadata={"notify": True},
                )
                self.assertTrue(final_res.success)
                self.assertEqual(api_calls[-2][0], "/api/v1/message/react")
                self.assertEqual(api_calls[-2][1]["reaction"], "-🧠")
                self.assertEqual(api_calls[-1][0], "/api/v1/message/bubble-1/edit")
                self.assertEqual(
                    api_calls[-1][1]["editedMessage"],
                    "Office hours are Tue 2-4pm and Thu 1-3pm.",
                )

            with patch.object(
                BlueBubblesAdapter.__mro__[2],
                "_process_message_background",
                new=fake_super_process_1,
            ):
                await adapter._process_message_background(event, "sess-tapback-1")

            # Turn 2: Hermes chooses a creative emoji via bluebubbles_tapback (e.g. 🦖).
            # Verify it overrides 🧠 and is NOT removed when the turn finishes!
            api_calls.clear()
            event2 = MessageEvent(
                text="rawr",
                message_type=MessageType.TEXT,
                source=source,
                message_id="user-msg-102",
            )

            async def fake_super_process_2(_self, ev, sk):
                await asyncio.sleep(0.03)
                # Agent explicitly calls add_reaction (via bluebubbles_tapback) with 🦖
                res = await adapter.add_reaction("+15551234567", "🦖", "user-msg-102")
                self.assertTrue(res["success"])
                # Tool progress line for bluebubbles_tapback should be stripped and not create a bubble
                prog_res = await adapter.send(
                    "+15551234567",
                    '⚙️ bluebubbles_tapback {"reaction": "🦖"}',
                    metadata={"_interim_send": True},
                )
                self.assertTrue(prog_res.success)
                await adapter.send(
                    "+15551234567",
                    "Dino mode activated!",
                    metadata={"notify": True},
                )

            with patch.object(
                BlueBubblesAdapter.__mro__[2],
                "_process_message_background",
                new=fake_super_process_2,
            ):
                await adapter._process_message_background(event2, "sess-tapback-2")

            reactions_sent = [p["reaction"] for path, p in api_calls if path == "/api/v1/message/react"]
            self.assertEqual(reactions_sent, ["👀", "🧠", "🦖"])

        asyncio.run(scenario())

    def test_markdown_rich_text_parsing_send_and_edit(self):
        from plugin.adapter import _parse_markdown_rich_text

        # 1. Verify bold, italic, underline, strikethrough, bold+italic, code protection, and UTF-16 emoji offsets
        plain, ranges = _parse_markdown_rich_text(
            "🔥 **Bold** and *italic* and _also italic_ and __underlined__ and ~~struck~~ and ***both*** with `snake_case *raw*`!"
        )
        self.assertEqual(
            plain,
            "🔥 Bold and italic and also italic and underlined and struck and both with snake_case *raw*!",
        )
        # Note: "🔥 " is 3 UTF-16 code units (2 for surrogate pair 🔥 + 1 for space)
        self.assertEqual(
            ranges,
            [
                {"start": 3, "length": 4, "styles": ["bold"]},
                {"start": 12, "length": 6, "styles": ["italic"]},
                {"start": 23, "length": 11, "styles": ["italic"]},
                {"start": 39, "length": 10, "styles": ["underline"]},
                {"start": 54, "length": 6, "styles": ["strikethrough"]},
                {"start": 65, "length": 4, "styles": ["bold", "italic"]},
            ],
        )

        # 2. Verify ATX heading + nested inline formatting + links
        plain_h, ranges_h = _parse_markdown_rich_text(
            "## Office Hours (*Spring*)\nVisit [**Course Page**](https://example.com/course)"
        )
        self.assertEqual(plain_h, "Office Hours (Spring)\nVisit Course Page\nhttps://example.com/course")
        self.assertEqual(
            ranges_h,
            [
                {"start": 0, "length": 14, "styles": ["bold"]},
                {"start": 14, "length": 6, "styles": ["bold", "italic"]},
                {"start": 20, "length": 1, "styles": ["bold"]},
                {"start": 28, "length": 11, "styles": ["bold"]},
            ],
        )

        # 3. Verify send() and edit_message() emit __bb_custom formatted_text specs
        adapter = BlueBubblesAdapter(
            PlatformConfig(extra={"server_url": "http://localhost:1234", "password": "fake"})
        )
        adapter.client = AsyncMock()
        adapter._private_api_enabled = True
        adapter._helper_connected = True
        adapter._guid_cache["+15551234567"] = "iMessage;-;+15551234567"
        adapter._guid_cache["iMessage;-;+15551234567"] = "iMessage;-;+15551234567"

        api_calls: list[tuple[str, dict]] = []

        async def fake_post_message(path: str, payload: dict):
            api_calls.append((path, dict(payload)))
            if path == "/api/v1/message/text":
                return SendResult(success=True, message_id="msg-rich-1")
            return SendResult(success=True, message_id="ok")

        adapter._post_message = AsyncMock(side_effect=fake_post_message)

        async def run_send_and_edit():
            res = await adapter.send("+15551234567", "• **Tuesdays**, __1:45–3:45 p.m.__ — *Room 301D*")
            self.assertTrue(res.success)
            self.assertEqual(api_calls[-1][0], "/api/v1/message/text")
            sent_payload = api_calls[-1][1]
            self.assertEqual(sent_payload["message"], "• Tuesdays, 1:45–3:45 p.m. — Room 301D")
            self.assertTrue(sent_payload["effectId"].startswith("__bb_custom:"))
            sent_spec = json.loads(sent_payload["effectId"][len("__bb_custom:"):])
            self.assertEqual(sent_spec["mode"], "formatted_text")
            self.assertEqual(
                sent_spec["ranges"],
                [
                    {"start": 2, "length": 8, "styles": ["bold"]},
                    {"start": 12, "length": 14, "styles": ["underline"]},
                    {"start": 29, "length": 9, "styles": ["italic"]},
                ],
            )

            edit_res = await adapter.edit_message(
                "+15551234567",
                "msg-rich-1",
                "• **Thursdays**, ~~1–3 p.m.~~ __2–4 p.m.__",
                finalize=True,
            )
            self.assertTrue(edit_res.success)
            self.assertEqual(api_calls[-1][0], "/api/v1/message/msg-rich-1/edit")
            edit_payload = api_calls[-1][1]
            self.assertEqual(edit_payload["editedMessage"], "• Thursdays, 1–3 p.m. 2–4 p.m.")
            self.assertTrue(edit_payload["backwardsCompatibilityMessage"].startswith("__bb_custom:"))
            edit_spec = json.loads(edit_payload["backwardsCompatibilityMessage"][len("__bb_custom:"):])
            self.assertEqual(edit_spec["mode"], "formatted_text")
            self.assertEqual(edit_spec["backward_compat"], 'Edited to "• Thursdays, 1–3 p.m. 2–4 p.m."')
            self.assertEqual(
                edit_spec["ranges"],
                [
                    {"start": 2, "length": 9, "styles": ["bold"]},
                    {"start": 13, "length": 8, "styles": ["strikethrough"]},
                    {"start": 22, "length": 8, "styles": ["underline"]},
                ],
            )

        asyncio.run(run_send_and_edit())

    def test_pre_turn_burst_grouping_typing_hold_and_in_window_edit_unsend(self):
        class FakeWebhookReq:
            def __init__(self, payload: dict):
                self.query = {"password": "pw"}
                self.headers = {}
                self._raw = json.dumps(payload).encode("utf-8")

            async def read(self) -> bytes:
                return self._raw

        async def run_burst_suite():
            dispatched = []

            async def capture_handle(event):
                event._gateway_accepted = True
                dispatched.append(event)

            adapter = BlueBubblesAdapter(
                PlatformConfig(
                    enabled=True,
                    extra={
                        "server_url": "http://127.0.0.1:1234",
                        "password": "pw",
                        "send_read_receipts": False,
                        "require_mention": False,
                        "inbound_debounce_seconds": 0.08,
                        "inbound_typing_extend_seconds": 0.22,
                        "inbound_debounce_max_seconds": 0.60,
                    },
                )
            )
            adapter.handle_message = capture_handle
            self.assertEqual(
                adapter._desired_webhook_events(),
                ("new-message", "updated-message", "typing-indicator"),
            )

            def msg_payload(guid: str, text: str, *, sender: str = "+15551234567", chat_guid: str = "iMessage;-;+15551234567", style: int = 45, date_edited: int = 0, date_retracted: int = 0, event_type: str = "new-message"):
                rec = {
                    "guid": guid,
                    "text": text,
                    "handle": {"address": sender},
                    "chats": [{"guid": chat_guid, "style": style}],
                    "attachments": [],
                }
                if date_edited:
                    rec["dateEdited"] = date_edited
                if date_retracted:
                    rec["dateRetracted"] = date_retracted
                return {"type": event_type, "data": rec}

            # 1. Rapid 6-bubble burst ("so", "you", "reply", "to", "all", "this?") -> single turn!
            for idx, word in enumerate(["so", "you", "reply", "to", "all", "this?"], start=1):
                resp = await adapter._handle_webhook(FakeWebhookReq(msg_payload(f"b-{idx}", word)))
                self.assertEqual(resp.status, 200)
                await asyncio.sleep(0.015)

            self.assertEqual(len(dispatched), 0)
            await asyncio.sleep(0.12)
            self.assertEqual(len(dispatched), 1)
            self.assertEqual(dispatched[0].text, "so\nyou\nreply\nto\nall\nthis?")
            self.assertEqual(dispatched[0].message_id, "b-6")
            self.assertEqual(dispatched[0].source.message_id, "b-6")

            # 2. Typing indicator extends the quiet window while the user keeps typing, then flushes cleanly.
            dispatched.clear()
            await adapter._handle_webhook(FakeWebhookReq(msg_payload("t-1", "first thought")))
            await asyncio.sleep(0.03)
            await adapter._handle_webhook(
                FakeWebhookReq(
                    {"type": "typing-indicator", "data": {"display": True, "guid": "iMessage;-;+15551234567"}}
                )
            )
            # Past normal 0.08s debounce, still held because user is typing (0.22s typing hold)
            await asyncio.sleep(0.09)
            self.assertEqual(len(dispatched), 0)
            # User hits Send: typing-indicator display=False arrives slightly BEFORE new-message
            await adapter._handle_webhook(
                FakeWebhookReq(
                    {"type": "typing-indicator", "data": {"display": False, "guid": "iMessage;-;+15551234567"}}
                )
            )
            await asyncio.sleep(0.02)
            self.assertEqual(len(dispatched), 0)
            await adapter._handle_webhook(FakeWebhookReq(msg_payload("t-2", "second thought")))
            await asyncio.sleep(0.12)
            self.assertEqual(len(dispatched), 1)
            self.assertEqual(dispatched[0].text, "first thought\nsecond thought")
            self.assertEqual(dispatched[0].message_id, "t-2")

            # 3. In-window edit and unsend update/remove buffered bubbles cleanly before dispatch.
            dispatched.clear()
            await adapter._handle_webhook(FakeWebhookReq(msg_payload("e-1", "typo line")))
            await adapter._handle_webhook(FakeWebhookReq(msg_payload("e-2", "remove me")))
            await adapter._handle_webhook(
                FakeWebhookReq(msg_payload("e-1", "fixed line", date_edited=100, event_type="updated-message"))
            )
            await adapter._handle_webhook(
                FakeWebhookReq(msg_payload("e-2", "remove me", date_retracted=200, event_type="updated-message"))
            )
            await asyncio.sleep(0.12)
            self.assertEqual(len(dispatched), 1)
            self.assertEqual(dispatched[0].text, "fixed line")
            self.assertEqual(dispatched[0].message_id, "e-1")

            # 4. Different senders in a group chat never merge into each other's burst.
            dispatched.clear()
            grp = "iMessage;+;chat0123456789abcdef0123456789ab"
            await adapter._handle_webhook(
                FakeWebhookReq(msg_payload("g-1", "alice part 1", sender="+15551111111", chat_guid=grp, style=43))
            )
            await adapter._handle_webhook(
                FakeWebhookReq(msg_payload("g-2", "bob interjects", sender="+15552222222", chat_guid=grp, style=43))
            )
            # Alice's burst flushed immediately when Bob spoke!
            self.assertEqual(len(dispatched), 1)
            self.assertEqual(dispatched[0].text, "alice part 1")
            self.assertEqual(dispatched[0].source.user_id, "+15551111111")
            await asyncio.sleep(0.12)
            self.assertEqual(len(dispatched), 2)
            self.assertEqual(dispatched[1].text, "bob interjects")
            self.assertEqual(dispatched[1].source.user_id, "+15552222222")

        asyncio.run(run_burst_suite())


if __name__ == "__main__":
    unittest.main()




