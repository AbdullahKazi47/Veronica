"""
VERONICA Thread Memory V1 — persistent, extensible episodic memory.

Public surface (the rest of VERONICA imports from this package):

    ThreadMemory        the API facade (remember / recall / update / forget / search /
                        semantic_search / create_episode / consolidate / export / import)
    MemoryRecord        persisted memory unit (typed, sanitized)
    MemoryType          controlled vocabulary of memory kinds
    Episode             structured "one thing happened" record → persistable records
    MemoryStore         JSONL catalog (defense-in-depth sanitization)
    VectorStore         vector-backend interface (VERONICA never depends on a backend type)
    LocalVectorStore    zero-dependency V1 implementation
    EmbeddingProvider   embedding interface
    MemoryConsolidator  deterministic consolidation
    ThreadRetriever     deterministic weighted retrieval

Layer boundary (mandated):  Memory API → VectorStore interface → LocalVectorStore → backend.
No ChromaDB/future-backend type leaks upward from this package.
"""
from veronica.memory.thread.consolidation import MemoryConsolidator
from veronica.memory.thread.embeddings import EmbeddingProvider, LocalEmbeddingProvider, make_embedding_provider
from veronica.memory.thread.models import Episode, MemoryRecord, MemoryType, new_memory_id, now_iso
from veronica.memory.thread.privacy import sanitize_memory_record
from veronica.memory.thread.retrieval import RetrievalConfig, RetrievalQuery, RetrievalResult, ThreadRetriever
from veronica.memory.thread.serialization import (SCHEMA_NAME, SCHEMA_VERSION, export_memories,
                                                  import_thread, validate_thread_export)
from veronica.memory.thread.store import MemoryStore
from veronica.memory.thread.thread_memory import ThreadMemory
from veronica.memory.thread.vector_store import LocalVectorStore, VectorStore, create_vector_store

V1 = "1.0"

__version__ = V1
__all__ = [
    "ThreadMemory",
    "MemoryRecord",
    "MemoryType",
    "Episode",
    "MemoryStore",
    "VectorStore",
    "LocalVectorStore",
    "create_vector_store",
    "EmbeddingProvider",
    "LocalEmbeddingProvider",
    "make_embedding_provider",
    "MemoryConsolidator",
    "ThreadRetriever",
    "RetrievalConfig",
    "RetrievalQuery",
    "RetrievalResult",
    "sanitize_memory_record",
    "export_memories",
    "import_thread",
    "validate_thread_export",
    "SCHEMA_NAME",
    "SCHEMA_VERSION",
    "now_iso",
    "new_memory_id",
]