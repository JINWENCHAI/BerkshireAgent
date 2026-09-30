"""Integration test for ``tools.qdrant_memory``.

Requires live Qdrant access (the cluster URL is hard-coded in the helper). Set
``SKIP_QDRANT_TESTS=1`` in CI where outbound to Qdrant Cloud is not available;
the test exits early with success in that case.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import uuid

import pytest
# Load ``tools/qdrant_memory.py`` by file path so the test does not depend on
# the repo being on ``sys.path`` (pytest's rootdir takes priority over our
# ``sys.path.insert`` once it freezes importlib state).
_TOOLS_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "tools", "qdrant_memory.py")
)
_spec = importlib.util.spec_from_file_location("qdrant_memory", _TOOLS_PATH)
if _spec is None or _spec.loader is None:  # pragma: no cover - sanity
    raise ImportError(f"could not load qdrant_memory from {_TOOLS_PATH}")
qm = importlib.util.module_from_spec(_spec)
# dataclasses introspect ``sys.modules[cls.__module__]``; without the
# registration the wrapped ``_is_type`` lookup hits ``None.__dict__``.
sys.modules["qdrant_memory"] = qm
_spec.loader.exec_module(qm)


# 384-dim unit vectors — only the first coordinate is unique so similarity
# ranks them predictably in a multi-collection search.
def _vec(value: float) -> list[float]:
    return [value] + [0.0] * (qm.VECTOR_SIZE - 1)


@pytest.fixture(scope="module", autouse=True)
def _ensure_collections():
    if os.environ.get("SKIP_QDRANT_TESTS") == "1":
        pytest.skip("SKIP_QDRANT_TESTS=1; live Qdrant unavailable")
    qm.ensure_collections()
    # Clean up any leftover test markers from previous runs that used the
    # hard-coded ``_vec(0.91)`` embedding. Without this the cluster accumulates
    # near-duplicate points and the ``startswith("user1-private marker")``
    # assertion in ``test_upsert_user_then_search_user_only`` becomes flaky
    # once the top-5 search results stop including the freshly inserted point.
    _cleanup_test_markers()
    yield


_LEGACY_MARKER_VECTORS = {
    0.91,  # user1-private marker
    0.77,  # isolation-marker
    0.66,  # family-marker
    0.55,  # private-only
}


def _legacy_first_dim(first: float) -> bool:
    """Loose match for legacy test marker vectors.

    Qdrant normalizes the inserted vectors, so ``0.91`` may come back as
    ``0.99999994`` after server-side L2 normalization. Compare within 5%.
    """
    if not isinstance(first, (int, float)):
        return False
    return any(abs(first - m) < 0.05 for m in _LEGACY_MARKER_VECTORS)

_TEST_TEXT_PREFIXES = (
    "user1-private marker",
    "isolation-marker-",
    "family-marker-",
    "private-only-",
)


def _is_test_marker(point: dict) -> bool:
    """Decide whether a cluster point is a leaked test marker.

    Identifies markers by payload-text prefix. Qdrant L2-normalizes all
    inserted vectors, so a unit vector with first-dim ``0.91`` after insert
    comes back as ``[1.0, 0.0, …, 0.0]`` after normalization — the original
    first-dim is unrecoverable from the stored vector. Payload-text prefix
    is the only reliable signal.
    """
    payload = point.get("payload") or {}
    text = payload.get("text") or ""
    return any(text.startswith(prefix) for prefix in _TEST_TEXT_PREFIXES)


@pytest.fixture(autouse=True)
def _per_test_marker_cleanup():
    """Scrub leaked test markers before AND after each test.

    The legacy tests only delete the point id they inserted, but they query
    with ``limit=5/10/20`` which can fill with **other** previously-leaked
    markers of the same hard-coded test vector. Without idempotent cleanup
    the cluster accumulates test data and top-N search assertions become
    flaky across runs.
    """
    _cleanup_test_markers()
    yield
    _cleanup_test_markers()


def _cleanup_test_markers():
    with qm._client() as client:  # type: ignore[attr-defined]
        for collection_name in [qm.user_collection_name(uid) for uid in qm.PLACEHOLDER_USERS] + [qm.FAMILY_COLLECTION]:
            resp = client.post(
                f"/collections/{collection_name}/points/scroll",
                json={"limit": 200, "with_vectors": True, "with_payload": True},
            )
            if resp.status_code != 200:
                continue
            pts = resp.json().get("result", {}).get("points", [])
            stale_ids = [str(p["id"]) for p in pts if _is_test_marker(p)]
            if stale_ids:
                client.request(
                    "DELETE",
                    f"/collections/{collection_name}/points/delete",
                    json={"points": stale_ids},
                )


def test_collections_present():
    cols = set(qm.list_collections())
    expected = {qm.user_collection_name(uid) for uid in qm.PLACEHOLDER_USERS}
    expected.add(qm.FAMILY_COLLECTION)
    missing = expected - cols
    assert not missing, f"missing collections on cluster: {missing}"


def test_upsert_user_then_search_user_only():
    user = qm.PLACEHOLDER_USERS[0]
    pid = qm.upsert(
        qm.MemoryPoint(
            text=f"user1-private marker {uuid.uuid4()}",
            embedding=_vec(0.91),
            metadata={"kind": "private"},
            scope="user",
            owner_user_id=user,
        )
    )
    try:
        # include_family=False must not surface any family points, but the
        # user collection alone should still contain our private point.
        hits = qm.search(user, _vec(0.91), limit=5, include_family=False)
        assert any(h["text"].startswith("user1-private marker") for h in hits), hits
        # All hits must belong to the queried user.
        for h in hits:
            assert h["scope"] == "user"
            assert h["owner_user_id"] == user
    finally:
        qm.delete(pid, user_id=user, scope="user")


def test_cross_user_isolation():
    """A point written for user1 must not appear in user2's search results."""
    user1 = qm.PLACEHOLDER_USERS[0]
    user2 = qm.PLACEHOLDER_USERS[1]
    unique = f"isolation-marker-{uuid.uuid4()}"
    pid = qm.upsert(
        qm.MemoryPoint(
            text=unique,
            embedding=_vec(0.77),
            metadata={},
            scope="user",
            owner_user_id=user1,
        )
    )
    try:
        hits = qm.search(user2, _vec(0.77), limit=20, include_family=False)
        assert not any(h["text"] == unique for h in hits), f"leak: user2 saw {hits}"
    finally:
        qm.delete(pid, user_id=user1, scope="user")


def test_family_scope_visible_to_all_users():
    """A family-scope point is visible to every persona's search."""
    marker = f"family-marker-{uuid.uuid4()}"
    pid = qm.upsert(
        qm.MemoryPoint(
            text=marker,
            embedding=_vec(0.66),
            metadata={},
            scope="family",
            owner_user_id=None,
        )
    )
    try:
        for user in qm.PLACEHOLDER_USERS:
            hits = qm.search(user, _vec(0.66), limit=20, include_family=True)
            assert any(h["text"] == marker and h["scope"] == "family" for h in hits), (
                f"family point missing for {user}",
            )
    finally:
        qm.delete(pid, user_id="ignored", scope="family")


def test_user_does_not_see_another_users_family_writes_via_user_collection():
    """Sanity: a user-scope write for user1 must not appear in user3's
    user-collection-only search (family scope still keeps it out by design)."""
    user1 = qm.PLACEHOLDER_USERS[0]
    user3 = qm.PLACEHOLDER_USERS[2]
    unique = f"private-only-{uuid.uuid4()}"
    pid = qm.upsert(
        qm.MemoryPoint(
            text=unique,
            embedding=_vec(0.55),
            metadata={},
            scope="user",
            owner_user_id=user1,
        )
    )
    try:
        hits = qm.search(user3, _vec(0.55), limit=20, include_family=False)
        assert not any(h["text"] == unique for h in hits)
    finally:
        qm.delete(pid, user_id=user1, scope="user")


def test_invalid_embedding_dim_rejected():
    with pytest.raises(ValueError):
        qm.upsert(
            qm.MemoryPoint(
                text="x",
                embedding=[0.0] * 100,  # wrong size
                metadata={},
                scope="user",
                owner_user_id=qm.PLACEHOLDER_USERS[0],
            )
        )


def test_invalid_scope_rejected():
    with pytest.raises(ValueError):
        qm.MemoryPoint(
            text="x",
            embedding=_vec(0.1),
            metadata={},
            scope="public",
            owner_user_id=None,
        )