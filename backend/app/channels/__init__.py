"""IM Channel integration for BerkshireAgent.

Provides a pluggable channel system that connects external messaging platforms
(Feishu/Lark, Slack, Telegram) to the BerkshireAgent agent via the ChannelManager,
which uses ``langgraph-sdk`` to communicate with Gateway's LangGraph-compatible API.
"""

from app.channels.base import Channel
from app.channels.message_bus import InboundMessage, MessageBus, OutboundMessage

__all__ = [
    "Channel",
    "InboundMessage",
    "MessageBus",
    "OutboundMessage",
]
