"""Configuration store and live BlueBubbles server introspection for ``bluebubbles-extender``.

Reads and writes per-profile ``config.yaml`` and ``.env`` files across the Hermes root
(``default`` and ``~/.hermes/profiles/<name>``), probes the configured BlueBubbles server,
and syncs the ``bluebubbles-connector/plugin`` directory into profile plugin trees.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urlencode
from urllib.request import Request as UrlRequest, urlopen

try:
    import hermes_yaml as _hermes_yaml
except ImportError:
    _hermes_yaml = None  # type: ignore[assignment]

try:
    import yaml as _pyyaml
except ImportError:
    _pyyaml = None  # type: ignore[assignment]


PASSWORD_MASK = "••••••••"

SETTINGS_SCHEMA: list[dict[str, Any]] = [
    {
        "key": "server_url",
        "env": "BLUEBUBBLES_SERVER_URL",
        "yaml_path": "platforms.bluebubbles.extra.server_url",
        "label": "BlueBubbles Server URL",
        "section": "connection",
        "type": "string",
        "default": "http://127.0.0.1:1234",
        "placeholder": "http://127.0.0.1:1234",
        "description": "Base URL of your macOS BlueBubbles Server (Settings → API).",
    },
    {
        "key": "password",
        "env": "BLUEBUBBLES_PASSWORD",
        "yaml_path": "platforms.bluebubbles.extra.password",
        "label": "BlueBubbles Server Password",
        "section": "connection",
        "type": "secret",
        "default": "",
        "placeholder": "Server API password",
        "description": "API password configured in BlueBubbles Server (stored in .env).",
    },
    {
        "key": "platform_enabled",
        "env": None,
        "yaml_path": "platforms.bluebubbles.enabled",
        "label": "Enable BlueBubbles Platform Adapter",
        "section": "activation",
        "type": "boolean",
        "default": True,
        "description": "Enable platforms.bluebubbles.enabled in this profile's config.yaml.",
    },
    {
        "key": "plugin_enabled",
        "env": None,
        "yaml_path": "plugins.enabled[platforms/bluebubbles]",
        "label": "Enable Profile Plugin Override (platforms/bluebubbles)",
        "section": "activation",
        "type": "boolean",
        "default": True,
        "description": "Load this profile's custom platforms/bluebubbles plugin instead of the bundled copy.",
    },
    {
        "key": "toolsets_enabled",
        "env": None,
        "yaml_path": "platform_toolsets.bluebubbles",
        "label": "Enable BlueBubbles Agent Toolset",
        "section": "activation",
        "type": "boolean",
        "default": True,
        "description": "Expose bluebubbles_tapback, bluebubbles_edit_message, bluebubbles_unsend_message, bluebubbles_send_effect, bluebubbles_chat_action, and bluebubbles_lookup to the agent.",
    },
    {
        "key": "allowed_chat_guids",
        "env": "BLUEBUBBLES_ALLOWED_CHAT_GUIDS",
        "yaml_path": "platforms.bluebubbles.extra.allowed_chat_guids",
        "label": "Allowed Chat GUIDs",
        "section": "routing",
        "type": "list",
        "default": [],
        "placeholder": "any;+;74ca28f2630d48908a8b72a9d3865376, any;-;+16179431941",
        "description": "Restrict this profile to specific chat GUIDs (;-; for DMs, ;+; for group chats). Leave empty to allow any chat matching the DM/group toggles.",
    },
    {
        "key": "ignore_direct_messages",
        "env": "BLUEBUBBLES_IGNORE_DIRECT_MESSAGES",
        "yaml_path": "platforms.bluebubbles.extra.ignore_direct_messages",
        "label": "Ignore Direct Messages (;-;)",
        "section": "routing",
        "type": "boolean",
        "default": False,
        "description": "Drop all 1:1 direct message chats on this profile (use on group-only profiles).",
    },
    {
        "key": "ignore_group_messages",
        "env": "BLUEBUBBLES_IGNORE_GROUP_MESSAGES",
        "yaml_path": "platforms.bluebubbles.extra.ignore_group_messages",
        "label": "Ignore Group Chats (;+;)",
        "section": "routing",
        "type": "boolean",
        "default": False,
        "description": "Drop all group chats on this profile (use on personal DM-only profiles).",
    },
    {
        "key": "allowed_users",
        "env": "BLUEBUBBLES_ALLOWED_USERS",
        "yaml_path": "platforms.bluebubbles.allowed_users",
        "label": "Allowed Senders (Phone / Email)",
        "section": "access",
        "type": "list",
        "default": [],
        "placeholder": "+16179431941, user@icloud.com",
        "description": "Phone numbers (E.164) or Apple ID emails allowed to talk to the bot.",
    },
    {
        "key": "allow_all_users",
        "env": "BLUEBUBBLES_ALLOW_ALL_USERS",
        "yaml_path": "platforms.bluebubbles.extra.allow_all_users",
        "label": "Allow All Senders",
        "section": "access",
        "type": "boolean",
        "default": False,
        "description": "Allow any iMessage sender in allowed chats without checking the sender allowlist.",
    },
    {
        "key": "home_channel",
        "env": "BLUEBUBBLES_HOME_CHANNEL",
        "yaml_path": "platforms.bluebubbles.home_channel.chat_id",
        "label": "Home Channel (Chat GUID or Address)",
        "section": "home",
        "type": "string",
        "default": "",
        "placeholder": "any;-;+16179431941 or any;+;74ca28f2630d48908a8b72a9d3865376",
        "description": "Default iMessage chat GUID or handle for cron job delivery and notifications.",
    },
    {
        "key": "home_channel_name",
        "env": "BLUEBUBBLES_HOME_CHANNEL_NAME",
        "yaml_path": "platforms.bluebubbles.home_channel.name",
        "label": "Home Channel Display Name",
        "section": "home",
        "type": "string",
        "default": "",
        "placeholder": "Family Group / Personal DM",
        "description": "Optional friendly label for the home channel.",
    },
    {
        "key": "suppress_restart_push",
        "env": None,
        "yaml_path": "platforms.push.gateway_restart_notification",
        "label": "Suppress Gateway Restart Push Notifications",
        "section": "home",
        "type": "boolean",
        "default": False,
        "description": "Set platforms.push.gateway_restart_notification: false so gateway restarts stay silent.",
    },
    {
        "key": "require_mention",
        "env": "BLUEBUBBLES_REQUIRE_MENTION",
        "yaml_path": "platforms.bluebubbles.extra.require_mention",
        "label": "Require Wake-Word / Mention in Groups",
        "section": "behavior",
        "type": "boolean",
        "default": False,
        "description": "Only respond in group chats when a wake-word pattern matches (or the bot's message is replied to).",
    },
    {
        "key": "mention_patterns",
        "env": "BLUEBUBBLES_MENTION_PATTERNS",
        "yaml_path": "platforms.bluebubbles.extra.mention_patterns",
        "label": "Group Wake-Word Regex Patterns",
        "section": "behavior",
        "type": "list",
        "default": [],
        "placeholder": "\\bhermes\\b, @bot",
        "description": "Custom wake-word regular expressions when Require Mention is enabled.",
    },
    {
        "key": "send_read_receipts",
        "env": "BLUEBUBBLES_SEND_READ_RECEIPTS",
        "yaml_path": "platforms.bluebubbles.extra.send_read_receipts",
        "label": "Automatically Send Read Receipts",
        "section": "behavior",
        "type": "boolean",
        "default": True,
        "description": "Mark inbound messages as read via the Private API when accepted by the gateway.",
    },
    {
        "key": "webhook_host",
        "env": "BLUEBUBBLES_WEBHOOK_HOST",
        "yaml_path": "platforms.bluebubbles.extra.webhook_host",
        "label": "Webhook Listener Host",
        "section": "webhook",
        "type": "string",
        "default": "127.0.0.1",
        "placeholder": "127.0.0.1",
        "description": "Local bind IP for the standalone webhook listener (when not multiplexed on the gateway API server).",
    },
    {
        "key": "webhook_port",
        "env": "BLUEBUBBLES_WEBHOOK_PORT",
        "yaml_path": "platforms.bluebubbles.extra.webhook_port",
        "label": "Webhook Listener Port",
        "section": "webhook",
        "type": "integer",
        "default": 8645,
        "placeholder": "8645",
        "description": "Local port for the standalone webhook listener.",
    },
    {
        "key": "webhook_path",
        "env": "BLUEBUBBLES_WEBHOOK_PATH",
        "yaml_path": "platforms.bluebubbles.extra.webhook_path",
        "label": "Webhook Listener Path",
        "section": "webhook",
        "type": "string",
        "default": "/bluebubbles-webhook",
        "placeholder": "/bluebubbles-webhook",
        "description": "HTTP path where BlueBubbles posts webhook events.",
    },
]


def resolve_hermes_root(explicit_root: Optional[Path] = None) -> Path:
    """Resolve the top-level ``~/.hermes`` root directory (unwrapping ``/profiles/<name>`` if set)."""
    if explicit_root is not None:
        base = Path(explicit_root).expanduser().resolve(strict=False)
    else:
        env_home = os.environ.get("HERMES_HOME", "").strip()
        if env_home:
            base = Path(env_home).expanduser().resolve(strict=False)
        else:
            try:
                from hermes_constants import get_default_hermes_root
                base = Path(get_default_hermes_root()).expanduser().resolve(strict=False)
            except Exception:
                base = (Path.home() / ".hermes").resolve(strict=False)
    if base.parent.name == "profiles":
        return base.parent.parent
    return base


def resolve_profile_dir(profile: Optional[str] = None, *, root: Optional[Path] = None) -> tuple[str, Path]:
    """Return ``(profile_name, profile_dir)`` for ``default`` or a named profile under ``<root>/profiles``."""
    hermes_root = resolve_hermes_root(root)
    clean = (profile or "default").strip() or "default"
    if clean == "default":
        return "default", hermes_root
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", clean):
        raise ValueError(f"Invalid profile name: {clean!r}")
    return clean, hermes_root / "profiles" / clean


def list_profiles(*, root: Optional[Path] = None) -> list[dict[str, Any]]:
    """List all available Hermes profiles and their BlueBubbles status summary."""
    hermes_root = resolve_hermes_root(root)
    candidates: list[tuple[str, Path]] = [("default", hermes_root)]
    profiles_dir = hermes_root / "profiles"
    if profiles_dir.is_dir():
        for child in sorted(profiles_dir.iterdir(), key=lambda p: p.name):
            if child.is_dir() and not child.name.startswith("."):
                candidates.append((child.name, child))

    out: list[dict[str, Any]] = []
    for name, pdir in candidates:
        state = read_profile_settings(name, root=hermes_root)
        eff = state["effective"]
        out.append({
            "name": name,
            "path": str(pdir),
            "platform_enabled": bool(eff.get("platform_enabled")),
            "plugin_enabled": bool(eff.get("plugin_enabled")),
            "plugin_installed": bool(state["meta"]["plugin_installed"]),
            "routing_mode": state["meta"]["routing_mode"],
            "home_channel": eff.get("home_channel") or "",
            "allowed_chat_guids_count": len(eff.get("allowed_chat_guids") or []),
            "allowed_users_count": len(eff.get("allowed_users") or []),
            "has_credentials": bool(eff.get("server_url") and state["meta"]["has_password"]),
        })
    return out


# --- YAML & .env I/O helpers ------------------------------------------------

def _load_yaml_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8-sig")
    if not text.strip():
        return {}
    if _hermes_yaml is not None:
        try:
            y = _hermes_yaml.YAML(typ="safe")
            data = y.load(text)
            return dict(data) if isinstance(data, dict) else {}
        except Exception:
            pass
    if _pyyaml is not None:
        try:
            data = _pyyaml.safe_load(text)
            return dict(data) if isinstance(data, dict) else {}
        except Exception:
            pass
    return {}


def _dump_yaml_to_string(data: dict[str, Any]) -> str:
    if _hermes_yaml is not None:
        try:
            import io
            buf = io.StringIO()
            y = _hermes_yaml.YAML()
            y.default_flow_style = False
            y.dump(data, buf)
            return buf.getvalue()
        except Exception:
            pass
    if _pyyaml is not None:
        return _pyyaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    return json.dumps(data, indent=2)


def _save_yaml_file(path: Path, mutator) -> dict[str, Any]:
    """Load ``path`` (preserving comments if ruamel/hermes_yaml is available), apply ``mutator(doc)``, and write atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if _hermes_yaml is not None and hasattr(_hermes_yaml, "YAML"):
        try:
            import io
            y = _hermes_yaml.YAML()
            y.preserve_quotes = True
            doc = y.load(path.read_text(encoding="utf-8-sig")) if path.is_file() else None
            if not isinstance(doc, dict):
                doc = {}
            mutator(doc)
            buf = io.StringIO()
            y.dump(doc, buf)
            tmp = path.with_suffix(".yaml.tmp")
            tmp.write_text(buf.getvalue(), encoding="utf-8")
            tmp.replace(path)
            return _load_yaml_file(path)
        except Exception:
            pass
    doc = _load_yaml_file(path)
    mutator(doc)
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(_dump_yaml_to_string(doc), encoding="utf-8")
    tmp.replace(path)
    return doc


def _parse_dotenv(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        k, v = line.split("=", 1)
        key = k.strip()
        val = v.strip()
        if len(val) >= 2 and ((val[0] == val[-1] == '"') or (val[0] == val[-1] == "'")):
            val = val[1:-1]
        if key:
            out[key] = val
    return out


def _format_dotenv_value(val: str) -> str:
    if val == "":
        return ""
    if any(ch in val for ch in (' ', '#', '"', "'", ';', '\t')):
        escaped = val.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return val


def _update_dotenv_file(path: Path, updates: dict[str, Optional[str]]) -> None:
    """Update or remove keys in ``.env`` while preserving all unrelated lines and comments."""
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_lines = path.read_text(encoding="utf-8-sig").splitlines() if path.is_file() else []
    remaining = dict(updates)
    new_lines: list[str] = []

    for raw in existing_lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            new_lines.append(raw)
            continue
        check = stripped[7:].strip() if stripped.startswith("export ") else stripped
        key = check.split("=", 1)[0].strip()
        if key in remaining:
            val = remaining.pop(key)
            if val is not None and str(val).strip() != "":
                new_lines.append(f"{key}={_format_dotenv_value(str(val))}")
        else:
            new_lines.append(raw)

    for key, val in remaining.items():
        if val is not None and str(val).strip() != "":
            new_lines.append(f"{key}={_format_dotenv_value(str(val))}")

    content = "\n".join(new_lines).rstrip() + ("\n" if new_lines else "")
    tmp = path.with_suffix(".env.tmp")
    tmp.write_text(content, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)


# --- Coercion helpers -------------------------------------------------------

def _as_bool(val: Any, default: bool = False) -> bool:
    if isinstance(val, bool):
        return val
    if val is None:
        return default
    s = str(val).strip().lower()
    if s in {"1", "true", "yes", "on"}:
        return True
    if s in {"0", "false", "no", "off"}:
        return False
    return default


def _as_list(val: Any) -> list[str]:
    if val is None:
        return []
    if isinstance(val, (list, tuple)):
        return [str(x).strip() for x in val if str(x).strip()]
    s = str(val).strip()
    if not s:
        return []
    if s.startswith("["):
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except Exception:
            pass
    return [part.strip() for part in re.split(r"[,\n]+", s) if part.strip()]


def _as_int(val: Any, default: int) -> int:
    try:
        return int(str(val).strip())
    except (TypeError, ValueError):
        return default


def _compute_routing_mode(allowed_guids: list[str], ignore_dm: bool, ignore_group: bool, home_channel: str) -> str:
    """Summarize the profile's DM/group routing posture."""
    guids = list(allowed_guids)
    if home_channel and (";-;" in home_channel or ";+;" in home_channel):
        guids.append(home_channel)
    has_dm = any(";-;" in g for g in guids)
    has_group = any(";+;" in g for g in guids)

    eff_ignore_dm = ignore_dm or (has_group and not has_dm)
    eff_ignore_group = ignore_group or (has_dm and not has_group)

    if eff_ignore_dm and not eff_ignore_group:
        return "group_only"
    if eff_ignore_group and not eff_ignore_dm:
        return "dm_only"
    if eff_ignore_dm and eff_ignore_group:
        return "none"
    if allowed_guids:
        return "custom_allowlist"
    return "all"


# --- Read & Write Profile Settings ------------------------------------------

def read_profile_settings(
    profile: Optional[str] = None,
    *,
    root: Optional[Path] = None,
    reveal_password: bool = False,
) -> dict[str, Any]:
    """Read and merge ``.env`` and ``config.yaml`` settings for ``profile``."""
    hermes_root = resolve_hermes_root(root)
    prof_name, prof_dir = resolve_profile_dir(profile, root=hermes_root)

    root_env = _parse_dotenv(hermes_root / ".env")
    prof_env = _parse_dotenv(prof_dir / ".env") if prof_dir != hermes_root else dict(root_env)
    merged_env = {**root_env, **prof_env}

    cfg = _load_yaml_file(prof_dir / "config.yaml")
    plugins_sec = cfg.get("plugins") if isinstance(cfg.get("plugins"), dict) else {}
    enabled_plugins = plugins_sec.get("enabled") if isinstance(plugins_sec.get("enabled"), list) else []

    toolsets_sec = cfg.get("platform_toolsets") if isinstance(cfg.get("platform_toolsets"), dict) else {}
    bb_toolsets = toolsets_sec.get("bluebubbles") if isinstance(toolsets_sec.get("bluebubbles"), list) else []

    platforms_sec = cfg.get("platforms") if isinstance(cfg.get("platforms"), dict) else {}
    bb_cfg = platforms_sec.get("bluebubbles") if isinstance(platforms_sec.get("bluebubbles"), dict) else {}
    bb_extra = bb_cfg.get("extra") if isinstance(bb_cfg.get("extra"), dict) else {}
    bb_home = bb_cfg.get("home_channel") if isinstance(bb_cfg.get("home_channel"), dict) else {}
    push_cfg = platforms_sec.get("push") if isinstance(platforms_sec.get("push"), dict) else {}

    def _pick(yaml_val: Any, env_key: Optional[str], default: Any) -> Any:
        if yaml_val is not None and yaml_val != "":
            return yaml_val
        if env_key and env_key in merged_env and merged_env[env_key] != "":
            return merged_env[env_key]
        return default

    raw_password = str(_pick(bb_extra.get("password"), "BLUEBUBBLES_PASSWORD", "") or "")
    has_password = bool(raw_password)

    server_url = str(_pick(bb_extra.get("server_url"), "BLUEBUBBLES_SERVER_URL", "http://127.0.0.1:1234") or "").rstrip("/")
    allowed_chat_guids = _as_list(_pick(bb_extra.get("allowed_chat_guids"), "BLUEBUBBLES_ALLOWED_CHAT_GUIDS", []))
    ignore_dm = _as_bool(_pick(bb_extra.get("ignore_direct_messages"), "BLUEBUBBLES_IGNORE_DIRECT_MESSAGES", False))
    ignore_group = _as_bool(_pick(bb_extra.get("ignore_group_messages"), "BLUEBUBBLES_IGNORE_GROUP_MESSAGES", False))

    yaml_allowed_users = bb_cfg.get("allowed_users") if bb_cfg.get("allowed_users") is not None else bb_extra.get("allowed_users")
    allowed_users = _as_list(_pick(yaml_allowed_users, "BLUEBUBBLES_ALLOWED_USERS", []))
    allow_all_users = _as_bool(_pick(bb_extra.get("allow_all_users"), "BLUEBUBBLES_ALLOW_ALL_USERS", False))

    home_channel = str(_pick(bb_home.get("chat_id"), "BLUEBUBBLES_HOME_CHANNEL", "") or "")
    home_channel_name = str(_pick(bb_home.get("name"), "BLUEBUBBLES_HOME_CHANNEL_NAME", "") or "")

    require_mention = _as_bool(_pick(bb_extra.get("require_mention"), "BLUEBUBBLES_REQUIRE_MENTION", False))
    mention_patterns = _as_list(_pick(bb_extra.get("mention_patterns"), "BLUEBUBBLES_MENTION_PATTERNS", []))
    send_read_receipts = _as_bool(_pick(bb_extra.get("send_read_receipts"), "BLUEBUBBLES_SEND_READ_RECEIPTS", True), True)

    webhook_host = str(_pick(bb_extra.get("webhook_host"), "BLUEBUBBLES_WEBHOOK_HOST", "127.0.0.1") or "127.0.0.1")
    webhook_port = _as_int(_pick(bb_extra.get("webhook_port"), "BLUEBUBBLES_WEBHOOK_PORT", 8645), 8645)
    webhook_path = str(_pick(bb_extra.get("webhook_path"), "BLUEBUBBLES_WEBHOOK_PATH", "/bluebubbles-webhook") or "/bluebubbles-webhook")

    platform_enabled = _as_bool(bb_cfg.get("enabled"), default=bool(server_url and has_password))
    plugin_enabled = any(str(x).strip() in {"platforms/bluebubbles", "bluebubbles-platform"} for x in enabled_plugins)
    toolsets_enabled = "bluebubbles" in [str(x).strip() for x in bb_toolsets]
    suppress_restart_push = push_cfg.get("gateway_restart_notification") is False

    effective = {
        "server_url": server_url,
        "password": raw_password if reveal_password else (PASSWORD_MASK if has_password else ""),
        "platform_enabled": platform_enabled,
        "plugin_enabled": plugin_enabled,
        "toolsets_enabled": toolsets_enabled,
        "allowed_chat_guids": allowed_chat_guids,
        "ignore_direct_messages": ignore_dm,
        "ignore_group_messages": ignore_group,
        "allowed_users": allowed_users,
        "allow_all_users": allow_all_users,
        "home_channel": home_channel,
        "home_channel_name": home_channel_name,
        "suppress_restart_push": suppress_restart_push,
        "require_mention": require_mention,
        "mention_patterns": mention_patterns,
        "send_read_receipts": send_read_receipts,
        "webhook_host": webhook_host,
        "webhook_port": webhook_port,
        "webhook_path": webhook_path,
    }

    plugin_dir = prof_dir / "plugins" / "platforms" / "bluebubbles"
    routing_mode = _compute_routing_mode(allowed_chat_guids, ignore_dm, ignore_group, home_channel)

    preview_yaml = {
        "plugins": {"enabled": enabled_plugins},
        "platform_toolsets": {"bluebubbles": bb_toolsets},
        "platforms": {
            **({"push": push_cfg} if push_cfg else {}),
            "bluebubbles": bb_cfg,
        },
    }
    env_preview_lines = []
    for item in SETTINGS_SCHEMA:
        env_name = item.get("env")
        if not env_name:
            continue
        if env_name in prof_env:
            val = PASSWORD_MASK if (env_name == "BLUEBUBBLES_PASSWORD" and not reveal_password) else prof_env[env_name]
            env_preview_lines.append(f"{env_name}={_format_dotenv_value(val)}")

    return {
        "profile": prof_name,
        "profile_dir": str(prof_dir),
        "effective": effective,
        "meta": {
            "has_password": has_password,
            "password_in_profile_env": "BLUEBUBBLES_PASSWORD" in prof_env,
            "password_in_root_env": "BLUEBUBBLES_PASSWORD" in root_env,
            "plugin_installed": (plugin_dir / "plugin.yaml").is_file(),
            "plugin_dir": str(plugin_dir),
            "routing_mode": routing_mode,
            "config_yaml_path": str(prof_dir / "config.yaml"),
            "dotenv_path": str(prof_dir / ".env"),
            "yaml_preview": _dump_yaml_to_string(preview_yaml),
            "dotenv_preview": "\n".join(env_preview_lines),
        },
    }


def write_profile_settings(
    profile: Optional[str],
    values: dict[str, Any],
    *,
    root: Optional[Path] = None,
    sync_dotenv: bool = True,
) -> dict[str, Any]:
    """Persist updated BlueBubbles settings to ``config.yaml`` and ``.env`` for ``profile``."""
    hermes_root = resolve_hermes_root(root)
    prof_name, prof_dir = resolve_profile_dir(profile, root=hermes_root)
    current = read_profile_settings(prof_name, root=hermes_root, reveal_password=True)["effective"]

    merged = dict(current)
    for k, v in (values or {}).items():
        if k == "password":
            if v is not None and str(v) != PASSWORD_MASK and str(v) != "":
                merged["password"] = str(v)
            elif v == "" and values.get("clear_password"):
                merged["password"] = ""
        elif k in merged:
            merged[k] = v

    server_url = str(merged.get("server_url") or "http://127.0.0.1:1234").strip().rstrip("/")
    password = str(merged.get("password") or "")
    platform_enabled = _as_bool(merged.get("platform_enabled"), True)
    plugin_enabled = _as_bool(merged.get("plugin_enabled"), True)
    toolsets_enabled = _as_bool(merged.get("toolsets_enabled"), True)

    allowed_chat_guids = _as_list(merged.get("allowed_chat_guids"))
    ignore_dm = _as_bool(merged.get("ignore_direct_messages"), False)
    ignore_group = _as_bool(merged.get("ignore_group_messages"), False)

    allowed_users = _as_list(merged.get("allowed_users"))
    allow_all_users = _as_bool(merged.get("allow_all_users"), False)

    home_channel = str(merged.get("home_channel") or "").strip()
    home_channel_name = str(merged.get("home_channel_name") or "").strip()
    suppress_restart_push = _as_bool(merged.get("suppress_restart_push"), False)

    require_mention = _as_bool(merged.get("require_mention"), False)
    mention_patterns = _as_list(merged.get("mention_patterns"))
    send_read_receipts = _as_bool(merged.get("send_read_receipts"), True)

    webhook_host = str(merged.get("webhook_host") or "127.0.0.1").strip() or "127.0.0.1"
    webhook_port = _as_int(merged.get("webhook_port"), 8645)
    webhook_path = str(merged.get("webhook_path") or "/bluebubbles-webhook").strip() or "/bluebubbles-webhook"
    if not webhook_path.startswith("/"):
        webhook_path = "/" + webhook_path

    def _mutate_yaml(doc: dict[str, Any]) -> None:
        # 1. plugins.enabled
        plugins_sec = doc.setdefault("plugins", {})
        if not isinstance(plugins_sec, dict):
            plugins_sec = {}
            doc["plugins"] = plugins_sec
        enabled_list = plugins_sec.get("enabled")
        if not isinstance(enabled_list, list):
            enabled_list = []
            plugins_sec["enabled"] = enabled_list
        has_bb_plugin = any(str(x).strip() in {"platforms/bluebubbles", "bluebubbles-platform"} for x in enabled_list)
        if plugin_enabled and not has_bb_plugin:
            enabled_list.append("platforms/bluebubbles")
        elif not plugin_enabled and has_bb_plugin:
            plugins_sec["enabled"] = [
                x for x in enabled_list if str(x).strip() not in {"platforms/bluebubbles", "bluebubbles-platform"}
            ]

        # 2. platform_toolsets.bluebubbles
        toolsets_sec = doc.setdefault("platform_toolsets", {})
        if not isinstance(toolsets_sec, dict):
            toolsets_sec = {}
            doc["platform_toolsets"] = toolsets_sec
        if toolsets_enabled:
            cur_ts = toolsets_sec.get("bluebubbles")
            if not isinstance(cur_ts, list):
                cur_ts = []
            for req_ts in ("hermes-bluebubbles", "bluebubbles"):
                if req_ts not in cur_ts:
                    cur_ts.append(req_ts)
            toolsets_sec["bluebubbles"] = cur_ts
        else:
            if isinstance(toolsets_sec.get("bluebubbles"), list):
                toolsets_sec["bluebubbles"] = [x for x in toolsets_sec["bluebubbles"] if x != "bluebubbles"]

        # 3. platforms.push.gateway_restart_notification
        platforms_sec = doc.setdefault("platforms", {})
        if not isinstance(platforms_sec, dict):
            platforms_sec = {}
            doc["platforms"] = platforms_sec
        if suppress_restart_push:
            push_sec = platforms_sec.setdefault("push", {})
            if not isinstance(push_sec, dict):
                push_sec = {}
                platforms_sec["push"] = push_sec
            push_sec["gateway_restart_notification"] = False
        elif isinstance(platforms_sec.get("push"), dict):
            platforms_sec["push"].pop("gateway_restart_notification", None)
            if not platforms_sec["push"]:
                platforms_sec.pop("push", None)

        # 4. platforms.bluebubbles
        bb_sec = platforms_sec.setdefault("bluebubbles", {})
        if not isinstance(bb_sec, dict):
            bb_sec = {}
            platforms_sec["bluebubbles"] = bb_sec
        bb_sec["enabled"] = platform_enabled

        if allowed_users:
            bb_sec["allowed_users"] = allowed_users
        else:
            bb_sec.pop("allowed_users", None)

        if home_channel:
            hc: dict[str, Any] = {"platform": "bluebubbles", "chat_id": home_channel}
            if home_channel_name:
                hc["name"] = home_channel_name
            bb_sec["home_channel"] = hc
        else:
            bb_sec.pop("home_channel", None)

        extra = bb_sec.setdefault("extra", {})
        if not isinstance(extra, dict):
            extra = {}
            bb_sec["extra"] = extra

        extra["require_mention"] = require_mention
        if mention_patterns:
            extra["mention_patterns"] = mention_patterns
        else:
            extra.pop("mention_patterns", None)

        extra["send_read_receipts"] = send_read_receipts

        if allowed_chat_guids:
            extra["allowed_chat_guids"] = allowed_chat_guids
        else:
            extra.pop("allowed_chat_guids", None)

        if ignore_dm:
            extra["ignore_direct_messages"] = True
        else:
            extra.pop("ignore_direct_messages", None)

        if ignore_group:
            extra["ignore_group_messages"] = True
        else:
            extra.pop("ignore_group_messages", None)

        if allow_all_users:
            extra["allow_all_users"] = True
        else:
            extra.pop("allow_all_users", None)

        if webhook_host != "127.0.0.1":
            extra["webhook_host"] = webhook_host
        else:
            extra.pop("webhook_host", None)

        if webhook_port != 8645:
            extra["webhook_port"] = webhook_port
        else:
            extra.pop("webhook_port", None)

        if webhook_path != "/bluebubbles-webhook":
            extra["webhook_path"] = webhook_path
        else:
            extra.pop("webhook_path", None)

    _save_yaml_file(prof_dir / "config.yaml", _mutate_yaml)

    if sync_dotenv:
        env_updates: dict[str, Optional[str]] = {
            "BLUEBUBBLES_SERVER_URL": server_url,
            "BLUEBUBBLES_ALLOWED_USERS": ",".join(allowed_users) if allowed_users else None,
            "BLUEBUBBLES_ALLOW_ALL_USERS": "true" if allow_all_users else None,
            "BLUEBUBBLES_HOME_CHANNEL": home_channel or None,
            "BLUEBUBBLES_HOME_CHANNEL_NAME": home_channel_name or None,
            "BLUEBUBBLES_ALLOWED_CHAT_GUIDS": ",".join(allowed_chat_guids) if allowed_chat_guids else None,
            "BLUEBUBBLES_IGNORE_DIRECT_MESSAGES": "true" if ignore_dm else None,
            "BLUEBUBBLES_IGNORE_GROUP_MESSAGES": "true" if ignore_group else None,
            "BLUEBUBBLES_REQUIRE_MENTION": "true" if require_mention else "false",
            "BLUEBUBBLES_MENTION_PATTERNS": json.dumps(mention_patterns) if mention_patterns else None,
            "BLUEBUBBLES_SEND_READ_RECEIPTS": "true" if send_read_receipts else "false",
            "BLUEBUBBLES_WEBHOOK_HOST": webhook_host if webhook_host != "127.0.0.1" else None,
            "BLUEBUBBLES_WEBHOOK_PORT": str(webhook_port) if webhook_port != 8645 else None,
            "BLUEBUBBLES_WEBHOOK_PATH": webhook_path if webhook_path != "/bluebubbles-webhook" else None,
        }
        if password:
            env_updates["BLUEBUBBLES_PASSWORD"] = password
        elif values.get("clear_password"):
            env_updates["BLUEBUBBLES_PASSWORD"] = None
        _update_dotenv_file(prof_dir / ".env", env_updates)

    return read_profile_settings(prof_name, root=hermes_root)


# --- Live BlueBubbles Server HTTP helpers -----------------------------------

def _bb_request(
    server_url: str,
    password: str,
    path: str,
    *,
    method: str = "GET",
    payload: Optional[dict[str, Any]] = None,
    timeout: float = 8.0,
) -> dict[str, Any]:
    base = server_url.rstrip("/")
    sep = "&" if "?" in path else "?"
    url = f"{base}{path}{sep}{urlencode({'password': password})}"
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = UrlRequest(url, data=data, headers=headers, method=method)
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw.strip() else {}


def _normalize_handle_key(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    if "@" in s:
        return s.lower()
    digits = re.sub(r"\D+", "", s)
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return f"+{digits}" if digits else s.lower()


def _build_contact_lookup(contacts_data: list[dict[str, Any]]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for c in contacts_data or []:
        if not isinstance(c, dict):
            continue
        disp = (c.get("displayName") or "").strip()
        if not disp:
            first = (c.get("firstName") or "").strip()
            last = (c.get("lastName") or "").strip()
            disp = f"{first} {last}".strip() or (c.get("nickname") or "").strip()
        if not disp:
            continue
        for p in c.get("phoneNumbers") or []:
            addr = (p.get("address") if isinstance(p, dict) else str(p)) or ""
            key = _normalize_handle_key(addr)
            if key:
                mapping[key] = disp
        for em in c.get("emails") or []:
            addr = (em.get("address") if isinstance(em, dict) else str(em)) or ""
            key = _normalize_handle_key(addr)
            if key:
                mapping[key] = disp
    return mapping


def probe_bluebubbles_server(profile: Optional[str] = None, *, root: Optional[Path] = None) -> dict[str, Any]:
    """Probe the BlueBubbles Server configured for ``profile`` and return health & Private API details."""
    state = read_profile_settings(profile, root=root, reveal_password=True)
    eff = state["effective"]
    server_url = eff.get("server_url") or ""
    password = eff.get("password") or ""
    if not server_url or not password:
        return {
            "ok": False,
            "connected": False,
            "error": "BLUEBUBBLES_SERVER_URL or BLUEBUBBLES_PASSWORD is not configured for this profile.",
            "server_url": server_url,
        }
    try:
        info_res = _bb_request(server_url, password, "/api/v1/server/info", timeout=6.0)
        info = info_res.get("data") if isinstance(info_res, dict) else {}
        if not isinstance(info, dict):
            info = {}

        webhooks: list[dict[str, Any]] = []
        try:
            wh_res = _bb_request(server_url, password, "/api/v1/webhook", timeout=5.0)
            raw_wh = wh_res.get("data") if isinstance(wh_res, dict) else []
            if isinstance(raw_wh, list):
                for w in raw_wh:
                    if isinstance(w, dict):
                        url_str = str(w.get("url") or "")
                        # Redact password query parameter in webhook URLs
                        redacted_url = re.sub(r"(password=)[^&]+", r"\1***", url_str)
                        webhooks.append({
                            "id": w.get("id"),
                            "url": redacted_url,
                            "events": [
                                ev.get("value") if isinstance(ev, dict) else str(ev)
                                for ev in (w.get("events") or [])
                            ],
                        })
        except Exception:
            pass

        findmy_friends_cached = 0
        try:
            fm_res = _bb_request(server_url, password, "/api/v1/icloud/findmy/friends", timeout=5.0)
            fm_data = fm_res.get("data") if isinstance(fm_res, dict) else []
            if isinstance(fm_data, list):
                findmy_friends_cached = len(fm_data)
        except Exception:
            pass

        return {
            "ok": True,
            "connected": True,
            "server_url": server_url,
            "server_version": info.get("server_version") or "",
            "os_version": info.get("os_version") or "",
            "private_api": bool(info.get("private_api")),
            "helper_connected": bool(info.get("helper_connected")),
            "proxy_service": info.get("proxy_service") or "",
            "detected_icloud": info.get("detected_icloud") or "",
            "computer_id": info.get("computer_id") or "",
            "webhooks": webhooks,
            "findmy_friends_cached": findmy_friends_cached,
        }
    except Exception as exc:
        return {
            "ok": False,
            "connected": False,
            "server_url": server_url,
            "error": str(exc) or type(exc).__name__,
        }


def fetch_live_chats(
    profile: Optional[str] = None,
    *,
    root: Optional[Path] = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Fetch recent BlueBubbles chats enriched with contact names and DM/Group classification."""
    state = read_profile_settings(profile, root=root, reveal_password=True)
    eff = state["effective"]
    server_url = eff.get("server_url") or ""
    password = eff.get("password") or ""
    if not server_url or not password:
        return {"ok": False, "error": "Server URL or password not configured", "chats": []}

    try:
        contact_map: dict[str, str] = {}
        try:
            c_res = _bb_request(server_url, password, "/api/v1/contact", timeout=8.0)
            if isinstance(c_res.get("data"), list):
                contact_map = _build_contact_lookup(c_res["data"])
        except Exception:
            pass

        q_res = _bb_request(
            server_url,
            password,
            "/api/v1/chat/query",
            method="POST",
            payload={
                "limit": max(1, min(int(limit), 250)),
                "offset": 0,
                "with": ["participants", "lastMessage"],
                "sort": "lastmessage",
            },
            timeout=10.0,
        )
        raw_chats = q_res.get("data") if isinstance(q_res, dict) else []
        if not isinstance(raw_chats, list):
            raw_chats = []

        chats: list[dict[str, Any]] = []
        for ch in raw_chats:
            if not isinstance(ch, dict):
                continue
            guid = str(ch.get("guid") or "").strip()
            if not guid:
                continue
            chat_identifier = str(ch.get("chatIdentifier") or "").strip()
            is_group = ";+;" in guid or ch.get("style") == 43
            canonical_guid = f"any;+;{chat_identifier}" if (is_group and chat_identifier) else (
                f"any;-;{chat_identifier}" if chat_identifier else guid
            )

            participants_raw = ch.get("participants") if isinstance(ch.get("participants"), list) else []
            participants: list[dict[str, str]] = []
            for p in participants_raw:
                if not isinstance(p, dict):
                    continue
                addr = str(p.get("address") or "").strip()
                if not addr:
                    continue
                name = contact_map.get(_normalize_handle_key(addr), "")
                participants.append({"address": addr, "name": name})

            display_name = str(ch.get("displayName") or "").strip()
            if not display_name:
                if not is_group and participants:
                    p0 = participants[0]
                    display_name = p0["name"] or p0["address"]
                elif is_group and participants:
                    names = [p["name"] or p["address"] for p in participants[:4]]
                    display_name = ", ".join(names) + (f" (+{len(participants) - 4})" if len(participants) > 4 else "")
                else:
                    display_name = chat_identifier or guid

            last_msg = ch.get("lastMessage") if isinstance(ch.get("lastMessage"), dict) else {}
            last_text = str(last_msg.get("text") or "").strip()
            last_date = last_msg.get("dateCreated") or 0

            chats.append({
                "guid": guid,
                "canonical_guid": canonical_guid,
                "chat_identifier": chat_identifier,
                "display_name": display_name,
                "kind": "group" if is_group else "dm",
                "delimiter": ";+;" if is_group else ";-;",
                "participants": participants,
                "last_message_text": last_text[:120],
                "last_message_date": last_date,
            })

        return {"ok": True, "chats": chats}
    except Exception as exc:
        return {"ok": False, "error": str(exc) or type(exc).__name__, "chats": []}


def fetch_live_contacts(profile: Optional[str] = None, *, root: Optional[Path] = None) -> dict[str, Any]:
    """Fetch contacts from BlueBubbles ``/api/v1/contact`` for the sender allowlist picker."""
    state = read_profile_settings(profile, root=root, reveal_password=True)
    eff = state["effective"]
    server_url = eff.get("server_url") or ""
    password = eff.get("password") or ""
    if not server_url or not password:
        return {"ok": False, "error": "Server URL or password not configured", "contacts": []}

    try:
        c_res = _bb_request(server_url, password, "/api/v1/contact", timeout=8.0)
        raw_contacts = c_res.get("data") if isinstance(c_res, dict) else []
        if not isinstance(raw_contacts, list):
            raw_contacts = []

        contacts: list[dict[str, Any]] = []
        seen: set[str] = set()
        for c in raw_contacts:
            if not isinstance(c, dict):
                continue
            disp = (c.get("displayName") or "").strip()
            if not disp:
                first = (c.get("firstName") or "").strip()
                last = (c.get("lastName") or "").strip()
                disp = f"{first} {last}".strip() or (c.get("nickname") or "").strip()
            if not disp:
                continue

            phones: list[str] = []
            for p in c.get("phoneNumbers") or []:
                addr = (p.get("address") if isinstance(p, dict) else str(p)) or ""
                norm = _normalize_handle_key(addr)
                if norm and norm not in phones:
                    phones.append(norm)

            emails: list[str] = []
            for em in c.get("emails") or []:
                addr = (em.get("address") if isinstance(em, dict) else str(em)) or ""
                norm = _normalize_handle_key(addr)
                if norm and norm not in emails:
                    emails.append(norm)

            if not phones and not emails:
                continue
            dedup_key = f"{disp.lower()}|{'|'.join(phones)}|{'|'.join(emails)}"
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            contacts.append({
                "display_name": disp,
                "phones": phones,
                "emails": emails,
                "handles": phones + emails,
            })

        contacts.sort(key=lambda item: item["display_name"].lower())
        return {"ok": True, "contacts": contacts}
    except Exception as exc:
        return {"ok": False, "error": str(exc) or type(exc).__name__, "contacts": []}


# --- Plugin Deployment Sync & Gateway Restart -------------------------------

def find_connector_plugin_source(*, root: Optional[Path] = None) -> Optional[Path]:
    """Locate the canonical ``bluebubbles-connector/plugin`` source directory."""
    here = Path(__file__).resolve().parent
    repo_candidate = here.parent / "plugin"
    if (repo_candidate / "plugin.yaml").is_file() and (repo_candidate / "adapter.py").is_file():
        return repo_candidate

    hermes_root = resolve_hermes_root(root)
    candidates = [
        hermes_root / "profiles" / "family" / "bluebubbles-connector" / "plugin",
        hermes_root / "bluebubbles-connector" / "plugin",
        Path.home() / "Documents" / "ChatGPT" / "bluebubbles-connector" / "plugin",
    ]
    for cand in candidates:
        if (cand / "plugin.yaml").is_file() and (cand / "adapter.py").is_file():
            return cand
    return None


def sync_connector_plugin(
    target_profiles: Optional[list[str]] = None,
    *,
    root: Optional[Path] = None,
    source_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """Copy ``bluebubbles-connector/plugin`` into each target profile's ``plugins/platforms/bluebubbles``."""
    hermes_root = resolve_hermes_root(root)
    src = Path(source_dir) if source_dir else find_connector_plugin_source(root=hermes_root)
    if src is None or not src.is_dir():
        return {"ok": False, "error": "Could not locate bluebubbles-connector/plugin source directory."}

    if not target_profiles:
        target_profiles = [
            p["name"] for p in list_profiles(root=hermes_root)
            if p["name"] != "default" or p["plugin_installed"]
        ]
        if not target_profiles:
            target_profiles = ["default"]

    synced: list[dict[str, str]] = []
    for prof in target_profiles:
        prof_name, prof_dir = resolve_profile_dir(prof, root=hermes_root)
        dest = prof_dir / "plugins" / "platforms" / "bluebubbles"
        if src.resolve() == dest.resolve():
            synced.append({"profile": prof_name, "dest": str(dest), "status": "same_path"})
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
        synced.append({"profile": prof_name, "dest": str(dest), "status": "synced"})

    return {"ok": True, "source": str(src), "synced": synced}


def restart_gateway() -> dict[str, Any]:
    """Restart the Hermes gateway service via launchctl (macOS) or hermes CLI."""
    uid = os.getuid() if hasattr(os, "getuid") else 501
    if sys.platform == "darwin":
        try:
            proc = subprocess.run(
                ["launchctl", "kickstart", "-k", f"gui/{uid}/ai.hermes.gateway"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if proc.returncode == 0:
                return {
                    "ok": True,
                    "method": f"launchctl kickstart -k gui/{uid}/ai.hermes.gateway",
                    "output": (proc.stdout + proc.stderr).strip(),
                }
        except Exception as exc:
            last_err = str(exc)
        else:
            last_err = (proc.stdout + proc.stderr).strip()
    else:
        last_err = "non-darwin platform"

    try:
        hermes_bin = shutil.which("hermes") or str(Path.home() / ".hermes" / "hermes-agent" / "bin" / "hermes")
        proc = subprocess.run(
            [hermes_bin, "-p", "default", "gateway", "restart"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if proc.returncode == 0:
            return {
                "ok": True,
                "method": "hermes -p default gateway restart",
                "output": (proc.stdout + proc.stderr).strip(),
            }
        return {"ok": False, "error": (proc.stdout + proc.stderr).strip() or last_err}
    except Exception as exc:
        return {"ok": False, "error": f"{last_err}; fallback failed: {exc}"}
