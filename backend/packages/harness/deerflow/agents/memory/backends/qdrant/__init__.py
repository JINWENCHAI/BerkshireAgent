"""Qdrant memory backend — vector store adapter for the household memory system.

Drop-in contract: folder name ``qdrant`` == backend name ==
``MemoryConfig.manager_class`` value. The factory
(``deerflow.agents.memory.manager._scan_backends``) discovers this package
and registers ``MANAGER_CLASS`` for the ``manager_class: qdrant`` config.

Public surface (per the noop backend's portability note):
- ``QdrantMemoryManager`` (in ``qdrant_manager.py``) — the ``MemoryManager``
  subclass wrapping ``tools.qdrant_memory`` + ``tools.embedding``.
- ``QdrantConfig`` (in ``config.py``) — operator-tunable knobs
  (``score_threshold`` / ``top_k_cap`` / ``include_family`` /
  ``read_policy`` / ``allowed_categories``).

The ABC contract ``from deerflow.agents.memory.manager import
MemoryManager`` is the ONE allowed ``deerflow`` import in this folder
(see the noop backend's portability golden rule). Everything else (the
``tools/`` helpers, embedding model, Qdrant cluster config) lives outside
the backend package so swapping Qdrant for another vector store is a single
folder copy.
"""

from .qdrant_manager import QdrantMemoryManager

#: Discovered by ``deerflow.agents.memory.manager._scan_backends`` under the
#: folder name ``qdrant``. Set ``manager_class: qdrant`` in config.yaml.
MANAGER_CLASS = QdrantMemoryManager

__all__ = ["MANAGER_CLASS", "QdrantMemoryManager"]
