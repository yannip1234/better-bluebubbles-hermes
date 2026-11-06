"""Agent tools for BlueBubbles Private API; destination is exclusively the admitted current turn."""
import json
from typing import Any, Callable

_REACTIONS = ("love", "like", "dislike", "laugh", "emphasize", "question")
_EFFECTS = (
    "slam",
    "loud",
    "gentle",
    "invisible",
    "balloons",
    "confetti",
    "lasers",
    "fireworks",
    "shooting-star",
    "celebration",
    "love",
    "echo",
    "spotlight",
)
_CHAT_ACTIONS = (
    "mark_read",
    "mark_unread",
    "notify_anyway",
    "rename_group",
    "add_participant",
    "remove_participant",
    "set_group_icon",
    "remove_group_icon",
    "leave_group",
)
_LOOKUP_QUERIES = (
    "focus_status",
    "imessage_availability",
    "facetime_availability",
    "findmy_friends",
    "findmy_devices",
)


def _fail(message: str) -> str:
    return json.dumps({"success": False, "error": message})


def _bound_turn_context(*, require_message: bool = True, action_label: str = "BlueBubbles tool"):
    """Validate that execution is inside an authenticated BlueBubbles turn (never env fallbacks)."""
    from gateway import session_context as sc

    def bound(name: str) -> str:
        value = sc._VAR_MAP[name].get()
        return value if value is not sc._UNSET and isinstance(value, str) else ""

    if bound("HERMES_SESSION_PLATFORM") != "bluebubbles":
        return None, f"{action_label} requires an active BlueBubbles turn."
    chat = bound("HERMES_SESSION_CHAT_ID")
    message = bound("HERMES_SESSION_MESSAGE_ID")
    if not all((chat, bound("HERMES_SESSION_KEY"), bound("HERMES_SESSION_USER_ID"))) or (
        require_message and not message
    ):
        return None, "Current authenticated message context is unavailable."
    from gateway.config import Platform
    from tools.send_message_senders import _live_adapter

    runner, adapter = _live_adapter(Platform.BLUEBUBBLES)
    if runner is None or adapter is None or getattr(runner, "_gateway_loop", None) is None:
        return None, "No live BlueBubbles gateway adapter is available."
    return (runner, adapter, chat, message), None


def _dispatch_adapter_coro(runner: Any, make_coro: Callable[[], Any], error_label: str) -> str:
    from model_tools import _run_async
    from tools.send_message_tool import _dispatch_on_gateway_loop

    try:
        result = _run_async(_dispatch_on_gateway_loop(runner, make_coro, error_label))
        if hasattr(result, "success"):
            payload = {"success": bool(result.success)}
            if getattr(result, "message_id", None):
                payload["message_id"] = result.message_id
            if getattr(result, "error", None):
                payload["error"] = result.error
            return json.dumps(payload)
        if isinstance(result, bool):
            return json.dumps({"success": result})
        return json.dumps(result)
    except Exception:
        return _fail(f"{error_label}; delivery may be uncertain.")


def tapback(args, **_kwargs):
    if not isinstance(args, dict) or set(args) != {"reaction"} or args["reaction"] not in _REACTIONS:
        return _fail("Supply only one supported reaction.")
    ctx, err = _bound_turn_context(require_message=True, action_label="Tapbacks")
    if err:
        return _fail("Tapbacks require an active BlueBubbles turn." if "active BlueBubbles" in err else err)
    runner, adapter, chat, message = ctx
    return _dispatch_adapter_coro(
        runner,
        lambda: adapter.add_reaction(chat, args["reaction"], message),
        "Tapback dispatch failed",
    )


def remove_tapback(args, **_kwargs):
    if not isinstance(args, dict) or not set(args).issubset({"message_id"}):
        return _fail("Unsupported arguments for bluebubbles_remove_tapback.")
    ctx, err = _bound_turn_context(require_message=True, action_label="Removing a tapback")
    if err:
        return _fail(err)
    runner, adapter, chat, message = ctx
    target_msg = (args.get("message_id") or "").strip() or message
    return _dispatch_adapter_coro(
        runner,
        lambda: adapter.remove_reaction(chat, target_msg),
        "Remove tapback dispatch failed",
    )


def edit_message(args, **_kwargs):
    if not isinstance(args, dict) or not set(args).issubset({"text", "message_id", "part_index"}):
        return _fail("Unsupported arguments for bluebubbles_edit_message.")
    text = args.get("text")
    if not isinstance(text, str) or not text.strip():
        return _fail("A non-empty 'text' string is required.")
    part_index = args.get("part_index", 0)
    if not isinstance(part_index, int) or isinstance(part_index, bool) or part_index < 0:
        return _fail("'part_index' must be a non-negative integer.")
    ctx, err = _bound_turn_context(require_message=False, action_label="Editing a message")
    if err:
        return _fail(err)
    runner, adapter, chat, _message = ctx
    target_id = str(args.get("message_id") or "last").strip() or "last"
    return _dispatch_adapter_coro(
        runner,
        lambda: adapter.edit_message(chat, target_id, text, part_index=part_index),
        "Edit message dispatch failed",
    )


def unsend_message(args, **_kwargs):
    if not isinstance(args, dict) or not set(args).issubset({"message_id", "part_index"}):
        return _fail("Unsupported arguments for bluebubbles_unsend_message.")
    part_index = args.get("part_index", 0)
    if not isinstance(part_index, int) or isinstance(part_index, bool) or part_index < 0:
        return _fail("'part_index' must be a non-negative integer.")
    ctx, err = _bound_turn_context(require_message=False, action_label="Unsending a message")
    if err:
        return _fail(err)
    runner, adapter, chat, _message = ctx
    target_id = str(args.get("message_id") or "last").strip() or "last"
    return _dispatch_adapter_coro(
        runner,
        lambda: adapter.unsend_message(chat, target_id, part_index=part_index),
        "Unsend message dispatch failed",
    )


def send_effect(args, **_kwargs):
    allowed = {"text", "effect", "subject", "reply_to_current"}
    if not isinstance(args, dict) or not set(args).issubset(allowed):
        return _fail("Unsupported arguments for bluebubbles_send_effect.")
    text = str(args.get("text") or "").strip()
    effect = args.get("effect")
    subject = str(args.get("subject") or "").strip() or None
    if effect is not None and effect not in _EFFECTS:
        return _fail(f"Unsupported effect. Choose one of: {', '.join(_EFFECTS)}")
    if not text and not subject:
        return _fail("Provide 'text' and/or 'subject'.")
    if not effect and not subject:
        return _fail("Provide 'effect' and/or 'subject'.")
    reply_to_current = bool(args.get("reply_to_current", False))
    ctx, err = _bound_turn_context(require_message=reply_to_current, action_label="Sending with effect")
    if err:
        return _fail(err)
    runner, adapter, chat, message = ctx
    reply_to = message if reply_to_current else None
    return _dispatch_adapter_coro(
        runner,
        lambda: adapter.send_effect(chat, text, effect=effect, subject=subject, reply_to=reply_to),
        "Send effect dispatch failed",
    )


def chat_action(args, **_kwargs):
    allowed = {"action", "display_name", "address", "file_path", "message_id"}
    if not isinstance(args, dict) or not set(args).issubset(allowed):
        return _fail("Unsupported arguments for bluebubbles_chat_action.")
    action = args.get("action")
    if action not in _CHAT_ACTIONS:
        return _fail(f"Unsupported action. Choose one of: {', '.join(_CHAT_ACTIONS)}")
    ctx, err = _bound_turn_context(require_message=False, action_label="Chat action")
    if err:
        return _fail(err)
    runner, adapter, chat, message = ctx

    if action == "mark_read":
        return _dispatch_adapter_coro(runner, lambda: adapter.mark_read(chat), "Mark read failed")
    if action == "mark_unread":
        return _dispatch_adapter_coro(runner, lambda: adapter.mark_unread(chat), "Mark unread failed")
    if action == "notify_anyway":
        target_msg = str(args.get("message_id") or "").strip() or "last"
        if target_msg == "current" and message:
            target_msg = message
        return _dispatch_adapter_coro(
            runner, lambda: adapter.notify_anyway(chat, target_msg), "Notify anyway failed"
        )
    if action == "rename_group":
        display_name = str(args.get("display_name") or "").strip()
        if not display_name:
            return _fail("'display_name' is required for rename_group.")
        return _dispatch_adapter_coro(
            runner, lambda: adapter.rename_group(chat, display_name), "Rename group failed"
        )
    if action in ("add_participant", "remove_participant"):
        address = str(args.get("address") or "").strip()
        if not address:
            return _fail(f"'address' is required for {action}.")
        op = "add" if action == "add_participant" else "remove"
        return _dispatch_adapter_coro(
            runner,
            lambda: adapter.manage_group_participant(chat, address, op),
            f"Group {action} failed",
        )
    if action == "set_group_icon":
        file_path = str(args.get("file_path") or "").strip()
        if not file_path:
            return _fail("'file_path' is required for set_group_icon.")
        return _dispatch_adapter_coro(
            runner, lambda: adapter.set_group_icon(chat, file_path), "Set group icon failed"
        )
    if action == "remove_group_icon":
        return _dispatch_adapter_coro(
            runner, lambda: adapter.remove_group_icon(chat), "Remove group icon failed"
        )
    return _dispatch_adapter_coro(runner, lambda: adapter.leave_group(chat), "Leave group failed")


def lookup(args, **_kwargs):
    allowed = {"query", "address", "refresh"}
    if not isinstance(args, dict) or not set(args).issubset(allowed):
        return _fail("Unsupported arguments for bluebubbles_lookup.")
    query = args.get("query")
    if query not in _LOOKUP_QUERIES:
        return _fail(f"Unsupported query. Choose one of: {', '.join(_LOOKUP_QUERIES)}")
    ctx, err = _bound_turn_context(require_message=False, action_label="BlueBubbles lookup")
    if err:
        return _fail(err)
    runner, adapter, chat, _message = ctx
    address = str(args.get("address") or "").strip() or None
    refresh = bool(args.get("refresh", False))

    if query == "focus_status":
        return _dispatch_adapter_coro(
            runner, lambda: adapter.get_focus_status(address, chat_id=chat), "Focus status lookup failed"
        )
    if query == "imessage_availability":
        return _dispatch_adapter_coro(
            runner,
            lambda: adapter.check_availability(address, service="imessage", chat_id=chat),
            "iMessage availability lookup failed",
        )
    if query == "facetime_availability":
        return _dispatch_adapter_coro(
            runner,
            lambda: adapter.check_availability(address, service="facetime", chat_id=chat),
            "FaceTime availability lookup failed",
        )
    target = "friends" if query == "findmy_friends" else "devices"
    return _dispatch_adapter_coro(
        runner,
        lambda: (
            adapter.get_findmy_locations(target, refresh=refresh, address=address)
            if address
            else adapter.get_findmy_locations(target, refresh=refresh)
        ),
        "FindMy lookup failed",
    )


def register_tools(ctx):
    tapback_desc = (
        "Add an iMessage Tapback to the current incoming BlueBubbles message only. "
        "Requires the BlueBubbles Private API helper. Never targets another message or chat."
    )
    ctx.register_tool(
        name="bluebubbles_tapback",
        toolset="bluebubbles",
        handler=tapback,
        description=tapback_desc,
        schema={
            "name": "bluebubbles_tapback",
            "description": tapback_desc,
            "parameters": {
                "type": "object",
                "properties": {"reaction": {"type": "string", "enum": list(_REACTIONS)}},
                "required": ["reaction"],
                "additionalProperties": False,
            },
        },
    )

    remove_tapback_desc = (
        "Remove a previously sent iMessage Tapback from the current incoming message (or a specified "
        "message_id in the current chat). Requires the BlueBubbles Private API helper."
    )
    ctx.register_tool(
        name="bluebubbles_remove_tapback",
        toolset="bluebubbles",
        handler=remove_tapback,
        description=remove_tapback_desc,
        schema={
            "name": "bluebubbles_remove_tapback",
            "description": remove_tapback_desc,
            "parameters": {
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "Optional message GUID to remove the tapback from (defaults to the current incoming message).",
                    }
                },
                "additionalProperties": False,
            },
        },
    )

    edit_desc = (
        "Edit a previously sent iMessage in the current BlueBubbles chat. Defaults to editing the last "
        "message sent by the bot in this chat if message_id is omitted. Requires the Private API helper."
    )
    ctx.register_tool(
        name="bluebubbles_edit_message",
        toolset="bluebubbles",
        handler=edit_message,
        description=edit_desc,
        schema={
            "name": "bluebubbles_edit_message",
            "description": edit_desc,
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "New message text to replace the sent message with."},
                    "message_id": {
                        "type": "string",
                        "description": "Optional message GUID to edit (defaults to 'last', the most recent bot message in this chat).",
                    },
                    "part_index": {
                        "type": "integer",
                        "description": "Message part index to edit (default 0).",
                    },
                },
                "required": ["text"],
                "additionalProperties": False,
            },
        },
    )

    unsend_desc = (
        "Unsend (retract) a previously sent iMessage in the current BlueBubbles chat. Defaults to unsending "
        "the last message sent by the bot in this chat if message_id is omitted. Requires the Private API helper."
    )
    ctx.register_tool(
        name="bluebubbles_unsend_message",
        toolset="bluebubbles",
        handler=unsend_message,
        description=unsend_desc,
        schema={
            "name": "bluebubbles_unsend_message",
            "description": unsend_desc,
            "parameters": {
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "Optional message GUID to unsend (defaults to 'last', the most recent bot message in this chat).",
                    },
                    "part_index": {
                        "type": "integer",
                        "description": "Message part index to unsend (default 0).",
                    },
                },
                "additionalProperties": False,
            },
        },
    )

    effect_desc = (
        "Send an iMessage in the current BlueBubbles chat with an expressive bubble effect (slam, loud, gentle, "
        "invisible), full-screen effect (balloons, confetti, lasers, fireworks, shooting-star, celebration, love, "
        "echo, spotlight), and/or a bold subject line. Requires the Private API helper."
    )
    ctx.register_tool(
        name="bluebubbles_send_effect",
        toolset="bluebubbles",
        handler=send_effect,
        description=effect_desc,
        schema={
            "name": "bluebubbles_send_effect",
            "description": effect_desc,
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Message body text."},
                    "effect": {
                        "type": "string",
                        "enum": list(_EFFECTS),
                        "description": "Bubble or full-screen iMessage effect.",
                    },
                    "subject": {
                        "type": "string",
                        "description": "Optional bold iMessage subject header.",
                    },
                    "reply_to_current": {
                        "type": "boolean",
                        "description": "Whether to thread the message as an inline reply to the current incoming message.",
                    },
                },
                "additionalProperties": False,
            },
        },
    )

    chat_action_desc = (
        "Perform a BlueBubbles Private API action on the current chat: mark_read, mark_unread, notify_anyway "
        "(break through recipient Focus/silenced notifications), rename_group, add_participant, remove_participant, "
        "set_group_icon, remove_group_icon, or leave_group."
    )
    ctx.register_tool(
        name="bluebubbles_chat_action",
        toolset="bluebubbles",
        handler=chat_action,
        description=chat_action_desc,
        schema={
            "name": "bluebubbles_chat_action",
            "description": chat_action_desc,
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": list(_CHAT_ACTIONS)},
                    "display_name": {
                        "type": "string",
                        "description": "New group name when action is 'rename_group'.",
                    },
                    "address": {
                        "type": "string",
                        "description": "Phone number or email when action is 'add_participant' or 'remove_participant'.",
                    },
                    "file_path": {
                        "type": "string",
                        "description": "Local image path when action is 'set_group_icon'.",
                    },
                    "message_id": {
                        "type": "string",
                        "description": "Optional message GUID when action is 'notify_anyway' (defaults to last sent message).",
                    },
                },
                "required": ["action"],
                "additionalProperties": False,
            },
        },
    )

    lookup_desc = (
        "Query BlueBubbles Private API status and iCloud services: check recipient Focus/DND status "
        "(focus_status), check whether an address supports iMessage (imessage_availability) or FaceTime "
        "(facetime_availability), or fetch FindMy locations (findmy_friends, findmy_devices) enriched with "
        "contact names."
    )
    ctx.register_tool(
        name="bluebubbles_lookup",
        toolset="bluebubbles",
        handler=lookup,
        description=lookup_desc,
        schema={
            "name": "bluebubbles_lookup",
            "description": lookup_desc,
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "enum": list(_LOOKUP_QUERIES)},
                    "address": {
                        "type": "string",
                        "description": (
                            "Optional phone number or email for focus_status / availability checks "
                            "(defaults to the current DM handle), or an optional contact name / handle "
                            "filter for findmy_friends / findmy_devices."
                        ),
                    },
                    "refresh": {
                        "type": "boolean",
                        "description": "Whether to force a live location refresh for findmy_friends or findmy_devices (default false).",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    )
