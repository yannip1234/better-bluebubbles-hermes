"""BlueBubbles iMessage platform adapter: local BlueBubbles macOS server for outbound REST sends and
inbound webhooks (text, media attachments, typing indicators, read receipts)."""

import asyncio
import json
import logging
import os
import re
import uuid
from collections import OrderedDict
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

from gateway.config import Platform, PlatformConfig
from gateway.platforms._shared import extra_or_secret as _extra_or_secret, get_scoped_secret as _get_scoped_secret
from gateway.platforms._shared import config_connected_extra
from gateway.platforms.base import (
    BasePlatformAdapter, SendResult,
    cache_image_from_bytes_async, cache_audio_from_bytes_async, cache_document_from_bytes_async,
)
from gateway.platforms.media_cache import ext_for_mime
from gateway.platforms.helpers import MessageDeduplicator, compile_mention_patterns, strip_markdown
from utils import TRUTHY_STRINGS
from .inbound import (
    BlueBubblesInboundMixin,
    _DM_DELIMITER,
    _GROUP_DELIMITER,
    _InboundMessage,
    _canonical_chat_guid_key,
    _chat_guid_delimiter,
    _is_dm_chat_guid,
    _is_group_chat_guid,
    _split_chat_guid,
)

# Historical BlueBubbles mime→ext maps, preserved verbatim as overrides for the shared dispatch in
# gateway.platforms.media_cache. Both maps are CLOSED: unlisted mimes fall back to .jpg / .mp3.
_BLUEBUBBLES_IMAGE_EXT_OVERRIDES = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp",
    "image/heic": ".jpg", "image/heif": ".jpg", "image/tiff": ".jpg",  # historical mapping
}
_BLUEBUBBLES_AUDIO_EXT_OVERRIDES = {
    "audio/mp3": ".mp3", "audio/mpeg": ".mp3", "audio/ogg": ".ogg", "audio/wav": ".wav",
    "audio/x-caf": ".mp3", "audio/mp4": ".m4a",
    "audio/aac": ".m4a",  # historical mapping (shared table says .aac)
}

logger = logging.getLogger(__name__)

DEFAULT_WEBHOOK_HOST = "127.0.0.1"
# Webhook events are small JSON/form payloads (attachments come through the REST API); 1 MiB keeps
# oversized/chunked bodies from buffering unbounded.
_WEBHOOK_MAX_BODY_BYTES = 1_048_576
DEFAULT_WEBHOOK_PORT = 8645
DEFAULT_WEBHOOK_PATH = "/bluebubbles-webhook"
MAX_TEXT_LENGTH = 4000

# iMessage has no stable bot mention identity (unlike <@U...>/@botname/MXID), so
# `require_mention: true` without custom aliases uses Hermes wake words.
DEFAULT_MENTION_PATTERNS = [r"(?<![\w@])@?hermes\s+agent\b[,:\-]?", r"(?<![\w@])@?hermes\b[,:\-]?"]

_WEBHOOK_EVENTS = ("new-message", "updated-message")

_PHONE_RE = re.compile(r"\+?\d{7,15}")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_PAGINATION_SUFFIX_RE = re.compile(r"\s*\(\d+/\d+\)$")
_ADDRESS_RE = re.compile(r"^\+\d+")

_GUID_CACHE_SIZE = 500  # LRU cap for resolved chat-GUID lookups
_LOCAL_HOSTS = {"0.0.0.0", "127.0.0.1", "localhost", "::"}


def _redact(text: str) -> str:
    """Redact phone numbers and emails from log output."""
    return _EMAIL_RE.sub("[REDACTED]", _PHONE_RE.sub("[REDACTED]", text))


def check_bluebubbles_requirements() -> bool:
    try:
        import aiohttp
    except ImportError:
        return False
    return True


def _normalize_server_url(raw: str) -> str:
    value = (raw or "").strip()
    if value and not re.match(r"^https?://", value, flags=re.IGNORECASE):
        value = f"http://{value}"
    return value.rstrip("/")


def _closed_ext(mime: str, overrides: dict[str, str], fallback: str) -> str:
    """Historical maps were closed: unlisted mimes fall back without consulting mimetypes."""
    return ext_for_mime(mime, overrides=overrides, use_defaults=False, use_mimetypes=False,
                        fallback=fallback) or fallback


def _temp_guid() -> str:
    return f"temp-{datetime.utcnow().timestamp()}"


def _ok():
    """Plain ``ok`` acknowledgement for webhook events we accept but don't process."""
    from aiohttp import web
    return web.Response(text="ok")


def _parse_optional_bool(raw: Any) -> Optional[bool]:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if not text:
        return None
    return text in TRUTHY_STRINGS


def _parse_chat_guid_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        return [str(item).strip() for item in raw if str(item).strip()]
    text = str(raw).strip()
    if not text:
        return []
    if text.startswith("["):
        with suppress(ValueError, TypeError):
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
    return [part.strip() for part in re.split(r"[,\n]", text) if part.strip()]


class BlueBubblesAdapter(BlueBubblesInboundMixin, BasePlatformAdapter):
    # Answers /p/<profile>/... on the default listener for a served secondary (shared_ingress).
    serves_profile_prefix: bool = True
    platform = Platform.BLUEBUBBLES
    SUPPORTS_MESSAGE_EDITING = False
    MAX_MESSAGE_LENGTH = MAX_TEXT_LENGTH
    splits_long_messages = True  # send() chunks via truncate_message(MAX_MESSAGE_LENGTH)

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.BLUEBUBBLES)
        extra = config.extra or {}
        self.server_url = _normalize_server_url(_extra_or_secret(extra, "server_url", "BLUEBUBBLES_SERVER_URL"))
        self.password = extra.get("password") or _get_scoped_secret("BLUEBUBBLES_PASSWORD", "")
        self.webhook_host = _extra_or_secret(extra, "webhook_host", "BLUEBUBBLES_WEBHOOK_HOST", DEFAULT_WEBHOOK_HOST)
        self.webhook_port = int(_extra_or_secret(extra, "webhook_port", "BLUEBUBBLES_WEBHOOK_PORT", str(DEFAULT_WEBHOOK_PORT)))
        path = str(_extra_or_secret(extra, "webhook_path", "BLUEBUBBLES_WEBHOOK_PATH", DEFAULT_WEBHOOK_PATH))
        self.webhook_path = path if path.startswith("/") else f"/{path}"
        self.send_read_receipts = bool(extra.get("send_read_receipts", True))
        _require_mention = extra.get("require_mention")
        if _require_mention is None:
            _require_mention = _get_scoped_secret("BLUEBUBBLES_REQUIRE_MENTION")
        self.require_mention = str(_require_mention).strip().lower() in TRUTHY_STRINGS
        self._mention_patterns = self._compile_mention_patterns(
            extra["mention_patterns"] if "mention_patterns" in extra else _get_scoped_secret("BLUEBUBBLES_MENTION_PATTERNS"))
        raw_allowed_chats = (
            extra.get("allowed_chat_guids")
            if "allowed_chat_guids" in extra
            else extra.get("allowed_chats")
            if "allowed_chats" in extra
            else _get_scoped_secret("BLUEBUBBLES_ALLOWED_CHAT_GUIDS")
        )
        self.allowed_chat_guids: list[str] = _parse_chat_guid_list(raw_allowed_chats)
        self._allowed_chat_keys: set[str] = set()
        self._allowed_dm_identifiers: set[str] = set()
        self._allowed_group_identifiers: set[str] = set()
        for entry in self.allowed_chat_guids:
            key = _canonical_chat_guid_key(entry)
            _svc, delim, ident = _split_chat_guid(entry)
            if key and ident:
                self._allowed_chat_keys.add(key)
                if delim == _GROUP_DELIMITER:
                    self._allowed_group_identifiers.add(ident.lower())
                elif delim == _DM_DELIMITER:
                    self._allowed_dm_identifiers.add(ident.lower())
            elif ident:
                self._allowed_dm_identifiers.add(ident.lower())
                self._allowed_group_identifiers.add(ident.lower())

        home_obj = getattr(config, "home_channel", None)
        raw_home = (
            getattr(home_obj, "chat_id", None)
            or (home_obj.get("chat_id") if isinstance(home_obj, dict) else None)
            or extra.get("home_channel")
            or _get_scoped_secret("BLUEBUBBLES_HOME_CHANNEL")
        )
        if isinstance(raw_home, dict):
            raw_home = raw_home.get("chat_id")
        self.home_channel_chat_id: Optional[str] = str(raw_home).strip() if raw_home else None

        raw_ignore_dm = (
            extra.get("ignore_direct_messages")
            if "ignore_direct_messages" in extra
            else _get_scoped_secret("BLUEBUBBLES_IGNORE_DIRECT_MESSAGES")
        )
        raw_ignore_group = (
            extra.get("ignore_group_messages")
            if "ignore_group_messages" in extra
            else _get_scoped_secret("BLUEBUBBLES_IGNORE_GROUP_MESSAGES")
        )
        explicit_ignore_dm = _parse_optional_bool(raw_ignore_dm)
        explicit_ignore_group = _parse_optional_bool(raw_ignore_group)

        has_allowed_groups = bool(self._allowed_group_identifiers)
        has_allowed_dms = bool(self._allowed_dm_identifiers)

        if explicit_ignore_dm is not None:
            self.ignore_direct_messages = explicit_ignore_dm
        elif has_allowed_groups and not has_allowed_dms:
            self.ignore_direct_messages = True
        elif not self.allowed_chat_guids and _is_group_chat_guid(self.home_channel_chat_id):
            self.ignore_direct_messages = True
        else:
            self.ignore_direct_messages = False

        if explicit_ignore_group is not None:
            self.ignore_group_messages = explicit_ignore_group
        elif has_allowed_dms and not has_allowed_groups:
            self.ignore_group_messages = True
        elif not self.allowed_chat_guids and _is_dm_chat_guid(self.home_channel_chat_id):
            self.ignore_group_messages = True
        else:
            self.ignore_group_messages = False

        if self._allowed_group_identifiers and isinstance(self.config.extra, dict):
            group_chats = list(_parse_chat_guid_list(self.config.extra.get("group_allowed_chats")))
            for group_id in sorted(self._allowed_group_identifiers):
                for prefix in ("any", "iMessage", "SMS", ""):
                    candidate = f"{prefix}{_GROUP_DELIMITER}{group_id}"
                    if candidate not in group_chats:
                        group_chats.append(candidate)
            self.config.extra["group_allowed_chats"] = group_chats

        self.client: Optional[httpx.AsyncClient] = None
        self._runner = None
        self._private_api_enabled: Optional[bool] = None
        self._helper_connected: bool = False
        self._guid_cache: OrderedDict[str, str] = OrderedDict()
        self._inbound_messages: OrderedDict[str, _InboundMessage] = OrderedDict()
        self._inbound_routing_lock = asyncio.Lock()
        self._inbound_chat_tails = {}
        self._sent_tapbacks: OrderedDict[tuple[str, str], str] = OrderedDict()
        # The runner carries this shared cache across adapter rebuilds. Store only accepted content.
        self._dedup = MessageDeduplicator(max_size=2000, ttl_seconds=48 * 3600)

    async def _dispatch_inline_reply(self, event, *, log_cmd=None):
        # Installed core predates PR 116401's inline admission receipt.
        event._gateway_accepted = True
        return await super()._dispatch_inline_reply(event, log_cmd=log_cmd)

    async def _queue_text_debounce(self, session_key, event):
        await super()._queue_text_debounce(session_key, event)
        event._gateway_accepted = True

    async def _process_message_background(self, event, session_key):
        # Core debounce/queue merges update event.message_id, not source.message_id.
        # Bind the final admitted event anchor before the runner snapshots its context.
        event.source.message_id = event.message_id
        return await super()._process_message_background(event, session_key)

    # --- API helpers ---

    def _api_url(self, path: str) -> str:
        return f"{self.server_url}{path}{'&' if '?' in path else '?'}password={quote(self.password, safe='')}"

    @staticmethod
    def _compile_mention_patterns(raw: Any) -> list[re.Pattern]:
        """Compile group-mention wake words; ``raw`` is a list, a raw env string (JSON list or
        comma/newline-separated), or None (Hermes defaults)."""
        return compile_mention_patterns(raw, log_prefix="bluebubbles", defaults=DEFAULT_MENTION_PATTERNS,
                                        logger_=logger)

    def _message_matches_mention_patterns(self, text: str) -> bool:
        return bool(text) and any(pattern.search(text) for pattern in self._mention_patterns)

    def _clean_mention_text(self, text: str) -> str:
        """Strip a leading wake word only — patterns are regexes, so stripping anywhere later in the
        prompt could delete ordinary words."""
        stripped = (text or "").lstrip()
        for pattern in self._mention_patterns:
            if match := pattern.match(stripped):
                return stripped[match.end():].lstrip(" ,:-") or text
        return text

    async def _api_json(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        """Authenticated request to the BlueBubbles REST API; raises on HTTP errors, returns decoded JSON."""
        assert self.client is not None
        res = await getattr(self.client, method)(self._api_url(path), **kwargs)
        res.raise_for_status()
        return res.json()

    async def _api_get(self, path: str) -> dict[str, Any]:
        return await self._api_json("get", path)

    async def _api_post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._api_json("post", path, json=payload)

    async def _post_message(self, path: str, payload: dict[str, Any]) -> SendResult:
        """POST a message payload and wrap the outcome as a SendResult."""
        try:
            res = await self._api_post(path, payload)
            status = res.get("status")
            if status is not None and (not isinstance(status, int) or not 200 <= status < 300):
                return SendResult(success=False, error=str(res.get("message") or "BlueBubbles request failed"))
            data = res.get("data") or {}
            msg_id = str(data.get("guid") or data.get("messageGuid") or "ok")
            return SendResult(success=True, message_id=msg_id, raw_response=res)
        except Exception as exc:
            return SendResult(success=False, error=str(exc) or type(exc).__name__)

    async def _private_api_chat_call(self, chat_id: str, action: str, method: str) -> bool:
        """Fire a private-API chat action (typing/read); True only if the call was made."""
        if not self._private_api_enabled or not self._helper_connected or not self.client:
            return False
        if not self._is_target_allowed(chat_id):
            return False
        with suppress(Exception):
            if guid := await self._resolve_chat_guid(chat_id):
                if not self._is_chat_allowed(guid, chat_identifier=chat_id, is_group=_is_group_chat_guid(guid)):
                    return False
                url = self._api_url(f"/api/v1/chat/{quote(guid, safe='')}/{action}")
                await getattr(self.client, method)(url, timeout=5)
                return True
        return False

    # --- Lifecycle ---

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not self.server_url or not self.password:
            logger.error("[bluebubbles] BLUEBUBBLES_SERVER_URL and BLUEBUBBLES_PASSWORD are required")
            return False
        from aiohttp import web
        # Tighter keepalive so idle CLOSE_WAIT drains promptly.
        # See #18451.
        from gateway.platforms._http_client_limits import platform_httpx_limits
        self.client = httpx.AsyncClient(timeout=30.0, limits=platform_httpx_limits())
        try:
            await self._api_get("/api/v1/ping")
            info = await self._api_get("/api/v1/server/info")
            server_data = (info or {}).get("data", {})
            self._private_api_enabled = bool(server_data.get("private_api"))
            self._helper_connected = bool(server_data.get("helper_connected"))
            logger.info("[bluebubbles] connected to %s (private_api=%s, helper=%s)",
                        self.server_url, self._private_api_enabled, self._helper_connected)
        except Exception as exc:
            logger.error("[bluebubbles] cannot reach server at %s: %s", self.server_url, exc)
            await self._close_client()
            return False
        # client_max_size makes aiohttp enforce the cap on every read path, incl. chunked requests
        # with no Content-Length.
        # Explicit body cap: BlueBubbles webhook events are small JSON (or form-encoded) payloads.
        # client_max_size makes aiohttp enforce the cap on every read path — including chunked requests that
        # carry no Content-Length (same pattern as webhook.py / raft, #58536/#58902).
        app = web.Application(client_max_size=_WEBHOOK_MAX_BODY_BYTES)
        app.router.add_get("/health", lambda _: web.Response(text="ok"))
        app.router.add_post(self.webhook_path, self._handle_webhook)
        # The webhook auth value rides in the query string (BlueBubbles cannot send custom headers)
        # — keep it out of aiohttp access logs.
        # Shared-listener mode (multiplex secondary): no bind; served at /p/<profile>/<webhook_path>.
        from gateway.platforms.shared_ingress import bind_listener
        self._runner = await bind_listener(
            self, app, self.webhook_host, self.webhook_port, self.webhook_path, access_log=None)
        self._mark_connected()
        if self._runner is not None:
            logger.info("[bluebubbles] webhook listening on http://%s:%s%s", self.webhook_host, self.webhook_port,
                        self.webhook_path)
        # The server only sends events to webhooks registered through its API. Do not report a
        # connected adapter with a live listener but no verified desired registration.
        if not await self._register_webhook():
            logger.error("[bluebubbles] webhook registration failed; adapter will retry connection")
            if self._runner:
                await self._runner.cleanup()
                self._runner = None
            await self._close_client()
            self._mark_disconnected()
            return False
        # Plugin-registered native handlers (ctx.register_platform_handler).
        self._wire_plugin_handlers(None)
        return True

    async def _close_client(self) -> None:
        if self.client:
            await self.client.aclose()
            self.client = None

    async def disconnect(self) -> None:
        retries = [state.retry_task for state in self._inbound_messages.values()
                   if state.retry_task is not None]
        for task in retries:
            task.cancel()
        if retries:
            await asyncio.gather(*retries, return_exceptions=True)
        await self._unregister_webhook()
        await self._close_client()
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self._mark_disconnected()

    @property
    def _webhook_url(self) -> str:
        """External webhook URL for BlueBubbles registration (local binds → localhost). In
        shared-listener mode it is the default listener's ``/p/<profile>/`` URL."""
        shared = getattr(self, "_shared_ingress_url", None)
        if shared:
            return shared
        host = "localhost" if self.webhook_host in _LOCAL_HOSTS else self.webhook_host
        return f"http://{host}:{self.webhook_port}{self.webhook_path}"

    def _webhook_register_url_with(self, password_param: str) -> str:
        return f"{self._webhook_url}?password={password_param}" if self.password else self._webhook_url

    @property
    def _webhook_register_url(self) -> str:
        """Registered webhook URL with the password as a query param: BlueBubbles posts to the exact
        registered URL and cannot set custom headers, so this is the only way to authenticate inbound
        webhooks without disabling auth."""
        return self._webhook_register_url_with(quote(self.password, safe=""))

    @property
    def _webhook_register_url_for_log(self) -> str:
        return self._webhook_register_url_with("***")

    async def _find_registered_webhooks(self, url: str) -> list:
        """Return every BlueBubbles webhook entry matching *url*.

        Listing failures must propagate: treating an unavailable API as an empty list creates duplicate
        registrations on reconnect.
        """
        data = (await self._api_get("/api/v1/webhook")).get("data")
        if not isinstance(data, list):
            raise ValueError("BlueBubbles webhook list response did not contain a list")
        return [wh for wh in data if isinstance(wh, dict) and wh.get("url") == url]

    async def _register_webhook(self) -> bool:
        """Ensure the same-URL registration has the desired inbound event set.

        BlueBubbles ``addWebhook`` is idempotent by URL and does not update an existing row. A stale
        same-URL registration therefore has to be removed before its replacement is created. If creation
        fails, restore the previous event set best-effort rather than leaving the server with no webhook.
        """
        if not self.client:
            return False
        webhook_url, log_url = self._webhook_register_url, self._webhook_register_url_for_log
        desired = set(_WEBHOOK_EVENTS)
        existing: list[dict] = []
        try:
            existing = await self._find_registered_webhooks(webhook_url)
            exact = [wh for wh in existing if set(wh.get("events") or []) == desired and wh.get("id")]
            if exact:
                keeper = exact[0]
                for wh in existing:
                    if wh is not keeper and wh.get("id"):
                        await self._delete_webhook_id(wh["id"])
                logger.info("[bluebubbles] webhook already registered: %s", log_url)
                return True

            # The URL is unique in BlueBubbles. POST-before-delete merely returns the stale row unchanged.
            for wh in existing:
                if wh.get("id"):
                    await self._delete_webhook_id(wh["id"])

            res = await self._api_post(
                "/api/v1/webhook", {"url": webhook_url, "events": list(_WEBHOOK_EVENTS)})
            status = res.get("status", 0)
            data = res.get("data") or {}
            if not 200 <= status < 300 or not isinstance(data, dict) or set(data.get("events") or []) != desired:
                raise RuntimeError(f"webhook registration returned unverified status {status}")
            verified = await self._find_registered_webhooks(webhook_url)
            if not any(set(wh.get("events") or []) == desired and wh.get("id") for wh in verified):
                raise RuntimeError("webhook replacement was not visible after creation")

            logger.info("[bluebubbles] webhook registered with server: %s", log_url)
            return True
        except Exception as exc:
            # If replacement failed after deleting a stale same-URL row, restore its old event set.
            if existing:
                old_events = existing[0].get("events") or []
                with suppress(Exception):
                    await self._api_post(
                        "/api/v1/webhook", {"url": webhook_url, "events": list(old_events)})
            logger.warning("[bluebubbles] failed to reconcile webhook registration: %s", exc)
            return False

    async def _unregister_webhook(self) -> bool:
        """Remove *all* registrations matching our URL (cleans up crash duplicates)."""
        if not self.client:
            return False
        removed = False
        try:
            for wh in await self._find_registered_webhooks(self._webhook_register_url):
                if wh_id := wh.get("id"):
                    (await self.client.delete(self._api_url(f"/api/v1/webhook/{wh_id}"))).raise_for_status()
                    removed = True
            if removed:
                logger.info("[bluebubbles] webhook unregistered: %s", self._webhook_register_url_for_log)
        except Exception as exc:
            logger.debug("[bluebubbles] failed to unregister webhook (non-critical): %s", exc)
        return removed

    async def _delete_webhook_id(self, webhook_id: Any) -> None:
        assert self.client is not None
        (await self.client.delete(self._api_url(f"/api/v1/webhook/{webhook_id}"))).raise_for_status()

    def _remember_chat_guid(self, address: str, guid: str) -> None:
        for key in (address, _canonical_chat_guid_key(guid), _canonical_chat_guid_key(address)):
            if not key:
                continue
            self._guid_cache[key] = guid
            self._guid_cache.move_to_end(key)
        while len(self._guid_cache) > _GUID_CACHE_SIZE:
            self._guid_cache.popitem(last=False)

    def _is_chat_allowed(self, chat_guid: Optional[str], *, chat_identifier: Optional[str] = None,
                         is_group: Optional[bool] = None) -> bool:
        """Check whether *chat_guid* is permitted by this profile's DM (``;-;``) / group (``;+;``)
        routing and ``allowed_chat_guids`` allowlist."""
        delim = _chat_guid_delimiter(chat_guid)
        if delim == _GROUP_DELIMITER:
            if is_group is False:
                return False
            effective_is_group = True
        elif delim == _DM_DELIMITER:
            if is_group is True:
                return False
            effective_is_group = False
        else:
            effective_is_group = bool(is_group)

        if effective_is_group and self.ignore_group_messages:
            return False
        if not effective_is_group and self.ignore_direct_messages:
            return False

        if not self.allowed_chat_guids:
            return True

        key = _canonical_chat_guid_key(chat_guid)
        _svc, _delim, guid_ident = _split_chat_guid(chat_guid)
        candidates = {
            ident.strip().lower()
            for ident in (guid_ident, chat_identifier)
            if isinstance(ident, str) and ident.strip()
        }
        if effective_is_group:
            if self._allowed_group_identifiers:
                return bool((key and key in self._allowed_chat_keys)
                            or (candidates & self._allowed_group_identifiers))
            return not self.ignore_group_messages
        if self._allowed_dm_identifiers:
            return bool((key and key in self._allowed_chat_keys)
                        or (candidates & self._allowed_dm_identifiers))
        return not self.ignore_direct_messages

    def _is_target_allowed(self, target: Optional[str]) -> bool:
        """Check whether an outbound *target* (chat GUID or bare DM handle) is permitted on this profile."""
        raw = (target or "").strip()
        if not raw:
            return False
        delim = _chat_guid_delimiter(raw)
        if delim == _GROUP_DELIMITER:
            return self._is_chat_allowed(raw, is_group=True)
        if delim == _DM_DELIMITER:
            return self._is_chat_allowed(raw, is_group=False)
        lowered = raw.lower()
        if self._allowed_group_identifiers and lowered in self._allowed_group_identifiers and not (
                "@" in raw or _ADDRESS_RE.match(raw) or _PHONE_RE.fullmatch(raw)):
            return self._is_chat_allowed(f"any{_GROUP_DELIMITER}{raw}", chat_identifier=raw, is_group=True)
        return self._is_chat_allowed(f"any{_DM_DELIMITER}{raw}", chat_identifier=raw, is_group=False)

    # --- Chat GUID resolution ---

    async def _resolve_chat_guid(self, target: str) -> Optional[str]:
        """Resolve an email/phone to a chat GUID (raw ``a;-;b`` GUIDs pass through). Matches strictly on
        ``chatIdentifier`` / ``identifier`` and chat type (``;-;`` for DMs vs ``;+;`` for groups);
        participant membership is intentionally NOT a fallback — the same contact appears in a 1:1 DM
        and any number of groups, so a participant match could leak a DM reply into a group thread.
        ``None`` lets the caller create a fresh DM when DMs are permitted.

        See #24157.
        """
        target = (target or "").strip()
        if not target:
            return None
        delim = _chat_guid_delimiter(target)
        if delim is not None:
            is_group = delim == _GROUP_DELIMITER
            if not self._is_chat_allowed(target, is_group=is_group):
                return None
            service, _delim, ident = _split_chat_guid(target)
            if service:
                return target
            for lookup in (target, _canonical_chat_guid_key(target), ident if not is_group else None):
                if lookup and lookup in self._guid_cache:
                    cached = self._guid_cache[lookup]
                    if _chat_guid_delimiter(cached) == delim and self._is_chat_allowed(cached, is_group=is_group):
                        self._guid_cache.move_to_end(lookup)
                        return cached
            return f"any{target}"
        if not self._is_target_allowed(target):
            return None
        expect_group = (
            bool(self._allowed_group_identifiers)
            and target.lower() in self._allowed_group_identifiers
            and not ("@" in target or _ADDRESS_RE.match(target) or _PHONE_RE.fullmatch(target))
        )
        expected_delim = _GROUP_DELIMITER if expect_group else _DM_DELIMITER
        for lookup in (target, f"{expected_delim}{target.lower()}"):
            if lookup in self._guid_cache:
                cached = self._guid_cache[lookup]
                if (_chat_guid_delimiter(cached) == expected_delim
                        and self._is_chat_allowed(cached, chat_identifier=target, is_group=expect_group)):
                    self._guid_cache.move_to_end(lookup)
                    return cached
        with suppress(Exception):
            offset = 0
            seen = set()
            while True:
                payload = await self._api_post("/api/v1/chat/query", {"limit": 100, "offset": offset})
                chats = payload.get("data", []) or []
                for chat in chats:
                    guid = chat.get("guid") or chat.get("chatGuid")
                    if (chat.get("chatIdentifier") or chat.get("identifier")) == target and guid:
                        if _chat_guid_delimiter(guid) != expected_delim:
                            continue
                        with suppress(TypeError, ValueError):
                            if not expect_group and int(chat.get("style") or 0) == 43:
                                continue
                        if not self._is_chat_allowed(guid, chat_identifier=target, is_group=expect_group):
                            continue
                        self._remember_chat_guid(target, guid)
                        return guid
                page_ids = {chat.get("guid") or chat.get("chatGuid") for chat in chats}
                if len(chats) < 100 or page_ids <= seen:
                    break
                seen.update(page_ids)
                offset += len(chats)
        return None

    async def _create_chat_for_handle(self, address: str, message: str) -> SendResult:
        """Create a new chat by sending the first message to *address*."""
        return await self._post_message(
            "/api/v1/chat/new", {"addresses": [address], "message": message, "tempGuid": _temp_guid()})

    # --- Text sending ---

    @staticmethod
    def truncate_message(content: str, max_length: int = MAX_TEXT_LENGTH) -> list[str]:
        # Base splitter minus "(1/3)" pagination suffixes — iMessage bubbles flow naturally.
        return [_PAGINATION_SUFFIX_RE.sub("", c) for c in BasePlatformAdapter.truncate_message(content, max_length)]

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[dict[str, Any]] = None) -> SendResult:
        text = self.format_message(content)
        if not text:
            return SendResult(success=False, error="BlueBubbles send requires text")
        if not self._is_target_allowed(chat_id):
            return SendResult(success=False, error=f"BlueBubbles chat not allowed for this profile: {chat_id}")
        # Each paragraph becomes its own iMessage bubble; truncate any still too long.
        paragraphs = [p.strip() for p in re.split(r'\n\s*\n', text) if p.strip()] or [text]
        chunks = [c for para in paragraphs for c in (
            [para] if len(para) <= self.MAX_MESSAGE_LENGTH else self.truncate_message(para, self.MAX_MESSAGE_LENGTH))]
        last = SendResult(success=True)
        for chunk in chunks:
            guid = await self._resolve_chat_guid(chat_id)
            if not guid:
                if (self._private_api_enabled and not self.ignore_direct_messages
                        and ";" not in (chat_id or "")
                        and ("@" in chat_id or _ADDRESS_RE.match(chat_id))):  # address → new chat
                    return await self._create_chat_for_handle(chat_id, chunk)
                return SendResult(success=False, error=f"BlueBubbles chat not found for target: {chat_id}")
            if not self._is_chat_allowed(guid, chat_identifier=chat_id, is_group=_is_group_chat_guid(guid)):
                return SendResult(success=False, error=f"BlueBubbles chat not allowed for this profile: {guid}")
            payload: dict[str, Any] = {"chatGuid": guid, "tempGuid": _temp_guid(), "message": chunk}
            if reply_to and self._private_api_enabled and self._helper_connected:
                payload.update(method="private-api", selectedMessageGuid=reply_to, partIndex=0)
            if not (last := await self._post_message("/api/v1/message/text", payload)).success:
                return last
        return last

    async def add_reaction(self, chat_id: str, emoji: str, message_id: Optional[str] = None) -> dict[str, Any]:
        """Send one of the six native tapbacks using the gateway reaction interface."""
        aliases = {"❤️": "love", "❤": "love", "👍": "like", "👎": "dislike",
                   "😂": "laugh", "🤣": "laugh", "‼️": "emphasize", "❗": "emphasize",
                   "❓": "question", "?": "question"}
        reaction = aliases.get(emoji, emoji)
        if reaction not in {"love", "like", "dislike", "laugh", "emphasize", "question"}:
            return {"success": False, "error": "Unsupported iMessage tapback"}
        if not message_id:
            return {"success": False, "error": "A message GUID is required"}
        if not self.client or not self._private_api_enabled or not self._helper_connected:
            return {"success": False, "error": "BlueBubbles Private API helper is unavailable"}
        if not self._is_target_allowed(chat_id):
            return {"success": False, "error": "Chat not allowed for this profile"}
        guid = await self._resolve_chat_guid(chat_id)
        if not guid or not self._is_chat_allowed(guid, chat_identifier=chat_id, is_group=_is_group_chat_guid(guid)):
            return {"success": False, "error": "Chat not found"}
        result = await self._post_message("/api/v1/message/react", {
            "chatGuid": guid, "selectedMessageGuid": message_id, "reaction": reaction, "partIndex": 0})
        if not result.success:
            return {"success": False, "error": result.error}
        key = (guid, message_id)
        self._sent_tapbacks[key] = reaction
        self._sent_tapbacks.move_to_end(key)
        while len(self._sent_tapbacks) > 2000:
            self._sent_tapbacks.popitem(last=False)
        return {"success": True, "message_id": result.message_id}

    async def remove_reaction(self, chat_id: str, message_id: Optional[str] = None) -> dict[str, Any]:
        if not message_id:
            return {"success": False, "error": "A message GUID is required"}
        if not self.client or not self._private_api_enabled or not self._helper_connected:
            return {"success": False, "error": "BlueBubbles Private API helper is unavailable"}
        if not self._is_target_allowed(chat_id):
            return {"success": False, "error": "Chat not allowed for this profile"}
        guid = await self._resolve_chat_guid(chat_id)
        key = (guid, message_id)
        reaction = self._sent_tapbacks.get(key)
        if not guid or not reaction or not self._is_chat_allowed(
                guid, chat_identifier=chat_id, is_group=_is_group_chat_guid(guid)):
            return {"success": False, "error": "No tapback recorded by this adapter for that message"}
        result = await self._post_message("/api/v1/message/react", {
            "chatGuid": guid, "selectedMessageGuid": message_id, "reaction": "-" + reaction, "partIndex": 0})
        if not result.success:
            return {"success": False, "error": result.error}
        self._sent_tapbacks.pop(key, None)
        return {"success": True, "message_id": result.message_id}

    # --- Media sending (outbound) ---

    async def _send_attachment(self, chat_id: str, file_path: str, filename: Optional[str] = None,
                               caption: Optional[str] = None, is_audio_message: bool = False) -> SendResult:
        """Send a file attachment via BlueBubbles multipart upload."""
        if not self.client:
            return SendResult(success=False, error="Not connected")
        if not self._is_target_allowed(chat_id):
            return SendResult(success=False, error=f"Chat not allowed for this profile: {chat_id}")
        if not await asyncio.to_thread(os.path.isfile, file_path):
            return SendResult(success=False, error=f"File not found: {file_path}")
        guid = await self._resolve_chat_guid(chat_id)
        if not guid or not self._is_chat_allowed(guid, chat_identifier=chat_id, is_group=_is_group_chat_guid(guid)):
            return SendResult(success=False, error=f"Chat not found: {chat_id}")
        fname = filename or os.path.basename(file_path)
        try:
            # httpx's async multipart iterator reads file objects through a sync chunk generator —
            # read the bytes off the event-loop thread first.
            payload = await asyncio.to_thread(Path(file_path).read_bytes)
            data: dict[str, str] = {"chatGuid": guid, "name": fname, "tempGuid": uuid.uuid4().hex}
            if is_audio_message:
                data["isAudioMessage"] = "true"
            res = await self.client.post(self._api_url("/api/v1/message/attachment"), data=data, timeout=120,
                                         files={"attachment": (fname, payload, "application/octet-stream")})
            res.raise_for_status()
            result = res.json()
            if caption:
                await self.send(chat_id, caption)
            if result.get("status") == 200:
                rdata = result.get("data") or {}
                return SendResult(success=True, message_id=rdata.get("guid") if isinstance(rdata, dict) else None,
                                  raw_response=result)
            return SendResult(success=False, error=result.get("message", "Attachment upload failed"))
        except Exception as e:
            return SendResult(success=False, error=str(e))

    async def send_image(self, chat_id: str, image_url: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, metadata: Optional[dict[str, Any]] = None) -> SendResult:
        try:
            from gateway.platforms.base import cache_image_from_url
            return await self._send_attachment(chat_id, await cache_image_from_url(image_url), caption=caption)
        except Exception:
            return await super().send_image(chat_id, image_url, caption, reply_to)

    async def send_image_file(self, chat_id, image_path, caption=None, reply_to=None, **kw) -> SendResult:
        return await self._send_attachment(chat_id, image_path, caption=caption)

    async def send_voice(self, chat_id, audio_path, caption=None, reply_to=None, is_voice=None, **kw) -> SendResult:
        # is_voice=False is a plain audio MEDIA file (music, a sound effect): a normal attachment,
        # never an iMessage voice memo.
        return await self._send_attachment(chat_id, audio_path, caption=caption, is_audio_message=is_voice is not False)

    async def send_video(self, chat_id, video_path, caption=None, reply_to=None, **kw) -> SendResult:
        return await self._send_attachment(chat_id, video_path, caption=caption)

    async def send_document(self, chat_id, file_path, caption=None, file_name=None, reply_to=None, **kw) -> SendResult:
        return await self._send_attachment(chat_id, file_path, filename=file_name, caption=caption)

    async def send_animation(self, chat_id, animation_url, caption=None, reply_to=None, metadata=None) -> SendResult:
        return await self.send_image(chat_id, animation_url, caption, reply_to, metadata)

    # --- Typing indicators / read receipts (private API only) ---

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        await self._private_api_chat_call(chat_id, "typing", "post")

    async def stop_typing(self, chat_id: str) -> None:
        await self._private_api_chat_call(chat_id, "typing", "delete")

    async def mark_read(self, chat_id: str) -> bool:
        return await self._private_api_chat_call(chat_id, "read", "post")

    # --- Chat info ---

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        is_group = ";+;" in (chat_id or "")
        info: dict[str, Any] = {"name": chat_id, "type": "group" if is_group else "dm"}
        with suppress(Exception):
            if guid := await self._resolve_chat_guid(chat_id):
                res = await self._api_get(f"/api/v1/chat/{quote(guid, safe='')}?with=participants")
                data = (res or {}).get("data", {})
                info["name"] = data.get("displayName") or data.get("chatIdentifier") or chat_id
                participants = [addr for p in data.get("participants", []) or []
                                if (addr := (p.get("address") or "").strip())]
                if participants:
                    info["participants"] = participants
        return info

    def format_message(self, content: str) -> str:
        return strip_markdown(content, keep_link_targets=True)  # iMessage auto-links bare URLs only

    # --- Inbound attachment downloading ---

    async def _download_attachment(self, att_guid: str, att_meta: dict[str, Any]) -> Optional[str]:
        """Download an attachment and cache it locally; local path or None on failure."""
        if not self.client:
            return None
        try:
            resp = await self.client.get(self._api_url(f"/api/v1/attachment/{quote(att_guid, safe='')}/download"),
                                         timeout=60, follow_redirects=True)
            resp.raise_for_status()
            data = resp.content
            mime = (att_meta.get("mimeType") or "").lower()
            if mime.startswith("image/"):
                return await cache_image_from_bytes_async(data, _closed_ext(mime, _BLUEBUBBLES_IMAGE_EXT_OVERRIDES, ".jpg"))
            if mime.startswith("audio/"):
                return await cache_audio_from_bytes_async(data, _closed_ext(mime, _BLUEBUBBLES_AUDIO_EXT_OVERRIDES, ".mp3"))
            # Videos, documents, and everything else
            return await cache_document_from_bytes_async(data, att_meta.get("transferName", "") or f"file_{uuid.uuid4().hex[:8]}")
        except Exception as exc:
            logger.warning("[bluebubbles] failed to download attachment %s: %s", _redact(att_guid), exc)
            return None

    # --- Webhook handling ---


# --- Plugin hooks (registered via register(ctx) below) ---

_INSTALL_HINT = "BlueBubbles: aiohttp/httpx missing or BLUEBUBBLES_SERVER_URL/BLUEBUBBLES_PASSWORD not configured"


def _is_connected(config) -> bool:
    """Server URL + password present in ``PlatformConfig.extra`` (the env table seeds it) or the env."""
    extra = getattr(config, "extra", None) or {}
    if extra.get("server_url") and extra.get("password"):
        return True
    import hermes_cli.gateway as gateway_mod
    return all((gateway_mod.get_env_value(n) or "").strip() for n in ("BLUEBUBBLES_SERVER_URL", "BLUEBUBBLES_PASSWORD"))


async def _standalone_send(pconfig, chat_id: str, message: str, *, thread_id: Optional[str] = None,
                           media_files: Optional[list] = None, force_document: bool = False, **_kw) -> dict[str, Any]:
    """Out-of-process send (cron without a co-resident gateway): one-shot adapter connect → send →
    disconnect against the BlueBubbles REST API. Text only; ``thread_id``/``media_files``/
    ``force_document`` are signature parity (send_message drops media with a warning)."""
    from gateway.platforms._shared import send_error
    if not check_bluebubbles_requirements():
        return send_error("BlueBubbles requirements not met (need aiohttp + httpx).")
    try:
        adapter = BlueBubblesAdapter(
            PlatformConfig(
                home_channel=getattr(pconfig, "home_channel", None),
                extra=dict(getattr(pconfig, "extra", None) or {}),
            )
        )
        if not await adapter.connect():
            return send_error("BlueBubbles: failed to connect to server")
        try:
            result = await adapter.send(chat_id, message)
        finally:
            await adapter.disconnect()
        if not result.success:
            return send_error(f"BlueBubbles send failed: {result.error}")
        return {"success": True, "platform": "bluebubbles", "chat_id": chat_id, "message_id": result.message_id}
    except Exception as e:
        logger.debug("BlueBubbles standalone send failed", exc_info=True)
        return send_error(f"BlueBubbles send failed: {e}")


def interactive_setup() -> None:
    """Configure BlueBubbles iMessage gateway (``hermes setup`` / ``hermes gateway setup``)."""
    from hermes_cli.setup import _info, print_header, print_success, prompt, prompt_yes_no
    from hermes_cli.setup_platforms import _prompt_allowlist, _save_port, declines_reconfigure, save_prompted
    print_header("BlueBubbles (iMessage)")
    if declines_reconfigure("BlueBubbles", "Reconfigure BlueBubbles?", "BLUEBUBBLES_SERVER_URL"):
        return
    _info("Connects Hermes to iMessage via BlueBubbles — a free, open-source",
          "macOS server that bridges iMessage to any device.",
          "   Requires a Mac running BlueBubbles Server v1.0.0+",
          "   Download: https://bluebubbles.app/", None,
          "In BlueBubbles Server → Settings → API, note your Server URL and Password.", None)
    for label, env_var, secret, what, transform in (
        ("BlueBubbles server URL (e.g. http://192.168.1.10:1234)", "BLUEBUBBLES_SERVER_URL", False, "Server URL",
         lambda v: v.rstrip("/")),
        ("BlueBubbles server password", "BLUEBUBBLES_PASSWORD", True, "Password", None),
    ):
        if not save_prompted(env_var, label, password=secret, transform=transform,
                              skip_msg=f"{what} is required — skipping BlueBubbles setup"):
            return
    print_success("BlueBubbles credentials saved")
    _info(None, "🔒 Security: Restrict who can message your bot",
          "   Use iMessage addresses: email (user@icloud.com) or phone (+15551234567)", None)
    _prompt_allowlist("BLUEBUBBLES_ALLOWED_USERS", "Allowed iMessage addresses (comma-separated, leave empty for open access)",
                      "BlueBubbles allowlist configured", "⚠️  No allowlist set — anyone who can iMessage you can use the bot!")
    _info(None, "📬 Home Channel: phone or email for cron job delivery and notifications.",
          "   You can also set this later with /set-home in your iMessage chat.")
    save_prompted("BLUEBUBBLES_HOME_CHANNEL", "Home channel address (leave empty to set later)")
    _info(None, "Advanced settings (defaults are fine for most setups):")
    if prompt_yes_no("Configure webhook listener settings?", False):
        _save_port("BLUEBUBBLES_WEBHOOK_PORT", prompt("Webhook listener port (default: 8645)"), "8645")
    _info(None, "Requires the BlueBubbles Private API helper for typing indicators,",
          "read receipts, and tapback reactions. Basic messaging works without it.",
          "   Install: https://docs.bluebubbles.app/helper-bundle/installation")


# System-prompt platform hint (PlatformEntry.platform_hint); cached-prompt bytes, keep stable.
_PLATFORM_HINT = (
    # The adapter runs strip_markdown(keep_link_targets=True): markers vanish, the layout stays.
    "You are texting via iMessage (BlueBubbles). Replies arrive as plain text bubbles, so write like a person "
    "texting: short and conversational, answer first, no preamble or recap. Markdown does not render and is "
    "stripped, so skip headers, tables, code fences and backticks; for a few items use short lines or a "
    "sentence rather than nested bullets. Put a command or code snippet on its own line as plain text so it "
    "can be copied. Write links as bare URLs (iMessage auto-links them). "
    "You can send files natively: write MEDIA:/absolute/path/to/file in your response. "
    "Images (.jpg, .png, .heic) appear as photos and other files arrive as attachments."
)


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system."""
    from . import gateway_env  # env -> GatewayConfig bridge (gateway.config_env)
    from .tools import register_tools
    register_tools(ctx)
    ctx.register_platform(
        name="bluebubbles", label="BlueBubbles (iMessage)", adapter_factory=BlueBubblesAdapter,
        dashboard_description="Use Hermes through iMessage via a BlueBubbles server.",
        docs_url="https://bluebubbles.app/",
        dashboard_env_vars=("BLUEBUBBLES_SERVER_URL", "BLUEBUBBLES_PASSWORD", "BLUEBUBBLES_ALLOWED_USERS"),
        check_fn=check_bluebubbles_requirements, is_connected=_is_connected,
        config_connected_fn=config_connected_extra("server_url", "password"),
        required_env=["BLUEBUBBLES_SERVER_URL", "BLUEBUBBLES_PASSWORD"], install_hint=_INSTALL_HINT,
        setup_fn=interactive_setup,
        cron_deliver_env_var="BLUEBUBBLES_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,  # out-of-process cron / send_message
        # 0 = send_message does not pre-chunk: the adapter's send() splits per paragraph and
        # truncates itself (MAX_MESSAGE_LENGTH), exactly as the former core sender did.
        max_message_length=0, pii_safe=True, emoji="💬", allow_update_command=True,
        platform_hint=_PLATFORM_HINT,
        display_tier="low",
        env_overrides=gateway_env.ENV_OVERRIDES,
        env_enable_credentials=gateway_env.ENV_ENABLE_CREDENTIALS, **gateway_env.AUTHZ,
    )
