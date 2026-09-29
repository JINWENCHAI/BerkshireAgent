"""Operator-scoped Qdrant vector retrieval for BerkshireAgent.

This package mirrors the layout of :mod:`deerflow.community.lightrag` and
:mod:`deerflow.community.ragflow` so the Qdrant integration slots into the
existing ``knowledge_search`` tool registration without touching the harness
core. The package is **not** imported at startup; it is loaded lazily by the
``deerflow.community.qdrant.tools:knowledge_search_tool`` reference that
operators add to ``config.yaml``.
"""

from __future__ import annotations

from .client import QdrantAPIError, QdrantClient, QdrantConnectionError, QdrantError, QdrantProtocolError
from .formatting import format_search_results
from .tools import knowledge_search_tool, list_knowledge_bases_tool

__all__ = [
    "QdrantAPIError",
    "QdrantClient",
    "QdrantConnectionError",
    "QdrantError",
    "QdrantProtocolError",
    "format_search_results",
    "knowledge_search_tool",
    "list_knowledge_bases_tool",
]