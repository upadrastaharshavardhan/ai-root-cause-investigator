"""Persistent memory layer for continuous learning from past investigations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from src.config.settings import get_settings
from src.models.schemas import InvestigationResult
from src.utils.logging import get_logger

logger = get_logger(__name__)


class MemoryStore:
    """
    Lightweight vector + metadata store.
    Uses ChromaDB when available; falls back to JSON file store for zero-dep demos.
    """

    def __init__(self) -> None:
        self.settings = get_settings()
        self.persist_dir = Path(self.settings.chroma_persist_dir)
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        self._fallback_path = self.persist_dir / "cases.jsonl"
        self._client = None
        self._collection = None
        self._init_chroma()

    def _init_chroma(self) -> None:
        if not self.settings.memory_enabled:
            return
        try:
            import chromadb
            from chromadb.config import Settings as ChromaSettings

            self._client = chromadb.PersistentClient(
                path=str(self.persist_dir / "chroma"),
                settings=ChromaSettings(anonymized_telemetry=False),
            )
            self._collection = self._client.get_or_create_collection(
                name="rci_cases",
                metadata={"hnsw:space": "cosine"},
            )
            logger.info("chroma_initialized", path=str(self.persist_dir))
        except Exception as exc:  # noqa: BLE001
            logger.warning("chroma_unavailable_fallback", error=str(exc))
            self._client = None
            self._collection = None

    async def search_similar_cases(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        if self._collection is not None:
            try:
                results = self._collection.query(query_texts=[query], n_results=top_k)
                out: list[dict[str, Any]] = []
                docs = results.get("documents", [[]])[0]
                metas = results.get("metadatas", [[]])[0]
                dists = results.get("distances", [[]])[0]
                for doc, meta, dist in zip(docs, metas, dists):
                    score = 1.0 - float(dist) if dist is not None else 0.0
                    out.append(
                        {
                            "title": (meta or {}).get("title", "past case"),
                            "text": doc,
                            "score": score,
                            "tags": (meta or {}).get("tags", []),
                            "case_id": (meta or {}).get("case_id"),
                        }
                    )
                return out
            except Exception as exc:  # noqa: BLE001
                logger.warning("chroma_query_failed", error=str(exc))

        # Fallback: naive keyword search over JSONL
        return self._fallback_search(query, top_k)

    def _fallback_search(self, query: str, top_k: int) -> list[dict[str, Any]]:
        if not self._fallback_path.exists():
            return []
        tokens = set(query.lower().split())
        scored: list[tuple[float, dict[str, Any]]] = []
        with self._fallback_path.open() as f:
            for line in f:
                try:
                    case = json.loads(line)
                    text = case.get("text", "").lower()
                    overlap = len(tokens & set(text.split()))
                    if overlap:
                        scored.append((overlap / max(len(tokens), 1), case))
                except Exception:  # noqa: BLE001
                    continue
        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            {
                "title": c.get("title", "past case"),
                "text": c.get("text", ""),
                "score": s,
                "tags": c.get("tags", []),
                "case_id": c.get("case_id"),
            }
            for s, c in scored[:top_k]
        ]

    async def persist_investigation(self, result: InvestigationResult) -> str:
        case_id = str(uuid4())
        primary = result.primary_root_cause
        text_parts = [
            f"Status: {result.status.value}",
            f"Primary: {primary.title if primary else 'none'}",
            primary.description if primary else "",
            f"Categories: {', '.join(h.category for h in result.hypotheses)}",
            result.explainability_report[:2000],
        ]
        text = "\n".join(p for p in text_parts if p)
        tags = list(
            {
                *(primary.affected_modules if primary else []),
                *(primary.category if primary else [],),
            }
        )
        meta = {
            "case_id": case_id,
            "title": primary.title if primary else f"Investigation {result.investigation_id}",
            "tags": tags,
            "investigation_id": str(result.investigation_id),
            "confidence": primary.confidence if primary else 0.0,
        }

        if self._collection is not None:
            try:
                self._collection.add(
                    ids=[case_id],
                    documents=[text],
                    metadatas=[{k: (v if not isinstance(v, list) else ",".join(map(str, v))) for k, v in meta.items()}],
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("chroma_persist_failed", error=str(exc))

        # Always append to JSONL as durable backup
        record = {"case_id": case_id, "text": text, "title": meta["title"], "tags": tags, **meta}
        with self._fallback_path.open("a") as f:
            f.write(json.dumps(record) + "\n")

        logger.info("memory_persisted", case_id=case_id)
        return case_id
