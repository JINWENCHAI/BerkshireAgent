"""BerkshireAgent long-term memory helpers backed by Qdrant Cloud.

Architecture (Step 2 of the BerkshireAgent implementation plan)
-----------------------------------------------------------------
Three placeholder users (PLACEHOLDER_USER_1/2/3) get one private Qdrant
collection each, and the household gets one shared collection. Each persona
skill (buffett / munger) reads/writes only the collection matching the active
``user_id`` plus the family collection. Visibility is enforced by which
collection a write targets — there is no per-point filter, so a mis-targeted
write cannot leak into another user's collection.

Collection layout
-----------------
* ``berkshire_agent_user_<user_id>``       — private memory (write never visible
                                            cross-tenant; default 384-dim
                                            ``bge-small-en-v1.5`` vectors)
* ``berkshire_agent_family_shared``        — household-shared memory (one
                                            collection, all three personas
                                            read/write the same one)

The user-facing skills (``munger-persona`` / ``buffett-persona``) consume this
module through its public functions only. They never call the HTTP API
directly so the URL/key plumbing and TLS quirk live here.
"""

from __future__ import annotations

import logging
import os
import ssl
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# --- Public constants -------------------------------------------------------

DEFAULT_USER_PREFIX = "berkshire_agent_user_"
FAMILY_COLLECTION = "berkshire_agent_family_shared"
VECTOR_SIZE = 384  # bge-small-en-v1.5 default embedding dimension
DISTANCE = "Cosine"

PLACEHOLDER_USERS: tuple[str, ...] = (
    "PLACEHOLDER_USER_1",
    "PLACEHOLDER_USER_2",
    "PLACEHOLDER_USER_3",
)

# --- Internal helpers --------------------------------------------------------

_BASE_URL = "https://d1a86318-5e52-4016-9601-143a68ffd40b.eu-west-2-0.aws.cloud.qdrant.io"
_ENV_PATH = os.path.join(os.path.dirname(__file__), "..", ".env")


def _load_api_key() -> str:
    """Resolve ``QDRANT_API_KEY`` from ``os.environ`` first, then project ``.env``.

    The Gateway process loads ``.env`` itself, but plain scripts (smoke tests,
    one-off admin operations) do not. We avoid ``python-dotenv`` as a hard
    dependency and parse the two ``KEY=VALUE`` lines we need by hand.
    """
    key = os.environ.get("QDRANT_API_KEY", "").strip()
    if key:
        return key

    env_path = os.path.normpath(_ENV_PATH)
    if os.path.isfile(env_path):
        with open(env_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("QDRANT_API_KEY="):
                    return line.split("=", 1)[1].strip()
    raise RuntimeError("QDRANT_API_KEY not set; configure it in the project .env")


def _client() -> httpx.Client:
    """Build an HTTPX client pinned to TLS 1.2.

    Qdrant Cloud on eu-west-2 fails ``SSL: UNEXPECTED_EOF_WHILE_READING`` when
    httpx negotiates TLS 1.3 against the regional load balancer. Forcing
    ``PROTOCOL_TLSv1_2`` is the workaround used by ``knowledge_search_tool``'s
    smoke test in the same cluster.

    ``ssl.PROTOCOL_TLSv1_2`` triggers a ``DeprecationWarning`` on 3.12+; the
    actual negotiation works fine, so we keep the call and silence the noise
    at the ``ssl`` module level instead of touching the public context.
    """
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLSv1_2)
    api_key = _load_api_key()
    return httpx.Client(
        base_url=_BASE_URL,
        headers={"api-key": api_key, "Content-Type": "application/json"},
        timeout=30.0,
        verify=ctx,
    )


def user_collection_name(user_id: str) -> str:
    """Resolve the private Qdrant collection for a user."""
    if not user_id or not user_id.strip():
        raise ValueError("user_id must be a non-empty string")
    return f"{DEFAULT_USER_PREFIX}{user_id}"


# --- Collection lifecycle ---------------------------------------------------


def ensure_collections(user_ids: tuple[str, ...] = PLACEHOLDER_USERS) -> list[str]:
    """Create the placeholder-user collections plus the family collection.

    Safe to call repeatedly: Qdrant returns ``409 Conflict`` when the
    collection already exists, which we treat as success.
    """
    targets = [user_collection_name(uid) for uid in user_ids]
    targets.append(FAMILY_COLLECTION)
    created: list[str] = []

    with _client() as client:
        for name in targets:
            payload = {"vectors": {"size": VECTOR_SIZE, "distance": DISTANCE}}
            resp = client.put(f"/collections/{name}", json=payload)
            if resp.status_code in (200, 201, 409):
                if resp.status_code != 409:
                    created.append(name)
                logger.info("ensure_collections: %s ready (status=%s)", name, resp.status_code)
            else:
                raise RuntimeError(f"failed to create collection {name}: {resp.status_code} {resp.text[:200]}")
    return created


def list_collections() -> list[str]:
    """Return the names of all collections currently on the cluster."""
    with _client() as client:
        resp = client.get("/collections")
        resp.raise_for_status()
        body = resp.json()
    return [c["name"] for c in body.get("result", {}).get("collections", [])]


# --- Point I/O --------------------------------------------------------------


@dataclass(frozen=True)
class MemoryPoint:
    """A single memory record. ``embedding`` may be a 384-dim vector or
    ``None`` to indicate "compute the embedding at the skill layer" (out of
    scope here; the persona skills call this with a pre-computed vector)."""

    text: str
    embedding: list[float] | None
    metadata: dict[str, Any]
    scope: str  # "user" or "family"
    owner_user_id: str | None  # required when scope == "user"

    def __post_init__(self) -> None:
        if self.scope not in ("user", "family"):
            raise ValueError(f"scope must be 'user' or 'family', got {self.scope!r}")
        if self.scope == "user" and not self.owner_user_id:
            raise ValueError("scope='user' requires owner_user_id")
        # Catch a wrong-dim embedding at construction so the persona skill
        # surfaces the error before the network round-trip. Empty / None
        # embedding is allowed (skill layer can fill it in), but a non-empty
        # vector must already match VECTOR_SIZE.
        if self.embedding is not None and len(self.embedding) != 0 and len(self.embedding) != VECTOR_SIZE:
            raise ValueError(
                f"embedding must have {VECTOR_SIZE} dims (bge-small-en-v1.5), got {len(self.embedding)}"
            )


def _validate(point: MemoryPoint) -> None:
    if point.scope not in ("user", "family"):
        raise ValueError(f"scope must be 'user' or 'family', got {point.scope!r}")
    if point.scope == "user" and not point.owner_user_id:
        raise ValueError("scope='user' requires owner_user_id")
    if point.embedding is not None and len(point.embedding) != VECTOR_SIZE:
        raise ValueError(
            f"embedding must have {VECTOR_SIZE} dims (bge-small-en-v1.5), got {len(point.embedding)}"
        )


def upsert(point: MemoryPoint) -> str:
    """Insert or update a single memory point. Returns the assigned ``point_id``.

    The persona skills never see the raw Qdrant payload shape; they call
    ``upsert`` with a ``MemoryPoint`` and use the returned id for later
    deletes if needed.
    """
    _validate(point)
    target = (
        user_collection_name(point.owner_user_id or "")
        if point.scope == "user"
        else FAMILY_COLLECTION
    )
    point_id = str(uuid.uuid4())
    body = {
        "points": [
            {
                "id": point_id,
                "vector": point.embedding or [],  # empty vector allowed by Qdrant
                "payload": {
                    "text": point.text,
                    "scope": point.scope,
                    "owner_user_id": point.owner_user_id,
                    **point.metadata,
                },
            }
        ]
    }
    with _client() as client:
        resp = client.put(f"/collections/{target}/points", json=body)
        if resp.status_code >= 400:
            raise RuntimeError(f"upsert failed for {target}: {resp.status_code} {resp.text[:200]}")
    return point_id


def search(
    user_id: str,
    query_embedding: list[float],
    *,
    limit: int = 5,
    score_threshold: float | None = None,
    include_family: bool = True,
) -> list[dict[str, Any]]:
    """Search the user's private collection (and optionally the family one).

    Returns a list of result dicts with keys ``text``, ``score``, ``scope``,
    ``owner_user_id`` (when scope=user), and ``metadata``. Family hits have
    ``scope='family'`` and no ``owner_user_id``. Results are deduped by point
    id when both collections are searched.
    """
    if not user_id:
        raise ValueError("user_id is required")
    if len(query_embedding) != VECTOR_SIZE:
        raise ValueError(
            f"query_embedding must have {VECTOR_SIZE} dims, got {len(query_embedding)}"
        )

    targets = [user_collection_name(user_id)]
    if include_family:
        targets.append(FAMILY_COLLECTION)

    seen: dict[str, dict[str, Any]] = {}
    with _client() as client:
        for target in targets:
            body: dict[str, Any] = {
                "vector": query_embedding,
                "limit": limit,
                "with_payload": True,
            }
            if score_threshold is not None:
                body["score_threshold"] = score_threshold
            resp = client.post(f"/collections/{target}/points/search", json=body)
            if resp.status_code == 404:
                # A collection may not exist yet for a brand-new user; skip it
                # instead of failing the whole query.
                logger.info("search skipped missing collection %s", target)
                continue
            if resp.status_code >= 400:
                raise RuntimeError(f"search failed for {target}: {resp.status_code} {resp.text[:200]}")
            for hit in resp.json().get("result", []):
                payload = hit.get("payload", {})
                hit_id = str(hit.get("id"))
                if hit_id in seen:
                    continue
                seen[hit_id] = {
                    "text": payload.get("text", ""),
                    "score": hit.get("score"),
                    "scope": payload.get("scope"),
                    "owner_user_id": payload.get("owner_user_id"),
                    "metadata": {k: v for k, v in payload.items() if k not in {"text", "scope", "owner_user_id"}},
                }
    # Highest score first (Cosine similarity, larger = better)
    return sorted(seen.values(), key=lambda r: r["score"] or 0.0, reverse=True)


def delete(point_id: str, *, user_id: str, scope: str) -> bool:
    """Delete a point by id from the user's collection or the family one.

    Returns ``True`` if the delete call succeeded; ``False`` if the point was
    not found. Cross-tenant deletes are impossible because the target
    collection is derived from ``user_id`` + ``scope`` — a wrong pair raises
    before the network call.

    Qdrant 1.x stopped accepting the bare ``{"points": [id]}`` body shape and
    now uses the ``PointsSelector`` enum on the ``filter`` field. We issue
    ``{"filter": {"must": [{"has_id": [point_id]}]}}`` which Qdrant resolves
    to the same per-point delete. Empty filter result (point not found) is
    reported as ``False`` rather than an error.
    """
    if scope == "user":
        if not user_id:
            raise ValueError("scope='user' requires user_id")
        target = user_collection_name(user_id)
    elif scope == "family":
        target = FAMILY_COLLECTION
    else:
        raise ValueError(f"scope must be 'user' or 'family', got {scope!r}")

    body = {"filter": {"must": [{"has_id": [point_id]}]}}
    with _client() as client:
        resp = client.post(f"/collections/{target}/points/delete", json=body)
        if resp.status_code == 404:
            return False
        if resp.status_code >= 400:
            raise RuntimeError(f"delete failed for {target}: {resp.status_code} {resp.text[:200]}")
        # Qdrant returns 200 with a result object; a missing point simply
        # produces an empty deletion set, which is not an error.
    return True


# --- Smoke entry point ------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover - manual smoke
    logging.basicConfig(level=logging.INFO)
    print("Collections on cluster:")
    for name in list_collections():
        print(f"  - {name}")
    print("Ensuring placeholder collections + family collection...")
    ensure_collections()
    print("OK")