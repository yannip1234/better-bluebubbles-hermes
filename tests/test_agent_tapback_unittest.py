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
from plugin.tools import tapback


class ToolRuntimeTest(unittest.TestCase):
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
