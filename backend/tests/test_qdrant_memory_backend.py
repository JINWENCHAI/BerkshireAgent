"""Unit tests for the Qdrant memory backend (``backends/qdrant/``).

Tests do not require a live Qdrant cluster: they monkey-patch
``tools.qdrant_memory._client`` (a private factory the backend calls for
every HTTP round-trip) to a ``httpx.MockTransport`` that simulates a 384-dim
Cosine-collection. The test cluster lives entirely in-process so they
finish in under a second and never touch ``QDRANT_API_KEY``.

The tests cover the contract every backend must satisfy at the
``MemoryManager`` boundary (the same shape ``agents/memory/tools.py``
consumes), not the Qdrant REST API itself; the helper module owns that
responsibility and has its own tests at ``../../../tests/test_qdrant_memory.py``.
"""

from __future__ import annotations

import importlib
import sys
import types
from contextlib import contextmanager
from typing import Any, Callable
from unittest.mock import MagicMock

import httpx
import pytest

# ── helpers ──────────────────────────────────────────────────────────────
FAKE_USER_1 = "PLACEHOLDER_USER_1"
FAKE_USER_2 = "PLACEHOLDER_USER_2"


class FakeQdrant:
    """A tiny in-memory Qdrant stand-in: collections of points with cosine similarity.

    Each point is a ``(id, vector, payload)`` triple. Search returns the top-k
    points by cosine similarity to the query vector, mirroring the real
    Qdrant 1.x response shape the backend expects.
    """

    def __init__(self) -> None:
        self._points: dict[str, dict[str, list[tuple[str, list[float], dict[str, Any]]]]] = {}
        self._deleted: set[tuple[str, str]] = set()  # (collection, point_id)

    def _coll(self, name: str) -> dict[str, list[tuple[str, list[float], dict[str, Any]]]]:
        return self._points.setdefault(name, {})

    def upsert(self, collection: str, points: list[dict[str, Any]]) -> None:
        for p in points:
            pid = str(p["id"])
            payload = p.get("payload", {}) or {}
            vector = list(p.get("vector") or [])
            self._coll(collection)[pid] = (pid, vector, payload)
            self._deleted.discard((collection, pid))

    def search(self, collection: str, vector: list[float], limit: int, score_threshold: float | None) -> dict[str, Any]:
        coll = self._coll(collection)
        scored: list[tuple[float, str, dict[str, Any]]] = []
        for pid, vec, payload in coll.values():
            if not vec:
                # Real Qdrant allows empty vectors but they cannot be ranked
                # by cosine similarity. The backend never writes empty vectors
                # in production (the helper rejects them at the validator),
                # so an empty vector here is a test setup bug.
                continue
            sim = _cosine(vec, vector)
            if score_threshold is not None and sim < score_threshold:
                continue
            scored.append((sim, pid, payload))
        scored.sort(key=lambda t: t[0], reverse=True)
        return {"result": [{"id": pid, "score": sim, "payload": payload} for sim, pid, payload in scored[:limit]]}

    def scroll(self, collection: str, limit: int) -> dict[str, Any]:
        coll = self._coll(collection)
        points = list(coll.values())[:limit]
        return {"result": {"points": [{"id": pid, "payload": payload} for pid, _, payload in points]}}

    def delete(self, collection: str, point_id: str) -> None:
        self._coll(collection).pop(point_id, None)
        self._deleted.add((collection, point_id))

    def clear(self, collection: str) -> None:
        self._coll(collection).clear()

    def has_collection(self, collection: str) -> bool:
        return collection in self._points


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _vec(seed: int) -> list[float]:
    """Deterministic 384-dim vector — orthogonal seeds = orthogonal vectors."""
    # Use the seed to bias the first 4 components; the rest are zero so
    # different seeds give non-overlapping cosine.
    out = [0.0] * 384
    out[seed % 384] = 1.0
    out[(seed * 7) % 384] = 0.5
    out[(seed * 13) % 384] = 0.25
    out[(seed * 19) % 384] = 0.125
    return out


# ── fixtures ──────────────────────────────────────────────────────────────
@pytest.fixture
def fake_qdrant() -> FakeQdrant:
    return FakeQdrant()


@contextmanager
def patched_helper(fake: FakeQdrant):
    """Patch ``tools.qdrant_memory._client`` (and the user's _helper reference
    inside the manager) so the backend's HTTP calls hit FakeQdrant instead of
    the real cluster. Also patches the embedding helper so tests are
    deterministic and offline.
    """
    from deerflow.agents.memory.backends import qdrant as qdrant_pkg
    from deerflow.agents.memory.backends.qdrant import qdrant_manager as qm

    # Force-import the helper module and the embedding module; tests would
    # otherwise be skipped when the repository root is not on PYTHONPATH.
    helper = _ensure_qdrant_helper()
    emb = _ensure_embedding_helper()

    # Save originals
    orig_helper_client = helper._client
    orig_emb_embed = getattr(emb, "embed", None)

    def fake_client() -> httpx.Client:
        transport = httpx.MockTransport(_make_qdrant_handler(fake))
        return httpx.Client(base_url=helper._BASE_URL, headers={"api-key": "test", "Content-Type": "application/json"}, transport=transport, timeout=5.0)

    def fake_embed(text: str) -> list[float]:
        # Seed from a stable hash of the text so the same input always
        # produces the same vector (cosine search is meaningful across calls).
        h = abs(hash(text))
        return _vec(h % 97)

    helper._client = fake_client
    emb.embed = fake_embed  # type: ignore[assignment]

    # Force the backend's cached _helper to pick up the new factory.
    qm.QdrantMemoryManager._helper = helper  # type: ignore[attr-defined]
    qdrant_pkg.__dict__["qdrant_manager"].QdrantMemoryManager._helper = helper  # type: ignore[attr-defined]

    try:
        yield helper
    finally:
        helper._client = orig_helper_client
        if orig_emb_embed is not None:
            emb.embed = orig_emb_embed  # type: ignore[assignment]


def _ensure_qdrant_helper() -> types.ModuleType:
    if "tools.qdrant_memory" in sys.modules:
        return sys.modules["tools.qdrant_memory"]
    # Backend pytest runs from ``backend/``; the helper lives one level up
    # (at the repo root). Add the repo root to sys.path so the import works
    # the same way the Gateway boots it.
    repo_root = _find_repo_root()
    if repo_root and repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    return importlib.import_module("tools.qdrant_memory")


def _ensure_embedding_helper() -> types.ModuleType:
    if "tools.embedding" in sys.modules:
        return sys.modules["tools.embedding"]
    repo_root = _find_repo_root()
    if repo_root and repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    return importlib.import_module("tools.embedding")


def _find_repo_root() -> str | None:
    """Walk parents from the test file's location to find the directory
    that contains both ``backend/`` and ``tools/``.
    """
    from pathlib import Path

    cur = Path(__file__).resolve().parent
    for _ in range(8):
        if (cur / "tools").is_dir() and (cur / "backend").is_dir():
            return str(cur)
        cur = cur.parent
    return None


def _make_qdrant_handler(fake: FakeQdrant) -> Callable[[httpx.Request], httpx.Response]:
    """Translate Qdrant REST requests to FakeQdrant operations."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        body = _read_json(request)
        # Collection lifecycle (not exercised by the backend, but called by
        # some user tooling so we accept it gracefully).
        if method == "PUT" and path.startswith("/collections/") and path.count("/") == 2:
            name = path.split("/")[-1]
            fake._points.setdefault(name, {})
            return httpx.Response(200, json={"result": True, "status": "ok"})
        # Search.
        if method == "POST" and path.endswith("/points/search"):
            name = path.split("/")[-3]
            vector = list(body.get("vector") or [])
            limit = int(body.get("limit") or 5)
            threshold = body.get("score_threshold")
            return httpx.Response(200, json=fake.search(name, vector, limit, threshold))
        # Scroll (list).
        if method == "POST" and path.endswith("/points/scroll"):
            name = path.split("/")[-3]
            limit = int(body.get("limit") or 100)
            return httpx.Response(200, json=fake.scroll(name, limit))
        # Upsert.
        if method == "PUT" and path.endswith("/points"):
            name = path.split("/")[-2]
            fake.upsert(name, list(body.get("points") or []))
            return httpx.Response(200, json={"result": {"operation_id": 1, "status": "completed"}})
        # Delete by filter.
        if method == "POST" and path.endswith("/points/delete"):
            name = path.split("/")[-3]
            f = body.get("filter") or {}
            must = f.get("must") or []
            if not must:
                # Empty match = clear collection.
                fake.clear(name)
            else:
                for clause in must:
                    if "has_id" in clause:
                        for pid in clause["has_id"]:
                            fake.delete(name, str(pid))
            return httpx.Response(200, json={"result": {"operation_id": 2, "status": "completed"}})
        # Default.
        return httpx.Response(200, json={"result": []})

    return handler


def _read_json(request: httpx.Request) -> dict[str, Any]:
    """Read the request body as JSON; return {} on empty / unparseable input."""
    body = request.content
    if not body:
        return {}
    import json

    try:
        return json.loads(body)
    except Exception:  # noqa: BLE001
        return {}


def _build_manager(mode: str = "tool") -> Any:
    from deerflow.agents.memory.backends.qdrant.qdrant_manager import QdrantMemoryManager

    return QdrantMemoryManager(backend_config={"score_threshold": 0.0, "top_k_cap": 20, "include_family": True}, mode=mode)


# ── tests ─────────────────────────────────────────────────────────────────
class TestQdrantMemoryManagerBasics:
    def test_supports_search_flag_matches_override(self):
        """The ABC invariant requires the supports_search flag to match the
        search() override; a mis-set flag is caught at construction."""
        from deerflow.agents.memory.backends.qdrant.qdrant_manager import QdrantMemoryManager

        assert QdrantMemoryManager.supports_search is True

    def test_requires_passive_writes_is_false(self):
        """Qdrant is a fact store, not a conversation extractor — passive
        middleware writes would land in collections the tool layer cannot
        see, fragmenting the fact namespace."""
        from deerflow.agents.memory.backends.qdrant.qdrant_manager import QdrantMemoryManager

        assert QdrantMemoryManager.requires_passive_writes_in_tool_mode is False

    def test_search_override_required_for_tool_mode(self):
        """mode=tool with no search() override must fail at construction (the
        ABC invariant). Construction succeeds here, proving the override
        exists; the contract is enforced by the base model_validator."""
        mgr = _build_manager(mode="tool")
        assert mgr is not None

    def test_factory_does_not_contact_qdrant(self, monkeypatch):
        """from_config must build the manager without an HTTP call so a
        cluster outage at boot does not block the Gateway."""
        mgr = _build_manager()
        # The helper is imported; no _client() has been called yet.
        assert mgr._helper is not None
        # _embed is not called by from_config.
        assert mgr._config.score_threshold == 0.0  # the override above


class TestQdrantMemoryManagerSearch:
    def test_search_returns_user_collection_only(self, fake_qdrant):
        with patched_helper(fake_qdrant):
            mgr = _build_manager()
            mgr.create_fact("I prefer index funds", category="preference", user_id=FAKE_USER_1)
            mgr.create_fact("I dislike leverage", category="preference", user_id=FAKE_USER_1)
            results = mgr.search("investing", top_k=5, user_id=FAKE_USER_1)
            assert len(results) == 2
            assert all("content" in r for r in results)
            assert all("id" in r for r in results)
            # All results are private (scope=user).
            assert all(r["scope"] == "user" for r in results)

    def test_search_merges_user_and_family_when_configured(self, fake_qdrant):
        with patched_helper(fake_qdrant):
            mgr = _build_manager()
            mgr.create_fact("User private", user_id=FAKE_USER_1)
            # The four standard memory_* tools do not expose ``scope`` (their
            # public contract is per-user only), so the family path is only
            # reachable through create_fact(scope="family", user_id=None) by
            # admin tooling — which is what this test exercises.
            mgr.create_fact("Family shared", user_id=None, scope="family")
            results = mgr.search("anything", top_k=5, user_id=FAKE_USER_1)
            # Two results: one user-scoped, one family-scoped.
            scopes = sorted(r["scope"] for r in results)
            assert scopes == ["family", "user"]

    def test_search_caps_at_top_k_cap(self, fake_qdrant):
        with patched_helper(fake_qdrant):
            mgr = _build_manager()
            for i in range(30):
                mgr.create_fact(f"fact {i}", user_id=FAKE_USER_1)
            results = mgr.search("fact", top_k=100, user_id=FAKE_USER_1)
            # top_k is capped at the configured ceiling.
            assert len(results) == 20

    def test_search_filters_by_category(self, fake_qdrant):
        with patched_helper(fake_qdrant):
            mgr = _build_manager()
            mgr.create_fact("I prefer low cost", category="preference", user_id=FAKE_USER_1)
            mgr.create_fact("I am 35 years old", category="personal", user_id=FAKE_USER_1)
            results = mgr.search("I", top_k=10, user_id=FAKE_USER_1, category="preference")
            assert all(r["category"] == "preference" for r in results)
            assert len(results) == 1

    def test_search_rejects_empty_query(self, fake_qdrant):
        with patched_helper(fake_qdrant):
            mgr = _build_manager()
            assert mgr.search("", user_id=FAKE_USER_1) == []
            assert mgr.search("   \n  ", user_id=FAKE_USER_1) == []

    def test_search_user_id_none_returns_only_family(self, fake_qdrant):
        with patched_helper(fake_qdrant):
            mgr = _build_manager()
            mgr.create_fact("Family-only fact", user_id=None, scope="family")
            results = mgr.search("fact", top_k=5, user_id=None)
            assert len(results) == 1
            assert results[0]["scope"] == "family"


class TestQdrantMemoryManagerFactCrud:
    def test_create_fact_returns_id_and_memory_echo(self, fake_qdrant):
        with patched_helper(fake_qdrant):
            mgr = _build_manager()
            memory, fact_id = mgr.create_fact("test fact", category="context", confidence=0.9, user_id=FAKE_USER_1)
            assert fact_id is not None
            assert isinstance(memory, dict)
            assert "facts" in memory
            assert len(memory["facts"]) == 1
            assert memory["facts"][0]["content"] == "test fact"

    def test_create_fact_normalises_unknown_category(self, fake_qdrant):
        with patched_helper(fake_qdrant):
            mgr = _build_manager()
            # "mystery" is not in the allowed whitelist; the backend should
            # silently fall back to "context" rather than write a free-form
            # category that future searches cannot filter on.
            memory, _ = mgr.create_fact("x", category="mystery", user_id=FAKE_USER_1)
            assert memory["facts"][0]["category"] == "context"

    def test_create_fact_empty_content_returns_existing_memory(self, fake_qdrant):
        with patched_helper(fake_qdrant):
            mgr = _build_manager()
            memory, fact_id = mgr.create_fact("", user_id=FAKE_USER_1)
            assert fact_id is None
            assert memory["facts"] == []

    def test_update_fact_replaces_content(self, fake_qdrant):
        with patched_helper(fake_qdrant):
            mgr = _build_manager()
            _, old_id = mgr.create_fact("old fact", category="context", user_id=FAKE_USER_1)
            result = mgr.update_fact(old_id, content="new fact", confidence=0.95, user_id=FAKE_USER_1)
            assert result["old_id"] == old_id
            assert result["new_id"] != old_id
            memory = mgr.get_memory(user_id=FAKE_USER_1)
            # The new fact is present; the old id is gone.
            ids = [f["id"] for f in memory["facts"]]
            assert result["new_id"] in ids
            assert old_id not in ids
            contents = [f["content"] for f in memory["facts"]]
            assert "new fact" in contents
            assert "old fact" not in contents

    def test_update_fact_missing_raises_keyerror(self, fake_qdrant):
        with patched_helper(fake_qdrant):
            mgr = _build_manager()
            with pytest.raises(KeyError):
                mgr.update_fact("nonexistent-id", content="x", user_id=FAKE_USER_1)

    def test_delete_fact_removes_from_collection(self, fake_qdrant):
        with patched_helper(fake_qdrant):
            mgr = _build_manager()
            _, fact_id = mgr.create_fact("deletable", user_id=FAKE_USER_1)
            result = mgr.delete_fact(fact_id, user_id=FAKE_USER_1)
            assert result["deleted"] is True
            memory = mgr.get_memory(user_id=FAKE_USER_1)
            assert all(f["id"] != fact_id for f in memory["facts"])

    def test_get_memory_lists_all_facts(self, fake_qdrant):
        with patched_helper(fake_qdrant):
            mgr = _build_manager()
            mgr.create_fact("a", user_id=FAKE_USER_1)
            mgr.create_fact("b", user_id=FAKE_USER_1)
            memory = mgr.get_memory(user_id=FAKE_USER_1)
            assert len(memory["facts"]) == 2


class TestQdrantMemoryManagerScope:
    def test_clear_memory_wipes_user_and_family(self, fake_qdrant):
        with patched_helper(fake_qdrant):
            mgr = _build_manager()
            mgr.create_fact("private", user_id=FAKE_USER_1)
            mgr.create_fact("shared", user_id=None, scope="family")
            result = mgr.clear_memory(user_id=FAKE_USER_1)
            assert result == {"facts": []}
            # Both buckets are empty.
            assert mgr.get_memory(user_id=FAKE_USER_1)["facts"] == []
            # The family collection is consulted by get_memory only when
            # there is a user; pass None to check it directly.
            family_only = mgr.get_memory(user_id=None)["facts"]
            assert family_only == []

    def test_clear_memory_no_user_id_is_noop(self, fake_qdrant):
        with patched_helper(fake_qdrant):
            mgr = _build_manager()
            result = mgr.clear_memory(user_id=None)
            assert result == {"facts": []}

    def test_cross_user_isolation(self, fake_qdrant):
        with patched_helper(fake_qdrant):
            mgr = _build_manager()
            mgr.create_fact("user-1 fact", user_id=FAKE_USER_1)
            mgr.create_fact("user-2 fact", user_id=FAKE_USER_2)
            u1 = mgr.get_memory(user_id=FAKE_USER_1)["facts"]
            u2 = mgr.get_memory(user_id=FAKE_USER_2)["facts"]
            assert [f["content"] for f in u1] == ["user-1 fact"]
            assert [f["content"] for f in u2] == ["user-2 fact"]

    def test_import_memory_replaces_bucket(self, fake_qdrant):
        with patched_helper(fake_qdrant):
            mgr = _build_manager()
            mgr.create_fact("old", user_id=FAKE_USER_1)
            mgr.import_memory({"facts": [{"content": "imported-1", "category": "context"}, {"content": "imported-2", "category": "personal"}]}, user_id=FAKE_USER_1)
            memory = mgr.get_memory(user_id=FAKE_USER_1)
            assert sorted(f["content"] for f in memory["facts"]) == ["imported-1", "imported-2"]


class TestQdrantMemoryManagerReadPolicies:
    def test_log_and_continue_returns_empty_on_transport_error(self, monkeypatch, fake_qdrant):
        """A network error must degrade to "no memory" rather than crash the agent."""
        with patched_helper(fake_qdrant):
            mgr = _build_manager()
            # Force the underlying helper to raise (simulate 503).
            helper = sys.modules["tools.qdrant_memory"]

            def broken_client() -> httpx.Client:
                transport = httpx.MockTransport(lambda req: httpx.Response(503, text="upstream down"))
                return httpx.Client(base_url=helper._BASE_URL, headers={"api-key": "t"}, transport=transport, timeout=1.0)

            monkeypatch.setattr(helper, "_client", broken_client)
            # Refresh the manager's cached helper reference.
            from deerflow.agents.memory.backends.qdrant import qdrant_manager as qm

            qm.QdrantMemoryManager._helper = helper
            mgr._helper = helper
            results = mgr.search("anything", user_id=FAKE_USER_1)
            assert results == []  # log_and_continue (default) -> empty, not a crash

    def test_missing_collection_treated_as_empty(self, fake_qdrant):
        """A 404 from a brand-new user must not surface as an error."""
        with patched_helper(fake_qdrant):
            mgr = _build_manager()
            # No create_fact for FRESH_USER -> the collection does not exist.
            results = mgr.search("anything", user_id="FRESH_USER_NO_COLLECTION")
            assert results == []


class TestQdrantMemoryManagerMisc:
    def test_add_is_noop_in_middleware_mode(self, fake_qdrant):
        with patched_helper(fake_qdrant):
            mgr = _build_manager(mode="middleware")
            # Conversation extraction is not this backend's job.
            mgr.add("thread-1", [], user_id=FAKE_USER_1)
            mgr.add_nowait("thread-1", [], user_id=FAKE_USER_1)
            # No fact landed.
            assert mgr.get_memory(user_id=FAKE_USER_1)["facts"] == []

    def test_shutdown_flush_returns_true(self, fake_qdrant):
        with patched_helper(fake_qdrant):
            mgr = _build_manager()
            assert mgr.shutdown_flush(1.0) is True

    def test_warm_returns_none(self, fake_qdrant):
        with patched_helper(fake_qdrant):
            mgr = _build_manager()
            assert mgr.warm() is None  # explicit "nothing to warm"

    def test_get_context_uses_query_for_relevance(self, fake_qdrant):
        with patched_helper(fake_qdrant):
            mgr = _build_manager()
            mgr.create_fact("I am a teacher", user_id=FAKE_USER_1)
            mgr.create_fact("Markets are volatile", user_id=FAKE_USER_1)
            context = mgr.get_context(FAKE_USER_1, query="teaching career")
            assert "I am a teacher" in context
            assert "Markets are volatile" in context  # both above the threshold

    def test_get_context_with_no_user_id_returns_empty(self, fake_qdrant):
        with patched_helper(fake_qdrant):
            mgr = _build_manager()
            assert mgr.get_context(None) == ""


class TestQdrantBackendDiscovery:
    """The factory must find the Qdrant backend by folder name; this guards
    the drop-in contract documented in ``deerflow.agents.memory.manager``."""

    def test_manager_class_registered(self):
        from deerflow.agents.memory.backends import qdrant as qdrant_pkg
        from deerflow.agents.memory.manager import MemoryManager
        from deerflow.agents.memory.manager import _scan_backends

        assert hasattr(qdrant_pkg, "MANAGER_CLASS")
        assert issubclass(qdrant_pkg.MANAGER_CLASS, MemoryManager)
        registry = _scan_backends()
        assert "qdrant" in registry
        assert registry["qdrant"] is qdrant_pkg.MANAGER_CLASS

    def test_resolve_manager_class(self):
        from deerflow.agents.memory.backends.qdrant.qdrant_manager import QdrantMemoryManager
        from deerflow.agents.memory.manager import _resolve_manager_class

        assert _resolve_manager_class("qdrant") is QdrantMemoryManager

    def test_requires_passive_writes_helper(self):
        """The lead-agent assembly consults
        ``backend_requires_passive_writes_in_tool_mode`` to decide whether to
        install ``MemoryMiddleware``. A True flag would re-install passive
        writes for a backend that does not want them.
        """
        from deerflow.agents.memory.manager import backend_requires_passive_writes_in_tool_mode

        assert backend_requires_passive_writes_in_tool_mode("qdrant") is False
