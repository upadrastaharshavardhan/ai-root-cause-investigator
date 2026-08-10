"""Historical failure and memory retrieval collector."""

from __future__ import annotations

from typing import Any

from src.models.schemas import (
    EvidenceBundle,
    EvidenceItem,
    EvidenceType,
    InvestigationContext,
)
from src.plugins.base import BaseCollector
from src.utils.logging import get_logger

logger = get_logger(__name__)


class HistoryCollector(BaseCollector):
    name = "history"
    priority = 40

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self.top_k = (config or {}).get("top_k", 5)

    async def collect(self, context: InvestigationContext) -> EvidenceBundle:
        items: list[EvidenceItem] = []
        try:
            from src.memory.vector_store import MemoryStore

            store = MemoryStore()
            queries = []
            for t in context.failed_tests[:5]:
                queries.append(f"{t.test_title} {t.error_message[:200]}")
            if not queries and context.pipeline.commit_sha:
                queries.append(f"commit {context.pipeline.commit_sha}")

            for q in queries:
                similar = await store.search_similar_cases(q, top_k=self.top_k)
                for case in similar:
                    items.append(
                        EvidenceItem(
                            type=EvidenceType.HISTORICAL_FAILURE,
                            source=self.name,
                            title=f"Similar past case: {case.get('title', 'unknown')}",
                            content=case,
                            metadata={"score": case.get("score", 0.0)},
                            related_entities=case.get("tags", []),
                        )
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning("history_collect_failed", error=str(exc))
            return EvidenceBundle(
                collector=self.name,
                items=[],
                errors=[str(exc)],
            )

        return EvidenceBundle(collector=self.name, items=items)
