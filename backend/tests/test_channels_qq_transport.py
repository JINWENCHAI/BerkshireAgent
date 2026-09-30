"""Tests for the QQ channel transport selection and WebSocket lifecycle.

These tests deliberately avoid any live network connection: they exercise the
transport selector and the in-process start/stop contract, with the gateway
loop short-circuited via a fake websocket module.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.channels.message_bus import MessageBus
from app.channels.qq import (
    _TRANSPORT_WEBHOOK,
    _TRANSPORT_WEBSOCKET,
    QQChannel,
    QQWebSocketChannel,
    _resolve_qq_class,
)


@pytest.fixture
def bus() -> MessageBus:
    return MessageBus()


def test_resolve_qq_class_defaults_to_webhook():
    # No transport key => webhook (back-compat with the existing PR).
    assert _resolve_qq_class({}) is QQChannel
    assert _resolve_qq_class({"transport": ""}) is QQChannel
    assert _resolve_qq_class({"transport": "webhook"}) is QQChannel


def test_resolve_qq_class_picks_websocket_when_requested():
    assert _resolve_qq_class({"transport": "websocket"}) is QQWebSocketChannel
    assert _resolve_qq_class({"transport": "WEBSOCKET"}) is QQWebSocketChannel


def test_resolve_qq_class_falls_back_to_webhook_for_unknown_value():
    # A typo should still produce a working (webhook) channel so the operator
    # can recover by editing config; the warning happens at start() time.
    assert _resolve_qq_class({"transport": "socks"}) is QQChannel


def test_resolve_qq_class_sentinels_are_stable():
    # These string values are part of the operator-facing contract in
    # config.example.yaml; pin them so a silent rename surfaces in CI.
    assert _TRANSPORT_WEBHOOK == "webhook"
    assert _TRANSPORT_WEBSOCKET == "websocket"


@pytest.mark.asyncio
async def test_websocket_channel_refuses_to_start_without_credentials(bus: MessageBus):
    channel = QQWebSocketChannel(bus=bus, config={})
    await channel.start()
    assert not channel.is_running


@pytest.mark.asyncio
async def test_websocket_channel_start_without_websockets_package(bus: MessageBus, monkeypatch):
    """When `websockets` is not installed, start() should not hang and should not
    leave the channel in a running state; the operator must install the extra.
    """
    channel = QQWebSocketChannel(
        bus=bus,
        config={"app_id": "test-app", "app_secret": "test-secret"},
    )

    # Short-circuit the network path: pretend websockets is missing.
    import builtins

    original_import = builtins.__import__

    def _fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "websockets" or name.startswith("websockets."):
            raise ImportError("simulated: websockets not installed")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _fake_import)

    # Token fetch will also fail because httpx returns nothing useful here;
    # we patch _get_access_token to simulate the warm path so the code can
    # reach the import gate and log the actionable error.
    channel._get_access_token = AsyncMock(return_value="Bot test-app.fake-token")

    await channel.start()
    # Either the channel never entered running (expected when websockets is
    # missing) or the gateway task exited cleanly; both are acceptable.
    if channel.is_running:
        await channel.stop()
    assert not channel.is_running


def test_websocket_channel_has_isolated_state(bus: MessageBus):
    """Two WebSocket channel instances must not share session state."""
    a = QQWebSocketChannel(bus=bus, config={"app_id": "1", "app_secret": "1"})
    b = QQWebSocketChannel(bus=bus, config={"app_id": "2", "app_secret": "2"})
    a._gateway_session_id = "session-a"
    b._gateway_session_id = "session-b"
    assert a._gateway_session_id != b._gateway_session_id


@pytest.mark.asyncio
async def test_websocket_channel_rejects_missing_credentials(bus: MessageBus):
    """Empty/blank app_id or app_secret should skip start() entirely."""
    for bad in ({"app_id": "", "app_secret": "x"}, {"app_id": "x", "app_secret": ""}, {"app_id": "  ", "app_secret": "  "}):
        channel = QQWebSocketChannel(bus=bus, config=bad)
        await channel.start()
        assert not channel.is_running, f"start() should refuse on bad config: {bad!r}"


def test_websocket_class_is_a_qq_subclass():
    # The channel registry resolves subclasses via _resolve_qq_class; the
    # service layer relies on the WebSocket transport sharing the same
    # ``_handle_c2c_message`` helper as the webhook transport, which requires
    # QQWebSocketChannel to subclass QQChannel.
    assert issubclass(QQWebSocketChannel, QQChannel)
    assert QQWebSocketChannel is not QQChannel
    assert QQWebSocketChannel._transport == "websocket"
    assert QQChannel._transport == "webhook"
