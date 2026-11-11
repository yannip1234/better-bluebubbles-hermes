# bluebubbles-connector

BlueBubbles iMessage platform plugin (`bluebubbles-platform`) for Hermes Agent. It connects Hermes to a macOS BlueBubbles server over HTTP for outbound messages, reactions, typing indicators, and read receipts, and registers a local webhook listener for inbound `new-message` and `updated-message` events.

## Repository Layout

| Path | Required? | Purpose |
| --- | --- | --- |
| `plugin/` | **Required** | Core Hermes platform plugin (`platforms/bluebubbles`): webhook listener, multi-profile DM/group router, Markdown rich-text formatter, FindMy helper, and `bluebubbles` agent toolset. |
| `bluebubbles-extender/` | Optional | Companion Hermes plugin that adds a **BlueBubbles** settings UI to Hermes Desktop / Web Dashboard plus `hermes bluebubbles-config` CLI commands. |
| `native/` | Optional | Objective-C wrapper (`BlueBubblesEmojiTapback.m`) for `BlueBubblesHelper.dylib` enabling arbitrary emoji tapbacks, rich-text message edits on macOS 15+, animated text effects, Send Later, polls, and stickers. |
| `scripts/` | Optional | Idempotent installer (`patch_bluebubbles_emoji_tapback.py`) that compiles `native/` and patches `BlueBubbles.app` in place to unlock custom emoji tapbacks and extended Private API features. |
| `patches/` | Optional | Small Hermes gateway patch (`hermes-bf0203a-admission-receipts.patch`) preventing duplicate mid-turn steer / plain-text approval handling on follow-up `updated-message` webhooks. |
| `tests/` | Dev only | Unit and integration test suite for inbound webhook hydration, multi-profile routing, rich-text formatting, tapbacks, and `bluebubbles-extender`. |

## Features

- **Multi-profile DM and group routing.** Differentiates direct message chat GUIDs (`;-;`) from group chat GUIDs (`;+;`) so multiple Hermes profiles can share one BlueBubbles server and only accept or reply to their designated chats.
- **Webhook hydration, late media, and inbound edits/unsends.** Merges sparse `new-message` and `updated-message` webhook deliveries by message GUID, hydrates missing chat or attachment metadata from `/api/v1/message/<guid>?with=chats,attachments`, forces Private API downloads for offloaded iCloud attachments, retries pending attachments for up to five minutes without replaying admitted text, and delivers user message edits (`dateEdited`) and unsends (`dateRetracted`) with distinct revision keys.
- **Full BlueBubbles Private API support & agent tools.** Supports Tapback reactions (add & remove), editing and unsending sent messages, iMessage bubble & screen effects, bold subject lines, attachment reply threading, read/unread state, "Notify Anyway" on silenced chats, group chat management (rename, add/remove participants, set/remove group icon, leave group), recipient Focus/DND & iMessage/FaceTime availability checks, and FindMy Friends & Devices lookups (with a native macOS 14+ `FindMyLocateSession` + `MapKit` helper, BlueBubbles `/api/v1/contact` name enrichment, and name/handle filtering).
- **`bluebubbles-extender` Settings UI plugin.** Ships a companion Hermes plugin (`bluebubbles-extender/`) that adds a **BlueBubbles** tab (`/bluebubbles`) to the Hermes Web Dashboard, a standalone web UI server (`hermes bluebubbles-config serve`), CLI configuration commands (`hermes bluebubbles-config show|set|sync`), and an in-session `/bb-config` slash command — complete with multi-profile switching, live BlueBubbles chat/contact pickers, Private API diagnostics, one-click plugin deployment sync, and gateway restart.

## Agent Tools (`bluebubbles` toolset)

All chat-mutating tools authenticate against the active BlueBubbles turn context (`gateway.session_context`) and enforce the profile's DM/group routing boundaries:

| Tool | Description |
| --- | --- |
| `bluebubbles_tapback` | Add one of the six native iMessage Tapbacks (`love`, `like`, `dislike`, `laugh`, `emphasize`, `question`) to the current incoming message |
| `bluebubbles_remove_tapback` | Remove a previously sent Tapback from the current incoming message (or a specified `message_id` in the current chat) |
| `bluebubbles_edit_message` | Edit a previously sent bot message in the current chat (`text` required; `message_id` defaults to `"last"`) |
| `bluebubbles_unsend_message` | Unsend (retract) a previously sent bot message in the current chat (`message_id` defaults to `"last"`) |
| `bluebubbles_send_effect` | Send an iMessage with a bubble effect (`slam`, `loud`, `gentle`, `invisible`), full-screen effect (`balloons`, `confetti`, `lasers`, `fireworks`, `shooting-star`, `celebration`, `love`, `echo`, `spotlight`), and/or bold `subject` line |
| `bluebubbles_chat_action` | Run a Private API chat action on the current chat: `mark_read`, `mark_unread`, `notify_anyway`, `rename_group`, `add_participant`, `remove_participant`, `set_group_icon`, `remove_group_icon`, or `leave_group` |
| `bluebubbles_lookup` | Query recipient Focus/DND status (`focus_status`), service availability (`imessage_availability`, `facetime_availability`), or FindMy locations (`findmy_friends`, `findmy_devices` with optional `refresh: true` and optional `address` filter by contact name, phone, email, or device name) |

## Installation

1. Copy `plugin/` into each target profile's platform plugin directory:

```bash
mkdir -p ~/.hermes/profiles/<profile>/plugins/platforms/bluebubbles
rsync -av --delete --exclude="__pycache__" plugin/ \
  ~/.hermes/profiles/<profile>/plugins/platforms/bluebubbles/
```

2. Enable the plugin and toolset in `config.yaml` for each profile:

```yaml
plugins:
  enabled:
    - platforms/bluebubbles
platform_toolsets:
  bluebubbles:
    - hermes-bluebubbles
    - bluebubbles
```

3. (Optional) Apply the gateway admission receipt patch to the Hermes checkout:

```bash
git -C ~/.hermes/hermes-agent apply patches/hermes-bf0203a-admission-receipts.patch
```

4. Restart the gateway:

```bash
hermes -p default gateway restart
```

### Installing the `bluebubbles-extender` Settings UI Plugin

1. Copy `bluebubbles-extender/` into `~/.hermes/plugins/bluebubbles-extender/`:

```bash
mkdir -p ~/.hermes/plugins/bluebubbles-extender
rsync -av --delete --exclude="__pycache__" bluebubbles-extender/ \
  ~/.hermes/plugins/bluebubbles-extender/
```

2. Enable `bluebubbles-extender` in `~/.hermes/config.yaml` (and in any profile `config.yaml` files you view in the dashboard):

```yaml
plugins:
  enabled:
    - bluebubbles-extender
```

3. Open the **BlueBubbles** tab in the Hermes Web Dashboard (`/bluebubbles`, e.g. `http://127.0.0.1:9119/bluebubbles`), or run the standalone UI / CLI commands:

```bash
# Launch standalone Web UI on http://127.0.0.1:8649
hermes bluebubbles-config serve --host 127.0.0.1 --port 8649

# Show or modify settings for a profile from the CLI
hermes bluebubbles-config show -p family
hermes bluebubbles-config set ignore_direct_messages true -p family --restart

# Sync bluebubbles-connector/plugin into all configured profiles
hermes bluebubbles-config sync --restart
```

### Updating Hermes and the optional `run_busy.py` patch

- **Profile plugins survive `hermes update`.** `hermes update` updates `~/.hermes/hermes-agent/` and does not modify `~/.hermes/profiles/<profile>/plugins/platforms/bluebubbles/` or profile `config.yaml` files. When `platforms/bluebubbles` is listed in `plugins.enabled`, Hermes loads the profile plugin instead of the bundled copy.
- **When `patches/hermes-bf0203a-admission-receipts.patch` is needed.** Upstream Hermes already sets `event._gateway_accepted = True` for normal turns (`gateway/platforms/base.py`) and queued busy turns (`gateway/run_busy.py`), and `BlueBubblesAdapter` sets it for inline slash commands and text debounce. The optional patch to `gateway/run_busy.py` only covers mid-turn live steering (`steer` / `interrupt`) and plain-text `"yes"` / `"no"` tool approvals so a subsequent `updated-message` webhook for the same message is not steered or approved a second time.
- If the patch is applied to `~/.hermes/hermes-agent/gateway/run_busy.py`, revert it before running `hermes update` (`git -C ~/.hermes/hermes-agent checkout -- gateway/run_busy.py`) and re-apply afterward if needed.

## Configuration

### Environment variables

| Variable | Description |
| --- | --- |
| `BLUEBUBBLES_SERVER_URL` | BlueBubbles server URL, such as `http://127.0.0.1:1234` (required) |
| `BLUEBUBBLES_PASSWORD` | BlueBubbles server API password (required) |
| `BLUEBUBBLES_ALLOWED_USERS` | Comma-separated phone numbers or emails allowed to message the bot |
| `BLUEBUBBLES_ALLOW_ALL_USERS` | Allow any sender when set to `true` |
| `BLUEBUBBLES_HOME_CHANNEL` | Default chat GUID or address for notifications and cron delivery |
| `BLUEBUBBLES_ALLOWED_CHAT_GUIDS` | Comma-separated chat GUIDs allowed on this profile |
| `BLUEBUBBLES_IGNORE_DIRECT_MESSAGES` | Ignore direct messages (`;-;` chat GUIDs) when `true` |
| `BLUEBUBBLES_IGNORE_GROUP_MESSAGES` | Ignore group chats (`;+;` chat GUIDs) when `true` |
| `BLUEBUBBLES_REQUIRE_MENTION` | Require a wake word in group chats (`true` or `false`, default `false`) |
| `BLUEBUBBLES_MENTION_PATTERNS` | Group wake-word regexes (JSON list or comma-separated) |
| `BLUEBUBBLES_SEND_READ_RECEIPTS` | Mark inbound messages read automatically (`true` or `false`, default `true`) |
| `BLUEBUBBLES_WEBHOOK_HOST` | Webhook bind host (default `127.0.0.1`) |
| `BLUEBUBBLES_WEBHOOK_PORT` | Webhook bind port (default `8645`) |
| `BLUEBUBBLES_WEBHOOK_PATH` | Webhook path (default `/bluebubbles-webhook`) |

### Sharing one BlueBubbles server across multiple profiles

When two Hermes profiles connect to the same BlueBubbles server, BlueBubbles delivers every webhook event to both profiles. The adapter scopes each profile using `allowed_chat_guids`, `ignore_direct_messages`, `ignore_group_messages`, and `home_channel`:

- Chat GUIDs containing `;+;` are group chats (for example `any;+;chat0123456789abcdef0123456789ab` or `iMessage;+;chat0123456789abcdef0123456789ab`).
- Chat GUIDs containing `;-;` are direct messages (for example `any;-;+15551234567` or `iMessage;-;+15551234567`).
- Service prefixes (`any`, `iMessage`, `SMS`, or a leading `;-;` / `;+;`) match the same chat identifier while keeping DMs and groups separate.
- If a profile sets `home_channel.chat_id` or `allowed_chat_guids` to only `;-;` DM GUIDs, `ignore_group_messages` defaults to `true`.
- If a profile sets `home_channel.chat_id` or `allowed_chat_guids` to only `;+;` group GUIDs, `ignore_direct_messages` defaults to `true`.
- Outbound sends, attachments, reactions, typing indicators, and read receipts enforce the same chat rules so a profile cannot reply to another profile's chat GUID.

#### Example: group-only profile (`~/.hermes/profiles/family/config.yaml`)

```yaml
platforms:
  bluebubbles:
    enabled: true
    home_channel:
      platform: bluebubbles
      chat_id: any;+;chat0123456789abcdef0123456789ab
    extra:
      require_mention: false
      allowed_chat_guids:
        - "any;+;chat0123456789abcdef0123456789ab"
      ignore_direct_messages: true
```

#### Example: DM-only profile (`~/.hermes/profiles/personal/config.yaml`)

```yaml
platforms:
  push:
    gateway_restart_notification: false
  bluebubbles:
    enabled: true
    allowed_users:
      - "user@icloud.com"
      - "+15551234567"
    home_channel:
      platform: bluebubbles
      chat_id: "any;-;+15551234567"
    extra:
      require_mention: false
```

## Tests

Run the test suite with an isolated `HERMES_HOME`:

```bash
TMPDIR=/private/tmp \
HERMES_HOME=$(mktemp -d /private/tmp/hh.XXXXXX) \
HERMES_DISABLE_LAZY_INSTALLS=1 \
HERMES_SUPERVISED_CHILD=1 \
PYTHONPATH=".:$HOME/.hermes/hermes-agent" \
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
pytest -p pytest_asyncio.plugin tests
```
