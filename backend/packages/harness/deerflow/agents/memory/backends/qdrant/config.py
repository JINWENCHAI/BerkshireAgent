"""Configuration for the Qdrant memory backend.

The Qdrant backend wraps the existing ``tools/qdrant_memory.py`` helper module
(``C:/Users/13281/Documents/BerkshireAgent/tools/qdrant_memory.py``) which
already owns Qdrant cluster URL, the TLS 1.2 quirk, the 384-dim
``bge-small-en-v1.5`` contract, and the per-user / family collection layout.
The backend only adds the host-config knobs (read policy, score threshold,
search ceiling) the operator may want to override per deployment.

Default config (no overrides) is correct for a stock Qdrant Cloud cluster
with a populated ``.env`` (``QDRANT_API_KEY`` set). The collection creation /
search / delete primitives live in the helper module; this config does not
duplicate them.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field


class QdrantConfig(BaseModel):
    """Operator-tunable knobs for the Qdrant memory backend.

    All fields are read at construction (the manager singleton is built once);
    hot reload of the ``memory.backend_config`` block is not supported because
    Qdrant cluster URL / API key changes warrant a process restart anyway.
    """

    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)

    # Search recall strictness. ``score_threshold`` is the cosine-similarity
    # floor: 0.6 mirrors the persona skill's existing default; raise it to
    # trade recall for precision, lower it to widen recall. ``top_k_cap``
    # bounds the maximum a single ``memory_search`` tool call can return so a
    # wide query cannot blow the prompt budget.
    score_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    top_k_cap: int = Field(default=20, ge=1, le=100)

    # When True (``default``), every ``memory_search`` call also pulls from the
    # family-shared collection. The persona skills expect this; set False to
    # restrict recall to the asker's private collection.
    include_family: bool = True

    # Read-failure policy. ``log_and_continue`` matches the existing helper
    # module (a missing collection is logged and skipped, not raised); flip to
    # ``fail_closed`` to surface a read error to the gateway instead of
    # silently returning empty results.
    read_policy: str = "log_and_continue"

    # What categories (e.g. ``preference`` / ``correction`` / ``context``) the
    # backend is allowed to assign when a tool caller omits a category. The
    # default whitelist matches the longterm-memory skill's existing taxonomy.
    allowed_categories: tuple[str, ...] = (
        "context",
        "preference",
        "correction",
        "personal",
        "family",
        "behavior",
        "finance",
    )

    # Backend class-level constants a backends registry may consume.
    backend_kind: ClassVar[str] = "qdrant"

    @classmethod
    def from_backend_config(cls, backend_config: dict[str, Any] | None) -> QdrantConfig:
        """Parse a raw ``memory.backend_config`` dict into a typed config.

        Accepts ``None`` and an empty dict; unknown keys are preserved (the
        helper module's own knobs - cluster URL, dim, collection prefix - may
        surface here in the future without forcing a schema bump).
        """
        return cls.model_validate(backend_config or {})
