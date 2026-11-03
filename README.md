# BlueBubbles connector (experimental; not deployment-ready)

This is a standalone replacement platform plugin for the Hermes source revision
`bf0203a794a07aac3ff764972ab9087baa4e4f4e`. Nothing has been installed or
restarted. `plugin/` keeps the `bluebubbles-platform` plugin identity and the
`bluebubbles` platform identity, settings and authorization environment from
Hermes. It derives from Hermes BlueBubbles and PR #116401, head
`7228bc9f6c5a0a4ff2168519035491a61c68a411` (MIT; see LICENSE). This is a
snapshot of an unmerged PR, not a released upstream component.

## Important compatibility blocker

**Do not deploy as a safe replacement yet.** The installed Hermes gateway is
missing admission receipts for successful steering/redirect and plaintext
approval. A plugin cannot infer acceptance from the generic handled flag, which
also covers refusal. A patch for revision `bf0203a` only is supplied at
`patches/hermes-bf0203a-admission-receipts.patch`. It was tested against a scratch
copy of `gateway/`, not installed or deployed. The plugin locally adapts inline
and debounce receipts; it cannot repair runner internals without the core patch.

The patch records acceptance as soon as it recognizes a plaintext approval, before
invoking its handler: a handler can resolve an approval and then raise. It consumes
exceptions instead of falling
through to queue/steer, preventing unsafe duplicate handling. This deliberately
chooses at-most-once admission: if the handler fails before resolving anything,
the approval may remain pending, and a 200 response is **not** proof of resolution.
Handler and confirmation-delivery failures are logged; an operator must inspect
and resolve uncertain approvals. Process crashes, cancellation, cache expiry and
persistence are separate failure modes. A 503 on queue overflow indicates the
message was not admitted; BlueBubbles retry behavior is unverified and must not
be assumed. Review and broader testing remain required.

The intended replacement location, after that blocker is resolved and in a
separate planned change, is the profile's `plugins/platforms/bluebubbles/`
directory. A user override shadows the bundled plugin but is **not** auto-enabled:
its profile must explicitly enable `platforms/bluebubbles` in `plugins.enabled`.
Without this, discovery can log that the override shadows the bundled copy while
leaving no family-scoped adapter. Isolated real-Hermes multiplex discovery tests
in `tests/test_plugin_discovery_mux.py` reproduce both the failure and successful
adapter construction when explicitly enabled. This is a configuration fix, not
an adapter code change. For a future separately approved deployment, enable it
in the target profile with `hermes plugins enable platforms/bluebubbles
--no-allow-tool-override` under that profile's Hermes home; preserve the other
plugin entries. Do not run built-in and replacement adapters concurrently
against one webhook/server. Back up existing profile configuration and data.
Neither a live gateway nor end-to-end BlueBubbles behavior has been tested.
No credentials are included here.

## Behavior and limits

* Same-message updates are merged under per-GUID ownership, with accepted
  content deduplication; chat GUID resolution is mandatory and groups never
  fall back to sender DMs. Unknown routes are retained in memory for a later
  update, not guessed.
* Partial and late attachments are retained; pending media is retried for up to
  five minutes; later media arrives separately without repeating accepted text.
  In-memory state survives supported adapter handoff but not process restart;
  dedup entries expire after 48 hours or cache eviction.
* `add_reaction(chat_id, emoji, message_id)` and `remove_reaction(chat_id,
  message_id)` implement the Hermes send-message reaction interface using the
  BlueBubbles Private API. Explicit message GUID and Private API/helper are
  required. The six native tapbacks and common emoji aliases are supported.
  Removal only knows reactions sent by this adapter instance; it cannot infer
  reactions after restart. The `bluebubbles_tapback` agent tool accepts only a
  reaction name, never a destination. It uses the authenticated gateway turn's
  current chat and triggering message GUID, with no environment fallback, and
  requires a live profile-scoped adapter. Queued merged text uses the latest
  merged message as its anchor. It fails closed outside a BlueBubbles turn.
  No live server was contacted and no unrestricted outbound tool is enabled.

  For a future separately approved installation **after the blocker above is
  resolved**, back up the existing profile plugin directory, copy this repo's
  `plugin/` contents to `~/.hermes/profiles/family/plugins/platforms/bluebubbles/`,
  explicitly enable `platforms/bluebubbles` as described above, and make the
  `bluebubbles` toolset available on that platform with
  `HERMES_HOME="$HOME/.hermes/profiles/family" hermes tools enable bluebubbles --platform bluebubbles`.
  Restart the family gateway only in a separately authorized maintenance window.
  Verify actual tool visibility and Private API behavior after deployment; enabling
  a plugin or toolset does not prove that a particular agent can call it.
* Incoming tapbacks and self messages remain ignored, as in the bundled adapter.

## Arbitrary emoji Tapbacks: blocked on capability negotiation

Arbitrary Unicode Tapbacks are **not implemented** in this connector. The open
[server PR #839](https://github.com/BlueBubblesApp/bluebubbles-server/pull/839)
accepts custom emoji in the existing `reaction` field, and uses a leading `-`
for removal. Its diff does not expose an HTTP capability that proves both the
server and the currently connected helper support that contract. The ordinary
`private_api` and `helper_connected` flags do not prove emoji support. The
[helper PR #78](https://github.com/BlueBubblesApp/bluebubbles-helper/pull/78)
is a draft that advertises `custom_emoji_reactions` to the server in the helper
ping; that alone does not establish an HTTP capability available to this client.
Do not assume an OS or version number, a connected helper, or a local opt-in
proves runtime capability. The related helper PR #68 is closed.

Until a server-level runtime capability contract is available, custom emoji
fail closed and classic Tapbacks remain unchanged. Implementation will also
need to address capability changes/disconnects, Unicode single-emoji validation
(including ZWJ, skin tones, flags and keycaps), alias collisions with classic
Tapbacks, safe add/remove semantics, and actual delivery verification. Mock
transport tests cannot prove these behaviors on the target Messages runtime.

## Tests

Tests use synthetic messages and a fake transport; they do not contact an
actual BlueBubbles server. Upstream tests are adapted to import this plugin.
Use the Hermes Python environment with its runtime dependencies and install
`pytest` and `pytest-asyncio` in an isolated environment. To avoid touching a
real profile or triggering Hermes maintenance/re-exec, set `HERMES_HOME` to an
empty temporary directory, `HERMES_DISABLE_LAZY_INSTALLS=1`, and
`HERMES_SUPERVISED_CHILD=1`. Include this project and the Hermes checkout in
`PYTHONPATH`, then run `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest
-p pytest_asyncio.plugin tests`. Disabling auto-loading avoids registering the
asyncio plugin twice in environments that discover it automatically.

Latest isolated run: **53 passed** with the patch applied to a scratch gateway
copy. This includes real-runner queue, steer, interrupt, plaintext approval and
deny, handler/delivery exceptions, replay, and a normal `yes` without a pending
approval. Four copied upstream gateway test files covering approval routing,
authority, redirect anchors, and steer/subagent behavior: **19 passed**. The
installed core remains unmodified and still needs the patch. Four
`datetime.utcnow()` deprecation warnings and an asyncio fixture-scope warning
were reported. These tests do not prove live group privacy, media recovery,
approval resolution on handler failure, or gateway-wide behavior. No live
messages were sent.
