# bluebubbles-connector

BlueBubbles iMessage platform plugin (`bluebubbles-platform`) for Hermes Agent. It connects Hermes to a macOS BlueBubbles server over HTTP for outbound messages, reactions, typing indicators, and read receipts, and registers a local webhook listener for inbound `new-message` and `updated-message` events.

## Features

- **Multi-profile DM and group routing.** Differentiates direct message chat GUIDs (`;-;`) from group chat GUIDs (`;+;`) so multiple Hermes profiles can share one BlueBubbles server and only accept or reply to their designated chats.
- **Webhook hydration and late media.** Merges sparse `new-message` and `updated-message` webhook deliveries by message GUID, hydrates missing chat or attachment metadata from `/api/v1/message/<guid>?with=chats,attachments`, and retries pending attachments for up to five minutes without replaying admitted text.
- **Tapback reactions.** Provides `add_reaction`, `remove_reaction`, and the `bluebubbles_tapback` agent tool for the six native iMessage tapbacks (`love`, `like`, `dislike`, `laugh`, `emphasize`, `question`) through the BlueBubbles Private API.

## Installation

1. Apply the gateway admission receipt patch to the Hermes checkout if not already applied:

```bash
git -C ~/.hermes/hermes-agent apply patches/hermes-bf0203a-admission-receipts.patch
```

2. Copy `plugin/` into each target profile's platform plugin directory:

```bash
mkdir -p ~/.hermes/profiles/<profile>/plugins/platforms/bluebubbles
rsync -av --delete --exclude="__pycache__" plugin/ \
  ~/.hermes/profiles/<profile>/plugins/platforms/bluebubbles/
```

3. Enable the plugin and toolset in `config.yaml` for each profile:

```yaml
plugins:
  enabled:
    - platforms/bluebubbles
platform_toolsets:
  bluebubbles:
    - hermes-bluebubbles
    - bluebubbles
```

4. Restart the gateway:

```bash
hermes -p default gateway restart
```

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

- Chat GUIDs containing `;+;` are group chats (for example `any;+;74ca28f2630d48908a8b72a9d3865376` or `iMessage;+;74ca28f2630d48908a8b72a9d3865376`).
- Chat GUIDs containing `;-;` are direct messages (for example `any;-;+16179431941` or `iMessage;-;+16179431941`).
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
      chat_id: any;+;74ca28f2630d48908a8b72a9d3865376
    extra:
      require_mention: false
      allowed_chat_guids:
        - "any;+;74ca28f2630d48908a8b72a9d3865376"
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
      - "yannipang@icloud.com"
      - "+16179431941"
    home_channel:
      platform: bluebubbles
      chat_id: "any;-;+16179431941"
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
