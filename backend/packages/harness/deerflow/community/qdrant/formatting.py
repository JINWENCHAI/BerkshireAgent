"""Compact, citation-friendly formatting for Qdrant search results."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _truncate(value: str, max_chars: int, *, marker: str = "…") -> str:
    if len(value) <= max_chars:
        return value
    if max_chars <= len(marker):
        return marker[:max_chars]
    return f"{value[: max_chars - len(marker)].rstrip()}{marker}"


def _points(value: object) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [point for point in value if isinstance(point, Mapping)]


def _format_payload(payload: Any) -> str:
    """Render a Qdrant point payload as compact, readable text.

    The payload structure depends entirely on what the operator stored, so we
    pick a few conventional fields and fall back to ``str(payload)``. Empty
    payloads render as ``"(no payload)"`` so the model still sees the chunk
    anchor.
    """
    if not isinstance(payload, Mapping):
        return str(payload) if payload else "(no payload)"

    for key in ("text", "content", "chunk", "page_content", "body"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value

    if payload:
        rendered = ", ".join(f"{k}={v!r}" for k, v in payload.items())
        return rendered
    return "(no payload)"


def _source_label(payload: Any, fallback_index: int) -> tuple[str, str | None]:
    """Pick an operator-readable source label and URL for a Qdrant point.

    Returns ``(label, url_or_none)``. Common payload conventions are checked
    first; anything else falls back to the point id and an opaque label so
    citations stay unique and the model never sees internal Qdrant ids.
    """
    if isinstance(payload, Mapping):
        for key in ("source", "file_path", "document", "url", "title"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                label = value.strip()
                url = payload.get("url") if key != "url" else label
                if not isinstance(url, str):
                    url = None
                return label, url
    return f"point-{fallback_index}", None


def format_search_results(
    results: list[Mapping[str, Any]],
    *,
    max_chars_per_chunk: int = 800,
    max_total_chars: int = 8000,
) -> str:
    """Format a Qdrant search payload into compact cited text.

    The Qdrant response shape is one ``result`` entry per returned point with
    ``id``, ``score``, ``payload``, and (optionally) ``vector`` fields. We
    surface the score, a source label, the payload content, and a citation
    number. Internal Qdrant point ids (numeric or UUID) are intentionally
    omitted from the citation label — operators should store a readable
    ``source`` or ``file_path`` payload field instead.
    """
    if not results:
        return "No relevant content found."

    entries: list[str] = []
    for index, point in enumerate(results, start=1):
        payload = point.get("payload")
        label, _url = _source_label(payload, index)
        score = point.get("score")
        score_text = f"{float(score):.4f}" if isinstance(score, (int, float)) else "n/a"
        content = _truncate(_format_payload(payload), max_chars_per_chunk)
        entries.append(f"[{index}] {label} (score: {score_text})\n{content}")

    formatted = "\n\n".join(entries)
    return _truncate(formatted, max_total_chars, marker="…[truncated]")