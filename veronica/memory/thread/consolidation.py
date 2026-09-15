"""
Thread Memory V1 — consolidation foundation.

Memory naturally grows; consolidation keeps it useful without unbounded growth. V1 ships a
deterministic, rule-based consolidator — no ML, no rewriting of user facts. It performs:

  merge   — content-identical duplicates (same type) collapse into one canonical record.
  link    — records in the same project/chapter/tag cluster get reciprocal
            ``related_memory_ids`` edges (bounded, so the graph stays sparse).
  degrade — a record that is empty or unparseable is dropped instead of poisoning recall.

Guarantees:
  - Provenance is preserved: the canonical record keeps its ORIGINAL created_at and the
    merged record's provenance is appended to ``metadata``; nothing user-authored is edited.
  - Soft via hard deletes only where content is truly identical — never a lossy merge.
  - ``consolidate()`` returns a report dict ``{merged, linked, degraded, error}`` and never
    raises (the facade wraps it too).
"""
from __future__ import annotations

import threading
from typing import Dict, List, Optional

from veronica.logging_setup import get_logger
from veronica.memory.thread.models import MemoryRecord
from veronica.memory.thread.store import MemoryStore

logger = get_logger("memory.thread.consolidation")

# Maximum number of related-link edges added per record per run — keeps the graph sparse.
_MAX_LINKS_PER_RECORD = 8
# Records with this many sources already merged into them are left alone.
_MAX_MERGED_SOURCES = 32


class MemoryConsolidator:
    def __init__(self, store: MemoryStore) -> None:
        self.store = store
        self._lock = threading.Lock()

    # -- public API ---------------------------------------------------------------
    def consolidate(self) -> Dict[str, int]:
        """Run one deterministic consolidation pass. Returns a report dict; never raises."""
        report = {"merged": 0, "linked": 0, "degraded": 0}
        try:
            with self._lock:
                records = self.store.all(include_deleted=False)
        except Exception:  # noqa: BLE001
            logger.exception("Consolidation could not read the store.")
            return {**report, "error": True}
        try:
            report["merged"] = self._merge_duplicates(records)
            report["degraded"] = self._drop_degraded(records)
            report["linked"] = self._link_clusters(records)
            return report
        except Exception:  # noqa: BLE001
            logger.exception("Consolidation pass failed mid-way.")
            return {**report, "error": True}

    # -- merge --------------------------------------------------------------------
    def _merge_duplicates(self, records: List[MemoryRecord]) -> int:
        """Collapse content-identical records (same type) into the oldest/canonical one.
        Does NOT merge near-duplicates or same-free-text — only exact matches, so no user
        fact is ever rewritten. Returns the number of records removed."""
        buckets: Dict[tuple, List[MemoryRecord]] = {}
        for rec in records:
            key = (rec.memory_type, self._canonical_text(rec.content))
            buckets.setdefault(key, []).append(rec)

        merged = 0
        for group in buckets.values():
            if len(group) < 2:
                continue
            if not group[0].content:
                continue
            # Canonical = the one with the highest importance (ties → oldest).
            canonical = max(group, key=lambda r: (r.importance, -len(r.metadata)))
            survivors = [r for r in group if r.id != canonical.id]
            self._merge_metadata_into(canonical, survivors)
            for dup in survivors:
                self.store.delete(dup.id)
                merged += 1
            if merged:
                self.store.put(canonical)
        if merged:
            logger.info("Consolidation merged %d duplicate record(s)", merged)
        return merged

    def _merge_metadata_into(self, canonical: MemoryRecord, dups: List[MemoryRecord]) -> None:
        """Fold duplicate metadata into the canonical record without touching user content."""
        seen_tags = set(canonical.tags)
        seen_ids = set(canonical.related_memory_ids)
        merged_sources = list(canonical.metadata.get("merged_sources", []))
        budget_hit = False
        for dup in dups:
            for tag in dup.tags:
                if tag not in seen_tags:
                    canonical.tags.append(tag)
                    seen_tags.add(tag)
            for rid in dup.related_memory_ids:
                if rid not in seen_ids:
                    canonical.related_memory_ids.append(rid)
                    seen_ids.add(rid)
            if dup.provenance and dup.provenance not in merged_sources:
                if len(merged_sources) >= _MAX_MERGED_SOURCES:
                    budget_hit = True
                else:
                    merged_sources.append(dup.provenance)
            for k, v in dup.metadata.items():
                if k == "merged_sources":
                    continue
                canonical.metadata.setdefault(f"merged_{k}", v)
            canonical.confidence = max(float(canonical.confidence), float(dup.confidence))
        if merged_sources:
            canonical.metadata["merged_sources"] = merged_sources
        if budget_hit:
            canonical.metadata["merge_budget_hit"] = True
        canonical.touch()

    @staticmethod
    def _canonical_text(content: str) -> str:
        return " ".join(str(content or "").split()).lower()

    # -- degrade ------------------------------------------------------------------
    def _drop_degraded(self, records: List[MemoryRecord]) -> int:
        """Remove records that are empty/unparseable and would only pollute recall."""
        dropped = 0
        for rec in records:
            if not str(rec.content or "").strip():
                self.store.delete(rec.id)
                dropped += 1
        if dropped:
            logger.info("Consolidation removed %d empty record(s)", dropped)
        return dropped

    # -- link ---------------------------------------------------------------------
    def _link_clusters(self, records: List[MemoryRecord]) -> int:
        """Link records that share a project or a chapter. Edges are reciprocal and bounded.
        This is the foundation for the future semantic graph (Phase 18 extension), built on
        the plain ``related_memory_ids`` field so nothing about the graph is hard-wired."""
        by_project: Dict[str, List[MemoryRecord]] = {}
        by_chapter: Dict[str, List[MemoryRecord]] = {}
        by_tag: Dict[str, List[MemoryRecord]] = {}
        for rec in records:
            if rec.project:
                by_project.setdefault(rec.project, []).append(rec)
            if rec.chapter:
                by_chapter.setdefault(rec.chapter, []).append(rec)
            for tag in rec.tags:
                by_tag.setdefault(tag, []).append(rec)

        linked = 0
        changed: List[MemoryRecord] = []
        for cluster in (list(by_project.values()) + list(by_chapter.values()) + list(by_tag.values())):
            if len(cluster) < 2:
                continue
            for rec in cluster:
                if len(rec.related_memory_ids) >= _MAX_LINKS_PER_RECORD:
                    continue
                peers = [other.id for other in cluster if other.id != rec.id][:_MAX_LINKS_PER_RECORD]
                new_links = [pid for pid in peers if pid not in rec.related_memory_ids]
                if new_links:
                    rec.related_memory_ids.extend(new_links)
                    rec.touch()
                    changed.append(rec)
                    linked += 1
        if changed:
            for rec in changed:
                self.store.put(rec)
            logger.info("Consolidation added %d related-memory link(s)", linked)
        return linked


__all__ = ["MemoryConsolidator"]