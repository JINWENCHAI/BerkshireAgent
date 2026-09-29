"""Minimal asynchronous client for the Qdrant REST API BerkshireAgent consumes.

Qdrant Cloud exposes the same REST API as self-hosted Qdrant, so a single
HTTP client handles both. We talk to Qdrant directly without the official
``qdrant-client`` SDK because the SDK drags in gRPC dependencies that the
BerkshireAgent runtime does not need for read-only vector search.
"""

from __future__ import annotations

from typing import Any

import httpx


class QdrantError(Exception):
    """Base class for normalized Qdrant failures."""


class QdrantAPIError(QdrantError):
    """Qdrant rejected the request with a readable failure."""


class QdrantConnectionError(QdrantError):
    """Qdrant could not be reached or timed out."""


class QdrantProtocolError(QdrantError):
    """Qdrant returned an invalid or unexpected HTTP response."""


class QdrantClient:
    """Direct HTTP client for BerkshireAgent's read-only retrieval tools.

    The client deliberately owns no cache or persistent state. A fresh HTTP
    session is opened for each method call so callers do not need to manage a
    client lifecycle. The API key is sent as the ``api-key`` request header,
    the single credential form Qdrant Cloud documents.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        timeout: float = 30,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._api_key = api_key
        self._transport = transport

    def _redact(self, value: object) -> str:
        text = str(value)
        if self._api_key:
            text = text.replace(self._api_key, "[REDACTED]")
        return text

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["api-key"] = self._api_key
        return headers

    def _error_message(self, payload: object, status_code: int) -> str | None:
        """Extract a redacted, human-readable message from an error payload.

        Qdrant failures carry text in ``status`` (search/upsert envelope) or
        ``detail`` (FastAPI error handler, either a string or a list of
        validation objects whose ``msg`` holds the reason).
        """
        if not isinstance(payload, dict):
            return None
        candidate = payload.get("status", {}).get("error") if isinstance(payload.get("status"), dict) else None
        if not isinstance(candidate, str) or not candidate.strip():
            detail = payload.get("detail")
            if isinstance(detail, str):
                candidate = detail
            elif isinstance(detail, list) and detail:
                first = detail[0]
                if isinstance(first, dict):
                    candidate = first.get("msg")
        if isinstance(candidate, str) and candidate.strip():
            return self._redact(candidate.strip())
        return None

    async def search(
        self,
        *,
        collection_name: str,
        query_text: str | None = None,
        query_vector: list[float] | None = None,
        limit: int = 5,
        score_threshold: float | None = None,
        with_payload: bool = True,
        with_vectors: bool = False,
    ) -> list[dict[str, Any]]:
        """Run a single search request and return the raw ``result`` list.

        Either ``query_text`` (semantic — requires a named vector + Qdrant's
        sparse/text inference) or ``query_vector`` (raw dense vector) must be
        provided. BerkshireAgent's runtime does not embed the user's query, so
        callers are expected to supply an already-embedded vector. The
        ``query_text`` path is included for forward compatibility — operators
        may configure a Qdrant-side inference model later.

        Raises:
            QdrantConnectionError: network or timeout failure
            QdrantAPIError: Qdrant rejected the request with a readable error
            QdrantProtocolError: Qdrant returned an unexpected payload shape
        """
        if query_vector is None and query_text is None:
            raise ValueError("Either query_vector or query_text must be provided.")

        body: dict[str, Any] = {
            "limit": limit,
            "with_payload": with_payload,
            "with_vectors": with_vectors,
        }
        if query_vector is not None:
            body["vector"] = query_vector
        else:
            body["query_text"] = query_text
        if score_threshold is not None:
            body["score_threshold"] = score_threshold

        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self._transport) as http:
                response = await http.post(
                    f"{self.base_url}/collections/{collection_name}/points/search",
                    json=body,
                    headers=self._headers(),
                )
        except httpx.TimeoutException as exc:
            raise QdrantConnectionError(f"Request to Qdrant timed out after {self.timeout}s") from exc
        except httpx.HTTPError as exc:
            raise QdrantConnectionError(f"Unable to reach Qdrant: {exc!r}") from exc

        if response.status_code >= 400:
            try:
                payload = response.json()
            except ValueError:
                payload = None
            message = self._error_message(payload, response.status_code) if payload else response.text
            if message:
                raise QdrantAPIError(f"{message} (HTTP {response.status_code})")
            raise QdrantAPIError(f"Qdrant returned HTTP {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise QdrantProtocolError("Qdrant returned a non-JSON response") from exc

        result = payload.get("result")
        if not isinstance(result, list):
            raise QdrantProtocolError("Qdrant search response missing 'result' list")
        return result

    async def list_collections(self) -> list[str]:
        """Return all collection names visible to the configured API key.

        Raises:
            QdrantConnectionError: network or timeout failure
            QdrantAPIError: Qdrant rejected the request with a readable error
            QdrantProtocolError: Qdrant returned an unexpected payload shape
        """
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self._transport) as http:
                response = await http.get(
                    f"{self.base_url}/collections",
                    headers=self._headers(),
                )
        except httpx.TimeoutException as exc:
            raise QdrantConnectionError(f"Request to Qdrant timed out after {self.timeout}s") from exc
        except httpx.HTTPError as exc:
            raise QdrantConnectionError(f"Unable to reach Qdrant: {exc!r}") from exc

        if response.status_code >= 400:
            try:
                payload = response.json()
            except ValueError:
                payload = None
            message = self._error_message(payload, response.status_code) if payload else response.text
            if message:
                raise QdrantAPIError(f"{message} (HTTP {response.status_code})")
            raise QdrantAPIError(f"Qdrant returned HTTP {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise QdrantProtocolError("Qdrant returned a non-JSON response") from exc

        result = payload.get("result", {}).get("collections")
        if not isinstance(result, list):
            raise QdrantProtocolError("Qdrant collections response missing 'result.collections' list")
        names: list[str] = []
        for entry in result:
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                names.append(entry["name"])
        return names