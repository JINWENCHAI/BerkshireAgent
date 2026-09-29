"""Read-only Agent tool for operator-scoped Qdrant vector retrieval."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

from deerflow.config import get_app_config

from .client import QdrantAPIError, QdrantClient, QdrantConnectionError, QdrantProtocolError
from .formatting import format_search_results

logger = logging.getLogger(__name__)

_NO_RELEVANT_CONTENT = "No relevant content found."


class _QdrantRetrievalSettings(BaseModel):
    """Validated provider settings stored on the knowledge_search tool entry.

    On duplicate ``knowledge_search`` tool names (i.e. an operator configured
    both RAGFlow/LightRAG and Qdrant), BerkshireAgent keeps the first entry and
    ignores the rest. Configure exactly one knowledge_search provider.
    """

    model_config = ConfigDict(validate_default=True)

    base_url: AnyHttpUrl = Field(default="http://localhost:6333")
    api_key: SecretStr | None = Field(default=None)
    collection_name: str = Field(default="", min_length=1)
    vector_name: str | None = Field(default=None)
    query_vector: list[float] | None = Field(default=None)
    limit: int = Field(default=5, ge=1, le=1000)
    score_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    timeout: float = Field(default=30, gt=0, le=600)
    max_chars_per_chunk: int = Field(default=800, ge=1, le=100_000)
    max_total_chars: int = Field(default=8000, ge=1, le=1_000_000)

    @field_validator("base_url")
    @classmethod
    def _reject_url_userinfo(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        if value.username is not None or value.password is not None:
            raise ValueError("base_url must not contain username or password information")
        return value


def _api_key(settings: _QdrantRetrievalSettings) -> str | None:
    value = settings.api_key
    if isinstance(value, SecretStr):
        value = value.get_secret_value()
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _redact(value: object, api_key: str | None) -> str:
    text = str(value)
    if api_key:
        text = text.replace(api_key, "[REDACTED]")
    return text


def _settings_from_extra(extra: Mapping[str, object]) -> _QdrantRetrievalSettings:
    return _QdrantRetrievalSettings.model_validate(dict(extra))


def _settings_or_error() -> tuple[_QdrantRetrievalSettings | None, str | None]:
    tool_config = get_app_config().get_tool_config("knowledge_search")
    if tool_config is None:
        return None, "Error: knowledge_search is not configured; add its Qdrant settings to the tools list in config.yaml."
    try:
        settings = _settings_from_extra(tool_config.model_extra or {})
    except ValidationError:
        logger.warning("Qdrant knowledge_search tool configuration is invalid")
        return None, "Error: Invalid Qdrant settings for knowledge_search; check config.yaml."
    return settings, None


def _build_client(settings: _QdrantRetrievalSettings) -> QdrantClient:
    return QdrantClient(
        base_url=str(settings.base_url).rstrip("/"),
        api_key=_api_key(settings),
        timeout=settings.timeout,
    )


def _tool_error(exc: Exception, settings: _QdrantRetrievalSettings) -> str:
    key = _api_key(settings)
    safe_detail = _redact(exc, key)
    base_url = _redact(str(settings.base_url).rstrip("/"), key)

    if isinstance(exc, QdrantAPIError):
        logger.warning("Qdrant API rejected a read-only tool request: %s", safe_detail)
        return f"Error: {safe_detail}"
    if isinstance(exc, QdrantConnectionError):
        logger.warning("Qdrant connection failed for %s (%s)", base_url, type(exc).__name__)
        return f"Error: Unable to connect to Qdrant ({base_url}): {safe_detail}"
    if isinstance(exc, QdrantProtocolError):
        logger.warning("Qdrant returned an invalid response for a read-only tool request (%s)", type(exc).__name__)
        return f"Error: Qdrant request failed: {safe_detail}"

    logger.warning("Unexpected Qdrant read-only tool failure (%s)", type(exc).__name__)
    return "Error: An unexpected Qdrant retrieval error occurred; try again later."


async def knowledge_search(query: str) -> str:
    """Search the operator-configured Qdrant collection.

    Qdrant has no dataset catalog to scope: the operator-configured
    ``collection_name`` is always searched with the operator-configured
    ``query_vector`` (or a future query_text path). The Agent does not embed
    the query itself; BerkshireAgent's runtime is vector-agnostic, so the
    operator pre-computes or supplies the dense vector via ``query_vector``
    in config.yaml.

    The ``query`` argument is the user's natural-language question — it is
    forwarded into the formatted response so the model can correlate the
    question with each cited chunk.
    """
    question = query.strip()
    if not question:
        return "Error: query must not be empty."

    settings, error = _settings_or_error()
    if settings is None:
        return error or "Error: Invalid Qdrant settings for knowledge_search; check config.yaml."

    if settings.query_vector is None:
        return (
            "Error: Qdrant knowledge_search is configured but no query_vector is set. "
            "Provide a dense embedding vector under tools[].query_vector in config.yaml "
            "(or wire a Qdrant-side inference model and switch to query_text)."
        )

    client = _build_client(settings)
    try:
        results = await client.search(
            collection_name=settings.collection_name,
            query_vector=settings.query_vector,
            limit=settings.limit,
            score_threshold=settings.score_threshold,
        )
        formatted = format_search_results(
            results,
            max_chars_per_chunk=settings.max_chars_per_chunk,
            max_total_chars=settings.max_total_chars,
        )
        return _redact(formatted, _api_key(settings))
    except Exception as exc:
        return _tool_error(exc, settings)


async def list_knowledge_bases() -> str:
    """List the Qdrant collections visible to the configured API key.

    Returns a compact JSON list of collection names so the Agent can present
    the available knowledge bases to the user before issuing a search. Empty
    deployments return ``[]``.
    """
    settings, error = _settings_or_error()
    if settings is None:
        return error or "Error: Invalid Qdrant settings for knowledge_search; check config.yaml."

    client = _build_client(settings)
    try:
        names = await client.list_collections()
        return json.dumps({"collections": names})
    except Exception as exc:
        return _tool_error(exc, settings)


def _tool_description() -> str:
    base = (
        "Search the operator-approved Qdrant vector collection and return "
        "compact, citation-numbered source chunks retrieved with the configured dense vector."
    )
    return f"{base} Internal Qdrant point ids are never shown to the model."


async def _knowledge_search_entrypoint(query: str) -> str:
    """Search the operator-configured Qdrant collection.

    Args:
        query: Specific question or search terms the Agent is trying to answer
            from the operator's vector store. The retrieved chunks are
            formatted as numbered citations; the Agent cites them by number.
    """
    return await knowledge_search(query)


async def _list_knowledge_bases_entrypoint() -> str:
    """List the Qdrant collections visible to the configured API key.

    Returns:
        A JSON object of the form ``{"collections": ["name1", "name2", ...]}``.
    """
    return await list_knowledge_bases()


knowledge_search_tool = StructuredTool.from_function(
    coroutine=_knowledge_search_entrypoint,
    name="knowledge_search",
    description=_tool_description(),
    parse_docstring=True,
)

list_knowledge_bases_tool = StructuredTool.from_function(
    coroutine=_list_knowledge_bases_entrypoint,
    name="list_knowledge_bases",
    description=(
        "List the Qdrant collections visible to the operator-configured API key. "
        "Returns a JSON list of collection names; empty list means the deployment has no collections yet."
    ),
    parse_docstring=True,
)