"""Unit tests for ``tools.embedding``.

These exercise the projection helper and the failure surface; they do NOT
hit MiniMax. Marked ``unit`` so they can be selected by ``-m unit``.
"""

from __future__ import annotations

import importlib.util
import os
import sys

import pytest

_TOOLS_PATH = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "tools", "embedding.py"))
_spec = importlib.util.spec_from_file_location("berkshire_embedding", _TOOLS_PATH)
if _spec is None or _spec.loader is None:
    raise ImportError(f"could not load embedding helper from {_TOOLS_PATH}")
em = importlib.util.module_from_spec(_spec)
sys.modules["berkshire_embedding"] = em
_spec.loader.exec_module(em)


def test_project_to_dim_passthrough():
    vec = [0.1] * em.EXPECTED_DIM
    assert em._project_to_dim(vec) == vec


def test_project_to_dim_truncate():
    vec = [float(i) for i in range(500)]
    out = em._project_to_dim(vec)
    assert len(out) == em.EXPECTED_DIM
    assert out[:5] == [0.0, 1.0, 2.0, 3.0, 4.0]


def test_project_to_dim_pad():
    vec = [0.1, 0.2, 0.3]
    out = em._project_to_dim(vec)
    assert len(out) == em.EXPECTED_DIM
    assert out[:3] == [0.1, 0.2, 0.3]
    assert out[3:] == [0.0] * (em.EXPECTED_DIM - 3)


def test_embed_rejects_empty_text():
    with pytest.raises(ValueError):
        em.embed("")
    with pytest.raises(ValueError):
        em.embed("   \n  \t")


def test_embed_falls_back_to_remote_when_local_unavailable(monkeypatch):
    """If sentence-transformers is missing and no key set, raise a clear error."""

    # Force the local loader to fail.
    monkeypatch.setattr(em, "_try_load_local", lambda: None)

    # Also force the remote path to fail (no key in this test env unless set).
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)

    with pytest.raises(RuntimeError) as exc:
        em.embed("hello")
    assert "No embedding backend available" in str(exc.value)