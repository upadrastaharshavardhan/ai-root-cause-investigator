"""Plugin base classes for collectors and analyzers."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any

from src.models.schemas import EvidenceBundle, EvidenceItem, InvestigationContext
from src.utils.logging import get_logger

logger = get_logger(__name__)


class BaseCollector(ABC):
    """Base class for evidence collectors."""

    name: str = "base"
    priority: int = 100  # lower = earlier
    enabled: bool = True

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}

    @abstractmethod
    async def collect(self, context: InvestigationContext) -> EvidenceBundle:
        """Collect evidence for the given investigation context."""
        ...

    async def safe_collect(self, context: InvestigationContext) -> EvidenceBundle:
        """Wrapper that never raises; records errors inside the bundle."""
        start = time.perf_counter()
        try:
            bundle = await self.collect(context)
            bundle.collector = self.name
            bundle.duration_ms = int((time.perf_counter() - start) * 1000)
            return bundle
        except Exception as exc:  # noqa: BLE001
            logger.exception("collector_failed", collector=self.name, error=str(exc))
            return EvidenceBundle(
                collector=self.name,
                items=[],
                errors=[f"{type(exc).__name__}: {exc}"],
                duration_ms=int((time.perf_counter() - start) * 1000),
            )


class BaseAnalyzer(ABC):
    """Base class for specialized analyzers that operate on collected evidence."""

    name: str = "base"
    enabled: bool = True

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}

    @abstractmethod
    async def analyze(
        self, context: InvestigationContext, evidence: list[EvidenceBundle]
    ) -> list[EvidenceItem]:
        """Return additional derived evidence or insights."""
        ...


class PluginRegistry:
    """Simple registry for collectors and analyzers."""

    def __init__(self) -> None:
        self._collectors: dict[str, BaseCollector] = {}
        self._analyzers: dict[str, BaseAnalyzer] = {}

    def register_collector(self, collector: BaseCollector) -> None:
        self._collectors[collector.name] = collector
        logger.info("registered_collector", name=collector.name)

    def register_analyzer(self, analyzer: BaseAnalyzer) -> None:
        self._analyzers[analyzer.name] = analyzer
        logger.info("registered_analyzer", name=analyzer.name)

    def get_collectors(self, enabled_only: bool = True) -> list[BaseCollector]:
        items = list(self._collectors.values())
        if enabled_only:
            items = [c for c in items if c.enabled]
        return sorted(items, key=lambda c: c.priority)

    def get_analyzers(self, enabled_only: bool = True) -> list[BaseAnalyzer]:
        items = list(self._analyzers.values())
        if enabled_only:
            items = [a for a in items if a.enabled]
        return items


# Global registry instance
registry = PluginRegistry()
