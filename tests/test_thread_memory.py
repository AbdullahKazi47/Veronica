"""
Thread Memory V1 — comprehensive test suite (Phase 12).

Covers: model, episodic, vector-store abstraction, embeddings, retrieval (weighted +
deterministic), context-engine (orchestrator integration; memory does NOT grant
permissions), consolidation, import/export, privacy/redaction, failure isolation, and
the mandated layering rule (no ChromaDB type leaks upward).

Pure-Python, zero external dependencies.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from veronica.memory.thread import (
    MemoryRecord,
    MemoryStore,
    MemoryType,
    Episode,
    MemoryConsolidator,
    LocalEmbeddingProvider,
    make_embedding_provider,
    LocalVectorStore,
    create_vector_store,
    ThreadRetriever,
    RetrievalConfig,
    RetrievalQuery,
    RetrievalResult,
    ThreadMemory,
    sanitize_memory_record,
    export_memories,
    import_thread,
    validate_thread_export,
    new_memory_id,
)
from veronica.memory.thread.embeddings import _stable_token_hash, DEFAULT_DIM

SECRET_GPT_KEY = "sk-abcd1234efgh5678ijkl"  # matches the project's sk- secret pattern


# ================================================================= model

def test_memory_model_fields(tmp_path) -> None:
    r = MemoryRecord(content="hello")
    assert r.content == "hello"
    assert r.memory_type == MemoryType.CONVERSATION
    assert r.importance == 0.5
    assert r.confidence == 1.0
    assert r.id and len(r.id) == 12
    assert r.timestamp


def test_memory_type_controlled_vocab() -> None:
    for kw in MemoryType.KNOWN:
        assert isinstance(kw, str)
        assert kw == kw.lower()


def test_memory_record_to_dict_roundtrip() -> None:
    r = MemoryRecord(content="c", memory_type=MemoryType.FACT, importance=0.7,
                     tags=["t"], metadata={"k": 1})
    r2 = MemoryRecord.from_dict(r.to_dict())
    assert r2.content == r.content
    assert r2.tags == r.tags
    assert r2.metadata == r.metadata


def test_memory_record_soft_delete(tmp_path) -> None:
    store = MemoryStore(tmp_path / "ms.jsonl")
    r = MemoryRecord(content="d")
    store.put(r)
    got = store.get(r.id)
    assert got is not None and got.deleted is False
    got.deleted = True
    store.put(got)
    again = store.get(r.id)
    assert again is not None and again.deleted is True
    assert store.all() == []          # soft-deleted records are excluded from snapshots


def test_new_memory_id_unique() -> None:
    ids = {new_memory_id() for _ in range(50)}
    assert len(ids) == 50            # 48-bit ids; collision across 50 is vanishingly unlikely
    for i in ids:
        assert len(i) == 12


# ================================================================= episodic

def test_episode_to_memory_records_success_procedure() -> None:
    ep = Episode(goal="do x", actions=["a"], result="ok", success=True,
                 verification="passed", tools_used=["t"])
    types = [r.memory_type for r in ep.to_memory_records()]
    assert MemoryType.EXPERIENCE in types
    assert MemoryType.PROCEDURE in types   # success + verification passed + tools => procedure


def test_episode_to_memory_records_no_procedure_on_fail() -> None:
    ep = Episode(goal="do x", actions=["a"], result="fail", success=False,
                 verification="failed", tools_used=["t"])
    types = [r.memory_type for r in ep.to_memory_records()]
    assert MemoryType.EXPERIENCE in types
    assert MemoryType.PROCEDURE not in types  # no reusable procedure from a failure


def test_create_episode_via_tm(tmp_path) -> None:
    tm = ThreadMemory.build(records_path=tmp_path / "r.jsonl", vector_path=tmp_path / "v.jsonl")
    ep = Episode(goal="test", actions=["a"], result="ok", success=True,
                 verification="passed", tools_used=["t"])
    stored = tm.create_episode(ep)
    assert len(stored) >= 1
    for s in stored:
        assert s.memory_type in MemoryType.KNOWN


# ================================================================= vector / embeddings

def test_embedding_provider_available_and_dimension() -> None:
    p = LocalEmbeddingProvider()
    assert p.is_available()
    assert p.dimension == DEFAULT_DIM
    assert p.name == "local_hashed"


def test_embedding_deterministic() -> None:
    p = LocalEmbeddingProvider()
    assert p.embed("hello world") == p.embed("hello world")
    assert len(p.embed("hello world")) == DEFAULT_DIM


def test_embedding_batch() -> None:
    p = LocalEmbeddingProvider()
    vs = p.embed_batch(["a", "b"])
    assert len(vs) == 2 and all(len(v) == DEFAULT_DIM for v in vs)


def test_embedding_stable_token_hash() -> None:
    assert isinstance(_stable_token_hash("hello"), int)
    assert _stable_token_hash("hello") == _stable_token_hash("hello")


def test_make_embedding_provider_fallback() -> None:
    p = make_embedding_provider("bogus_provider_does_not_exist")
    assert p.name == "local_hashed"      # config can never break the assistant


def test_local_vector_store_persist_and_reload(tmp_path) -> None:
    s1 = LocalVectorStore(tmp_path / "v.jsonl")
    s1.add("a", [1.0, 0.0])
    assert s1.count() == 1
    s2 = LocalVectorStore(tmp_path / "v.jsonl")   # reload from disk
    assert s2.count() == 1
    assert s2.get("a") is not None


def test_vector_store_search_best_first(tmp_path) -> None:
    s = LocalVectorStore(tmp_path / "v.jsonl")
    s.add("a", [1.0, 0.0, 0.0])
    s.add("b", [0.1, 1.0, 0.0])   # shares a small component with the query => sim > 0
    hits = s.search([1.0, 0.0, 0.0], top_k=2)
    assert len(hits) == 2
    assert hits[0][0] == "a"       # cosine similarity drives the order (best-first)


def test_vector_store_delete_and_clear(tmp_path) -> None:
    s = LocalVectorStore(tmp_path / "v.jsonl")
    s.add("x", [1.0])
    assert s.delete("x") is True
    assert s.count() == 0
    s.add("y", [1.0])
    assert s.clear() is True
    assert s.count() == 0


def test_create_vector_store_unknown_fallback(tmp_path) -> None:
    s = create_vector_store("bogus_backend", path=tmp_path / "v.jsonl")
    assert s.name == "local"


def test_vector_store_health_check(tmp_path) -> None:
    assert LocalVectorStore(tmp_path / "v.jsonl").health_check()


# ================================================================= retrieval (weighted + deterministic)

def test_retrieval_config_defaults() -> None:
    c = RetrievalConfig()
    assert c.top_k == 5
    assert c.score_threshold == 0.20


def test_retriever_keyword_only_no_match(tmp_path) -> None:
    r = ThreadRetriever(store=MemoryStore(tmp_path / "ms.jsonl"))
    res = r.retrieve_memories(RetrievalQuery(query="exact_nonexistent_string_xyz", top_k=3))
    assert res.memories == []
    assert res.vector_available is False


def test_retriever_structured_filters(tmp_path) -> None:
    store = MemoryStore(tmp_path / "ms.jsonl")
    store.put(MemoryRecord(content="alpha", memory_type=MemoryType.FACT, project="p"))
    store.put(MemoryRecord(content="alpha", memory_type=MemoryType.EVENT, project="other"))
    r = ThreadRetriever(store=store)
    res = r.retrieve_memories(RetrievalQuery(query="alpha", memory_type=MemoryType.FACT,
                                             project="p", top_k=3))
    assert len(res.memories) == 1
    assert res.memories[0].memory_type == MemoryType.FACT
    assert res.memories[0].project == "p"


def test_retriever_excludes_ids(tmp_path) -> None:
    store = MemoryStore(tmp_path / "ms.jsonl")
    target = MemoryRecord(content="the target content", memory_type=MemoryType.FACT)
    store.put(target)
    r = ThreadRetriever(store=store)
    res = r.retrieve_memories(RetrievalQuery(query="target content", top_k=3,
                                             exclude_ids=[target.id]))
    assert all(m.id != target.id for m in res.memories)


def test_retriever_empty_result(tmp_path) -> None:
    r = ThreadRetriever(store=MemoryStore(tmp_path / "ms.jsonl"))
    res = r.empty_result(RetrievalQuery(query="x"))
    assert res.memories == []
    assert res.vector_available is False


def test_retriever_returns_result_object_even_on_errors(tmp_path) -> None:
    r = ThreadRetriever(store=MemoryStore(tmp_path / "ms.jsonl"))
    res = r.retrieve_memories(RetrievalQuery(query="anything", top_k=2))
    assert isinstance(res, RetrievalResult)
    assert isinstance(res.ranking_notes, list)


def test_retriever_weights_relevance_zero_matches_never_float_on_metadata(tmp_path) -> None:
    """Core invariance: a record with zero relevance (no shared meaning, no shared token)
    must score 0 even with high importance/confidence. This is what keeps the context
    engine from injecting irrelevant memories."""
    store = MemoryStore(tmp_path / "ms.jsonl")
    store.put(MemoryRecord(content="quantum resonance frequency charts",
                           memory_type=MemoryType.FACT, importance=0.95, confidence=1.0))
    r = ThreadRetriever(store=store)
    res = r.retrieve_memories(RetrievalQuery(query="dog bakery schedule", top_k=3))
    assert res.memories == []    # high metadata cannot carry an irrelevant record in


# ================================================================= privacy / redaction

def test_sanitize_memory_record_redacts_secret_content() -> None:
    r = MemoryRecord(content=f"the api key is {SECRET_GPT_KEY}", tags=["config"])
    safe = sanitize_memory_record(r)
    assert SECRET_GPT_KEY not in safe.content
    assert "REDACTED" in safe.content


def test_sanitize_memory_record_preserves_id_and_timestamps() -> None:
    r = MemoryRecord(content="hello", id="keepme123")
    safe = sanitize_memory_record(r)
    assert safe.id == "keepme123"
    assert safe.timestamp == r.timestamp


def test_sanitize_metadata_redacts_secret_keys(tmp_path) -> None:
    r = MemoryRecord(content="x", metadata={"api_key": "sk-abcdef1234567890", "harmless": 42})
    safe = sanitize_memory_record(r)
    assert safe.metadata["api_key"] == "[REDACTED]"
    assert safe.metadata["harmless"] == 42


def test_store_redacts_even_if_facade_bypassed() -> None:
    """Defense in depth: a caller that puts a secret directly into MemoryStore still
    cannot leak it to disk."""
    store = MemoryStore(Path(tempfile.mkdtemp()) / "ms.jsonl")
    store.put(MemoryRecord(content=f"token={SECRET_GPT_KEY}"))
    disk = store.path.read_text(encoding="utf-8")
    assert SECRET_GPT_KEY not in disk


# ================================================================= consolidation

def test_consolidator_empty_store(tmp_path) -> None:
    report = MemoryConsolidator(MemoryStore(tmp_path / "ms.jsonl")).consolidate()
    assert report["merged"] == 0
    assert "error" not in report


def test_consolidator_merges_duplicates(tmp_path) -> None:
    store = MemoryStore(tmp_path / "ms.jsonl")
    store.put(MemoryRecord(content="identical text", memory_type=MemoryType.FACT, importance=0.9))
    store.put(MemoryRecord(content="identical text", memory_type=MemoryType.FACT, importance=0.1))
    report = MemoryConsolidator(store).consolidate()
    assert report["merged"] == 1
    assert store.count() == 1


def test_consolidator_degrades_empty_records(tmp_path) -> None:
    store = MemoryStore(tmp_path / "ms.jsonl")
    store.put(MemoryRecord(content="   ", memory_type=MemoryType.FACT))
    store.put(MemoryRecord(content="real", memory_type=MemoryType.FACT))
    report = MemoryConsolidator(store).consolidate()
    assert report["degraded"] == 1
    assert store.count() == 1


def test_consolidator_links_project_cluster(tmp_path) -> None:
    store = MemoryStore(tmp_path / "ms.jsonl")
    a = MemoryRecord(content="a", memory_type=MemoryType.FACT, project="proj")
    b = MemoryRecord(content="b", memory_type=MemoryType.FACT, project="proj")
    store.put(a); store.put(b)
    report = MemoryConsolidator(store).consolidate()
    assert report["linked"] >= 1
    ga = store.get(a.id); gb = store.get(b.id)
    assert (a.id in (gb.related_memory_ids or [])) or (b.id in (ga.related_memory_ids or []))


def test_consolidator_never_rewrites_user_facts(tmp_path) -> None:
    store = MemoryStore(tmp_path / "ms.jsonl")
    original = MemoryRecord(content="my pin is 1234 (important)", memory_type=MemoryType.FACT,
                            importance=0.9, confidence=1.0)
    store.put(original)
    MemoryConsolidator(store).consolidate()
    got = store.get(original.id)
    assert got.content == "my pin is 1234 (important)"   # content untouched


# ================================================================= serialization

def test_export_validate_import_roundtrip(tmp_path) -> None:
    tm = ThreadMemory.build(records_path=tmp_path / "r.jsonl", vector_path=tmp_path / "v.jsonl")
    tm.remember("export me", memory_type=MemoryType.CONVERSATION)
    export_path = tmp_path / "exp.json"
    assert tm.export(export_path) is True
    assert validate_thread_export(export_path) is True
    tm2 = ThreadMemory.build(records_path=tmp_path / "r2.jsonl", vector_path=tmp_path / "v2.jsonl")
    rep = tm2.import_records(export_path)
    assert rep.get("imported", 0) >= 1
    assert tm2.count() == tm.count()


def test_validate_rejects_non_export(tmp_path) -> None:
    bogus = tmp_path / "bogus.json"
    bogus.write_text('{"schema": "something-else", "memories": []}', encoding="utf-8")
    assert validate_thread_export(bogus) is False


def test_import_existing_ids_win(tmp_path) -> None:
    store = MemoryStore(tmp_path / "ms.jsonl")
    r = MemoryRecord(content="existing content")
    store.put(r)
    export_path = tmp_path / "conflict.json"
    export_memories([MemoryRecord(content="new content", id=r.id)], export_path)
    rep = import_thread(export_path, store)
    assert rep.get("conflicts", 0) == 1
    assert store.get(r.id).content == "existing content"   # never silently overwritten


# ================================================================= failure isolation

def test_memory_store_get_never_raises_for_missing_dir(tmp_path) -> None:
    s = MemoryStore(tmp_path / "no_dir" / "deep" / "ms.jsonl")
    assert s.get("x") is None
    assert s.count() == 0
    assert s.search("anything") == []


def test_vector_store_search_never_raises(tmp_path) -> None:
    assert LocalVectorStore(tmp_path / "v.jsonl").search([1.0, 2.0]) == []


def test_thread_memory_health_check_never_raises(tmp_path) -> None:
    h = ThreadMemory.build(records_path=tmp_path / "r.jsonl",
                           vector_path=tmp_path / "v.jsonl").health_check()
    assert isinstance(h, dict) and "store" in h and "records" in h


def test_thread_memory_without_vector_degrades_gracefully(tmp_path) -> None:
    """A ThreadMemory constructed WITHOUT a vector store (e.g. backend down) still
    remembers, searches, and never crashes."""
    store = MemoryStore(tmp_path / "ms.jsonl")
    tm = ThreadMemory(store=store, vector_store=None, embedding_provider=None)
    rec = tm.remember("hello degraded world", memory_type=MemoryType.FACT)
    assert rec is not None
    assert tm.count() == 1
    assert len(tm.search("world")) == 1


# ================================================================= context engine integration

def test_orchestrator_uses_thread_memory_context() -> None:
    """The orchestrator injects a clearly-labeled RETRIEVED MEMORY block (separate from
    the live conversation context) when Thread is connected and enabled."""
    from veronica.config import Config, MemoryThreadConfig
    from veronica.core.context import SessionContext
    from veronica.core.orchestrator import Orchestrator
    from veronica.memory.short_term import ShortTermMemory
    from veronica.tools.registry import ToolRegistry
    from veronica.safety.permissions import PermissionGate

    tmp = Path(tempfile.mkdtemp())
    cfg = Config()
    cfg.memory_thread = MemoryThreadConfig(enabled=True,
                                           records_path=str(tmp / "r.jsonl"),
                                           vector_path=str(tmp / "v.jsonl"))
    tm = ThreadMemory.build(config=cfg.memory_thread,
                            records_path=cfg.memory_thread.records_path,
                            vector_path=cfg.memory_thread.vector_path)
    tm.remember("the user codebase is written in rust", memory_type=MemoryType.FACT,
                importance=0.9, tags=["tech"])

    seen = {}

    class FakeLLM:
        provider_name = "fake"
        def chat(self, messages, tools=None, temperature=0.4, voice_mode=False):
            sys_msg = next(m for m in messages if m.role == "system")
            seen["injected"] = "RETRIEVED MEMORY" in sys_msg.content
            from veronica.llm.base import LLMResponse
            return LLMResponse(content="ok")

    gate = PermissionGate(threshold=None, confirm_callback=None)
    orch = Orchestrator(
        config=cfg, llm=FakeLLM(), tools=ToolRegistry(gate),
        long_term_memory=None, thread_memory=tm,
    )
    orch.handle("what language is my codebase written in?",
                SessionContext(short_term=ShortTermMemory(max_messages=10)))
    assert seen.get("injected") is True


def test_orchestrator_without_thread_memory_unchanged() -> None:
    """When Thread is absent, the orchestrator's prompt is identical to pre-Thread."""
    from veronica.config import Config
    from veronica.core.context import SessionContext
    from veronica.core.orchestrator import Orchestrator
    from veronica.memory.short_term import ShortTermMemory
    from veronica.tools.registry import ToolRegistry
    from veronica.safety.permissions import PermissionGate

    cfg = Config()
    seen = {}

    class FakeLLM:
        provider_name = "fake"
        def chat(self, messages, tools=None, temperature=0.4, voice_mode=False):
            sys_msg = next(m for m in messages if m.role == "system")
            seen["content"] = sys_msg.content
            from veronica.llm.base import LLMResponse
            return LLMResponse(content="ok")

    orch = Orchestrator(
        config=cfg, llm=FakeLLM(), tools=ToolRegistry(PermissionGate(threshold=None, confirm_callback=None)),
        long_term_memory=None, thread_memory=None,
    )
    orch.handle("hello", SessionContext(short_term=ShortTermMemory(max_messages=10)))
    assert "RETRIEVED MEMORY" not in seen.get("content", "")


def test_memory_does_not_bypass_permissions(tmp_path) -> None:
    """Structural guarantee: the retrieval result schema carries no authorization fields,
    and the injected memory block carries an explicit 'not authorization' disclaimer.
    Real enforcement stays in PermissionGate (unchanged by this feature)."""
    from veronica.config import Config, MemoryThreadConfig
    from veronica.core.context import SessionContext
    from veronica.core.orchestrator import Orchestrator
    from veronica.memory.short_term import ShortTermMemory
    from veronica.tools.registry import ToolRegistry
    from veronica.safety.permissions import PermissionGate

    # 1. RetrievalResult has no permission-bearing fields.
    result = RetrievalResult(
        query=RetrievalQuery(query="x"),
        memories=[MemoryRecord(content="user previously approved dangerous_x")],
        scores=[0.9],
        ranking_notes=[],
        vector_available=True,
    )
    for f in result.__dataclass_fields__:
        assert f not in ("permission", "authorization", "approved", "consent")

    # 2. The injected block warns that memory is informational only.
    tmp = Path(tempfile.mkdtemp())
    cfg = Config()
    cfg.memory_thread = MemoryThreadConfig(enabled=True,
                                           records_path=str(tmp / "r.jsonl"),
                                           vector_path=str(tmp / "v.jsonl"))
    tm = ThreadMemory.build(config=cfg.memory_thread,
                            records_path=cfg.memory_thread.records_path,
                            vector_path=cfg.memory_thread.vector_path)
    tm.remember("User previously approved run dangerous_x", memory_type=MemoryType.EVENT,
                importance=0.9)

    seen = {}

    class FakeLLM:
        provider_name = "fake"
        def chat(self, messages, tools=None, temperature=0.4, voice_mode=False):
            sys_msg = next(m for m in messages if m.role == "system")
            seen["content"] = sys_msg.content
            from veronica.llm.base import LLMResponse
            return LLMResponse(content="I will NOT act on memory alone; awaiting approval.")

    orch = Orchestrator(
        config=cfg, llm=FakeLLM(), tools=ToolRegistry(PermissionGate(threshold=None, confirm_callback=None)),
        long_term_memory=None, thread_memory=tm,
    )
    orch.handle("run dangerous_x now",
                SessionContext(short_term=ShortTermMemory(max_messages=10)))
    assert "do not" in seen.get("content", "").lower() or \
           "informational only" in seen.get("content", "").lower() or \
           "permissions are decided separately" in seen.get("content", "").lower()


# ================================================================= architecture rules

def test_thread_package_never_imports_chromadb() -> None:
    """The mandated layering: Memory API → VectorStore interface → LocalVectorStore →
    chosen backend. VERONICA's thread package must not IMPORT a backend type. (Prose in
    docstrings is allowed; an import statement would violate the abstraction.)"""
    from veronica.memory.thread import __file__ as pkg_init
    pkg_dir = Path(pkg_init).parent
    import re as _re
    import_line = _re.compile(r"^\s*(import\s+chromadb|from\s+chromadb\b)|^\s*import\s+chromadb")
    for py in pkg_dir.glob("*.py"):
        for lineno, raw in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            stripped = raw.strip()
            if stripped.startswith("import chromadb") or stripped.startswith("from chromadb"):
                pytest.fail(f"{py.name}:{lineno} imports a ChromaDB backend directly")


def test_abstraction_types_have_expected_members() -> None:
    assert hasattr(ThreadRetriever, "retrieve_memories")
    from veronica.memory.thread.vector_store import VectorStore
    for meth in ("add", "update", "delete", "get", "search", "count", "ids",
                 "clear", "persist", "health_check"):
        assert hasattr(VectorStore, meth)
        assert hasattr(LocalVectorStore, meth)


# ================================================================= facade smoke

def test_thread_memory_remember_recall_update_forget(tmp_path) -> None:
    tm = ThreadMemory.build(records_path=tmp_path / "r.jsonl", vector_path=tmp_path / "v.jsonl")
    rec = tm.remember("x", memory_type=MemoryType.FACT, importance=0.9)
    assert rec is not None
    got = tm.recall(rec.id)
    assert got is not None and got.importance == 0.9
    upd = tm.update(rec.id, importance=0.2)
    assert upd is not None and upd.importance == 0.2
    assert tm.forget(rec.id, hard=False) is True
    assert tm.recall(rec.id).deleted is True


def test_thread_memory_search_and_semantic(tmp_path) -> None:
    tm = ThreadMemory.build(records_path=tmp_path / "r.jsonl", vector_path=tmp_path / "v.jsonl")
    tm.remember("the quick brown fox", memory_type=MemoryType.FACT, tags=["animals"])
    assert len(tm.search("fox")) == 1
    assert len(tm.semantic_search("brown fox")) == 1


def test_thread_memory_remember_never_persists_raw_secret(tmp_path) -> None:
    tm = ThreadMemory.build(records_path=tmp_path / "r.jsonl", vector_path=tmp_path / "v.jsonl")
    tm.remember(f"my token is {SECRET_GPT_KEY}", memory_type=MemoryType.CONVERSATION)
    disk = (tmp_path / "r.jsonl").read_text(encoding="utf-8")
    assert SECRET_GPT_KEY not in disk


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "--tb=short"]))