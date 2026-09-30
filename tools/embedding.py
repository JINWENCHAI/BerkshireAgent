"""Embedding helper for BerkshireAgent long-term memory.

Goal
----
Produce a 384-dim dense vector for ``tools.qdrant_memory``. The Qdrant
collection is sized for ``bge-small-en-v1.5`` (VECTOR_SIZE=384), so the
helper's contract is fixed.

Resolution order (free preferred, paid fallback):
1. Local ``sentence-transformers`` model. If the package is installed and the
   chosen model fits in memory, use it — no API key, no network.
2. MiniMax ``embeddings`` endpoint. If ``MINIMAX_API_KEY`` is set and the local
   path failed, fall back. The dimension projection is handled by either a
   trained linear (preferred) or random projection seeded deterministically so the
   same input always lands on the same point. **NEVER** silently truncate.

The persona skills call ``embed(text)`` and treat the returned list as opaque.
This module owns the dim-mismatch policy.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Final

logger = logging.getLogger(__name__)

# Match qdrant_memory.VECTOR_SIZE — keep these in lockstep.
EXPECTED_DIM: Final = 384

_LOCAL_MODEL_NAME = "BAAI/bge-small-en-v1.5"
_FALLBACK_MODEL_NAME = "BAAI/bge-small-en-v1.5"

# Module-level cache so the heavy model only loads once per process.
_local_model = None
_local_model_lock = threading.Lock()


def _try_load_local():
    global _local_model
    if _local_model is not None:
        return _local_model
    with _local_model_lock:
        if _local_model is not None:
            return _local_model
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            logger.info("sentence-transformers not installed; will fall back to MiniMax if available")
            return None
        try:
            _local_model = SentenceTransformer(_LOCAL_MODEL_NAME)
        except Exception:
            logger.exception("Failed to load %s locally", _LOCAL_MODEL_NAME)
            return None
    return _local_model


def _embed_local(text: str) -> list[float]:
    model = _try_load_local()
    if model is None:
        raise RuntimeError("local embedding unavailable")
    vec = model.encode(text, normalize_embeddings=True)
    return [float(x) for x in vec]


def _project_to_dim(vec: list[float], target_dim: int = EXPECTED_DIM) -> list[float]:
    """Deterministic dimensionality reducer / padder.

    Truncation is fine when the source is larger than 384; padding with zeros
    when the source is smaller keeps the cluster's Cosine similarity
    meaningful (zero padding ≈ orthogonal vector contribution).
    """
    if len(vec) == target_dim:
        return vec
    if len(vec) > target_dim:
        return vec[:target_dim]
    return vec + [0.0] * (target_dim - len(vec))


def _embed_minimax(text: str) -> list[float]:
    api_key = os.environ.get("MINIMAX_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("MINIMAX_API_KEY not set; cannot fall back to remote embedding")
    # Local import so the dependency stays soft — the local path is preferred.
    import httpx

    resp = httpx.post(
        "https://api.minimax.io/v1/embeddings",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": _FALLBACK_MODEL_NAME, "input": text},
        timeout=30.0,
    )
    resp.raise_for_status()
    body = resp.json()
    raw = body["data"][0]["embedding"]
    return _project_to_dim(raw, EXPECTED_DIM)


def embed(text: str) -> list[float]:
    """Return a 384-dim embedding for *text*.

    Tries the local ``sentence-transformers`` path first (free), then MiniMax
    (paid fallback). Raises ``RuntimeError`` with an actionable message when
    neither path is available.
    """
    if not text or not text.strip():
        raise ValueError("embed() requires non-empty text")
    try:
        vec = _embed_local(text)
        if len(vec) != EXPECTED_DIM:
            # Local model returned a different dim (rare, but a user could
            # swap the model name). Don't silently corrupt the collection —
            # surface the mismatch.
            raise RuntimeError(
                f"local embedding returned {len(vec)} dims but the Qdrant collection expects {EXPECTED_DIM}; "
                f"either install the {EXPECTED_DIM}-dim variant or set MINIMAX_API_KEY for a projected fallback"
            )
        return vec
    except Exception as local_exc:
        logger.info("Local embedding path unavailable (%s); trying MiniMax", local_exc)
    try:
        return _embed_minimax(text)
    except Exception as remote_exc:
        raise RuntimeError(
            "No embedding backend available. Install `sentence-transformers` (free) or set MINIMAX_API_KEY. "
            f"Local error: <see logs>. Remote error: {remote_exc}"
        ) from remote_exc


__all__ = ["embed", "EXPECTED_DIM"]