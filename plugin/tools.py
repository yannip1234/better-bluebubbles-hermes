"""Agent Tapback tool; destination is exclusively the admitted current turn."""
import json

_REACTIONS = ("love", "like", "dislike", "laugh", "emphasize", "question")


def tapback(args, **_kwargs):
    def fail(message):
        return json.dumps({"success": False, "error": message})

    if not isinstance(args, dict) or set(args) != {"reaction"} or args["reaction"] not in _REACTIONS:
        return fail("Supply only one supported reaction.")
    # Never accept env fallbacks: only a bound gateway turn carries authority.
    from gateway import session_context as sc
    def bound(name):
        value = sc._VAR_MAP[name].get()
        return value if value is not sc._UNSET and isinstance(value, str) else ""
    if bound("HERMES_SESSION_PLATFORM") != "bluebubbles":
        return fail("Tapbacks require an active BlueBubbles turn.")
    chat = bound("HERMES_SESSION_CHAT_ID")
    message = bound("HERMES_SESSION_MESSAGE_ID")
    if not all((chat, message, bound("HERMES_SESSION_KEY"), bound("HERMES_SESSION_USER_ID"))):
        return fail("Current authenticated message context is unavailable.")
    from gateway.config import Platform
    from tools.send_message_senders import _live_adapter
    runner, adapter = _live_adapter(Platform.BLUEBUBBLES)
    if runner is None or adapter is None or getattr(runner, "_gateway_loop", None) is None:
        return fail("No live BlueBubbles gateway adapter is available.")
    from model_tools import _run_async
    from tools.send_message_tool import _dispatch_on_gateway_loop
    try:
        result = _run_async(_dispatch_on_gateway_loop(
            runner, lambda: adapter.add_reaction(chat, args["reaction"], message),
            "BlueBubbles Tapback dispatch failed"))
        return json.dumps(result)
    except Exception:
        return fail("Tapback dispatch failed; delivery may be uncertain.")


def register_tools(ctx):
    description = ("Add an iMessage Tapback to the current incoming BlueBubbles message only. "
                   "Requires the BlueBubbles Private API helper. Never targets another message or chat.")
    ctx.register_tool(name="bluebubbles_tapback", toolset="bluebubbles", handler=tapback,
                      description=description, schema={"name": "bluebubbles_tapback",
                      "description": description, "parameters": {"type": "object",
                      "properties": {"reaction": {"type": "string", "enum": list(_REACTIONS)}},
                      "required": ["reaction"], "additionalProperties": False}})
