from unittest.mock import AsyncMock

import pytest
from gateway.config import PlatformConfig
from gateway.platforms.base import SendResult
from plugin.adapter import BlueBubblesAdapter, register


def make_adapter():
    a = BlueBubblesAdapter(PlatformConfig(extra={"server_url": "http://example.invalid", "password": "test"}))
    a.client = object()
    a._private_api_enabled = a._helper_connected = True
    a._resolve_chat_guid = AsyncMock(return_value="iMessage;+;group")
    a._post_message = AsyncMock(return_value=SendResult(success=True, message_id="reaction-id"))
    return a


@pytest.mark.asyncio
async def test_add_and_remove_group_tapback():
    a = make_adapter()
    assert (await a.add_reaction("iMessage;+;group", "👍", "message-id"))["success"]
    assert (await a.remove_reaction("iMessage;+;group", "message-id"))["success"]
    assert (await a.add_reaction("iMessage;+;group", "🦄", "message-id"))["success"]
    assert (await a.remove_reaction("iMessage;+;group", "message-id"))["success"]
    calls = a._post_message.await_args_list
    assert [c.args[0] for c in calls] == ["/api/v1/message/react"] * 4
    assert [c.args[1] for c in calls] == [
        {"chatGuid": "iMessage;+;group", "selectedMessageGuid": "message-id", "reaction": "like", "partIndex": 0},
        {"chatGuid": "iMessage;+;group", "selectedMessageGuid": "message-id", "reaction": "-like", "partIndex": 0},
        {"chatGuid": "iMessage;+;group", "selectedMessageGuid": "message-id", "reaction": "🦄", "partIndex": 0},
        {"chatGuid": "iMessage;+;group", "selectedMessageGuid": "message-id", "reaction": "-🦄", "partIndex": 0}]


@pytest.mark.asyncio
async def test_reaction_fail_closed():
    a = make_adapter()
    assert not (await a.add_reaction("group", "👍"))["success"]
    assert not (await a.add_reaction("group", "not-an-emoji", "id"))["success"]
    assert not (await a.add_reaction("group", "-🔥", "id"))["success"]
    assert not (await a.remove_reaction("group", "id"))["success"]
    a._helper_connected = False
    assert not (await a.add_reaction("group", "like", "id"))["success"]
    a._post_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_failure_does_not_record_tapback():
    a = make_adapter()
    a._post_message.return_value = SendResult(success=False, error="failure")
    assert not (await a.add_reaction("group", "love", "id"))["success"]
    assert not (await a.remove_reaction("group", "id"))["success"]


def test_registration_uses_custom_factory():
    class Context:
        def register_tool(self, **kwargs):
            self.tool = kwargs
        def register_platform(self, **kwargs):
            self.options = kwargs
    ctx = Context()
    register(ctx)
    assert ctx.options["name"] == "bluebubbles"
    assert ctx.options["adapter_factory"] is BlueBubblesAdapter
