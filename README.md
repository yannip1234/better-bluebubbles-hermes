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
missing PR #116401's `gateway/run_busy.py` admission receipts for successful
steering/redirect and plaintext approval. A plugin cannot reliably infer
whether the runner accepted these events from its generic handled flag (which
also covers refusal). In real-runner regression tests, steer and interrupt
return 503 despite having been accepted, and a replay can be processed again.
A minimal upstream-derived patch is provided separately at
`patches/hermes-bf0203a-admission-receipts.patch`, for revision `bf0203a` only.
It was applied and tested to a scratch copy of `gateway/`, not to the installed
core. It is not a deployment or a substitute for review. The plugin locally adapts
the missing inline and debounce receipts; it does not repair runner internals.
The patch marks plaintext approvals accepted before the approval handler runs,
matching upstream: if that handler raises, an input may be marked accepted even
though approval resolution failed, and the existing runner can fall through to
other busy handling. A successful receipt is not proof of completed processing;
process crashes, cancellation and persistence remain separate failure modes.
Test approval errors and gateway-wide behavior in an isolated checkout before
considering any deployment.

The intended replacement location, after that blocker is resolved and in a
separate planned change, is the profile's `plugins/platforms/bluebubbles/`
directory. Do not run built-in and replacement adapters concurrently against
one webhook/server. Plugin override and end-to-end BlueBubbles behavior have
not been verified in a running gateway. Confirm plugin discovery and disable or
remove conflicting registrations before activation; back up existing profile
configuration and data. No credentials are included here.

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
  reactions after restart. No live server was contacted. The tool must actually
  be available to the calling Hermes session; this plugin does not bypass
  Hermes authorization or add an unrestricted outbound tool.
* Incoming tapbacks and self messages remain ignored, as in the bundled adapter.

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

Latest full run: **41 passed** with the patch applied to a scratch gateway copy;
**37 passed, 4 failed** against the unmodified installed revision (steer,
interrupt, plaintext yes and no). Tests use the installed Hermes runtime
packages and a scratch test environment. Two `datetime.utcnow()` deprecation
warnings and an asyncio fixture-scope warning were reported. The test suite does
not prove live group privacy, media recovery, approval error handling, or
cross-platform gateway behavior. No live messages were sent.
