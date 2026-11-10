"""BlueBubbles webhook hydration, routing, and admission ownership."""

import asyncio
import hashlib
import json
import logging
import re
import time
from copy import deepcopy
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import parse_qs, quote

import httpx

from gateway.platforms.event import MessageEvent, MessageType

logger = logging.getLogger(__name__)

_TAPBACK_CODES = {*range(2000, 2007), *range(3000, 3007)}
_TAPBACK_STRINGS = {
    "love", "like", "dislike", "laugh", "emphasize", "question", "emoji",
    "-love", "-like", "-dislike", "-laugh", "-emphasize", "-question", "-emoji",
    *(str(c) for c in _TAPBACK_CODES),
}
_MESSAGE_EVENTS = {"new-message", "message", "updated-message"}
_HYDRATE_RETRY_DELAYS = (0.0, 0.05, 0.15)
_ATTACHMENT_WAIT_SECONDS = 300.0
_ATTACHMENT_RETRY_SECONDS = 5.0
_INBOUND_CACHE_SIZE = 2000
_INBOUND_CACHE_TTL = 300
_PHONE_RE = re.compile(r"\+?\d{7,15}")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_DM_DELIMITER = ";-;"
_GROUP_DELIMITER = ";+;"
_PLUGIN_PAYLOAD_UTI = "dyn.age81a5dzq7y066dbtf0g82peqf4hk2pdrb00n5xy"


def _is_ignored_attachment(att: Any) -> bool:
    """True for internal iMessage link-preview / balloon payload blobs (.pluginPayloadAttachment)."""
    if not isinstance(att, dict):
        return True
    transfer_name = str(att.get("transferName") or att.get("filename") or "").strip().lower()
    if transfer_name.endswith(".pluginpayloadattachment"):
        return True
    uti = str(att.get("uti") or "").strip().lower()
    if uti == _PLUGIN_PAYLOAD_UTI:
        return True
    if att.get("hideAttachment") is True and not (att.get("mimeType") or "").strip():
        return True
    return False


def _user_attachments(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Return only real user-visible attachments on *record*, excluding link-preview payloads."""
    return [
        att for att in (record.get("attachments") or [])
        if isinstance(att, dict) and not _is_ignored_attachment(att)
    ]


def _chat_guid_delimiter(value: Optional[str]) -> Optional[str]:
    text = (value or "").strip()
    if _GROUP_DELIMITER in text:
        return _GROUP_DELIMITER
    if _DM_DELIMITER in text:
        return _DM_DELIMITER
    return None


def _is_dm_chat_guid(value: Optional[str]) -> bool:
    return _chat_guid_delimiter(value) == _DM_DELIMITER


def _is_group_chat_guid(value: Optional[str]) -> bool:
    return _chat_guid_delimiter(value) == _GROUP_DELIMITER


def _split_chat_guid(value: Optional[str]) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Return ``(service, delimiter, identifier)`` for a BlueBubbles chat GUID or bare target."""
    text = (value or "").strip()
    if not text:
        return None, None, None
    for delimiter in (_GROUP_DELIMITER, _DM_DELIMITER):
        if delimiter in text:
            service, identifier = text.split(delimiter, 1)
            return service.strip() or None, delimiter, identifier.strip() or None
    return None, None, text


def _canonical_chat_guid_key(value: Optional[str]) -> Optional[str]:
    """Service-agnostic chat key preserving the DM (``;-;``) vs group (``;+;``) delimiter."""
    _service, delimiter, identifier = _split_chat_guid(value)
    if not delimiter or not identifier:
        return None
    return f"{delimiter}{identifier.lower()}"


@dataclass
class _InboundMessage:
    record: dict[str, Any] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0
    touched: float = field(default_factory=time.monotonic)
    accepted: bool = False
    attachment_deadline: float = 0.0
    retry_task: Any = None
    retry_payload: Any = None
    delivered: set[str] = field(default_factory=set)
    downloaded: dict[str, tuple[str, str]] = field(default_factory=dict)
    force_requested: set[str] = field(default_factory=set)
    last_edit_ts: int = 0
    retracted: bool = False


def _ok():
    from aiohttp import web
    return web.Response(text="ok")


class BlueBubblesInboundMixin:
    @staticmethod
    async def _wait_for_attachment_retry(delay):
        await asyncio.sleep(delay)

    def _schedule_attachment_retry(self, payload, state, message_id):
        state.retry_payload = payload
        if not state.attachment_deadline:
            state.attachment_deadline = time.monotonic() + _ATTACHMENT_WAIT_SECONDS
        if state.retry_task is not None and not state.retry_task.done():
            return
        task = asyncio.create_task(self._retry_pending_attachments(state, message_id))
        state.retry_task = task
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _retry_pending_attachments(self, state, message_id):
        # Hold only this message between attempts; other messages may continue in this chat.
        state.users += 1
        try:
            while True:
                remaining = state.attachment_deadline - time.monotonic()
                await self._wait_for_attachment_retry(min(_ATTACHMENT_RETRY_SECONDS, max(0, remaining)))
                response = await self._process_inbound(state.retry_payload, {}, message_id, held=state)
                if response.status != 503 and state.accepted and all(
                        not isinstance(a, dict) or not a.get("guid") or a["guid"] in state.delivered
                        for a in _user_attachments(state.record)):
                    state.retry_payload = None
                    return
                if time.monotonic() >= state.attachment_deadline:
                    return
        finally:
            state.users -= 1
            state.retry_task = None

    def _extract_payload_record(self, payload: dict[str, Any]) -> Optional[dict[str, Any]]:
        data = payload.get("data")
        if isinstance(data, dict):
            return data
        if isinstance(data, list) and (first := next((i for i in data if isinstance(i, dict)), None)):
            return first
        if isinstance(payload.get("message"), dict):
            return payload.get("message")
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _value(*candidates: Any) -> Optional[str]:
        return next((c.strip() for c in candidates if isinstance(c, str) and c.strip()), None)

    @staticmethod
    def _parse_webhook_body(raw: bytes) -> Any:
        """Decode a webhook body: JSON, else form-encoded with a JSON field."""
        body = raw.decode("utf-8", errors="replace")
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            form = parse_qs(body)
            payload_str = (form.get("payload") or form.get("data") or form.get("message") or [""])[0]
            return json.loads(payload_str) if payload_str else {}

    async def _collect_attachments(self, record: dict[str, Any], *,
                                   state: Optional[_InboundMessage] = None):
        """Download inbound attachments and report the GUIDs included in this delivery."""
        media_urls: list[str] = []
        media_types: list[str] = []
        collected: set[str] = set()
        msg_type = MessageType.TEXT
        for att in _user_attachments(record):
            att_guid = att.get("guid", "")
            if not att_guid or (state is not None and att_guid in state.delivered):
                continue
            if att.get("transferState") not in (None, 5, "5"):
                if (getattr(self, "_private_api_enabled", False)
                        and getattr(self, "_helper_connected", False)
                        and getattr(self, "client", None) is not None
                        and (state is None or att_guid not in state.force_requested)):
                    if state is not None:
                        state.force_requested.add(att_guid)
                    force_fn = getattr(self, "_force_download_attachment", None)
                    if callable(force_fn):
                        with suppress(Exception):
                            await force_fn(att_guid)
                continue
            mime = (att.get("mimeType") or "").lower()
            prior = state.downloaded.get(att_guid) if state is not None else None
            cached = prior[0] if prior and prior[1] == mime else None
            if not cached:
                for delay in _HYDRATE_RETRY_DELAYS:
                    if delay:
                        await asyncio.sleep(delay)
                    remaining = (state.attachment_deadline - time.monotonic()
                                 if state is not None and state.attachment_deadline else _ATTACHMENT_WAIT_SECONDS)
                    if remaining <= 0:
                        break
                    try:
                        async with asyncio.timeout(remaining):
                            cached = await self._download_attachment(att_guid, att)
                    except TimeoutError:
                        break
                    if cached:
                        break
            if not cached:
                continue
            mime = (att.get("mimeType") or "").lower()
            if state is not None:
                state.downloaded[att_guid] = cached, mime
            media_urls.append(cached)
            media_types.append(mime)
            collected.add(att_guid)
            is_voice = mime.startswith("audio/") or (att.get("uti") or "").endswith("caf")
            msg_type = (MessageType.PHOTO if mime.startswith("image/") else MessageType.VOICE if is_voice
                        else MessageType.VIDEO if mime.startswith("video/") else MessageType.DOCUMENT)
        if len(media_urls) > 1 and any(m.split("/")[0] == "image" for m in media_types):  # any image → PHOTO
            msg_type = MessageType.PHOTO
        return media_urls, media_types, msg_type, collected

    def _webhook_token(self, request) -> Optional[str]:
        return (request.query.get("password") or request.query.get("guid") or request.headers.get("x-password")
                or request.headers.get("x-guid") or request.headers.get("x-bluebubbles-guid"))

    def _resolve_chat_and_sender(self, payload: dict[str, Any], record: dict[str, Any]):
        """Return ``(chat_guid, chat_identifier, sender)`` across BlueBubbles payload shapes."""
        chat_guid = self._value(record.get("chatGuid"), payload.get("chatGuid"), record.get("chat_guid"),
                                payload.get("chat_guid"))
        # ``guid`` on payload/record is the message GUID, never a chat GUID. BlueBubbles v1.9+
        # commonly nests chat identity under data.chats[0], including private API ``[auth-key]``.
        chats = record.get("chats") or []
        first_chat = chats[0] if chats and isinstance(chats[0], dict) else {}
        if not chat_guid:
            chat_guid = self._value(first_chat.get("guid"), first_chat.get("chatGuid"),
                                    first_chat.get("[auth-key]"))
        chat_identifier = self._value(
            record.get("chatIdentifier"), record.get("identifier"),
            payload.get("chatIdentifier"), payload.get("identifier"),
            first_chat.get("chatIdentifier"), first_chat.get("identifier"))
        handle = record.get("handle")
        sender = (self._value(handle.get("address") if isinstance(handle, dict) else None, record.get("sender"),
                              record.get("from"), record.get("address")) or chat_identifier or chat_guid)
        if not (chat_guid or chat_identifier) and sender:
            chat_identifier = sender
        return chat_guid, chat_identifier, sender

    @staticmethod
    def _is_group_record(record: dict[str, Any], chat_guid: Optional[str]) -> bool:
        delim = _chat_guid_delimiter(chat_guid)
        if delim == _GROUP_DELIMITER:
            return True
        if delim == _DM_DELIMITER:
            return False
        if record.get("isGroup"):
            return True
        for chat in record.get("chats") or []:
            if not isinstance(chat, dict):
                continue
            item_delim = _chat_guid_delimiter(
                chat.get("guid") or chat.get("chatGuid") or chat.get("[auth-key]")
            )
            if item_delim == _GROUP_DELIMITER:
                return True
            if item_delim == _DM_DELIMITER:
                return False
            with suppress(TypeError, ValueError):
                # BlueBubbles v1.9.9: group chats are style 43; one-to-one DMs are style 45.
                style = int(chat.get("style") or 0)
                if style == 43:
                    return True
                if style == 45:
                    return False
        return False

    @staticmethod
    def _canonical_session_chat_id(chat_guid: Optional[str], chat_identifier: Optional[str],
                                   sender: Optional[str], is_group: bool) -> Optional[str]:
        """Collapse BlueBubbles DM GUID aliases onto the address while preserving group GUIDs."""
        if is_group:
            return chat_guid
        address = chat_guid.split(";-;", 1)[-1] if chat_guid and ";-;" in chat_guid else None
        if chat_identifier and (chat_identifier in (sender, address)
                                or _PHONE_RE.fullmatch(chat_identifier) or _EMAIL_RE.fullmatch(chat_identifier)):
            return chat_identifier
        if sender and ";" not in sender:
            return sender
        if address:
            return address
        return chat_guid

    @staticmethod
    def _has_real_chat_guid(chat_guid: Optional[str]) -> bool:
        return bool(chat_guid and (";-;" in chat_guid or ";+;" in chat_guid))

    @staticmethod
    def _extract_retracted_ts(record: dict[str, Any]) -> int:
        raw = record.get("dateRetracted")
        with suppress(TypeError, ValueError):
            if raw is not None and int(raw) > 0:
                return int(raw)
        return 1 if record.get("isRetracted") else 0

    @staticmethod
    def _extract_edited_ts(record: dict[str, Any]) -> int:
        raw = record.get("dateEdited")
        with suppress(TypeError, ValueError):
            if raw is not None and int(raw) > 0:
                return int(raw)
        return 0

    @classmethod
    def _has_pending_edit_or_unsend(cls, state: _InboundMessage, record: dict[str, Any]) -> bool:
        if not state.accepted:
            return False
        retracted_ts = cls._extract_retracted_ts(record) or cls._extract_retracted_ts(state.record)
        if retracted_ts > 0 and not state.retracted:
            return True
        edited_ts = cls._extract_edited_ts(record) or cls._extract_edited_ts(state.record)
        return edited_ts > state.last_edit_ts

    @classmethod
    def _is_receipt_only_update(cls, event_type: str, record: dict[str, Any]) -> bool:
        # Receipt snapshots can also carry previously unseen content and completed attachments.
        if cls._extract_retracted_ts(record) > 0 or cls._extract_edited_ts(record) > 0:
            return False
        return (event_type == "updated-message"
                and not (any(record.get(key) for key in ("text", "message", "body")) or _user_attachments(record)))

    def _has_pending_inbound_content(self, message_id: Optional[str]) -> bool:
        state = self._inbound_messages.get(message_id)
        if state is None:
            return False
        if not state.accepted:
            return any(state.record.get(key) for key in ("text", "message", "body")) or bool(_user_attachments(state.record))
        return any(isinstance(att, dict) and att.get("guid") and att["guid"] not in state.delivered
                   for att in _user_attachments(state.record))

    async def _hydrate_inbound_record(self, message_id: Optional[str]) -> dict[str, Any]:
        """Fetch routing and attachment relationships with bounded in-request retries.

        BlueBubbles does not retry failed webhook POSTs, so transient API/relationship lag has to be
        absorbed before this request is acknowledged.
        """
        if not message_id or not self.client:
            return {}
        last_error: Optional[Exception] = None
        last_record: dict[str, Any] = {}
        for delay in _HYDRATE_RETRY_DELAYS:
            if delay:
                await asyncio.sleep(delay)
            try:
                data = (await self._api_get(
                    f"/api/v1/message/{quote(message_id, safe='')}?with=chats,attachments")).get("data")
                last_error = None
            except (httpx.HTTPError, OSError, ValueError) as exc:
                last_error = exc
                continue
            if isinstance(data, dict):
                last_record = data
                chats = [chat for chat in (data.get("chats") or []) if isinstance(chat, dict)]
                if chats and all(att.get("transferState") in (None, 5, "5")
                                 for att in _user_attachments(data)):
                    return data
        if last_error is not None:
            raise last_error
        return last_record

    @staticmethod
    def _merge_inbound_record(target: dict[str, Any], incoming: dict[str, Any]) -> None:
        """Refresh metadata by identity without erasing fields omitted by sparse echoes."""
        for key, value in incoming.items():
            if value in (None, "", []):
                continue
            if key in {"attachments", "chats"} and isinstance(value, list):
                current = target.setdefault(key, [])
                seen = {item.get("guid") or item.get("[auth-key]") or repr(item): item
                        for item in current if isinstance(item, dict)}
                for item in value:
                    identity = (item.get("guid") or item.get("[auth-key]") or repr(item)
                                if isinstance(item, dict) else repr(item))
                    if identity in seen and isinstance(item, dict):
                        BlueBubblesInboundMixin._merge_inbound_record(seen[identity], item)
                    else:
                        copied = deepcopy(item)
                        current.append(copied)
                        if isinstance(copied, dict):
                            seen[identity] = copied
            elif isinstance(value, dict) and isinstance(target.get(key), dict):
                BlueBubblesInboundMixin._merge_inbound_record(target[key], value)
            else:
                target[key] = deepcopy(value)

    def adopt_held_inbound(self, predecessor):
        """Use the runner's handoff seam to preserve partial media and sparse records."""
        for message_id, held in getattr(predecessor, "_inbound_messages", {}).items():
            current = self._inbound_messages.get(message_id)
            if current is None:
                self._inbound_messages[message_id] = held
            elif current is not held:
                merged = deepcopy(held.record)
                self._merge_inbound_record(merged, current.record)
                current.record.clear()
                current.record.update(merged)
                current.accepted = current.accepted or held.accepted
                current.retracted = current.retracted or held.retracted
                current.last_edit_ts = max(current.last_edit_ts, held.last_edit_ts)
                current.delivered.update(held.delivered)
                current.force_requested.update(held.force_requested)
                for guid, cached in held.downloaded.items():
                    current.downloaded.setdefault(guid, cached)
                current.touched = max(current.touched, held.touched)
                if held.retry_payload is not None:
                    current.retry_payload = held.retry_payload
                    current.attachment_deadline = held.attachment_deadline
        for message_id, state in list(self._inbound_messages.items()):
            if state.retry_payload is not None:
                self._schedule_attachment_retry(state.retry_payload, state, message_id)
        for message_id, state in list(self._inbound_messages.items()):
            if len(self._inbound_messages) <= _INBOUND_CACHE_SIZE:
                break
            if not state.users and (state.retry_task is None or state.retry_task.done()):
                self._inbound_messages.pop(message_id)

    def _restore_admitted_content(self, state, message_id):
        """Restore only accepted content from the runner's reconnect-carried cache."""
        if not message_id:
            return
        state.accepted = state.accepted or self._dedup.contains(json.dumps(["message", message_id]))
        for attachment in _user_attachments(state.record):
            if guid := attachment.get("guid"):
                if self._dedup.contains(json.dumps(["attachment", message_id, guid])):
                    state.delivered.add(guid)

    def _remember_admitted_content(self, message_id, attachments):
        if not message_id:
            return
        self._dedup.is_duplicate(json.dumps(["message", message_id]))
        for guid in attachments:
            self._dedup.is_duplicate(json.dumps(["attachment", message_id, guid]))

    def _get_inbound_message(self, message_id: Optional[str], record: dict[str, Any]) -> _InboundMessage:
        now = time.monotonic()
        for key, state in list(self._inbound_messages.items()):
            if (not state.users and (state.retry_task is None or state.retry_task.done())
                    and now - state.touched >= _INBOUND_CACHE_TTL):
                self._inbound_messages.pop(key)
        state = self._inbound_messages.get(message_id) if message_id else None
        if state is None:
            state = _InboundMessage()
            if message_id:
                for key, old in list(self._inbound_messages.items()):
                    if len(self._inbound_messages) < _INBOUND_CACHE_SIZE:
                        break
                    if not old.users and (old.retry_task is None or old.retry_task.done()):
                        self._inbound_messages.pop(key)
                self._inbound_messages[message_id] = state
        if message_id:
            self._inbound_messages.move_to_end(message_id)
        state.touched = now
        state.users += 1
        # Merge before waiting: an update can enrich the owner across hydration/download awaits.
        self._merge_inbound_record(state.record, record)
        self._restore_admitted_content(state, message_id)
        return state

    def _reserve_inbound_turn(self, chat_id: str):
        previous = self._inbound_chat_tails.get(chat_id)
        current = asyncio.get_running_loop().create_future()
        self._inbound_chat_tails[chat_id] = current
        return previous, current

    def _finish_inbound_turn(self, chat_id: str, previous, current) -> None:
        def release(_=None):
            if not current.done():
                current.set_result(None)
            if self._inbound_chat_tails.get(chat_id) is current:
                self._inbound_chat_tails.pop(chat_id, None)
        # A cancelled waiter must not let its successor overtake a still-running predecessor.
        if previous is not None and not previous.done():
            previous.add_done_callback(release)
        else:
            release()

    async def _route_inbound_message(self, payload, state, message_id):
        record = state.record
        chat_guid, chat_identifier, sender = self._resolve_chat_and_sender(payload, record)
        if not self._has_real_chat_guid(chat_guid):
            try:
                hydrated = await self._hydrate_inbound_record(message_id)
                self._merge_inbound_record(record, hydrated)
            except (httpx.HTTPError, OSError, ValueError) as exc:
                logger.warning("[bluebubbles] inbound hydration failed: %s", type(exc).__name__)
            chat_guid, chat_identifier, sender = self._resolve_chat_and_sender(payload, record)
        if not self._has_real_chat_guid(chat_guid):
            logger.warning("[bluebubbles] ignoring message with no resolvable chat GUID")
            return None
        is_group = self._is_group_record(record, chat_guid)
        if not self._is_chat_allowed(chat_guid, chat_identifier=chat_identifier, is_group=is_group):
            logger.debug("[bluebubbles] ignoring inbound %s message for unrouted chat %s",
                         "group" if is_group else "dm", chat_guid)
            return None
        chat_id = self._canonical_session_chat_id(chat_guid, chat_identifier, sender, is_group)
        if not sender or not chat_id:
            return None
        self._remember_chat_guid(chat_id, chat_guid)
        if chat_identifier and chat_identifier != chat_id and not is_group:
            self._remember_chat_guid(chat_identifier, chat_guid)
        if is_group and getattr(self, "_allowed_group_identifiers", None):
            extra = getattr(getattr(self, "config", None), "extra", None)
            if isinstance(extra, dict):
                group_allowed = list(extra.get("group_allowed_chats") or [])
                if chat_id not in group_allowed:
                    group_allowed.append(chat_id)
                    extra["group_allowed_chats"] = group_allowed
        return chat_guid, chat_id, chat_identifier, sender, is_group

    async def _deliver_inbound_message(self, payload, state, message_id, route):
        from aiohttp import web

        _chat_guid, chat_id, chat_identifier, sender, is_group = route
        record = state.record
        self._restore_admitted_content(state, message_id)
        retracted_ts = self._extract_retracted_ts(record)
        edited_ts = self._extract_edited_ts(record)
        pending_unsend = state.accepted and retracted_ts > 0 and not state.retracted
        pending_edit = state.accepted and not pending_unsend and edited_ts > state.last_edit_ts
        text = self._value(record.get("text"), record.get("message"), record.get("body")) or ""
        # Known unmentioned group text needs no media I/O; sparse text can still hydrate below.
        if is_group and self.require_mention and not pending_unsend and text and not self._message_matches_mention_patterns(text):
            return _ok()
        if _user_attachments(record) and not state.attachment_deadline:
            state.attachment_deadline = time.monotonic() + _ATTACHMENT_WAIT_SECONDS
        if (_user_attachments(record) and self.client
                and time.monotonic() < state.attachment_deadline):
            try:
                async with asyncio.timeout(state.attachment_deadline - time.monotonic()):
                    self._merge_inbound_record(record, await self._hydrate_inbound_record(message_id))
            except (httpx.HTTPError, OSError, ValueError, TimeoutError) as exc:
                logger.warning("[bluebubbles] attachment hydration failed: %s", type(exc).__name__)
        text = self._value(record.get("text"), record.get("message"), record.get("body")) or ""
        if is_group and self.require_mention:
            if not pending_unsend and not self._message_matches_mention_patterns(text):
                return _ok()
            if text:
                text = self._clean_mention_text(text)
        self._restore_admitted_content(state, message_id)
        retracted_ts = self._extract_retracted_ts(record)
        edited_ts = self._extract_edited_ts(record)
        pending_unsend = state.accepted and retracted_ts > 0 and not state.retracted
        pending_edit = state.accepted and not pending_unsend and edited_ts > state.last_edit_ts
        media_urls, media_types, msg_type, collected = await self._collect_attachments(record, state=state)
        attachments = {att.get("guid") for att in _user_attachments(record)
                       if att.get("guid")} - state.delivered
        if not attachments <= collected:
            if not state.attachment_deadline or time.monotonic() < state.attachment_deadline:
                self._schedule_attachment_retry(payload, state, message_id)
                return _ok()
            if state.accepted and not (pending_unsend or pending_edit):
                return web.json_response({"error": "attachments not ready"}, status=503)
            text = (text + "\n\n[An attachment was included, but could not be retrieved within five minutes.]").strip()
            attachments = collected
        edit_revision: Optional[str] = None
        if state.accepted:
            if pending_unsend:
                text = f"[Unsent message: {text}]" if text else "[Unsent message]"
                edit_revision = f"unsend:{retracted_ts}"
            elif pending_edit:
                if not text:
                    return _ok()
                text = f"[Edited message] {text}"
                edit_revision = f"edit:{edited_ts}"
            elif media_urls:
                text = "(attachment)"
                edit_revision = "attachments:" + hashlib.sha256(
                    json.dumps(sorted(attachments)).encode()).hexdigest()
            else:
                return _ok()
        elif not text and media_urls:
            text = "(attachment)"
        if not text:
            return web.json_response({"error": "missing message fields"}, status=400)
        chats = record.get("chats") or []
        chat_name = self._value(chats[0].get("displayName") if chats and isinstance(chats[0], dict) else None,
                                chat_identifier, chat_id)
        source = self.build_source(chat_id=chat_id, chat_name=chat_name,
                                   chat_type="group" if is_group else "dm", user_id=sender, user_name=sender,
                                   chat_id_alt=chat_identifier, message_id=message_id)
        event = MessageEvent(
            text=text, message_type=msg_type, source=source,
            raw_message={**payload, "data": deepcopy(record)}, message_id=message_id,
            reply_to_message_id=self._value(record.get("threadOriginatorGuid"), record.get("associatedMessageGuid")),
            media_urls=media_urls, media_types=media_types)
        if edit_revision:
            # Native admission keys by message GUID plus revision. New media or edit/unsend is a new input,
            # while the original GUID must remain intact for provider replies and reactions.
            event.edit_revision = edit_revision
        try:
            await self.handle_message(event)
        finally:
            # Admission, not model/output completion, is the point of no replay.
            if event._gateway_accepted:
                state.accepted = True
                if retracted_ts > 0:
                    state.retracted = True
                if edited_ts > state.last_edit_ts:
                    state.last_edit_ts = edited_ts
                self._remember_admitted_content(message_id, attachments)
                state.delivered.update(attachments)
                for guid in attachments:
                    state.downloaded.pop(guid, None)
        if not event._gateway_accepted:
            return web.json_response({"error": "gateway did not accept message"}, status=503)
        if self.send_read_receipts:
            task = asyncio.create_task(self.mark_read(chat_id))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
        return _ok()

    async def _handle_webhook(self, request):
        from aiohttp import web

        if self._webhook_token(request) != self.password:
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            payload = self._parse_webhook_body(await request.read())
            if not isinstance(payload, dict):
                raise ValueError("webhook body must be an object")
        except Exception:
            logger.exception("[bluebubbles] webhook parse error")
            return web.json_response({"error": "invalid payload"}, status=400)
        event_type = self._value(payload.get("type"), payload.get("event")) or ""
        if event_type and event_type not in _MESSAGE_EVENTS:
            return _ok()
        record = self._extract_payload_record(payload) or {}
        if record.get("isFromMe") or record.get("fromMe") or record.get("is_from_me"):
            return _ok()
        assoc_type = record.get("associatedMessageType")
        if (isinstance(assoc_type, int) and assoc_type in _TAPBACK_CODES) or (
            isinstance(assoc_type, str) and assoc_type.strip().lower() in _TAPBACK_STRINGS
        ):
            return _ok()
        message_id = self._value(record.get("guid"), record.get("messageGuid"), record.get("id"))
        # A routing-only update can complete a retained message after hydration failed.
        if (self._is_receipt_only_update(event_type, record)
                and not self._has_pending_inbound_content(message_id)):
            return _ok()
        return await self._process_inbound(payload, record, message_id)

    async def _process_inbound(self, payload, record, message_id, *, held=None):
        state = held or self._get_inbound_message(message_id, record)
        if held is None and (state.retry_task is None or state.retry_task.done()):
            state.attachment_deadline = 0.0
        if held is not None:
            state.users += 1
        try:
            async with state.lock:
                attachments = {att.get("guid") for att in _user_attachments(state.record)
                               if att.get("guid")}
                if (state.accepted and attachments <= state.delivered
                        and not self._has_pending_edit_or_unsend(state, record)):
                    return _ok()
                # Resolve sparse routes in order; attachment work runs outside the routing lock.
                async with self._inbound_routing_lock:
                    route = await self._route_inbound_message(payload, state, message_id)
                    if route is None:
                        return _ok()
                    previous, current = self._reserve_inbound_turn(route[1])
                try:
                    if previous is not None:
                        await asyncio.shield(previous)
                    return await self._deliver_inbound_message(payload, state, message_id, route)
                finally:
                    self._finish_inbound_turn(route[1], previous, current)
        finally:
            state.users -= 1
            state.touched = time.monotonic()
            task = state.retry_task
            if (state.accepted and task is not None and task is not asyncio.current_task()
                    and all(not isinstance(a, dict) or not a.get("guid") or a["guid"] in state.delivered
                            for a in _user_attachments(state.record))):
                state.retry_payload = None
                task.cancel()
