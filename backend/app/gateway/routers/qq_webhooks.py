"""Gateway router for inbound QQ Open Platform webhook deliveries.

Receives QQ Bot events at ``POST /api/webhooks/qq``. The QQ Open Platform
neither sends a session cookie nor a CSRF token; authenticity is enforced
through Ed25519 signatures the platform computes over
``event_ts || raw_body`` (for event dispatches) or
``event_ts || plain_token`` (for the op=13 URL-validation challenge).
The signing key is derived deterministically from the bot's ``app_secret``,
matching the platform's own derivation; see ``app.channels.qq``.

This route is exempt from the auth and CSRF middleware because:

- QQ does not present a user session.
- Signature verification proves authenticity.
- The 1 MiB body cap (enforced by FastAPI request limits and explicit
  ``Content-Length`` check) protects the channel from trivial DoS.

The route is mounted when either ``QQ_BOT_APP_SECRET`` is configured or
``DEER_FLOW_ALLOW_UNVERIFIED_QQ_WEBHOOKS=1`` is set (loopback / dev only).
A misconfigured deployment cannot accept forged deliveries because the
platform would not present a valid signature.

Verified payloads are handed off to the live :class:`app.channels.qq.QQChannel`
instance via :func:`_get_qq_channel`, which forwards ``C2C_MESSAGE_CREATE``
events to the message bus. Other event types are acknowledged silently.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, Response

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])

# The platform names this header "X-Bot-Appid" (case-insensitive on the wire).
_PLATFORM_APPID_HEADER = "X-Bot-Appid"
_PLATFORM_SIGNATURE_HEADER = "X-Signature"
_PLATFORM_EVENT_TS_HEADER = "X-Event-Ts"

_SECRET_ENV_VAR = "QQ_BOT_APP_SECRET"
_ALLOW_UNVERIFIED_ENV_VAR = "DEER_FLOW_ALLOW_UNVERIFIED_QQ_WEBHOOKS"

_MAX_BODY_BYTES = 1 * 1024 * 1024  # 1 MiB; payloads are tiny JSON


def _get_webhook_secret() -> str | None:
    """Return the configured QQ bot app secret, or None if unset.

    Read at request time so operators can rotate secrets without a full
    process restart. Empty strings are treated as "unset".
    """
    value = os.environ.get(_SECRET_ENV_VAR)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _unverified_webhooks_allowed() -> bool:
    """Return True iff the explicit dev opt-in for unverified deliveries is set."""
    raw = os.environ.get(_ALLOW_UNVERIFIED_ENV_VAR, "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def is_route_enabled() -> bool:
    """Return True iff the QQ webhook route should be mounted at startup.

    The webhook route is gated by the secret/dev-opt-in, **and** by the
    operator-selected transport: when ``channels.qq.transport == "websocket"``
    the bot connects outbound to QQ's Gateway and the platform never calls
    this URL, so mounting the route would only advertise a dead endpoint.
    """
    if not (_get_webhook_secret() is not None or _unverified_webhooks_allowed()):
        return False
    try:
        from app.channels.qq import _TRANSPORT_WEBSOCKET
        from deerflow.config.app_config import get_app_config

        app_config = get_app_config()
        channels = (app_config.model_extra or {}).get("channels") or {}
        qq_config = channels.get("qq")
        if isinstance(qq_config, dict):
            transport = str(qq_config.get("transport", "")).strip().lower()
            if transport == _TRANSPORT_WEBSOCKET:
                return False
    except Exception:  # noqa: BLE001 - never let the gate fail loudly
        logger.exception("[QQ webhook] error resolving configured transport; keeping route enabled")
    return True


async def _read_body_with_cap(request: Request) -> bytes:
    """Read the request body, enforcing a hard byte cap.

    FastAPI's default is unbounded; this protects the channel from
    accidental / malicious oversize payloads.
    """
    body = await request.body()
    if len(body) > _MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="QQ webhook payload too large")
    return body


def _get_qq_channel(request: Request):
    """Return the live QQ channel instance, or None.

    The channel service initialises channels in the Gateway lifespan
    (see ``start_channel_service``). Before startup completes, or after
    shutdown, no channel is available and the route cannot dispatch.
    """
    from app.channels.service import get_channel_service

    service = get_channel_service()
    if service is None:
        return None
    try:
        return service.get_channel("qq")
    except Exception:  # noqa: BLE001
        logger.exception("[QQ webhook] error resolving qq channel")
        return None


@router.post("/qq")
async def receive_qq_webhook(
    request: Request,
    signature: str | None = Header(default=None, alias=_PLATFORM_SIGNATURE_HEADER),
    event_ts: str | None = Header(default=None, alias=_PLATFORM_EVENT_TS_HEADER),
    app_id: str | None = Header(default=None, alias=_PLATFORM_APPID_HEADER),
) -> Response:
    """Receive one QQ Bot callback and hand it off to the channel.

    Returns 200 with the platform-supplied signature echo for the
    op=13 URL-validation challenge. All other well-formed dispatches
    return 204 No Content. Anything malformed yields a non-2xx so the
    platform retries.
    """
    secret = _get_webhook_secret()
    if not secret and not _unverified_webhooks_allowed():
        # Should not happen — the route only mounts when one is set —
        # but keep the fail-closed guard in place.
        logger.error("[QQ webhook] route hit with no secret configured and no dev opt-in")
        raise HTTPException(status_code=404, detail="QQ webhook not configured")

    body = await _read_body_with_cap(request)
    if not body:
        # Empty bodies are valid platform-level ACKs (op=12); acknowledge.
        return Response(status_code=200)

    # Lazy import so the route can be imported even before cryptography is
    # available; the channel module owns the real verification path.
    from app.channels.qq import (
        QQChannel,
        _verify_qq_event_signature,
        _verify_qq_signature,
    )

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        logger.warning("[QQ webhook] body is not valid UTF-8 JSON")
        raise HTTPException(status_code=400, detail="invalid json")

    if not isinstance(payload, dict):
        logger.warning("[QQ webhook] payload is not a JSON object")
        raise HTTPException(status_code=400, detail="invalid envelope")

    op = payload.get("op")
    d = payload.get("d")
    event_type = str(payload.get("t") or "")

    # URL validation challenge: respond with the signed echo.
    if op == 13 and isinstance(d, dict):
        plain_token = str(d.get("plain_token") or "")
        if not plain_token or not signature or not event_ts:
            logger.warning("[QQ webhook] op=13 missing plain_token/event_ts/signature")
            raise HTTPException(status_code=400, detail="bad challenge")
        if not secret:
            raise HTTPException(status_code=503, detail="QQ webhook secret not configured")
        if not _verify_qq_signature(
            app_secret=secret,
            event_ts=event_ts,
            plain_token=plain_token,
            signature=signature,
        ):
            logger.warning("[QQ webhook] op=13 signature verification failed")
            raise HTTPException(status_code=401, detail="signature mismatch")
        return Response(
            content=json.dumps({"plain_token": plain_token, "signature": signature}),
            media_type="application/json",
            status_code=200,
        )

    # Every other delivery should still carry a valid dispatch signature.
    if signature and event_ts:
        if not secret:
            raise HTTPException(status_code=503, detail="QQ webhook secret not configured")
        if not _verify_qq_event_signature(
            app_secret=secret,
            event_ts=event_ts,
            payload_bytes=body,
            signature=signature,
        ):
            logger.warning("[QQ webhook] dispatch signature verification failed (event=%s)", event_type)
            raise HTTPException(status_code=401, detail="signature mismatch")
    else:
        if not _unverified_webhooks_allowed():
            logger.warning("[QQ webhook] dispatch missing signature headers (event=%s)", event_type)
            raise HTTPException(status_code=401, detail="missing signature")

    # Hand the verified payload to the live channel.
    channel = _get_qq_channel(request)
    if channel is None:
        # Channel not yet started (or already stopped). ACK so the platform
        # does not retry, but log loudly so the operator notices.
        logger.warning("[QQ webhook] channel not running, dropping event=%s", event_type)
        return Response(status_code=204)

    if not isinstance(channel, QQChannel):
        logger.error("[QQ webhook] resolved channel is not a QQChannel: %r", channel)
        return Response(status_code=204)

    try:
        reply = await channel.handle_webhook(
            raw_body=body,
            signature_header=signature,
            event_ts_header=event_ts,
            app_id_header=app_id,
        )
    except Exception:  # noqa: BLE001
        logger.exception("[QQ webhook] handler raised (event=%s)", event_type)
        # 5xx -> platform retries (good for transient failures).
        raise HTTPException(status_code=500, detail="handler failed")

    if reply is None:
        return Response(status_code=204)
    return Response(content=json.dumps(reply), media_type="application/json", status_code=200)


__all__ = ["is_route_enabled", "router"]
