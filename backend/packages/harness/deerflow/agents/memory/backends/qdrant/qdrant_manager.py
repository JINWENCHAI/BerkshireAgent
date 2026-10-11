"""Qdrant memory backend — wraps ``tools/qdrant_memory.py`` as a ``MemoryManager``.

The agent process exposes ``tools/`` via ``DEER_FLOW_PROJECT_ROOT`` (set in
``scripts/serve.sh``) or by running from the repository root; the helper
module gives us ``ensure_collections`` / ``upsert`` / ``search`` / ``delete``
/ ``user_collection_name`` / ``FAMILY_COLLECTION`` / ``VECTOR_SIZE`` plus the
private ``_client()`` for low-level operations the public API does not
expose (list / clear). This backend adapts those primitives to the
``MemoryManager`` ABC so the four built-in tools in
``deerflow.agents.memory.tools`` (memory_search / memory_add /
memory_update / memory_delete) work against Qdrant.

Why a backend instead of writing four bespoke tools: the four tool
factories in ``agents/memory/tools.py`` are class-name-agnostic — they call
``get_memory_manager().{search, create_fact, update_fact, delete_fact,
get_memory}`` and return a JSON envelope. Swapping DeerMem for Qdrant flips
the storage without touching the tool layer. The lead agent, the buffett
sub-agent, and the munger sub-agent (which already document the standard
memory tool contract in their system prompts) all gain the same four tools
on ``manager_class: qdrant`` + ``mode: tool`` — no per-agent wiring change.

Why ``requires_passive_writes_in_tool_mode = False``: DeerMem's passive
writes are conversation extraction, not a fact store; Qdrant is a fact
store, not a conversation-extraction pipeline. With this flag False the
``MemoryMiddleware`` is not installed in tool mode, so the lead agent (and
the two persona sub-agents) is the sole writer. Passive ``add()`` from the
middleware would silently drop into a Qdrant collection that the tool
layer does not know about, fragmenting the fact namespace.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, ClassVar, Literal

from pydantic import PrivateAttr

# ABC contract — the ONE allowed ``from deerflow`` import in this backend folder.
from deerflow.agents.memory.manager import MemoryManager, MemoryManagerError, MemoryReadError

from .config import QdrantConfig

logger = logging.getLogger(__name__)


# ── helpers (private) ────────────────────────────────────────────────────
def _as_fact(point_id: str, payload: dict[str, Any], score: float | None) -> dict[str, Any]:
    """Map a Qdrant hit payload to the backend-neutral fact shape.

    Tool factories consume this shape (see ``deerflow.agents.memory.tools::
    memory_search_tool``); ``id`` / ``content`` / ``category`` / ``confidence``
    / ``createdAt`` / ``source`` are the keys the tool layer reads.

    Qdrant payload ``text`` (the helper module's convention) maps to
    ``content`` here so the tool layer sees a single canonical key.
    """
    return {
        "id": str(point_id),
        "content": str(payload.get("text", "")),
        "category": str(payload.get("category", "context")),
        "confidence": float(payload.get("confidence", 0.5) or 0.5),
        "createdAt": str(payload.get("created_at", "")),
        "source": str(payload.get("source", "qdrant")),
        "scope": str(payload.get("scope", "user")),
        "owner_user_id": payload.get("owner_user_id"),
        "score": float(score) if score is not None else None,
    }


def _now_iso() -> str:
    """UTC ISO-8601 timestamp, no microseconds (stable for equality checks)."""
    from datetime import datetime, timezone

    return datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat()


class QdrantMemoryManager(MemoryManager):
    """MemoryManager backed by the Berkshire household Qdrant collections.

    Collection layout (per ``tools/qdrant_memory.py``):
      - ``berkshire_agent_user_<user_id>``   private facts per persona bucket
      - ``berkshire_agent_family_shared``    household-shared facts

    The ``agent_name`` parameter of the ``MemoryManager`` ABC is bucketed
    inside the user's private collection as the ``agent_name`` payload field
    (so the same user may have separate ``buffett`` / ``munger`` fact
    namespaces) rather than as separate collections. The family collection
    is shared across agents.
    """

    # The search() override below is real, so the flag is True. The mode=tool
    # invariant in ``_check_invariants`` requires this to match the override.
    supports_search: ClassVar[bool] = True
    # We do NOT want the MemoryMiddleware to install passive writes under
    # mode=tool: the Qdrant backend is a fact store, not a conversation-
    # extraction pipeline. Passive writes would land in collections the tool
    # layer cannot see.
    requires_passive_writes_in_tool_mode: ClassVar[bool] = False

    _config: QdrantConfig = PrivateAttr()
    _helper: Any = PrivateAttr(default=None)  # ``tools.qdrant_memory`` module; resolved lazily.

    # ── construction ──────────────────────────────────────────────────────
    def model_post_init(self, __context: Any) -> None:
        self._config = QdrantConfig.from_backend_config(self.backend_config)
        # Defer the import until the manager is actually built so importing
        # ``deerflow.agents.memory.manager`` does not require ``tools/`` on
        # ``PYTHONPATH`` (only the resolved factory call does, and the factory
        # runs inside the gateway process where ``DEER_FLOW_PROJECT_ROOT`` is
        # already exported).
        import importlib

        try:
            self._helper = importlib.import_module("tools.qdrant_memory")
        except ImportError as exc:
            raise MemoryManagerError(
                "QdrantMemoryManager requires ``tools/qdrant_memory`` on the import path "
                "(set DEER_FLOW_PROJECT_ROOT or run from the repository root). "
                f"Underlying error: {exc}"
            ) from exc

    @classmethod
    def from_config(
        cls,
        backend_config: dict[str, Any] | None = None,
        *,
        mode: Literal["middleware", "tool"] = "middleware",
        **host_hooks: Any,
    ) -> QdrantMemoryManager:
        """Build the manager; do not contact Qdrant here.

        Qdrant contact is deferred to the first tool call so a cluster outage
        at boot does not block agent startup (memory writes are non-essential
        to serving user queries). A first-call failure surfaces as a clear
        ``MemoryReadError`` / ``MemoryManagerError`` in the tool response.
        """
        return cls(backend_config=backend_config, mode=mode)

    # ── private helpers ───────────────────────────────────────────────────
    def _ensure_helper(self) -> Any:
        if self._helper is None:
            raise MemoryManagerError("Qdrant helper module is not loaded; backend misconfiguration")
        return self._helper

    def _embed(self, text: str) -> list[float]:
        """Compute a 384-dim embedding for ``text`` via ``tools.embedding``.

        Deferred import so a missing ``sentence-transformers`` install fails
        at the first write, not at backend construction (which would crash
        every Gateway boot, not just the ones that touch memory).
        """
        from tools.embedding import embed

        return embed(text)

    def _require_user_id(self, user_id: str | None) -> str:
        if not user_id:
            raise MemoryManagerError("QdrantMemoryManager requires a non-empty user_id for the per-user collection")
        return str(user_id)

    def _resolve_search_targets(self, user_id: str | None, include_family: bool) -> list[str]:
        """List of collection names to query for a given user_id.

        A ``user_id=None`` caller (e.g. lead-agent itself with no auth) only
        sees the family collection, never another user's private points.
        """
        targets: list[str] = []
        if user_id:
            targets.append(self._helper.user_collection_name(user_id))
        if include_family and self._config.include_family:
            targets.append(self._helper.FAMILY_COLLECTION)
        return targets

    def _resolve_write_target(self, user_id: str | None, scope: str) -> str:
        """Map a (user_id, scope) pair to a Qdrant collection name.

        ``scope="user"`` requires a non-empty ``user_id``; ``scope="family"``
        writes to the shared collection regardless of ``user_id`` (the
        ``owner_user_id`` becomes None for family facts because visibility
        is enforced by the collection, not a filter).
        """
        if scope == "user":
            return self._helper.user_collection_name(self._require_user_id(user_id))
        if scope == "family":
            return self._helper.FAMILY_COLLECTION
        raise MemoryManagerError(f"QdrantMemoryManager scope must be 'user' or 'family', got {scope!r}")

    def _read_or_fallback(self, fallback: Any, fn: Any) -> Any:
        """Run ``fn``; on transport / 404 errors return ``fallback`` or raise.

        A 404 (collection does not exist yet) is treated as "no results" so
        the first user to chat in a fresh cluster does not see an error. Any
        other HTTP error follows the configured ``read_policy``.
        """
        try:
            return fn()
        except Exception as exc:  # ``tools.qdrant_memory`` raises ``RuntimeError`` on non-2xx
            message = str(exc)
            if "404" in message or "Not found" in message:
                logger.info("Qdrant read: target missing (treating as empty): %s", message)
                return fallback
            if self._config.read_policy == "log_and_continue":
                logger.warning("Qdrant read failed; returning empty: %s", message)
                return fallback
            raise MemoryReadError(f"qdrant read failed: {message}") from exc

    # ── low-level collection ops (use the helper's private client) ───────
    def _search_collection(self, target: str, embedding: list[float], limit: int) -> dict[str, Any]:
        """Search one Qdrant collection; return the raw ``{result: [...]}`` body.

        Goes through the helper's private ``_client()`` because the public
        ``search()`` strips the point id (only ``text`` / ``score`` /
        ``scope`` / ``owner_user_id`` / ``metadata`` survive the merge).
        ``memory_update`` / ``memory_delete`` need the id; this wrapper
        preserves it.
        """
        client_factory = self._helper._client
        with client_factory() as client:
            body: dict[str, Any] = {
                "vector": embedding,
                "limit": limit,
                "with_payload": True,
            }
            if self._config.score_threshold > 0:
                body["score_threshold"] = self._config.score_threshold
            resp = client.post(f"/collections/{target}/points/search", json=body)
        if resp.status_code == 404:
            # Helper convention: missing collection = no results, not an error.
            return {"result": []}
        if resp.status_code >= 400:
            raise RuntimeError(f"search failed for {target}: {resp.status_code} {resp.text[:200]}")
        return resp.json()

    def _scroll_collection(self, target: str, limit: int) -> dict[str, Any]:
        """List up to ``limit`` points in a collection via Qdrant's scroll API.

        Used by ``get_memory`` / ``clear_memory`` because the helper module
        does not expose a list operation. The scroll endpoint returns
        ``{result: {points: [...], next_page_offset: ...}}``.
        """
        client_factory = self._helper._client
        with client_factory() as client:
            resp = client.post(
                f"/collections/{target}/points/scroll",
                json={"limit": limit, "with_payload": True},
            )
        if resp.status_code == 404:
            return {"result": {"points": []}}
        if resp.status_code >= 400:
            raise RuntimeError(f"scroll failed for {target}: {resp.status_code} {resp.text[:200]}")
        return resp.json()

    def _delete_all_points(self, target: str) -> None:
        """Delete every point in ``target`` but leave the collection itself intact.

        An empty match filter (``{"must": []}``) matches every point in
        Qdrant 1.x; this is cheaper than drop+recreate because the
        collection's vector index stays warm.
        """
        client_factory = self._helper._client
        with client_factory() as client:
            resp = client.post(
                f"/collections/{target}/points/delete",
                json={"filter": {"must": []}},
            )
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise RuntimeError(f"clear failed for {target}: {resp.status_code} {resp.text[:200]}")
        return None

    # ── tier 1: write ─────────────────────────────────────────────────────
    def add(
        self,
        thread_id: str,
        messages: list[Any],
        *,
        agent_name: str | None = None,
        user_id: str | None = None,
        trace_id: str | None = None,
    ) -> None:
        """Passive-write hook (mode=middleware only).

        This backend does not support conversation extraction. When
        ``MemoryMiddleware`` is installed (``mode=middleware``) we
        deliberately no-op: Qdrant is a fact store, not an extraction
        pipeline. Tool-mode calls (the four built-in ``memory_*`` tools) do
        not go through this method.
        """
        return None

    def add_nowait(
        self,
        thread_id: str,
        messages: list[Any],
        *,
        agent_name: str | None = None,
        user_id: str | None = None,
    ) -> None:
        """See ``add``: this backend does not queue debounced writes."""
        return None

    # ── tier 1: read-inject ───────────────────────────────────────────────
    def get_context(
        self,
        user_id: str | None,
        *,
        agent_name: str | None = None,
        thread_id: str | None = None,
        query: str | None = None,
    ) -> str:
        """Return the user's most-relevant facts as injection-ready text.

        With ``query`` (the current turn's user question), we cosine-search
        the bucket and rank by score. Without ``query``, we list the bucket
        and rank by ``createdAt`` desc. The family collection is consulted
        when the operator's config allows it.
        """
        self._ensure_helper()
        if not user_id:
            # No identity → do not inject from anyone else's bucket.
            return ""
        embedding = self._embed(query or user_id)
        targets = self._resolve_search_targets(user_id, include_family=self._config.include_family)
        facts: list[dict[str, Any]] = []
        for target in targets:
            response = self._read_or_fallback(
                {"result": []},
                lambda t=target: self._search_collection(t, embedding, self._config.top_k_cap),
            )
            for hit in response.get("result", []) or []:
                facts.append(_as_fact(str(hit.get("id", "")), hit.get("payload", {}) or {}, hit.get("score")))
        if not facts:
            return ""
        # Order: cosine desc, then createdAt desc for ties.
        facts.sort(key=lambda f: ((f.get("score") or 0.0), f.get("createdAt", "")), reverse=True)
        return "\n".join(f"- {f['content']}" for f in facts if f.get("content"))

    # ── tier 2: search ────────────────────────────────────────────────────
    def search(
        self,
        query: str,
        top_k: int = 5,
        *,
        user_id: str | None = None,
        agent_name: str | None = None,
        category: str | None = None,
    ) -> list[dict[str, Any]]:
        """Cosine-search the user's private collection + (optionally) family.

        Caps ``top_k`` at the operator's ``top_k_cap`` so a wide query cannot
        blow the prompt budget. Returns the backend-neutral fact shape so the
        ``memory_search`` tool factory can pass it through unchanged.
        """
        self._ensure_helper()
        if not query or not query.strip():
            return []
        top_k = min(max(top_k, 1), self._config.top_k_cap)
        embedding = self._embed(query)
        targets = self._resolve_search_targets(user_id, include_family=self._config.include_family)
        if not targets:
            return []
        seen: dict[str, dict[str, Any]] = {}
        for target in targets:
            response = self._read_or_fallback(
                {"result": []},
                lambda t=target: self._search_collection(t, embedding, top_k),
            )
            for hit in response.get("result", []) or []:
                point_id = str(hit.get("id", ""))
                if point_id in seen:
                    continue
                seen[point_id] = _as_fact(point_id, hit.get("payload", {}) or {}, hit.get("score"))
        # Category filter AFTER the user+family union: the per-target limit
        # was already applied, so a category filter that matches zero in
        # target A but several in target B still surfaces the B matches.
        if category:
            seen = {k: v for k, v in seen.items() if v.get("category") == category}
        return sorted(
            seen.values(),
            key=lambda f: ((f.get("score") or 0.0), f.get("createdAt", "")),
            reverse=True,
        )[:top_k]

    def get_memory(
        self,
        *,
        user_id: str | None = None,
        agent_name: str | None = None,
    ) -> dict[str, Any]:
        """Return the full memory document for the bucket.

        ``get_memory`` is the read-everything path the tool factories use
        for duplicate detection (``memory_add``) and the ``/api/memory/import``
        endpoint. We list up to ``top_k_cap`` points from each in-scope
        collection and concatenate the fact shape.
        """
        self._ensure_helper()
        facts: list[dict[str, Any]] = []
        for target in self._resolve_search_targets(user_id, include_family=self._config.include_family):
            response = self._read_or_fallback(
                {"result": {"points": []}},
                lambda t=target: self._scroll_collection(t, self._config.top_k_cap),
            )
            for hit in (response.get("result", {}) or {}).get("points", []):
                facts.append(_as_fact(str(hit.get("id", "")), hit.get("payload", {}) or {}, None))
        return {"facts": facts}

    def clear_memory(
        self,
        *,
        user_id: str | None = None,
        agent_name: str | None = None,
    ) -> dict[str, Any]:
        """Delete every point in the user's private collection + family.

        ``agent_name=None`` means "the whole user bucket" (private +
        family-shared), matching the DeerMem contract documented in
        ``MemoryManager.clear_memory``. A non-empty ``user_id`` is required.
        """
        self._ensure_helper()
        if not user_id:
            return {"facts": []}
        for target in self._resolve_search_targets(user_id, include_family=True):
            try:
                self._delete_all_points(target)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Qdrant clear failed for %s: %s", target, exc)
        return {"facts": []}

    def cancel_by_agent(
        self,
        agent_name: str | None = None,
        *,
        user_id: str | None = None,
    ) -> int:
        """No debounce queue → nothing to cancel."""
        return 0

    def import_memory(
        self,
        memory_data: dict[str, Any],
        *,
        user_id: str | None = None,
        agent_name: str | None = None,
    ) -> dict[str, Any]:
        """Replace the bucket with ``memory_data['facts']``; return the result."""
        self._ensure_helper()
        if not user_id:
            return {"facts": []}
        self.clear_memory(user_id=user_id, agent_name=agent_name)
        for fact in memory_data.get("facts", []):
            content = str(fact.get("content", "")).strip()
            if not content:
                continue
            self.create_fact(
                content,
                category=str(fact.get("category", "context")),
                confidence=float(fact.get("confidence", 0.5) or 0.5),
                agent_name=agent_name,
                user_id=user_id,
            )
        return self.get_memory(user_id=user_id, agent_name=agent_name)

    def export_memory(
        self,
        *,
        user_id: str | None = None,
        agent_name: str | None = None,
    ) -> dict[str, Any]:
        return self.get_memory(user_id=user_id, agent_name=agent_name)

    # ── tier 3: hooks ─────────────────────────────────────────────────────
    def warm(self) -> bool | None:
        """No-op (Qdrant cluster warm-up happens on the first tool call).

        Returning ``None`` (the base default for "nothing to warm") keeps the
        Gateway lifespan log accurate; we deliberately do not return ``True``
        because no warm-up actually happened.
        """
        return None

    def reload_memory(
        self,
        *,
        user_id: str | None = None,
        agent_name: str | None = None,
    ) -> dict[str, Any]:
        """Qdrant is the source of truth; a reload is just a fresh list."""
        return self.get_memory(user_id=user_id, agent_name=agent_name)

    def create_fact(
        self,
        content: str,
        category: str = "context",
        confidence: float = 0.5,
        *,
        scope: str = "user",
        agent_name: str | None = None,
        user_id: str | None = None,
    ) -> tuple[dict[str, Any], str | None]:
        """Insert one fact via ``tools.qdrant_memory.MemoryPoint``.

        Returns ``(memory_data, fact_id)``; ``fact_id`` is the Qdrant-assigned
        UUID. The memory_data echo is the freshly-listed bucket so tool
        callers can show the user a "saved" confirmation. ``scope="family"``
        requires ``user_id=None`` (the helper's validator rejects a
        ``owner_user_id`` on family facts); ``scope="user"`` requires a
        non-empty ``user_id``.
        """
        self._ensure_helper()
        if scope == "user":
            uid = self._require_user_id(user_id)
            owner = uid
        elif scope == "family":
            uid = user_id  # may be None; family facts carry no owner.
            owner = None
        else:
            raise MemoryManagerError(f"QdrantMemoryManager scope must be 'user' or 'family', got {scope!r}")
        if not content or not content.strip():
            return self.get_memory(user_id=uid, agent_name=agent_name), None
        if category not in self._config.allowed_categories:
            category = "context"
        embedding = self._embed(content)
        point = self._helper.MemoryPoint(
            text=content,
            embedding=embedding,
            metadata={
                "category": category,
                "confidence": float(confidence),
                "created_at": _now_iso(),
                "source": "tool",
                "agent_name": agent_name or "",
            },
            scope=scope,
            owner_user_id=owner,
        )
        fact_id = self._helper.upsert(point)
        return self.get_memory(user_id=uid, agent_name=agent_name), fact_id

    def update_fact(
        self,
        fact_id: str,
        content: str | None = None,
        category: str | None = None,
        confidence: float | None = None,
        *,
        agent_name: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """Replace a fact by id.

        Qdrant does not support partial updates; we re-upsert with a new
        UUID, then delete the old point. The new fact_id is the one returned
        by ``upsert`` and the old id is gone after the delete.
        """
        self._ensure_helper()
        uid = self._require_user_id(user_id)
        existing = self._find_fact_by_id(fact_id, user_id=uid)
        if existing is None:
            raise KeyError(f"Fact not found: {fact_id}")
        new_content = content if content is not None else existing.get("content", "")
        new_category = category if category is not None else existing.get("category", "context")
        if new_category not in self._config.allowed_categories:
            new_category = "context"
        new_confidence = float(confidence) if confidence is not None else float(existing.get("confidence", 0.5) or 0.5)
        new_embedding = self._embed(new_content)
        new_point = self._helper.MemoryPoint(
            text=new_content,
            embedding=new_embedding,
            metadata={
                "category": new_category,
                "confidence": new_confidence,
                "created_at": _now_iso(),
                "updated_from": str(fact_id),
                "source": "tool",
                "agent_name": agent_name or "",
            },
            scope="user",
            owner_user_id=uid,
        )
        new_id = self._helper.upsert(new_point)
        # Best-effort delete of the old point; failure here is non-fatal
        # because the new point already holds the updated content.
        try:
            self._helper.delete(fact_id, user_id=uid, scope="user")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Old fact %s not deleted after update: %s", fact_id, exc)
        return {"old_id": fact_id, "new_id": new_id}

    def delete_fact(
        self,
        fact_id: str,
        *,
        agent_name: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """Delete a single fact by id from the user's private collection."""
        self._ensure_helper()
        uid = self._require_user_id(user_id)
        deleted = self._helper.delete(fact_id, user_id=uid, scope="user")
        return {"id": fact_id, "deleted": bool(deleted)}

    def _find_fact_by_id(self, fact_id: str, *, user_id: str) -> dict[str, Any] | None:
        """Linear scan to locate a fact by id (Qdrant has no by-id filter API)."""
        for fact in self.get_memory(user_id=user_id).get("facts", []):
            if str(fact.get("id", "")) == str(fact_id):
                return fact
        return None

    # ── lifecycle ─────────────────────────────────────────────────────────
    def shutdown_flush(self, timeout: float) -> bool:
        """Nothing to drain (Qdrant writes are synchronous in tool mode)."""
        return True

    def close(self) -> None:
        """Nothing to close (each call site opens its own short-lived client)."""
        return None

    # ── async placeholders (callers today use the sync path) ──────────────
    async def aadd(
        self,
        thread_id: str,
        messages: list[Any],
        *,
        agent_name: str | None = None,
        user_id: str | None = None,
        trace_id: str | None = None,
    ) -> None:
        return self.add(thread_id, messages, agent_name=agent_name, user_id=user_id, trace_id=trace_id)

    async def aget_context(
        self,
        user_id: str | None,
        *,
        agent_name: str | None = None,
        thread_id: str | None = None,
        query: str | None = None,
    ) -> str:
        return await asyncio.to_thread(
            self.get_context,
            user_id,
            agent_name=agent_name,
            thread_id=thread_id,
            query=query,
        )

    async def asearch(
        self,
        query: str,
        top_k: int = 5,
        *,
        user_id: str | None = None,
        agent_name: str | None = None,
        category: str | None = None,
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self.search,
            query,
            top_k,
            user_id=user_id,
            agent_name=agent_name,
            category=category,
        )
