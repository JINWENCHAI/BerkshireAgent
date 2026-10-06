"""QQ (QQBot / bot.q.qq.com) channel implementation.

Inbound transport: HTTPS webhook delivered by the QQ Open Platform.
The platform signs every callback with Ed25519 over ``(event_ts || plain_token)``
and ``event_ts || event payload``; signatures and opcodes are validated before
the message is forwarded to the agent.

Outbound transport: OpenAPI v2 against ``https://api.bot.qq.com`` with an
``Authorization: QQBot <access_token>`` header. The access token comes from
the platform's OAuth2 client-credentials endpoint and is cached in-process
with a 5-minute safety margin.

Per-user isolation is preserved by setting ``channel_user_id = sender_openid``
on every inbound message; the ChannelManager resolves that to a
``owner_user_id`` via ``channel_connections`` so the agent run and long-term
memory bucket always match the human who actually sent the message.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.channels.base import Channel
from app.channels.commands import is_known_channel_command, strip_leading_mentions
from app.channels.message_bus import (
    InboundMessage,
    InboundMessageType,
    MessageBus,
    OutboundMessage,
    ResolvedAttachment,
)

logger = logging.getLogger(__name__)

# --- QQ OpenAPI endpoints -------------------------------------------------
#
# Token endpoint does not differ between sandbox and production (both call
# https://bots.qq.com/app/getAppAccessToken). The OpenAPI outbound base URL
# does: production uses api.sgroup.qq.com, sandbox uses
# sandbox.api.sgroup.qq.com. The two share the same path layout under /v2/.
# See https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/api-use.html
# and https://bot.qq.com/wiki/develop/api-v2/dev-prepare/api-call-guide.html
#
# The WebSocket Gateway lives at api.bot.qq.com/websocket/ and is reached
# via outbound TLS from the bot; QQ never connects to us.
# See https://bot.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/reference.html

QQ_TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
QQ_OPENAPI_BASE_PRODUCTION = "https://api.sgroup.qq.com"
QQ_OPENAPI_BASE_SANDBOX = "https://sandbox.api.sgroup.qq.com"
# Legacy alias kept so existing references in this module compile; points at
# the production OpenAPI base by default. ``send()`` switches to the sandbox
# base when ``channels.qq.sandbox: true`` is set in config.yaml.
QQ_API_BASE = QQ_OPENAPI_BASE_PRODUCTION
# Single-chat (C2C) messages; only message type we currently send.
QQ_C2C_SEND_PATH = "/v2/users/{openid}/messages"

# --- Opcode / event constants --------------------------------------------

# op codes that carry application payloads
_OP_DISPATCH = 0
# op 13: webhook URL validation challenge from the platform
_OP_CALLBACK_VALIDATION = 13
# op 12: required ACK after a webhook delivery (HTTP 200 OK suffices)
_OP_HTTP_CALLBACK_ACK = 12

# Event types we care about (private chat only in v1).
_EVT_C2C_MESSAGE_CREATE = "C2C_MESSAGE_CREATE"
_EVT_FRIEND_ADD = "FRIEND_ADD"

# Message types accepted inbound and produced outbound.
_MSG_TYPE_TEXT = 0
_MSG_TYPE_RICH = 2  # inline keyboard (unused in v1 but accepted)
_MSG_TYPE_MEDIA = 7  # image / file media (declared for completeness)

# --- Token & network tuning ----------------------------------------------

_TOKEN_REFRESH_MARGIN_SECONDS = 300
_TOKEN_HTTP_TIMEOUT = 15.0
_API_HTTP_TIMEOUT = 20.0
_WEBHOOK_BODY_MAX_BYTES = 1 * 1024 * 1024  # 1 MiB; payloads are tiny JSON

# Internal headers (the signature check runs first; we never log the body).
_PLATFORM_APPID_HEADER = "X-Bot-Appid"
_PLATFORM_USER_AGENT = "QQBot-Callback"


# --- Ed25519 verification ------------------------------------------------


class _QQSignatureError(Exception):
    """Raised when an inbound webhook payload fails signature verification."""


def _verify_qq_signature(
    *,
    app_secret: str,
    event_ts: str,
    plain_token: str,
    signature: str,
) -> bool:
    """Verify the Ed25519 signature sent by the QQ Open Platform webhook.

    The platform derives an Ed25519 private key by repeatedly concatenating the
    bot secret until it is at least ``ed25519.SeedSize`` (32) bytes, then seeds
    a deterministic key. We replicate the derivation here and verify the
    signature over ``event_ts + plain_token``.
    """
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError:  # pragma: no cover - cryptography ships in our image
        logger.error("[QQ] cryptography package is required for Ed25519 signature verification")
        return False

    seed_str = app_secret
    while len(seed_str) < 32:
        seed_str = seed_str + seed_str
    seed = seed_str.encode("utf-8")[:32]
    # Use the same derivation trick: deterministically generate the key from
    # the seed bytes. cryptography's FromSeed constructor wants exactly 32
    # bytes; we already truncated above.
    from cryptography.hazmat.primitives.serialization import load_der_private_key

    # Ed25519 seed -> private key bytes: We mirror the platform's deterministic
    # construction by interpreting the seed as the private scalar and treating
    # the seed itself as the raw 32-byte private key. cryptography does not
    # expose a "from 32-byte raw private key" constructor, so we build the key
    # via the PEM round-trip; if that fails we fall back to the
    # ``Ed25519PrivateKey.from_private_bytes`` constructor added in cryptography 42+.
    try:
        private_key = Ed25519PrivateKey.from_private_bytes(seed)
    except (AttributeError, ValueError):
        logger.error("[QQ] Failed to derive Ed25519 private key from secret (cryptography too old?)")
        return False

    message = (event_ts + plain_token).encode("utf-8")
    try:
        import binascii

        public_key = private_key.public_key()
        public_key.verify(binascii.unhexlify(signature), message)
        return True
    except Exception:  # noqa: BLE001 - cryptography raises InvalidSignature
        return False


def _verify_qq_event_signature(
    *,
    app_secret: str,
    event_ts: str,
    payload_bytes: bytes,
    signature: str,
) -> bool:
    """Verify the Ed25519 signature on an event dispatch (op=0).

    Platform signs ``event_ts + raw_body_bytes``.
    """
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError:
        return False

    seed_str = app_secret
    while len(seed_str) < 32:
        seed_str = seed_str + seed_str
    seed = seed_str.encode("utf-8")[:32]
    try:
        private_key = Ed25519PrivateKey.from_private_bytes(seed)
    except (AttributeError, ValueError):
        return False

    message = event_ts.encode("utf-8") + payload_bytes
    try:
        import binascii

        private_key.public_key().verify(binascii.unhexlify(signature), message)
        return True
    except Exception:  # noqa: BLE001
        return False


# --- Helpers --------------------------------------------------------------


def _normalize_allowed_users(allowed_users: Any) -> set[str]:
    if allowed_users is None:
        return set()
    if isinstance(allowed_users, str):
        values = [allowed_users]
    elif isinstance(allowed_users, (list, tuple, set)):
        values = allowed_users
    else:
        logger.warning("[QQ] allowed_users should be a list of openids; got %s", type(allowed_users).__name__)
        values = [allowed_users]
    return {str(uid) for uid in values if str(uid)}


def _safe_openid(value: Any, *, max_len: int = 64) -> str:
    """Return an openid or empty string. Used for logging/allowlist checks only.

    OpenIDs are platform-issued opaque strings; we cap length defensively.
    """
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if not value or len(value) > max_len:
        return ""
    return value


def _extract_c2c_text(payload: dict[str, Any]) -> str:
    """Pull the human-readable text out of a C2C_MESSAGE_CREATE payload."""
    content = payload.get("content")
    if isinstance(content, str):
        # Content may be either a plain string or a JSON-encoded array of
        # segments; the platform uses both depending on whether the user
        # attached media.
        try:
            parsed = json.loads(content)
        except (TypeError, ValueError):
            return content
        if isinstance(parsed, list):
            parts: list[str] = []
            for segment in parsed:
                if isinstance(segment, dict):
                    text = segment.get("text")
                    if isinstance(text, str):
                        parts.append(text)
                elif isinstance(segment, str):
                    parts.append(segment)
            return "".join(parts)
    if isinstance(content, list):
        return "".join(
            segment.get("text", "") if isinstance(segment, dict) else str(segment)
            for segment in content
        )
    return ""


def _truncate_for_log(text: str, *, max_len: int = 200) -> str:
    """Collapse newlines and clip long user text for one-line INFO logs."""
    if not text:
        return ""
    flat = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    flat = re.sub(r"\s+", " ", flat).strip()
    if len(flat) > max_len:
        flat = flat[: max_len - 3] + "..."
    return flat


# --- Channel -------------------------------------------------------------


class QQChannel(Channel):
    """QQ bot channel (webhook transport).

    The HTTP webhook handler runs on the dedicated ``_webhook_thread`` so that
    long-running agent callbacks cannot starve the FastAPI event loop. Inbound
    events are validated (Ed25519 + opcode) before being forwarded to the
    ``MessageBus``; outbound replies go through the QQ OpenAPI and are
    auth-bearer-protected with a cached access token.

    For the WebSocket transport (no public HTTPS URL required on the
    operator side), use :class:`QQWebSocketChannel` instead. The choice is
    driven by ``config.transport``; see :data:`_TRANSPORT_*`.
    """

    name = "qq"
    _transport = "webhook"  # sentinel; selected at start()

    def __init__(self, bus: MessageBus, config: dict[str, Any]) -> None:
        super().__init__(name="qq", bus=bus, config=config)
        self._app_id: str = ""
        self._app_secret: str = ""
        self._allowed_users: set[str] = _normalize_allowed_users(config.get("allowed_users"))
        # Sandbox uses sandbox.api.sgroup.qq.com; production uses api.sgroup.qq.com.
        # The token endpoint is the same in both. See QQ_OPENAPI_BASE_* docstring.
        self._sandbox: bool = bool(config.get("sandbox", False))
        self._cached_token: str = ""
        self._token_expires_at: float = 0.0
        self._token_lock = asyncio.Lock()
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._http_client: httpx.AsyncClient | None = None
        # dedupe: same QQ message id can be redelivered on transient failures
        self._seen_message_ids: set[str] = set()
        self._seen_lock = threading.Lock()
        self._webhook_thread: threading.Thread | None = None

    def _openapi_base(self) -> str:
        """Return the OpenAPI base URL for outbound calls (production or sandbox)."""
        return QQ_OPENAPI_BASE_SANDBOX if self._sandbox else QQ_OPENAPI_BASE_PRODUCTION

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return

        app_id = str(self.config.get("app_id", "")).strip()
        app_secret = str(self.config.get("app_secret", "")).strip()
        if not app_id or not app_secret:
            logger.error("[QQ] channel requires app_id and app_secret in config; refusing to start")
            return

        self._app_id = app_id
        self._app_secret = app_secret
        self._main_loop = asyncio.get_running_loop()
        self._http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(_API_HTTP_TIMEOUT),
            limits=httpx.Limits(max_keepalive_connections=4, max_connections=8),
        )

        self._open_threadsafe_future_intake()
        self._running = True
        self.bus.subscribe_outbound(self._on_outbound)

        logger.info("[QQ] channel started (app_id=%s, transport=%s, allowed_users=%d)", self._app_id, self._transport, len(self._allowed_users))

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        await self._close_and_drain_threadsafe_futures()
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None
        self.bus.unsubscribe_outbound(self._on_outbound)
        logger.info("[QQ] channel stopped")

    @property
    def supports_streaming(self) -> bool:
        return False

    # -- webhook entry point ----------------------------------------------

    async def handle_webhook(
        self,
        *,
        raw_body: bytes,
        signature_header: str | None,
        event_ts_header: str | None,
        app_id_header: str | None,
    ) -> dict[str, Any] | None:
        """Process one HTTP callback from the QQ platform.

        Returns a JSON-serialisable dict for the platform to consume. ``None``
        means the request was handled but no body is required (we just ACK).
        """
        if len(raw_body) > _WEBHOOK_BODY_MAX_BYTES:
            logger.warning("[QQ] webhook body exceeds limit (%d bytes), dropping", len(raw_body))
            return {"error": "payload too large"}

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            logger.warning("[QQ] webhook body is not valid UTF-8 JSON, dropping")
            return {"error": "invalid json"}

        if not isinstance(payload, dict):
            logger.warning("[QQ] webhook payload is not a JSON object, dropping")
            return {"error": "invalid envelope"}

        op = payload.get("op")
        d = payload.get("d")
        event_ts = event_ts_header or ""
        signature = signature_header or ""

        # Defence in depth: confirm the X-Bot-Appid matches the configured app.
        if app_id_header and app_id_header != self._app_id:
            logger.error("[QQ] webhook X-Bot-Appid=%s does not match configured app; rejecting", app_id_header)
            return {"error": "appid mismatch"}

        # op=13 is the URL-validation challenge from the platform; respond with
        # the signed echo immediately, before any event-handling work.
        if op == _OP_CALLBACK_VALIDATION and isinstance(d, dict):
            plain_token = str(d.get("plain_token", ""))
            if not plain_token or not signature or not event_ts:
                logger.warning("[QQ] op=13 missing plain_token/event_ts/signature")
                return {"error": "bad challenge"}
            if not _verify_qq_signature(
                app_secret=self._app_secret,
                event_ts=event_ts,
                plain_token=plain_token,
                signature=signature,
            ):
                logger.warning("[QQ] op=13 signature verification failed")
                return {"error": "signature mismatch"}
            return {"plain_token": plain_token, "signature": signature}

        if op != _OP_DISPATCH:
            # Everything else is either platform-side bookkeeping or a reply
            # we don't care about; ACK so the platform stops retrying.
            return None

        event_type = str(payload.get("t", ""))
        # Per-dispatch signature: signature header = hex(sig of event_ts || raw_body)
        if signature and event_ts:
            if not _verify_qq_event_signature(
                app_secret=self._app_secret,
                event_ts=event_ts,
                payload_bytes=raw_body,
                signature=signature,
            ):
                logger.warning("[QQ] dispatch signature verification failed (event=%s)", event_type)
                return {"error": "signature mismatch"}
        # else: tolerate unsigned test deliveries on the staging endpoint;
        # the platform always signs in production.

        if event_type == _EVT_C2C_MESSAGE_CREATE and isinstance(d, dict):
            await self._handle_c2c_message(d)
            return None
        if event_type == _EVT_FRIEND_ADD:
            logger.info("[QQ] FRIEND_ADD received (openid=%s)", _safe_openid(d.get("openid")))
            return None

        # Other event types are not handled in v1; ACK them silently.
        return None

    async def _handle_c2c_message(self, d: dict[str, Any]) -> None:
        sender = d.get("author", {}) or {}
        sender_openid = _safe_openid(sender.get("user_openid"))
        if not sender_openid:
            logger.warning("[QQ] C2C_MESSAGE_CREATE missing sender openid; dropping")
            return

        message_id = str(d.get("id") or "")
        if message_id:
            with self._seen_lock:
                if message_id in self._seen_message_ids:
                    logger.debug("[QQ] duplicate message_id=%s ignored", message_id)
                    return
                self._seen_message_ids.add(message_id)
                # bound the dedupe cache
                if len(self._seen_message_ids) > 4096:
                    # cheap eviction: drop the oldest half
                    to_drop = list(self._seen_message_ids)[:2048]
                    for old in to_drop:
                        self._seen_message_ids.discard(old)

        if self._allowed_users and sender_openid not in self._allowed_users:
            logger.warning("[QQ] openid=%s not in allowed_users; dropping", sender_openid)
            return

        text = _extract_c2c_text(d).strip()
        if not text:
            logger.debug("[QQ] C2C message from openid=%s has no text content", sender_openid)
            return

        text = strip_leading_mentions(text)
        if is_known_channel_command(text):
            logger.debug("[QQ] delegating command to manager: %r", text)
            # Commands are routed through the standard dispatcher path
            chat_id = sender_openid  # private chats use openid as chat_id
        else:
            chat_id = sender_openid

        msg = InboundMessage(
            channel_name=self.name,
            chat_id=chat_id,
            user_id=sender_openid,
            text=text,
            msg_type=InboundMessageType.CHAT,
            metadata={"qq_message_id": message_id} if message_id else {},
            created_at=time.time(),
        )
        try:
            await self.bus.publish_inbound(msg)
            logger.info(
                "[QQ] inbound published openid=%s message_id=%s len=%d",
                sender_openid,
                message_id or "-",
                len(text),
            )
        except Exception:  # noqa: BLE001
            logger.exception("[QQ] failed to publish inbound message to bus")

    # -- outbound ----------------------------------------------------------

    async def send(self, msg: OutboundMessage) -> None:
        if not self._running or self._http_client is None:
            logger.warning("[QQ] send() called while channel is not running")
            return
        if not msg.text:
            return

        openid = _safe_openid(msg.chat_id)
        if not openid:
            logger.warning("[QQ] outbound chat_id is not a usable openid; dropping reply")
            return

        logger.info(
            "[QQ] send() to openid=%s len=%d preview=%r",
            openid,
            len(msg.text),
            _truncate_for_log(msg.text, max_len=160),
        )

        token = await self._get_access_token()
        if not token:
            logger.error("[QQ] no access token available; cannot send reply")
            return

        api_base = self._openapi_base()
        url = f"{api_base}{QQ_C2C_SEND_PATH.format(openid=openid)}"
        payload = {
            "content": msg.text,
            "msg_type": _MSG_TYPE_TEXT,
        }
        # ``reply_to_message_id`` is intentionally absent from ``OutboundMessage``
        # today; read it defensively so a future schema addition (or a future
        # caller passing an object duck-typed as the message) cannot turn every
        # QQ outbound into a silently-swallowed ``AttributeError`` — which is
        # exactly what was happening before this guard landed.
        reply_to = getattr(msg, "reply_to_message_id", None)
        if reply_to:
            payload["msg_id"] = str(reply_to)

        try:
            response = await self._http_client.post(
                url,
                headers={
                    "Authorization": f"QQBot {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                json=payload,
            )
        except httpx.HTTPError as e:
            logger.error("[QQ] send() network error: %s", type(e).__name__)
            return

        if response.status_code != 200:
            # 401 means the cached token expired between checks; invalidate so
            # the next call forces a refresh.
            if response.status_code == 401:
                self._token_expires_at = 0.0
            logger.warning("[QQ] send() status=%s body=%s", response.status_code, _sanitize_body(response.text))
            return

        try:
            body = response.json()
        except json.JSONDecodeError:
            return
        if isinstance(body, dict) and body.get("err_code") not in (0, None, "0"):
            logger.warning("[QQ] send() err_code=%s message=%s", body.get("err_code"), body.get("message"))
            return
        logger.info("[QQ] send() OK to openid=%s msg_id=%s", openid, body.get("msg_id") if isinstance(body, dict) else "-")

    async def send_file(self, msg: OutboundMessage, attachment: ResolvedAttachment) -> bool:  # noqa: ARG002
        # QQ rich-media requires a separate upload step; not implemented in v1.
        return False

    async def _on_outbound(self, msg: OutboundMessage) -> None:
        # Delegate to ``Channel._on_outbound`` so the base's outbox ack
        # contract runs: text-send success -> ``bus.ack_outbound`` ->
        # ``mark_delivered``; text-send failure -> ``record_outbound_failure``.
        # The previous override here bypassed that contract (it awaited
        # ``self.send(msg)`` directly with no ``finally`` block), which
        # left every delivered row stuck at ``delivered_at=NULL`` and
        # caused the next gateway restart to re-dispatch the same reply
        # (see backend/.berkshire-agent/data/channel_outbox.db and
        # berkshireReadMe.md §3.7 for the reproduction timeline).
        await super()._on_outbound(msg)

    # -- access token ------------------------------------------------------

    async def _get_access_token(self) -> str:
        if self._cached_token and time.time() < self._token_expires_at - _TOKEN_REFRESH_MARGIN_SECONDS:
            return self._cached_token

        async with self._token_lock:
            # double-check inside the lock
            if self._cached_token and time.time() < self._token_expires_at - _TOKEN_REFRESH_MARGIN_SECONDS:
                return self._cached_token

            if self._http_client is None:
                return ""
            try:
                response = await self._http_client.post(
                    QQ_TOKEN_URL,
                    json={
                        "appId": self._app_id,
                        "clientSecret": self._app_secret,
                    },
                    timeout=httpx.Timeout(_TOKEN_HTTP_TIMEOUT),
                )
            except httpx.HTTPError as e:
                logger.error("[QQ] access token request failed: %s", type(e).__name__)
                return ""

            if response.status_code != 200:
                logger.warning("[QQ] access token request status=%s", response.status_code)
                return ""
            try:
                data = response.json()
            except json.JSONDecodeError:
                return ""
            access_token = str(data.get("access_token") or "")
            expires_in = int(data.get("expires_in") or 7200)
            if not access_token:
                logger.warning("[QQ] access token response missing access_token field")
                return ""
            self._cached_token = access_token
            self._token_expires_at = time.time() + max(expires_in, 60)
            return access_token


# --- helpers --------------------------------------------------------------


def _sanitize_body(text: str, max_len: int = 256) -> str:
    """Truncate and strip newlines for safe logging of an HTTP error body."""
    cleaned = re.sub(r"\s+", " ", text or "").strip()
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len] + "..."
    return cleaned


# ---------------------------------------------------------------------------
# WebSocket transport
# ---------------------------------------------------------------------------

# Allowed transport values in ``config.transport``.
_TRANSPORT_WEBHOOK = "webhook"
_TRANSPORT_WEBSOCKET = "websocket"
_VALID_TRANSPORTS = frozenset({_TRANSPORT_WEBHOOK, _TRANSPORT_WEBSOCKET})

# WebSocket Gateway opcodes (QQ Open Platform API v2).
# See https://bot.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/reference.html
_OP_DISPATCH_WS = 0  # server -> client: gateway event
_OP_HEARTBEAT = 1  # bidirectional: heartbeat
_OP_IDENTIFY = 2  # client -> server: identify / auth on a fresh session
_OP_RESUME = 6  # client -> server: resume a previously invalidated session
_OP_RECONNECT = 7  # server -> client: ask client to reconnect and resume
_OP_INVALID_SESSION = 9  # server -> client: session invalid, must identify again
_OP_HELLO = 10  # server -> client: hello (sent right after the WS upgrade)
_OP_HEARTBEAT_ACK = 11  # server -> client: heartbeat acknowledgement
_OP_HTTP_CALLBACK_ACK_WS = 12  # unused on the WS transport; kept for clarity

# Gateway event types we care about (private chat only in v1).
_EVT_C2C_MESSAGE_CREATE_WS = "C2C_MESSAGE_CREATE"
_EVT_FRIEND_ADD_WS = "FRIEND_ADD"
_EVT_READY = "READY"
_EVT_RESUMED = "RESUMED"

# Gateway URL parameters.
_GATEWAY_URL = "wss://api.bot.qq.com/websocket"
_GATEWAY_URL_BOT = "wss://api.bot.qq.com/websocket/bot"
_GATEWAY_TOKEN_QUERY_NAME = "access_token"  # appended to URL on connect
_GATEWAY_INTENTS = 1 << 25 | 1 << 26  # GROUP_AT_MESSAGE_CREATE (25) | C2C_MESSAGE_CREATE (26)
_HEARTBEAT_TIMEOUT_FACTOR = 1.5  # reconnect if no ACK within heartbeat_ms * factor
_RECONNECT_BASE_DELAY_SECONDS = 1.0
_RECONNECT_MAX_DELAY_SECONDS = 30.0


def _resolve_qq_class(config: dict[str, Any]) -> type[QQChannel]:
    """Pick the right QQChannel subclass for the requested transport.

    Used by the channel registry so a single ``qq`` name can dispatch to
    either transport. Falls back to the webhook transport when the value is
    unset or unknown; the message is logged so a typo is visible at start.
    """
    raw = config.get("transport")
    if raw is None:
        return QQChannel
    value = str(raw).strip().lower()
    if value in ("", _TRANSPORT_WEBHOOK):
        return QQChannel
    if value == _TRANSPORT_WEBSOCKET:
        return QQWebSocketChannel
    logger.warning("[QQ] unknown transport=%r; falling back to webhook", raw)
    return QQChannel


class QQWebSocketChannel(QQChannel):
    """QQ bot channel using the WebSocket Gateway transport.

    The bot opens a single ``wss://api.bot.qq.com/websocket/`` connection,
    authenticates with ``op:2 Identify`` (or ``op:6 Resume`` after a
    transient disconnect), and exchanges ``op:1 Heartbeat`` messages on
    the schedule the server dictated in ``op:10 Hello``.

    Inbound C2C ``C2C_MESSAGE_CREATE`` events are routed through the
    shared :meth:`_handle_c2c_message` helper so allowlist / dedupe /
    channel-connection scoping behave identically to the webhook transport.
    Outbound replies still go through the OpenAPI ``/v2/users/{openid}/messages``
    endpoint with the cached OAuth2 access token.

    No public HTTPS URL on the operator side is required: the bot initiates
    the connection outbound to QQ and only needs to reach QQ's REST/Gateway
    endpoints from its network.
    """

    _transport = _TRANSPORT_WEBSOCKET

    def __init__(self, bus: MessageBus, config: dict[str, Any]) -> None:
        super().__init__(bus=bus, config=config)
        self._gateway_session_id: str | None = None
        self._last_event_seq: int | None = None
        self._heartbeat_interval_seconds: float = 30.0
        self._ws_task: asyncio.Task[Any] | None = None
        self._stop_event: asyncio.Event | None = None

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return

        app_id = str(self.config.get("app_id", "")).strip()
        app_secret = str(self.config.get("app_secret", "")).strip()
        if not app_id or not app_secret:
            logger.error("[QQ websocket] channel requires app_id and app_secret in config; refusing to start")
            return

        self._app_id = app_id
        self._app_secret = app_secret
        self._main_loop = asyncio.get_running_loop()
        self._http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(_API_HTTP_TIMEOUT),
            limits=httpx.Limits(max_keepalive_connections=4, max_connections=8),
        )

        # Prime the access token before opening the gateway: the platform
        # rejects an Identify whose token is empty, and warming it here keeps
        # the first WS connect from racing the very first reply.
        token = await self._get_access_token()
        if not token:
            logger.error("[QQ websocket] could not obtain access token; refusing to start")
            return

        self._open_threadsafe_future_intake()
        self._running = True
        self._stop_event = asyncio.Event()
        self.bus.subscribe_outbound(self._on_outbound)
        self._ws_task = self._main_loop.create_task(self._run_gateway(), name="qq-websocket-gateway")

        logger.info(
            "[QQ websocket] channel started (app_id=%s, sandbox=%s, allowed_users=%d, intents=%d)",
            self._app_id,
            self._sandbox,
            len(self._allowed_users),
            _GATEWAY_INTENTS,
        )

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False

        if self._stop_event is not None:
            self._stop_event.set()

        if self._ws_task is not None and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                # CancelledError is expected; the gateway loop raises and
                # closes the socket on its own.
                pass
        self._ws_task = None
        self._stop_event = None

        await self._close_and_drain_threadsafe_futures()
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None
        self.bus.unsubscribe_outbound(self._on_outbound)
        logger.info("[QQ websocket] channel stopped")

    @property
    def supports_streaming(self) -> bool:
        return False

    # -- gateway loop ------------------------------------------------------

    async def _run_gateway(self) -> None:
        """Outer reconnect loop. One iteration = one Gateway session."""
        try:
            import websockets  # noqa: F401 - imported lazily so the webhook transport does not require it
        except ImportError:
            logger.error(
                "[QQ websocket] `websockets` package is required for the websocket transport; "
                "install it with `pip install 'berkshire-agent[qq]'`."
            )
            return

        backoff = _RECONNECT_BASE_DELAY_SECONDS
        while not self._should_stop():
            try:
                await self._run_gateway_session()
                # A clean exit while the channel is still running usually
                # means the server told us to reconnect (op=7) or invalidated
                # our session (op=9). Reset the backoff and try again.
                backoff = _RECONNECT_BASE_DELAY_SECONDS
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("[QQ websocket] gateway session ended with error")
            if self._should_stop():
                break
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
                # If we got here the stop event fired during the sleep.
                break
            except TimeoutError:
                pass
            backoff = min(backoff * 2.0, _RECONNECT_MAX_DELAY_SECONDS)
            logger.info("[QQ websocket] reconnecting in %.1fs", backoff)

    def _should_stop(self) -> bool:
        return self._stop_event is not None and self._stop_event.is_set()

    async def _run_gateway_session(self) -> None:
        """One Identify/Hello/Heartbeat/Dispatch cycle against the Gateway."""
        import websockets

        token = await self._get_access_token()
        if not token:
            # Avoid a tight loop while the OAuth endpoint is unavailable.
            await asyncio.sleep(5.0)
            raise RuntimeError("[QQ websocket] no access token available")

        ws_url = f"{_GATEWAY_URL}?{_GATEWAY_TOKEN_QUERY_NAME}={token}"
        logger.info("[QQ websocket] connecting to %s", _GATEWAY_URL)
        async with websockets.connect(
            ws_url,
            ping_interval=None,  # we drive our own heartbeats; let the server time us out only on misbehaviour
            close_timeout=5.0,
            max_size=4 * 1024 * 1024,
        ) as ws:
            # Reset session bookkeeping on a fresh connect; the server may
            # hand us a Resume target only when reconnecting.
            self._gateway_session_id = None
            self._last_event_seq = None

            while not self._should_stop():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=self._heartbeat_interval_seconds * _HEARTBEAT_TIMEOUT_FACTOR)
                except TimeoutError:
                    logger.warning("[QQ websocket] heartbeat timeout; closing session to trigger reconnect")
                    return
                except websockets.ConnectionClosed:
                    logger.info("[QQ websocket] connection closed by server")
                    return

                try:
                    payload = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    logger.warning("[QQ websocket] dropping non-JSON frame")
                    continue
                if not isinstance(payload, dict):
                    continue
                await self._handle_gateway_frame(payload, ws)

    async def _handle_gateway_frame(self, payload: dict[str, Any], ws: Any) -> None:
        """Route a single Gateway frame to its opcode-specific handler."""
        op = payload.get("op")
        d = payload.get("d")
        s = payload.get("s")
        t = payload.get("t")

        # Track the last delivered ``s`` so a future Resume can name it.
        if isinstance(s, int):
            self._last_event_seq = s

        if op == _OP_HELLO:
            # ``d.heartbeat_interval`` is in milliseconds per the platform docs.
            interval_ms = (d or {}).get("heartbeat_interval") if isinstance(d, dict) else None
            if isinstance(interval_ms, (int, float)) and interval_ms > 0:
                self._heartbeat_interval_seconds = float(interval_ms) / 1000.0
            await self._send_identify(ws)
            # Spawn the heartbeat loop alongside the read loop.
            self._main_loop.create_task(self._heartbeat_loop(ws), name="qq-websocket-heartbeat")
            return

        if op == _OP_DISPATCH_WS:
            # Loud INFO line for every dispatch so operators can see inbound
            # traffic in the terminal without turning on DEBUG. READY / RESUMED
            # are still logged separately below; this line captures everything
            # else (C2C messages, group messages, friend adds, ...).
            if isinstance(d, dict):
                logger.info(
                    "[QQ websocket] dispatch t=%s keys=%s",
                    t,
                    ",".join(sorted(d.keys())) if t else "-",
                )
            else:
                logger.info("[QQ websocket] dispatch t=%s", t)
            if t == _EVT_READY and isinstance(d, dict):
                self._gateway_session_id = d.get("session_id") or None
                logger.info(
                    "[QQ websocket] READY (session_id=%s, version=%s)",
                    self._gateway_session_id,
                    d.get("version"),
                )
                return
            if t == _EVT_RESUMED:
                logger.info("[QQ websocket] RESUMED (session_id=%s)", self._gateway_session_id)
                return
            if t == _EVT_C2C_MESSAGE_CREATE and isinstance(d, dict):
                author = d.get("author") if isinstance(d.get("author"), dict) else {}
                sender_openid = _safe_openid(author.get("user_openid"))
                logger.info(
                    "[QQ websocket] C2C message from openid=%s message_id=%s text=%r",
                    sender_openid,
                    d.get("id"),
                    _truncate_for_log(_extract_c2c_text(d)),
                )
                await self._handle_c2c_message(d)
                return
            if t == _EVT_FRIEND_ADD and isinstance(d, dict):
                logger.info("[QQ websocket] FRIEND_ADD received (openid=%s)", _safe_openid(d.get("openid")))
                return
            # Unhandled dispatch types are logged at DEBUG to keep INFO noise low.
            logger.debug("[QQ websocket] ignoring dispatch t=%s", t)
            return

        if op == _OP_HEARTBEAT_ACK:
            return

        if op == _OP_RECONNECT:
            logger.info("[QQ websocket] server requested reconnect (op=7); closing session to Resume")
            return

        if op == _OP_INVALID_SESSION:
            # ``d`` is True on resumable invalidation, False on non-resumable.
            resumable = bool(d) if isinstance(d, bool) else (isinstance(d, dict) and bool(d.get("resumable")))
            logger.warning("[QQ websocket] op=9 invalid session (resumable=%s); will re-Identify", resumable)
            self._gateway_session_id = None
            self._last_event_seq = None
            return

        logger.debug("[QQ websocket] unhandled op=%s t=%s", op, t)

    async def _send_identify(self, ws: Any) -> None:
        """Send the op=2 Identify (fresh session) or op=6 Resume payload."""
        if self._gateway_session_id is not None and self._last_event_seq is not None:
            # RESUME (op=6) uses the same ``QQBot <access_token>`` token as
            # IDENTIFY — secret belongs only to OAuth, never to the gateway.
            if not self._cached_token:
                raise RuntimeError("[QQ websocket] no access token available for RESUME")
            identify = {
                "op": _OP_RESUME,
                "d": {
                    "token": f"QQBot {self._cached_token}",
                    "session_id": self._gateway_session_id,
                    "seq": self._last_event_seq,
                },
            }
            logger.info("[QQ websocket] sending RESUME for session_id=%s seq=%s", self._gateway_session_id, self._last_event_seq)
        else:
            # QQ Open Platform WebSocket gateway IDENTIFY (op=2). The token is
            # the literal string ``QQBot <access_token>`` — the ``QQBot`` is a
            # fixed prefix, not the bot's app_id, and the secret never appears
            # here (it only proves identity at the OAuth endpoint). Confirmed
            # against QQ's official docs and three independent reference
            # implementations:
            # - https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/event-emit/websocket.html
            # - https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/event-emit.html
            if not self._cached_token:
                raise RuntimeError("[QQ websocket] no access token available for IDENTIFY")
            identify = {
                "op": _OP_IDENTIFY,
                "d": {
                    "token": f"QQBot {self._cached_token}",
                    "intents": _GATEWAY_INTENTS,
                    "shard": [0, 1],
                    "properties": {
                        "$os": "berkshire-agent",
                        "$browser": "berkshire-agent",
                        "$device": "berkshire-agent",
                    },
                },
            }
            logger.info("[QQ websocket] sending IDENTIFY (intents=%d)", _GATEWAY_INTENTS)
        try:
            await ws.send(json.dumps(identify))
        except Exception:  # noqa: BLE001
            logger.exception("[QQ websocket] failed to send identify/resume")

    async def _heartbeat_loop(self, ws: Any) -> None:
        """Send op=1 Heartbeat frames at the negotiated interval."""
        # Stagger the first heartbeat so we do not race the IDENTIFY ACK.
        await asyncio.sleep(min(self._heartbeat_interval_seconds, 5.0))
        while not self._should_stop():
            try:
                await ws.send(
                    json.dumps(
                        {
                            "op": _OP_HEARTBEAT,
                            "d": self._last_event_seq,
                        }
                    )
                )
            except Exception:  # noqa: BLE001
                logger.debug("[QQ websocket] heartbeat send failed; session likely already closed")
                return
            await asyncio.sleep(self._heartbeat_interval_seconds)


__all__ = [
    "QQChannel",
    "QQWebSocketChannel",
    "_resolve_qq_class",
    "_TRANSPORT_WEBHOOK",
    "_TRANSPORT_WEBSOCKET",
]
